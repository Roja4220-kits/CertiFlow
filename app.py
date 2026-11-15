import io
import json
import os
import re
import threading
import time
import uuid
import zipfile
from collections import defaultdict, deque
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timedelta, timezone
from functools import wraps
from pathlib import Path

from flask import (
    Flask, jsonify, redirect, render_template, request,
    send_file, send_from_directory, url_for,
)
from werkzeug.utils import secure_filename

from certificate_generator import (
    FONT_EXTENSIONS, bundled_fonts, extract_pdf_template_fields,
    generate_certificate, normalize_field_name, normalize_position, validate_template,
)
from models import (
    CertificateJob, CertificateRecipient, JobStatus, RecipientStatus,
    configure_sqlite, db, ensure_schema,
)
from participants import cell_to_text, read_participant_file
from template_ocr import (
    clean_template_image, detect_template_placeholders, image_positions_from_detection,
)
from validation import FORMATS, build_report, label_columns, participant_label

app = Flask(__name__)


def _env_int(name, default):
    try:
        return int(os.environ.get(name, default))
    except ValueError:
        return default


# --------------------------------------------------------------------------
# Configuration (all overridable with environment variables)
# --------------------------------------------------------------------------
DATA_DIR = Path(os.environ.get("CERTIFLOW_DATA_DIR") or app.root_path)
MAX_UPLOAD_MB = _env_int("CERTIFLOW_MAX_UPLOAD_MB", 25)
MAX_ROWS = _env_int("CERTIFLOW_MAX_ROWS", 5000)
WORKERS = max(1, _env_int("CERTIFLOW_WORKERS", 2))
RETENTION_HOURS = max(1, _env_int("CERTIFLOW_RETENTION_HOURS", 24))
PREVIEW_ROWS = 100
MAX_FONT_MB = 8
MAX_FONTS_PER_UPLOAD = 6

UPLOAD_FOLDER = DATA_DIR / "uploads"
TEMPLATE_FOLDER = UPLOAD_FOLDER / "templates"
PARTICIPANT_FOLDER = UPLOAD_FOLDER / "participants"
FONT_FOLDER = UPLOAD_FOLDER / "fonts"
GENERATED_FOLDER = DATA_DIR / "generated_certificates"
INSTANCE_FOLDER = DATA_DIR / "instance"
CLEANED_TEMPLATE_FOLDER = INSTANCE_FOLDER / "cleaned_templates"
EXAMPLES_FOLDER = Path(app.root_path) / "examples"

for folder in (
    TEMPLATE_FOLDER, PARTICIPANT_FOLDER, FONT_FOLDER, GENERATED_FOLDER,
    INSTANCE_FOLDER, CLEANED_TEMPLATE_FOLDER,
):
    folder.mkdir(parents=True, exist_ok=True)

app.config["MAX_CONTENT_LENGTH"] = MAX_UPLOAD_MB * 1024 * 1024
app.config["SQLALCHEMY_DATABASE_URI"] = "sqlite:///" + str(INSTANCE_FOLDER / "certificates.db")
app.config["SQLALCHEMY_TRACK_MODIFICATIONS"] = False
app.config["SYNC_JOBS"] = False  # tests set True: run jobs inline instead of in the background

if os.environ.get("CERTIFLOW_TRUST_PROXY") == "1":  # behind nginx / Caddy / a platform router
    from werkzeug.middleware.proxy_fix import ProxyFix
    app.wsgi_app = ProxyFix(app.wsgi_app, x_for=1, x_proto=1, x_host=1)

configure_sqlite()
db.init_app(app)

TEMPLATE_EXTENSIONS = {".pdf", ".png", ".jpg", ".jpeg"}
PARTICIPANT_EXTENSIONS = {".csv", ".xlsx", ".pdf"}
SOURCE_LABELS = {".csv": "CSV file", ".xlsx": "Excel file", ".pdf": "participant file"}
UPLOAD_ID_PATTERN = re.compile(r"[a-fA-F0-9]{32}")

executor = ThreadPoolExecutor(max_workers=WORKERS, thread_name_prefix="certiflow")


