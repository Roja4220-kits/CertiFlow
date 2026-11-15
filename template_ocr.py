"""Detect {{placeholders}} in image templates with Tesseract and clean them out."""
import os
import re
from pathlib import Path

import cv2
import numpy as np
import pytesseract
from pytesseract import Output

from certificate_generator import (
    MAX_FONT_SIZE, MIN_FONT_SIZE, PX_TO_PT, normalize_field_name,
)

# Set TESSERACT_CMD to override. Defaults to the usual Windows install path.
_cmd = os.environ.get("TESSERACT_CMD") or (
    r"C:\Program Files\Tesseract-OCR\tesseract.exe" if os.name == "nt" else None
)
if _cmd:
    pytesseract.pytesseract.tesseract_cmd = _cmd

TARGET_OCR_WIDTH = 2400      # upscale small templates to about this width
MAX_OCR_SCALE = 3
MIN_CONFIDENCE = 20
FONT_SIZE_RATIO = 0.9        # font size (pt) = placeholder height (pt) * ratio
NEIGHBOUR_WIDTH_RATIO = 0.95 # a field may use ~95% of the gap to its neighbour
INPAINT_RADIUS = 3
TEXT_DIFF_THRESHOLD = 40     # pixel difference from background = "text"

# Tolerates OCR confusing {{ }} with [[ ]] or (( )).
# Allows spaces between the two braces: Tesseract often splits "{{a b}}" into "{{a", "b}", "}".
PLACEHOLDER_OCR = re.compile(r"[{\[(]\s*[{\[(]\s*([^{}\[\]()]+?)\s*[}\])]\s*[}\])]")


# --------------------------------------------------------------------------
# Detection
# --------------------------------------------------------------------------

def _placeholders_from_ocr(data):
    """Yield placeholders found in Tesseract output.

    Words are grouped per text line first, so '{{', 'student_name', '}}'
    (split into separate words) are still recognised as one placeholder.
    """
    lines = {}
    for i, raw in enumerate(data["text"]):
        text = str(raw).strip()
        if not text:
            continue
        try:
            confidence = float(data["conf"][i])
        except (ValueError, TypeError):
            continue
        if confidence < 0:  # -1 = layout element, not a word. Keep weak words: they are
            continue        # often the tail of a placeholder (judged as a whole below).
        key = (data["block_num"][i], data["par_num"][i], data["line_num"][i])
        lines.setdefault(key, []).append((data["left"][i], i, text))

    for words in lines.values():
        words.sort()
        joined, spans = "", []
        for _left, index, text in words:
            if joined:
                joined += " "
            spans.append((len(joined), len(joined) + len(text), index))
            joined += text

        for match in PLACEHOLDER_OCR.finditer(joined):
            field = normalize_field_name(match.group(1))
            hit = [i for start, end, i in spans if start < match.end() and end > match.start()]
            if not field or not hit:
                continue
            left = min(data["left"][i] for i in hit)
            top = min(data["top"][i] for i in hit)
            right = max(data["left"][i] + data["width"][i] for i in hit)
            bottom = max(data["top"][i] + data["height"][i] for i in hit)
            confidence = max(float(data["conf"][i]) for i in hit)
            if confidence < MIN_CONFIDENCE:
                continue
            yield field, match.group(0), left, top, right - left, bottom - top, confidence


def _sample_text_color(image, x, y, w, h):
    """Estimate the placeholder's text colour as '#rrggbb'."""
    height, width = image.shape[:2]
    crop = image[max(0, y):min(height, y + h), max(0, x):min(width, x + w)]
    if crop.size == 0:
        return "#000000"
    pixels = crop.reshape(-1, 3).astype(np.float32)
    background = np.median(pixels, axis=0)
    distance = np.linalg.norm(pixels - background, axis=1)
    count = max(1, int(len(pixels) * 0.1))
    strongest = pixels[np.argsort(distance)[-count:]]  # most "text-like" pixels
    b, g, r = (int(round(v)) for v in strongest.mean(axis=0))
    return f"#{r:02x}{g:02x}{b:02x}"


