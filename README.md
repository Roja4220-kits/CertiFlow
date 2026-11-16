# CertiFlow — bulk certificate generator

Upload your own certificate design (PDF, PNG or JPG) and an Excel/CSV list. CertiFlow checks the data, fills every certificate, and gives you the PDFs.

## Folder layout
```
app.py                  web app: pages, API, background jobs
certificate_generator.py  creates the PDFs (fonts, text fitting)
template_ocr.py         finds {{placeholders}} in PNG/JPG designs
models.py               database tables
validation.py           data checks and the messages users see
mapping.py              suggests matching columns
participants.py         reads Excel / CSV files
fieldnames.py           name matching (Student Name = student_name)

templates/index.html    the page (includes the "Prepare your files" guide)
static/certiflow.css    the only stylesheet
static/app.js           page behaviour
examples/               sample design (PDF/PNG) + sample lists (CSV/Excel)
fonts/                  optional: .ttf/.otf files that appear in everyone's "Font" list
scripts/make_examples.py  recreates the example files

tests/  test_app.py  conftest.py        tests
requirements.txt        packages to install
requirements-dev.txt    extra packages, only for running tests
Dockerfile  Procfile  .dockerignore  .gitignore      deployment and Git
```
Created automatically when the app runs (safe to delete while the app is stopped): `uploads/`, `generated_certificates/`, `instance/` (job history database).

## Run locally
```
python -m venv venv
venv\Scripts\activate          # Mac/Linux: source venv/bin/activate
pip install -r requirements.txt
python app.py                  # open http://127.0.0.1:5000
```
- **Tesseract** is needed only for PNG/JPG designs (PDF designs don't use it). On Windows install it and keep the default path (`C:\Program Files\Tesseract-OCR`), or set `TESSERACT_CMD`.
- Set `FLASK_DEBUG=1` for development mode. Never use it on a public server.

## Tests
```
pip install -r requirements-dev.txt
pytest -q                                   # everything: real PDFs, database, background jobs, ZIP downloads
python -m unittest tests.test_validation tests.test_validation_v2 tests.test_validation_api tests.test_generator_logic tests.test_examples -v
playwright install chromium
python tests/ui_check.py                    # drives the real page in a browser; screenshots go to your temp folder
```
If `tests.test_examples` fails, your copy of an example file was edited. Restore the CSV/Excel examples with `python scripts/make_examples.py data`.

## Telugu / Hindi / other languages
1. Download a font that supports the language (free): *Noto Sans Telugu*, *Noto Sans Devanagari*, … from fonts.google.com.
2. Either upload it in the app (**Match fields & choose the look → Upload font**, then pick it under **Font**, or press **Use for all fields**), or put the `.ttf` in `fonts/` once so it is available to every user.
3. Needs PyMuPDF 1.24 or newer. Always check with **Preview one certificate** before generating a big batch.

## Public deployment (Render, Railway, Fly.io, or your own server)
The `Dockerfile` installs everything (including Tesseract). On Render:
1. Push the project to a **private** GitHub repository (`.gitignore` keeps uploads and certificates out).
2. New → Web Service → connect the repository → Language **Docker** → **Starter** plan.
3. Add a **Disk** mounted at `/data`.
4. Environment variables: `CERTIFLOW_DATA_DIR=/data` and `CERTIFLOW_TRUST_PROXY=1`.
5. Health check path: `/api/health`. Create the service; HTTPS is included.

Or on your own computer/server: `docker build -t certiflow .` then `docker run -p 8000:8000 -v certiflow-data:/data certiflow`.

Keep **one gunicorn worker** (the Dockerfile does): background jobs run inside the worker process. If the first build fails, read the platform's build log; the most common causes are a missing file in the repository or too little memory on a free plan.

| Variable | Default | Meaning |
|---|---|---|
| `CERTIFLOW_DATA_DIR` | app folder | where uploads, PDFs and the database live (mount a disk here) |
| `CERTIFLOW_TRUST_PROXY` | off | set `1` behind a proxy/platform so rate limits see real client IPs |
| `CERTIFLOW_MAX_UPLOAD_MB` | 25 | largest upload |
| `CERTIFLOW_MAX_ROWS` | 5000 | most participants per batch |
| `CERTIFLOW_WORKERS` | 2 | background threads generating certificates |
| `CERTIFLOW_RETENTION_HOURS` | 24 | uploads, PDFs and job records are deleted after this long |
| `CERTIFLOW_UNICODE_FONT` | `fonts/NotoSans-Regular.ttf` | fallback font for non-Latin text when no font is chosen |
| `TESSERACT_CMD` | auto | path to Tesseract if it is not found automatically |
| `PORT` | 8000 | port the container listens on (hosting platforms set this for you) |
| `FLASK_DEBUG` | off | `1` = development mode (local only) |

**Built in:** unguessable job links, no server paths in API output, per-IP rate limits, security headers, automatic deletion of old data, restart recovery of interrupted jobs, uploaded fonts private to one session.
**Not built in:** user accounts/login, virus scanning, per-user quotas. Anyone with a job's link can download that job's PDFs. Add a login layer or an upload gateway before exposing it to untrusted users at scale.

## Validation Centre (what is checked)
Missing columns, missing values, **duplicate values** (per field: "Must be unique"), **formats** (per field: date / number / email), blank rows, unused columns, and placeholders with no matching column. Column names that differ (for example `Full Name` for `{{student_name}}`) are **suggested, never applied without a click**. Every check is chosen by the user per field; no field name is built in. Nothing is generated while problems remain, unless the user marks a field optional or chooses to skip the affected people.

## Known limits
- Image designs (PNG/JPG) are read with OCR: use clear, high-contrast text. Only one copy of each placeholder is filled in an image design (PDF designs can repeat them).
- Text is drawn on one line and shrinks to fit; very long text that would need a font under 6 pt is refused with a message.
- Output page size follows the design; PNG/JPG designs are converted at 96 dpi.
- Complex scripts (Telugu, Hindi, …) need a suitable font and PyMuPDF 1.24+. Check real output with your own names.

## Troubleshooting
| Problem | Fix |
|---|---|
| `tesseract is not installed` / image designs show "Text recognition failed" | Install Tesseract, or set `TESSERACT_CMD`. PDF designs don't need it. |
| Page looks unstyled or old after an update | Hard refresh (Ctrl+F5). |
| Database error after updating the code | Stop the app and delete `instance/certificates.db` (only the job history is lost). |
| `SyntaxError` mentioning odd characters in a `.py` file | The file contains pasted text instead of code. Replace it with the correct file. |
| `tests.test_examples` fails | Run `python scripts/make_examples.py data`. |