# --------------------------------------------------------------------------
# Public-access protection: rate limiting + security headers
# --------------------------------------------------------------------------
_hits = defaultdict(deque)
_hits_lock = threading.Lock()


def rate_limited(name, limit, window=60):
    """Allow `limit` calls per `window` seconds per client IP."""
    def decorator(view):
        @wraps(view)
        def wrapper(*args, **kwargs):
            if app.config.get("TESTING"):
                return view(*args, **kwargs)
            key, now = (name, request.remote_addr), time.time()
            with _hits_lock:
                queue = _hits[key]
                while queue and queue[0] < now - window:
                    queue.popleft()
                if len(queue) >= limit:
                    return jsonify({"error": "Too many requests. Please wait a minute and try again."}), 429
                queue.append(now)
            return view(*args, **kwargs)
        return wrapper
    return decorator


@app.after_request
def security_headers(response):
    response.headers.setdefault("X-Content-Type-Options", "nosniff")
    response.headers.setdefault("X-Frame-Options", "SAMEORIGIN")
    response.headers.setdefault("Referrer-Policy", "same-origin")
    if request.path.startswith("/api/"):
        response.headers.setdefault("Cache-Control", "no-store")
    return response


# --------------------------------------------------------------------------
# Helpers
# --------------------------------------------------------------------------
class ValidationFailed(Exception):
    def __init__(self, report):
        super().__init__(report["text"])
        self.report = report


def check_upload_id(upload_id):
    if not isinstance(upload_id, str) or not UPLOAD_ID_PATTERN.fullmatch(upload_id):
        raise ValueError("Missing or invalid upload_id. Please upload the template again.")
    return upload_id


def find_uploaded_template(upload_id):
    check_upload_id(upload_id)
    matches = list(TEMPLATE_FOLDER.glob(f"{upload_id}_*"))
    if len(matches) != 1 or not matches[0].is_file():
        raise ValueError("Uploaded template not found. Please upload it again.")
    path = matches[0]
    if path.suffix.lower() not in TEMPLATE_EXTENSIONS:
        raise ValueError("Unsupported certificate template format.")
    return path


def inspect_template(template_path, upload_id):
    """Detect fields and positions. OCR failure is reported, not fatal."""
    info = {
        "template_fields": [], "positions": {}, "cleaned_path": None,
        "ocr_detected": False, "ocr_fields": [], "ocr_error": None,
    }
    if template_path.suffix.lower() == ".pdf":
        info["template_fields"] = extract_pdf_template_fields(template_path)
        return info
    try:
        detection = detect_template_placeholders(template_path)
        if detection["fields"]:
            cleaned = CLEANED_TEMPLATE_FOLDER / f"{upload_id}_cleaned.png"
            clean_template_image(template_path, cleaned, detection_result=detection)
            info.update(
                template_fields=[item["field"] for item in detection["fields"]],
                positions=image_positions_from_detection(detection),
                cleaned_path=str(cleaned), ocr_detected=True, ocr_fields=detection["fields"],
            )
    except Exception as error:  # e.g. Tesseract not installed
        app.logger.exception("OCR failed")
        info["ocr_error"] = str(error)
    return info


def get_template_info(upload_id, template_path):
    """inspect_template(), cached on disk so OCR runs once per upload."""
    cache = CLEANED_TEMPLATE_FOLDER / f"{upload_id}.json"
    if cache.is_file():
        try:
            info = json.loads(cache.read_text(encoding="utf-8"))
            if not info.get("cleaned_path") or Path(info["cleaned_path"]).is_file():
                return info
        except (OSError, ValueError):
            pass
    info = inspect_template(template_path, upload_id)
    cache.write_text(json.dumps(info), encoding="utf-8")
    return info


def load_template(upload_id):
    template_path = find_uploaded_template(upload_id)
    info = get_template_info(upload_id, template_path)
    cleaned = Path(info["cleaned_path"]) if info.get("cleaned_path") else None
    return template_path, info, cleaned


def participants_file(upload_id):
    return PARTICIPANT_FOLDER / f"{check_upload_id(upload_id)}.json"


