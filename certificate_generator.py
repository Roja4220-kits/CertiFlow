"""Certificate PDF generation (PDF and image templates).

Coordinate conventions
----------------------
* Positions use PDF points, origin at the BOTTOM-LEFT (y grows upwards).
* A position is {"x", "y"} = centre of the target box, plus optional
  {"width", "height"} = size of that box. Alignment is applied relative to the
  box: left -> box left edge, center -> box centre, right -> box right edge.
* Image templates are converted to a PDF page with 1 px = PX_TO_PT points.
"""
import contextvars
import hashlib
import html as html_lib
import os
import re
from functools import lru_cache
from pathlib import Path

try:
    import pymupdf as fitz
except ImportError:  # older PyMuPDF
    import fitz

from PIL import Image, ImageFont, ImageOps, features

from fieldnames import normalize_field_name  # noqa: F401  (re-exported)

BASE_DIR = Path(__file__).resolve().parent
OUTPUT_FOLDER = BASE_DIR / "generated_certificates"

PX_TO_PT = 0.75            # shared with template_ocr.py (96 dpi -> 72 pt)
PAGE_MARGIN = 18           # text is never drawn closer than this to the edge
MIN_FONT_SIZE = 6
MAX_FONT_SIZE = 100
MAX_TEXT_WIDTH_RATIO = 0.8   # centred text may use at most 80% of the page width

FONT_NAMES = {
    "helv", "hebo", "heit", "hebi",
    "tiro", "tibo", "tiit", "tibi",
    "cour", "cobo", "coit", "cobi",
}
ALIGNMENTS = {"left", "center", "right"}

# Custom fonts: "custom:<32 hex>" (uploaded for one session) and
# "bundled:<name>" (a .ttf/.otf dropped into the project's fonts/ folder).
BUNDLED_FONT_DIR = BASE_DIR / "fonts"
FONT_EXTENSIONS = {".ttf", ".otf"}
CUSTOM_FONT_PATTERN = re.compile(r"custom:[0-9a-f]{32}")
BUNDLED_FONT_PATTERN = re.compile(r"bundled:[a-z0-9_\-]{1,60}")
_FONT_FILES = contextvars.ContextVar("certiflow_font_files", default={})

# Optional Unicode font used when a value contains non-Latin characters.
UNICODE_FONT_PATH = Path(
    os.environ.get("CERTIFLOW_UNICODE_FONT")
    or BASE_DIR / "fonts" / "NotoSans-Regular.ttf"
)

# {{ field name }}  (the name itself is normalised afterwards)
PLACEHOLDER_PATTERN = re.compile(r"\{\{\s*([^{}\n]+?)\s*\}\}")


# --------------------------------------------------------------------------
# Normalisation / validation
# --------------------------------------------------------------------------

def hex_to_rgb(value):
    if isinstance(value, (tuple, list)) and len(value) == 3:
        return tuple(max(0.0, min(1.0, float(c))) for c in value)
    value = str(value or "#000000").strip()
    if not re.fullmatch(r"#[0-9a-fA-F]{6}", value):
        raise ValueError("Font color must be a valid six-digit HEX color.")
    return tuple(int(value[i:i + 2], 16) / 255 for i in (1, 3, 5))


def normalize_position(config, field="field"):
    """Validate one field's settings.

    Optional keys (font_size, x, y, width, height, page) are only present in
    the result when they were supplied, so results can be merged over OCR data
    without overwriting detected coordinates.
    """
    if not isinstance(config, dict):
        raise ValueError(f"Settings for '{field}' must be an object.")

    def number(key, minimum=-100000, maximum=100000):
        value = config.get(key)
        if value is None:
            return None
        if isinstance(value, bool):
            raise ValueError(f"{field}.{key} must be numeric.")
        try:
            value = float(value)
        except (TypeError, ValueError):
            raise ValueError(f"{field}.{key} must be numeric.")
        if not minimum <= value <= maximum:
            raise ValueError(f"{field}.{key} is outside the allowed range.")
        return value

    align = config.get("align", config.get("alignment", "center"))
    font_name = config.get("font_name", "helv")
    if align not in ALIGNMENTS:
        raise ValueError(f"{field} alignment must be left, center, or right.")
    if not is_valid_font_name(font_name):
        raise ValueError(f"{field} has an unsupported font style.")

    use_template_position = config.get("use_template_position", True)
    if not isinstance(use_template_position, bool):
        raise ValueError(f"{field}.use_template_position must be true or false.")

    try:
        color = hex_to_rgb(config.get("color", "#000000"))
    except ValueError:
        raise ValueError(f"{field} must have a valid HEX font color.")

    result = {
        "align": align,
        "font_name": font_name,
        "color": color,
        "use_template_position": use_template_position,
    }

    font_size = number("font_size")
    if font_size is not None:
        if not MIN_FONT_SIZE <= font_size <= MAX_FONT_SIZE:
            raise ValueError(
                f"{field} font size must be between {MIN_FONT_SIZE} and {MAX_FONT_SIZE}."
            )
        result["font_size"] = font_size

    x, y = number("x"), number("y")
    if (x is None) != (y is None):
        raise ValueError(f"{field} needs both x and y.")
    if x is not None:
        result["x"], result["y"] = x, y

    for key in ("width", "height", "max_width"):
        value = number(key, 0, 100000)
        if value is not None:
            result[key] = value

    page = number("page", 0, 10000)
    if page is not None:
        result["page"] = int(page)
    return result