def detect_template_placeholders(image_path):
    """Return {'image_width', 'image_height', 'fields': [...]} in original pixels."""
    image_path = Path(image_path)
    image = cv2.imread(str(image_path))
    if image is None:
        raise ValueError(f"Could not read template image: {image_path}")

    height, width = image.shape[:2]
    scale = min(MAX_OCR_SCALE, max(1.0, TARGET_OCR_WIDTH / width))
    enlarged = cv2.resize(image, None, fx=scale, fy=scale, interpolation=cv2.INTER_CUBIC)
    gray = cv2.cvtColor(enlarged, cv2.COLOR_BGR2GRAY)

    best = {}
    for page_mode in (6, 11):
        data = pytesseract.image_to_data(
            gray, config=f"--oem 3 --psm {page_mode}", output_type=Output.DICT
        )
        for field, text, x, y, w, h, confidence in _placeholders_from_ocr(data):
            item = {
                "field": field,
                "placeholder": text,
                "x": round(x / scale),
                "y": round(y / scale),
                "width": round(w / scale),
                "height": round(h / scale),
                "confidence": round(confidence, 2),
            }
            if field not in best or confidence > best[field]["confidence"]:
                best[field] = item

    for item in best.values():
        item["color"] = _sample_text_color(
            image, item["x"], item["y"], item["width"], item["height"]
        )

    fields = sorted(best.values(), key=lambda item: (item["y"], item["x"]))
    return {"image_width": width, "image_height": height, "fields": fields}


# --------------------------------------------------------------------------
# Cleaning
# --------------------------------------------------------------------------

def _placeholder_region(image, field):
    """Work out exactly which pixels belong to one placeholder's text.

    Only the placeholder's own letters are selected. Long lines (e.g. an
    underline) and text of neighbouring rows are never touched.
    Returns (x1, y1, x2, y2, mask, background_colour, background_is_flat).
    """
    img_h, img_w = image.shape[:2]
    x, y, w, h = (int(field[k]) for k in ("x", "y", "width", "height"))
    pad = max(3, round(h * 0.5))
    x1, y1 = max(0, x - pad), max(0, y - pad)
    x2, y2 = min(img_w, x + w + pad), min(img_h, y + h + pad)
    roi = image[y1:y2, x1:x2].astype(np.int16)
    if roi.size == 0:
        return None

    # Background colour = median of the region's outer border.
    ring = np.concatenate([
        roi[:2].reshape(-1, 3), roi[-2:].reshape(-1, 3),
        roi[:, :2].reshape(-1, 3), roi[:, -2:].reshape(-1, 3),
    ])
    background = np.median(ring, axis=0)
    flat = bool((np.abs(ring - background).max(axis=1) <= 15).mean() >= 0.7)

    nonbg = (np.abs(roi - background).max(axis=2) > TEXT_DIFF_THRESHOLD).astype(np.uint8)

    # Protect long horizontal lines such as an underline below the name.
    run = min(max(25, int(h * 2)), roi.shape[1])
    lines = cv2.morphologyEx(
        nonbg, cv2.MORPH_OPEN, cv2.getStructuringElement(cv2.MORPH_RECT, (run, 1))
    )
    protected = cv2.dilate(lines, np.ones((3, 3), np.uint8))
    candidates = nonbg * (1 - protected)

    # Keep only connected shapes whose centre lies inside the placeholder box.
    left, top = x - x1, y - y1
    keep = np.zeros_like(nonbg)
    count, labels, stats, centroids = cv2.connectedComponentsWithStats(candidates, connectivity=8)
    for label in range(1, count):
        _cx, _cy, comp_w, comp_h, _area = stats[label]
        centre_x, centre_y = centroids[label]
        if (
            left <= centre_x <= left + w
            and top <= centre_y <= top + h
            and comp_w <= w * 1.05
            and comp_h <= h * 1.4
        ):
            keep[labels == label] = 1
    if not keep.any():  # fallback: the tight box, still sparing lines
        keep[top:top + h, left:left + w] = candidates[top:top + h, left:left + w]

    excluded = nonbg * (1 - keep)  # other content that must stay untouched
    mask = cv2.dilate(keep, np.ones((3, 3), np.uint8), iterations=2)  # anti-aliased edges
    mask[excluded > 0] = 0
    return x1, y1, x2, y2, (mask * 255).astype(np.uint8), background, flat