def load_participants(upload_id):
    path = participants_file(upload_id)
    if not path.is_file():
        raise ValueError("Uploaded participant data not found. Please upload your files again.")
    return json.loads(path.read_text(encoding="utf-8"))


def clean_mapping(value):
    if value in (None, ""):
        return {}
    if not isinstance(value, dict):
        raise ValueError("mapping must be a JSON object.")
    return {
        normalize_field_name(k): normalize_field_name(v or "")
        for k, v in value.items() if normalize_field_name(k)
    }


def clean_list(value):
    if value in (None, ""):
        return []
    if not isinstance(value, list):
        raise ValueError("optional_fields must be a list.")
    return [normalize_field_name(v) for v in value if normalize_field_name(v)]


def clean_formats(value):
    """{'start_date': 'date', ...} -> normalised names, only known format names."""
    if value in (None, ""):
        return {}
    if not isinstance(value, dict):
        raise ValueError("field_formats must be a JSON object.")
    result = {}
    for key, kind in value.items():
        field, kind = normalize_field_name(key), str(kind or "any").lower()
        if kind not in FORMATS:
            raise ValueError(f"Unknown format '{kind}' for '{field}'. Use any, date, number or email.")
        if field:
            result[field] = kind
    return result


def request_checks(data):
    """The validation options the user chose (mapping, optional, unique, formats, label)."""
    return {
        "mapping": clean_mapping(data.get("mapping")),
        "optional_fields": clean_list(data.get("optional_fields")),
        "unique_fields": clean_list(data.get("unique_fields")),
        "field_formats": clean_formats(data.get("field_formats")),
        "label_column": normalize_field_name(data.get("label_column") or ""),
    }


def validate_positions(positions, allowed_fields):
    if not isinstance(positions, dict):
        raise ValueError("positions must be a JSON object.")
    normalized = {}
    for raw_field, raw_config in positions.items():
        field = normalize_field_name(raw_field)
        if field not in allowed_fields:
            raise ValueError(f"Position settings refer to unknown template field '{field}'.")
        normalized[field] = normalize_position(raw_config, field)
    return normalized


def merge_positions(detected, supplied):
    """Per-field merge: user formatting overrides, detected x/y survive."""
    return {
        field: {**detected.get(field, {}), **supplied.get(field, {})}
        for field in set(detected) | set(supplied)
    }


def check_positions_ready(info, template_fields, positions):
    """Without placeholders in the template, every field needs manual x/y."""
    if info["template_fields"]:
        return
    missing = [
        f for f in template_fields
        if "x" not in positions.get(f, {}) or "y" not in positions.get(f, {})
    ]
    if missing:
        raise ValueError(
            "No placeholders were found in the template, so an X/Y position is "
            "required for: " + ", ".join(missing)
        )


# ---- uploaded fonts (private to one upload session) ------------------------
def uploaded_fonts(upload_id):
    """{'custom:<hex>': (Path, label)} for fonts uploaded in this session."""
    fonts = {}
    if isinstance(upload_id, str) and UPLOAD_ID_PATTERN.fullmatch(upload_id):
        for path in sorted(FONT_FOLDER.glob(f"{upload_id}__*")):
            parts = path.stem.split("__", 2)
            if len(parts) == 3 and re.fullmatch(r"[0-9a-f]{32}", parts[1]):
                fonts[f"custom:{parts[1]}"] = (path, parts[2].replace("_", " "))
    return fonts


def font_files_for(upload_id):
    return {font_id: path for font_id, (path, _label) in uploaded_fonts(upload_id).items()}


# --------------------------------------------------------------------------
# Pages
# --------------------------------------------------------------------------
@app.route("/")
def home():
    return redirect(url_for("dashboard"))


@app.route("/dashboard")
def dashboard():
    return render_template("index.html")


@app.route("/api/health")
def health():
    return jsonify({"status": "ok"})


@app.route("/examples/<path:filename>")
def download_example(filename):
    return send_from_directory(str(EXAMPLES_FOLDER), filename, as_attachment=True)