def validate_template(path):
    path = Path(path)
    if path.suffix.lower() == ".pdf":
        with fitz.open(str(path)) as document:
            if not len(document):
                raise ValueError("The PDF template contains no pages.")
    else:
        with Image.open(path) as image:
            image.verify()


def extract_pdf_template_fields(template_path):
    """Placeholder names in reading order (top to bottom), each listed once."""
    fields = {}
    with fitz.open(str(template_path)) as document:
        for page in document:
            for match in PLACEHOLDER_PATTERN.finditer(page.get_text("text")):
                name = normalize_field_name(match.group(1))
                if name:
                    fields.setdefault(name, None)
    return list(fields)


def create_output_path(certificate_id, output_dir=None):
    folder = Path(output_dir) if output_dir else OUTPUT_FOLDER
    folder.mkdir(parents=True, exist_ok=True)
    original = str(certificate_id)
    safe_id = re.sub(r"[^a-zA-Z0-9_-]", "_", original)
    if safe_id != original:  # avoid 'a/b' and 'a_b' colliding
        safe_id += "_" + hashlib.sha1(original.encode()).hexdigest()[:8]
    return folder / f"{safe_id}.pdf"


def prepare_participant_fields(participant_data):
    if not isinstance(participant_data, dict):
        raise ValueError("Participant data must be a dictionary.")
    result = {}
    for key, value in participant_data.items():
        name = normalize_field_name(key)
        if name:
            result[name] = "" if value is None else str(value).strip()
    return result


# --------------------------------------------------------------------------
# Text drawing
# --------------------------------------------------------------------------

def bundled_fonts():
    """{'bundled:<slug>': Path} for every font file in the project's fonts/ folder."""
    fonts = {}
    if BUNDLED_FONT_DIR.is_dir():
        for path in sorted(BUNDLED_FONT_DIR.iterdir()):
            if path.suffix.lower() in FONT_EXTENSIONS:
                slug = re.sub(r"[^a-z0-9_\-]+", "_", path.stem.lower())[:60]
                fonts[f"bundled:{slug}"] = path
    return fonts


def is_valid_font_name(name):
    name = str(name)
    return (
        name in FONT_NAMES
        or bool(CUSTOM_FONT_PATTERN.fullmatch(name))
        or bool(BUNDLED_FONT_PATTERN.fullmatch(name))
    )


def font_file_for(font_name):
    """Path of a custom/bundled font, or None for the built-in PDF fonts."""
    if font_name in FONT_NAMES:
        return None
    path = _FONT_FILES.get().get(font_name) or bundled_fonts().get(font_name)
    if not path or not Path(path).is_file():
        raise ValueError(
            "The selected custom font is no longer available. Please upload it again."
        )
    return Path(path)


@lru_cache(maxsize=8)
def _unicode_font(path):
    return fitz.Font(fontfile=str(path))


@lru_cache(maxsize=8)
def _shaping_font(path):
    """Pillow font with HarfBuzz/Raqm shaping (None when Raqm is unavailable)."""
    if not features.check("raqm"):
        return None
    return ImageFont.truetype(str(path), 200, layout_engine=ImageFont.Layout.RAQM)


def _measure_file(path, text, size):
    shaped = _shaping_font(str(path))
    if shaped is not None:
        return shaped.getlength(text) * size / 200  # correct width for conjuncts/matras
    return _unicode_font(str(path)).text_length(text, fontsize=size)


_COMPLEX_SCRIPTS = (  # Hebrew/Arabic, Indic (Devanagari...Sinhala), Thai/Lao, Myanmar, Khmer
    (0x0590, 0x08FF), (0x0900, 0x0DFF), (0x0E00, 0x0EFF), (0x1000, 0x109F), (0x1780, 0x17FF),
)


def _is_complex(text):
    return any(lo <= ord(ch) <= hi for ch in text for lo, hi in _COMPLEX_SCRIPTS)


def _needs_unicode(text):
    try:
        text.encode("latin-1")
        return False
    except UnicodeEncodeError:
        return True