def clean_template_image(
    image_path, output_path, detection_result=None, inpaint_radius=INPAINT_RADIUS,
):
    """Save a copy of the image with the placeholder text erased.

    On plain backgrounds the letters are painted over with the background
    colour; on textured backgrounds they are inpainted.
    """
    image_path, output_path = Path(image_path), Path(output_path)
    if detection_result is None:
        detection_result = detect_template_placeholders(image_path)

    image = cv2.imread(str(image_path))
    if image is None:
        raise ValueError("Unable to open the certificate image.")

    fields = detection_result.get("fields", [])
    if not fields:
        raise ValueError("No {{field_name}} placeholders were detected in this image.")

    cleaned = image.copy()
    inpaint_mask = np.zeros(image.shape[:2], dtype=np.uint8)

    for field in fields:
        region = _placeholder_region(image, field)
        if region is None:
            continue
        x1, y1, x2, y2, mask, background, flat = region
        if flat:
            cleaned[y1:y2, x1:x2][mask > 0] = background.astype(np.uint8)
        else:
            inpaint_mask[y1:y2, x1:x2] |= mask

    if inpaint_mask.any():
        cleaned = cv2.inpaint(cleaned, inpaint_mask, inpaint_radius, cv2.INPAINT_TELEA)

    output_path.parent.mkdir(parents=True, exist_ok=True)
    if not cv2.imwrite(str(output_path), cleaned):
        raise OSError(f"Could not save cleaned template: {output_path}")
    return {"cleaned_path": str(output_path), "detected_count": len(fields), "fields": fields}


# --------------------------------------------------------------------------
# Positions
# --------------------------------------------------------------------------

def image_positions_from_detection(detection_result):
    """Position + suggested style per field, in PDF points (origin bottom-left).

    * x, y, width, height : placeholder box
    * font_size           : derived from the placeholder's height
    * color               : sampled from the placeholder
    * max_width           : room before colliding with a neighbour on the same line
    The user's own formatting choices override font_size / color later.
    """
    image_height = detection_result["image_height"]
    fields = detection_result.get("fields", [])
    centres = {
        f["field"]: (f["x"] + f["width"] / 2, f["y"] + f["height"] / 2) for f in fields
    }

    positions = {}
    for field in fields:
        name = field["field"]
        cx, cy = centres[name]

        gaps = [
            abs(centres[o["field"]][0] - cx)
            for o in fields
            if o is not field
            and abs(centres[o["field"]][1] - cy) < 0.5 * min(o["height"], field["height"])
        ]

        size = field["height"] * PX_TO_PT * FONT_SIZE_RATIO
        position = {
            "x": round(cx * PX_TO_PT, 2),
            "y": round((image_height - cy) * PX_TO_PT, 2),
            "width": round(field["width"] * PX_TO_PT, 2),
            "height": round(field["height"] * PX_TO_PT, 2),
            "font_size": round(max(MIN_FONT_SIZE, min(MAX_FONT_SIZE, size)), 1),
            "color": field.get("color", "#000000"),
        }
        if gaps:
            room = min(gaps) * NEIGHBOUR_WIDTH_RATIO * PX_TO_PT
            position["max_width"] = round(max(room, position["width"]), 2)
        positions[name] = position
    return positions