# --------------------------------------------------------------------------
# Upload, validation, preview
# --------------------------------------------------------------------------
def _row_error_text(row, report):
    if report["missing_columns"]:
        return "Missing column(s): " + ", ".join(report["missing_columns"])
    parts = []
    for label, key in (("Missing value(s)", "cell_issues"), ("Duplicate value(s)", "duplicate_issues"),
                       ("Invalid format", "format_issues")):
        fields = [i["field"] for i in report[key] if i["row"] == row]
        if fields:
            parts.append(f"{label}: " + ", ".join(fields))
    return "; ".join(parts) or None


@app.route("/api/preview-upload", methods=["POST"])
@rate_limited("upload", 20)
def preview_upload():
    template = request.files.get("template")
    participant_file = request.files.get("participants")
    if not template or not template.filename:
        return jsonify({"error": "Please select a certificate template."}), 400
    if not participant_file or not participant_file.filename:
        return jsonify({"error": "Please select a participant file."}), 400

    template_name = secure_filename(template.filename)
    participant_name = secure_filename(participant_file.filename)
    template_ext = Path(template_name).suffix.lower()
    participant_ext = Path(participant_name).suffix.lower()
    if template_ext not in TEMPLATE_EXTENSIONS:
        return jsonify({"error": "Template must be PDF, PNG, JPG, or JPEG."}), 400
    if participant_ext not in PARTICIPANT_EXTENSIONS:
        return jsonify({"error": "Participant file must be CSV, XLSX, or PDF."}), 400

    upload_id = uuid.uuid4().hex
    template_path = TEMPLATE_FOLDER / f"{upload_id}_{template_name}"
    raw_path = PARTICIPANT_FOLDER / f"{upload_id}_{participant_name}"

    try:
        template.save(template_path)
        participant_file.save(raw_path)
        validate_template(template_path)
        data = read_participant_file(raw_path, participant_ext, max_rows=MAX_ROWS)
        source_label = SOURCE_LABELS[participant_ext]

        info = get_template_info(upload_id, template_path)
        template_fields = list(info["template_fields"])
        requires_positions = not template_fields
        if not template_fields:
            template_fields = list(data["headers"])

        report = build_report(
            template_fields, data["headers"], data["participants"],
            blank_rows_skipped=data["blank_rows_skipped"], source_label=source_label,
        )
        stored = dict(data, source_label=source_label)
        participants_file(upload_id).write_text(
            json.dumps(stored, ensure_ascii=False), encoding="utf-8"
        )

        preview = []
        for person in data["participants"][:PREVIEW_ROWS]:
            error = _row_error_text(person["row"], report)
            preview.append({
                "row": person["row"], "fields": person["fields"],
                "valid": error is None, "error": error,
            })
        invalid = report["total_rows"] if report["missing_columns"] else report["invalid_rows"]

        return jsonify({
            "message": "Files uploaded and fields inspected successfully.",
            "upload_id": upload_id,
            "template_fields": template_fields,
            "available_fields": data["headers"],
            "original_headers": data["original_headers"],
            "matched_fields": report["matched_fields"],
            "missing_fields": report["missing_columns"],
            "extra_fields": report["extra_columns"],
            "requires_positions": requires_positions,
            "automatic_positions": info["positions"],
            "ocr_detected": info["ocr_detected"],
            "ocr_fields": info["ocr_fields"],
            "ocr_error": info["ocr_error"],
            "cleaned_template_available": bool(info["cleaned_path"]),
            "source_label": source_label,
            "total": report["total_rows"],
            "valid": report["total_rows"] - invalid,
            "invalid": invalid,
            "blank_rows_skipped": data["blank_rows_skipped"],
            "preview_limit": PREVIEW_ROWS,
            "participants": preview,
            "report": report,
        }), 200

    except Exception as error:
        template_path.unlink(missing_ok=True)
        participants_file(upload_id).unlink(missing_ok=True)
        app.logger.warning("Upload preview failed: %s", error)
        return jsonify({"error": str(error)}), 400
    finally:
        raw_path.unlink(missing_ok=True)  # the parsed rows are kept; the raw file is not