def _draw_shaped(page, text, font_path, size, color, x, cy, width):
    """Draw Indic/Arabic text through MuPDF's HTML engine, which applies shaping."""
    if not hasattr(page, "insert_htmlbox"):
        raise ValueError(
            "Telugu/Hindi and other complex scripts need PyMuPDF 1.24 or newer "
            "(pip install -U pymupdf)."
        )
    rgb = "#%02x%02x%02x" % tuple(round(c * 255) for c in color)
    markup = (
        f'<div style="font-family:cfont;font-size:{size:.2f}pt;line-height:1.35;'
        f'color:{rgb};white-space:nowrap;margin:0;padding:0">{html_lib.escape(text)}</div>'
    )
    css = "@font-face{font-family:cfont;src:url('%s');}" % font_path.name
    rect = fitz.Rect(x - 2, cy - size * 0.85, x + width * 1.25 + 8, cy + size * 0.85)
    spare, _scale = page.insert_htmlbox(
        rect, markup, css=css, archive=fitz.Archive(str(font_path.parent)), scale_low=0
    )
    if spare < 0:
        raise ValueError(f"Could not fit '{text}' on the certificate.")


def draw_text(page, text, cfg, cx, cy, box_width=0.0, default_size=22):
    """Draw one line of text around (cx, cy) (top-left origin), shrinking to fit."""
    text = " ".join(str(text).split())
    if not text:
        return
    page_width = page.rect.width
    align = cfg["align"]

    if align == "left":
        anchor = cx - box_width / 2
        room = page_width - PAGE_MARGIN - anchor
    elif align == "right":
        anchor = cx + box_width / 2
        room = anchor - PAGE_MARGIN
    else:
        anchor = cx
        room = 2 * min(anchor - PAGE_MARGIN, page_width - PAGE_MARGIN - anchor)
        room = min(room, page_width * MAX_TEXT_WIDTH_RATIO)
        if cfg.get("max_width"):  # don't run into neighbouring fields
            room = min(room, cfg["max_width"])
    if room <= 0:
        raise ValueError(f"The position for '{text}' is outside the page.")

    font_path = font_file_for(cfg["font_name"])
    if font_path is None and _needs_unicode(text):
        if not UNICODE_FONT_PATH.is_file():
            raise ValueError(
                f"'{text}' contains non-Latin characters. Upload a font that supports "
                "this language (for example Noto Sans Telugu) and select it for this field."
            )
        font_path = UNICODE_FONT_PATH

    if font_path is not None:
        measure = lambda size: _measure_file(font_path, text, size)  # noqa: E731
    else:
        measure = lambda size: fitz.get_text_length(  # noqa: E731
            text, fontname=cfg["font_name"], fontsize=size)

    size = cfg.get("font_size") or default_size
    width = measure(size)
    if width > room:
        size = size * room / width
        if size < MIN_FONT_SIZE:
            raise ValueError(f"Text '{text}' is too long to fit on the certificate.")
        width = measure(size)

    if align == "left":
        x = anchor
    elif align == "right":
        x = anchor - width
    else:
        x = anchor - width / 2

    if font_path is not None and _is_complex(text):
        _draw_shaped(page, text, font_path, size, cfg["color"], x, cy, width)
        return

    baseline = cy + size * 0.3  # visually centre the text on cy
    if font_path is not None:
        extra = {
            "fontname": "F" + hashlib.md5(str(font_path).encode()).hexdigest()[:8],
            "fontfile": str(font_path),
        }
    else:
        extra = {"fontname": cfg["font_name"]}
    page.insert_text(fitz.Point(x, baseline), text, fontsize=size, color=cfg["color"], **extra)


# --------------------------------------------------------------------------
# PDF templates
# --------------------------------------------------------------------------