@app.route("/api/validate", methods=["POST"])
@rate_limited("validate", 120)
def validate_mapping():
    """Re-run validation after the user changes the field mapping or optional flags."""
    data = request.get_json(silent=True)
    if not isinstance(data, dict):
        return jsonify({"error": "Request body must be a JSON object."}), 400
    try:
        upload_id = data.get("upload_id")
        _template, info, _cleaned = load_template(upload_id)
        stored = load_participants(upload_id)
        template_fields = info["template_fields"] or [
            normalize_field_name(k) for k in (data.get("template_fields") or stored["headers"])
        ]
        report = build_report(
            template_fields, stored["headers"], stored["participants"],
            blank_rows_skipped=stored["blank_rows_skipped"],
            source_label=stored["source_label"], **request_checks(data),
        )
        return jsonify({"report": report})
    except Exception as error:
        return jsonify({"error": str(error)}), 400


@app.route("/api/preview-certificate", methods=["POST"])
@rate_limited("preview", 30)
def preview_certificate():
    data = request.get_json(silent=True)
    if not isinstance(data, dict):
        return jsonify({"error": "Request body must be a JSON object."}), 400

    try:
        upload_id = data.get("upload_id")
        template_path, info, cleaned_path = load_template(upload_id)
        fields = data.get("fields")
        if not isinstance(fields, dict) or not fields:
            raise ValueError("A participant row is required for preview.")
        normalized_fields = {
            normalize_field_name(k): "" if v is None else str(v).strip()
            for k, v in fields.items() if normalize_field_name(k)
        }
        template_fields = info["template_fields"] or list(normalized_fields)
        supplied = validate_positions(data.get("positions", {}), set(template_fields))
        positions = merge_positions(info["positions"], supplied)
        check_positions_ready(info, template_fields, positions)

        output_path = Path(generate_certificate(
            certificate_id="PREVIEW-" + uuid.uuid4().hex[:12].upper(),
            template_path=template_path, positions=positions,
            participant_data=normalized_fields, cleaned_template_path=cleaned_path,
            output_dir=GENERATED_FOLDER, font_files=font_files_for(upload_id),
        ))
    except Exception as error:
        app.logger.warning("Certificate preview failed: %s", error)
        return jsonify({"error": str(error)}), 400

    response = send_file(
        output_path, mimetype="application/pdf", as_attachment=False,
        download_name="certiflow-preview.pdf",
    )

    @response.call_on_close
    def remove_preview():
        try:
            output_path.unlink(missing_ok=True)
        except OSError:
            app.logger.warning("Could not remove temporary preview %s", output_path)

    return response


# --------------------------------------------------------------------------
# Fonts
# --------------------------------------------------------------------------
@app.route("/api/fonts", methods=["GET"])
def list_fonts():
    fonts = [
        {"id": font_id, "label": path.stem.replace("_", " ") + " (built-in)", "kind": "bundled"}
        for font_id, path in bundled_fonts().items()
    ]
    for font_id, (_path, label) in uploaded_fonts(request.args.get("upload_id")).items():
        fonts.append({"id": font_id, "label": label, "kind": "custom"})
    return jsonify({"fonts": fonts})


@app.route("/api/fonts", methods=["POST"])
@rate_limited("fonts", 20)
def upload_font():
    upload_id = request.form.get("upload_id")
    font = request.files.get("font")
    try:
        find_uploaded_template(upload_id)  # fonts belong to an existing upload session
        if not font or not font.filename:
            raise ValueError("Please choose a font file (.ttf or .otf).")
        name = secure_filename(font.filename)
        extension = Path(name).suffix.lower()
        if extension not in FONT_EXTENSIONS:
            raise ValueError("Fonts must be .ttf or .otf files.")
        if len(uploaded_fonts(upload_id)) >= MAX_FONTS_PER_UPLOAD:
            raise ValueError(f"You can add up to {MAX_FONTS_PER_UPLOAD} fonts per batch.")
        content = font.read(MAX_FONT_MB * 1024 * 1024 + 1)
        if len(content) > MAX_FONT_MB * 1024 * 1024:
            raise ValueError(f"The font file is larger than {MAX_FONT_MB} MB.")

        font_hex = uuid.uuid4().hex
        label = re.sub(r"[^A-Za-z0-9\-]+", "_", Path(name).stem).strip("_")[:40] or "font"
        path = FONT_FOLDER / f"{upload_id}__{font_hex}__{label}{extension}"
        path.write_bytes(content)
        try:  # make sure it really is a usable font before keeping it
            from PIL import ImageFont
            ImageFont.truetype(str(path), 20)
        except Exception:
            path.unlink(missing_ok=True)
            raise ValueError("This file is not a valid font.")
        return jsonify({"id": f"custom:{font_hex}", "label": label.replace("_", " "), "kind": "custom"}), 201
    except Exception as error:
        return jsonify({"error": str(error)}), 400


# --------------------------------------------------------------------------
# Bulk generation (background job)
# --------------------------------------------------------------------------
def collect_rows(data, upload_id):
    """(available columns, participants, blank rows, source label) for this request."""
    recipients = data.get("recipients")
    if recipients is not None:  # direct API use: rows are sent in the request
        if not isinstance(recipients, list) or not recipients:
            raise ValueError("recipients must be a non-empty list.")
        participants = []
        for index, recipient in enumerate(recipients, start=2):
            if not isinstance(recipient, dict) or not isinstance(recipient.get("fields"), dict):
                raise ValueError(f"Participant row {index} has invalid data.")
            fields = {
                normalize_field_name(k): cell_to_text(v)
                for k, v in recipient["fields"].items() if normalize_field_name(k)
            }
            participants.append({"row": index, "fields": fields})
        available = list(dict.fromkeys(k for p in participants for k in p["fields"]))
        return available, participants, 0, "CSV file"
    stored = load_participants(upload_id)  # normal use: rows were stored at upload time
    return stored["headers"], stored["participants"], stored["blank_rows_skipped"], stored["source_label"]


def run_job(job_id, template_path, positions, cleaned_path, font_files):
    """Generate every certificate of one job (runs in a worker thread)."""
    with app.app_context():
        try:
            job = db.session.get(CertificateJob, job_id)
            job.status = JobStatus.PROCESSING
            db.session.commit()
            record_ids = [r.id for r in job.recipients]
            ok = bad = 0
            for record_id in record_ids:
                record = db.session.get(CertificateRecipient, record_id)
                fields = {k: v for k, v in (record.participant_data or {}).items() if not k.startswith("_")}
                try:
                    output = generate_certificate(
                        certificate_id=record.certificate_id, template_path=template_path,
                        positions=positions, participant_data=fields,
                        cleaned_template_path=cleaned_path, output_dir=GENERATED_FOLDER,
                        font_files=font_files,
                    )
                    record.status = RecipientStatus.SUCCESS
                    record.file_path = str(Path(output).resolve())
                    ok += 1
                except Exception as error:
                    app.logger.warning("Certificate %s failed: %s", record.certificate_id, error)
                    record.status = RecipientStatus.FAILED
                    record.error_message = str(error)
                    bad += 1
                job = db.session.get(CertificateJob, job_id)
                job.successful, job.failed = ok, bad
                db.session.commit()  # commit per certificate so progress is visible
            job = db.session.get(CertificateJob, job_id)
            job.status = JobStatus.COMPLETED
            db.session.commit()
        except Exception as error:
            db.session.rollback()
            app.logger.exception("Job %s crashed", job_id)
            job = db.session.get(CertificateJob, job_id)
            job.status, job.error_message = JobStatus.FAILED, str(error)
            db.session.commit()


def job_payload(job, include_results=False):
    payload = {
        "job_id": job.token, "status": job.status,
        "total": job.total_recipients, "successful": job.successful, "failed": job.failed,
        "progress_percentage": job.progress_percentage, "error": job.error_message,
        "status_url": f"/api/jobs/{job.token}",
        "download_url": f"/api/jobs/{job.token}/download" if job.successful else None,
    }
    if include_results:
        results = []
        for r in job.recipients:
            item = r.to_dict()
            item.pop("file", None)  # never expose server paths
            if r.status == RecipientStatus.SUCCESS:
                item["download_url"] = f"/api/certificates/{r.certificate_id}"
            results.append(item)
        payload["results"] = results
    return payload