def generate_pdf_from_pdf_template(template_path, output_path, fields, positions):
    document = fitz.open(str(template_path))
    try:
        if not len(document):
            raise ValueError("The PDF template contains no pages.")

        found = []  # (page_index, field, rect)
        for page_index, page in enumerate(document):
            raws = dict.fromkeys(
                m.group(0) for m in PLACEHOLDER_PATTERN.finditer(page.get_text("text"))
            )
            for raw in raws:
                field = normalize_field_name(PLACEHOLDER_PATTERN.fullmatch(raw).group(1))
                rects = page.search_for(raw)  # search the exact text, spaces included
                if not rects:
                    raise ValueError(
                        f"Could not locate {raw} in the PDF. "
                        "The placeholder may be split across text elements."
                    )
                if field not in fields:
                    raise ValueError(
                        f"Template placeholder '{raw}' has no matching participant column."
                    )
                found.extend((page_index, field, rect) for rect in rects)

        # Remove placeholder text only (no white box, images untouched).
        for page_index in sorted({item[0] for item in found}):
            page = document[page_index]
            for p_index, _field, rect in found:
                if p_index == page_index:
                    page.add_redact_annot(rect, fill=False, cross_out=False)
            page.apply_redactions(images=fitz.PDF_REDACT_IMAGE_NONE)

        for page_index, field, rect in found:
            value = fields.get(field, "").strip()
            if not value:
                continue
            page = document[page_index]
            cfg = positions.get(field) or normalize_position({}, field)
            cx, cy, box_w = (rect.x0 + rect.x1) / 2, (rect.y0 + rect.y1) / 2, rect.width
            if not cfg["use_template_position"] and "x" in cfg:
                cx, cy, box_w = cfg["x"], page.rect.height - cfg["y"], cfg.get("width", 0)
            draw_text(page, value, cfg, cx, cy, box_w, min(24, max(9, rect.height * 1.1)))

        if not found:  # coordinate-based PDF without placeholders
            if not positions:
                raise ValueError("This PDF has no placeholders. Configure field positions first.")
            for field, cfg in positions.items():
                value = fields.get(field, "").strip()
                if not value:
                    continue
                if "x" not in cfg:
                    raise ValueError(f"Field '{field}' needs an x and y position.")
                page_number = cfg.get("page", 0)
                if page_number >= len(document):
                    raise ValueError(f"Field '{field}' refers to a missing page.")
                page = document[page_number]
                draw_text(
                    page, value, cfg, cfg["x"], page.rect.height - cfg["y"],
                    cfg.get("width", 0), 24,
                )

        output_path = Path(output_path)
        output_path.parent.mkdir(parents=True, exist_ok=True)
        document.save(str(output_path), garbage=4, deflate=True)
    finally:
        document.close()


# --------------------------------------------------------------------------
# Image templates
# --------------------------------------------------------------------------

def generate_pdf_from_image_template(
    template_path, output_path, fields, positions, cleaned_template_path=None
):
    background = Path(cleaned_template_path) if cleaned_template_path else Path(template_path)
    if not background.is_file():
        background = Path(template_path)

    stream = None
    with Image.open(background) as source:
        width_px, height_px = ImageOps.exif_transpose(source).size
        if source.getexif().get(274, 1) != 1:  # EXIF-rotated photo: bake rotation in
            from io import BytesIO
            buffer = BytesIO()
            ImageOps.exif_transpose(source).convert("RGB").save(buffer, format="PNG")
            stream = buffer.getvalue()

    document = fitz.open()
    try:
        page = document.new_page(width=width_px * PX_TO_PT, height=height_px * PX_TO_PT)
        if stream:
            page.insert_image(page.rect, stream=stream)
        else:  # embed the file as-is (no re-encoding)
            page.insert_image(page.rect, filename=str(background))

        for field, cfg in positions.items():
            value = fields.get(field, "").strip()
            if not value:
                continue
            if "x" not in cfg:
                raise ValueError(f"No position known for field '{field}'.")
            draw_text(
                page, value, cfg, cfg["x"], page.rect.height - cfg["y"],
                cfg.get("width", 0), 22,
            )

        output_path = Path(output_path)
        output_path.parent.mkdir(parents=True, exist_ok=True)
        document.save(str(output_path), garbage=4, deflate=True)
    finally:
        document.close()


# --------------------------------------------------------------------------
# Public entry point
# --------------------------------------------------------------------------

def generate_certificate(
    certificate_id, template_path, positions=None, participant_data=None,
    cleaned_template_path=None, output_dir=None, font_files=None,
):
    if not template_path:
        raise ValueError("A certificate template is required.")
    template_path = Path(template_path)
    if not template_path.is_file():
        raise FileNotFoundError(f"Template file not found: {template_path}")

    fields = prepare_participant_fields(participant_data or {})
    fields.setdefault("certificate_id", str(certificate_id))
    normalized_positions = {}
    for key, value in (positions or {}).items():
        name = normalize_field_name(key)
        if name:
            normalized_positions[name] = normalize_position(value, name)

    output_path = create_output_path(certificate_id, output_dir)
    extension = template_path.suffix.lower()

    token = _FONT_FILES.set(dict(font_files or {}))  # visible to draw_text in this thread only
    try:
        if extension == ".pdf":
            generate_pdf_from_pdf_template(template_path, output_path, fields, normalized_positions)
        elif extension in {".png", ".jpg", ".jpeg"}:
            if not normalized_positions:
                raise ValueError("Image templates require detected or manually configured field positions.")
            generate_pdf_from_image_template(
                template_path, output_path, fields, normalized_positions,
                cleaned_template_path=cleaned_template_path,
            )
        else:
            raise ValueError("Template must be PDF, PNG, JPG, or JPEG.")
    finally:
        _FONT_FILES.reset(token)
    return str(output_path)