@app.route("/api/certificates/bulk", methods=["POST"])
@rate_limited("bulk", 10)
def generate_bulk_certificates():
    data = request.get_json(silent=True)
    if not isinstance(data, dict):
        return jsonify({"error": "Request body must be a JSON object."}), 400

    try:
        upload_id = data.get("upload_id")
        template_path, info, cleaned_path = load_template(upload_id)
        available, participants, blank_rows, source_label = collect_rows(data, upload_id)

        raw_positions = data.get("positions", {})
        if not isinstance(raw_positions, dict):
            raise ValueError("positions must be a JSON object.")
        template_fields = (
            info["template_fields"]
            or [normalize_field_name(k) for k in raw_positions if normalize_field_name(k)]
            or available
        )

        checks = request_checks(data)
        report = build_report(
            template_fields, available, participants,
            blank_rows_skipped=blank_rows, source_label=source_label, **checks,
        )
        if report["missing_columns"]:
            raise ValidationFailed(report)  # never generate with a required column missing
        skip_invalid = data.get("on_invalid") == "skip"
        if report["problem_count"] and not skip_invalid:
            raise ValidationFailed(report)  # never generate with empty, repeated or badly formatted values

        supplied = validate_positions(raw_positions, set(template_fields))
        positions = merge_positions(info["positions"], supplied)
        check_positions_ready(info, template_fields, positions)

        bad_rows = set(report["invalid_row_numbers"])
        label_cols = label_columns(available, checks["label_column"])
        rows, skipped = [], []
        for person in participants:
            label = participant_label(person["fields"], label_cols, person["row"])
            if person["row"] in bad_rows:
                skipped.append({"row": person["row"], "participant": label})
                continue
            fields = {
                f: (person["fields"].get(report["column_for"][f], "") if report["column_for"][f] else "")
                for f in template_fields
            }
            rows.append({**fields, "_row": person["row"], "_label": label})
        if not rows:
            raise ValueError("There are no participants left to generate certificates for.")
        if len(rows) > MAX_ROWS:
            raise ValueError(f"A batch can contain at most {MAX_ROWS} participants.")
    except ValidationFailed as failure:
        return jsonify({"error": failure.report["text"], "report": failure.report}), 400
    except Exception as error:
        return jsonify({"error": str(error)}), 400

    job = CertificateJob(status=JobStatus.QUEUED, total_recipients=len(rows))
    db.session.add(job)
    db.session.flush()
    for row in rows:
        db.session.add(CertificateRecipient(
            job_id=job.id, certificate_id=f"CERT-{uuid.uuid4().hex[:10].upper()}",
            participant_data=row, status=RecipientStatus.PENDING,
        ))
    db.session.commit()

    args = (job.id, template_path, positions, cleaned_path, font_files_for(upload_id))
    if app.config.get("SYNC_JOBS"):
        job_id = job.id
        run_job(*args)
        db.session.rollback()  # run_job used its own session: drop this session's stale copies
        job = db.session.get(CertificateJob, job_id)
        payload = job_payload(job, include_results=True)
        payload.update(message="Bulk certificate processing finished.", skipped=skipped)
        return jsonify(payload), 200

    executor.submit(run_job, *args)
    payload = job_payload(job)
    payload.update(message="Your certificates are being generated.", skipped=skipped)
    return jsonify(payload), 202


# --------------------------------------------------------------------------
# Jobs and downloads (jobs are addressed by an unguessable token)
# --------------------------------------------------------------------------
def find_job(token):
    return db.session.execute(db.select(CertificateJob).filter_by(token=token)).scalar_one_or_none()


@app.route("/api/jobs/<token>", methods=["GET"])
def get_job_status(token):
    job = find_job(token)
    if job is None:
        return jsonify({"error": "Job not found."}), 404
    return jsonify(job_payload(job, include_results=request.args.get("results") == "1"))


@app.route("/api/certificates/<certificate_id>", methods=["GET"])
def get_certificate(certificate_id):
    recipient = db.session.execute(
        db.select(CertificateRecipient).filter_by(certificate_id=certificate_id)
    ).scalar_one_or_none()
    if recipient is None or recipient.status != RecipientStatus.SUCCESS or not recipient.file_path:
        return jsonify({"error": "Certificate not found or not generated successfully."}), 404
    path = Path(recipient.file_path)
    if not path.is_file():
        return jsonify({"error": "Generated certificate file is missing."}), 404
    return send_from_directory(str(path.parent), path.name, as_attachment=True)


@app.route("/api/jobs/<token>/download", methods=["GET"])
def download_job(token):
    job = find_job(token)
    if job is None:
        return jsonify({"error": "Job not found."}), 404
    files = [
        Path(r.file_path) for r in job.recipients
        if r.status == RecipientStatus.SUCCESS and r.file_path and Path(r.file_path).is_file()
    ]
    if not files:
        return jsonify({"error": "This job has no successful certificates to download."}), 404
    buffer = io.BytesIO()
    with zipfile.ZipFile(buffer, "w", zipfile.ZIP_DEFLATED) as archive:
        for path in files:
            archive.write(path, path.name)
    buffer.seek(0)
    return send_file(
        buffer, mimetype="application/zip", as_attachment=True,
        download_name="certiflow_certificates.zip",
    )


# --------------------------------------------------------------------------
# Errors
# --------------------------------------------------------------------------
@app.errorhandler(413)
def file_too_large(_error):
    return jsonify({"error": f"Upload too large. Maximum request size is {MAX_UPLOAD_MB} MB."}), 413


@app.errorhandler(404)
def not_found(_error):
    if request.path.startswith("/api/"):
        return jsonify({"error": "Not found."}), 404
    return "Page not found.", 404


@app.errorhandler(500)
def server_error(_error):
    if request.path.startswith("/api/"):
        return jsonify({"error": "Something went wrong on the server. Please try again."}), 500
    return "Something went wrong.", 500


# --------------------------------------------------------------------------
# Housekeeping: restart recovery + automatic deletion of old data
# --------------------------------------------------------------------------
def recover_interrupted_jobs():
    stuck = db.session.scalars(
        db.select(CertificateJob).where(
            CertificateJob.status.in_([JobStatus.QUEUED, JobStatus.PROCESSING])
        )
    ).all()
    for job in stuck:
        job.status = JobStatus.FAILED
        job.error_message = "The server restarted before this job finished. Please run it again."
        for record in job.recipients:
            if record.status in (RecipientStatus.PENDING, RecipientStatus.PROCESSING):
                record.status, record.error_message = RecipientStatus.FAILED, "Interrupted by a server restart."
                job.failed = (job.failed or 0) + 1
    db.session.commit()


def purge_expired():
    """Delete uploads, generated PDFs and job records older than the retention period."""
    cutoff = time.time() - RETENTION_HOURS * 3600
    for folder in (TEMPLATE_FOLDER, PARTICIPANT_FOLDER, CLEANED_TEMPLATE_FOLDER, FONT_FOLDER, GENERATED_FOLDER):
        for path in folder.iterdir():
            try:
                if path.is_file() and path.stat().st_mtime < cutoff:
                    path.unlink()
            except OSError:
                pass
    limit = datetime.now(timezone.utc).replace(tzinfo=None) - timedelta(hours=RETENTION_HOURS)
    with app.app_context():
        for job in db.session.scalars(db.select(CertificateJob).where(CertificateJob.created_at < limit)).all():
            db.session.delete(job)
        db.session.commit()


def start_background_tasks():
    if os.environ.get("CERTIFLOW_DISABLE_BACKGROUND") == "1":
        return

    def loop():
        while True:
            try:
                purge_expired()
            except Exception:
                app.logger.exception("Cleanup failed")
            time.sleep(1800)

    threading.Thread(target=loop, daemon=True, name="certiflow-cleanup").start()


with app.app_context():
    db.create_all()
    ensure_schema()
    recover_interrupted_jobs()
start_background_tasks()


if __name__ == "__main__":
    app.run(debug=os.environ.get("FLASK_DEBUG") == "1")