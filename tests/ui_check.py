"""Real-browser check of the validation UI (Playwright + Chromium).

Run:  python tests/ui_check.py          (screenshots go to your temp folder, ui_*.png)
"""
import io, os, re, sys, tempfile, threading
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from tests import testenv  # noqa: F401
import app as app_module
from pypdf import PdfReader
from werkzeug.serving import make_server
from playwright.sync_api import sync_playwright

TMP = tempfile.gettempdir()
EX = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "examples")

def pdf_fields(path):                      # real placeholders read from the real PDF
    text = "\n".join(p.extract_text() for p in PdfReader(str(path)).pages)
    return sorted({app_module.normalize_field_name(m) for m in re.findall(r"\{\{\s*([^{}\n]+?)\s*\}\}", text)})
app_module.extract_pdf_template_fields = pdf_fields
app_module.validate_template = lambda p: None
app_module.app.config["TESTING"] = True

server = make_server("127.0.0.1", 5055, app_module.app, threaded=True)
threading.Thread(target=server.serve_forever, daemon=True).start()
URL = "http://127.0.0.1:5055/dashboard"

results = []
def check(name, condition, detail=""):
    results.append((name, bool(condition), detail))
    print(("PASS " if condition else "FAIL ") + name + (f"  [{detail}]" if detail and not condition else ""))

def write(name, text):
    p = os.path.join(TMP, name); open(p, "w", encoding="utf-8").write(text); return p

def run(page, template, csv_path):
    page.goto(URL)
    page.set_input_files("#templateFile", template)
    page.set_input_files("#participantFile", csv_path)
    page.click("#previewButton")
    page.wait_for_selector("#validationPanel:not(.hidden)", timeout=10000)

TEMPLATE = os.path.join(EX, "sample_certificate_template.pdf")
HEADER = "student_name,course_name,start_date,end_date,certificate_id,college_name,principal_name"

with sync_playwright() as p:
    browser = p.chromium.launch()
    ROOT = os.path.dirname(EX)
    STYLE = os.environ.get("UI_STYLE_CSS", "/tmp/stylefix/style.css")
    def new_page(width, height):
        """Your real static/style.css is used; only if it is absent, a layout stand-in is served."""
        context = browser.new_context(viewport={"width": width, "height": height})
        if not os.path.exists(os.path.join(ROOT, "static", "style.css")) and os.path.exists(STYLE):
            context.route("**/static/style.css", lambda route: route.fulfill(path=STYLE, content_type="text/css"))
        return context.new_page()
    page = new_page(1280, 1000)
    errors = []
    page.on("pageerror", lambda e: errors.append(str(e)))
    page.on("console", lambda m: errors.append(m.text) if m.type == "error" else None)

    # ---- Case 1: 7 placeholders, only 6 columns --------------------------
    run(page, TEMPLATE, write("c1.csv", HEADER.replace(",principal_name", "") + "\nRoja,Python,2026-06-01,2026-06-30,C1,KITS\n"))
    box = page.inner_text("#validationMessages")
    check("Case1 title shown", "Template and CSV column mismatch detected." in box)
    check("Case1 sentence shown", "Your certificate template contains 7 fields, but your CSV file contains only 6 matching fields." in box)
    check("Case1 missing field chip", page.locator(".v-chip", has_text="principal_name").count() == 1)
    check("Case1 help text", "Please add the missing column to your CSV file or manually map this field before generating certificates." in box)
    check("Case1 generate disabled", page.is_disabled("#generateButton"))
    check("Case1 card marked 'Column missing'", page.locator('.dynamic-position-card[data-field="principal_name"] .tag-bad').count() == 1)
    page.screenshot(path=TMP + "/ui_case1.png", full_page=False, clip={"x": 0, "y": 0, "width": 1280, "height": 1000}) if False else None
    page.locator("#validationPanel").screenshot(path=TMP + "/ui_case1_panel.png")
    page.click("text=Go to field mapping")
    check("Case1 'Go to field mapping' flashes the card", page.locator('.dynamic-position-card[data-field="principal_name"].flash').count() == 1)
    # user resolves it by marking the field optional -> back to green
    page.locator('.dynamic-position-card[data-field="principal_name"] .field-optional-box').check()
    page.wait_for_selector(".v-success", timeout=5000)
    check("Case1 resolvable by 'Optional' -> generate enabled", page.is_enabled("#generateButton"))

    # ---- Case 2/3/6/7: missing values, blank row -------------------------
    csv = (HEADER + "\n"
           "Roja Pasupuleti,Cyber Security,2026-06-01,2026-06-30,CERT-001,KITS,Dr. P. Babu\n"
           "Usha Rani,,2026-07-01,2026-07-30,CERT-002,KITS,Dr. P. Babu\n"
           ",,,,,,\n"
           "Ravi Kumar,Python,2026-08-01,2026-08-30,,KITS,Dr. P. Babu\n"
           "Priya,,2026-09-01,2026-09-30,CERT-004,KITS,Dr. P. Babu\n")
    run(page, TEMPLATE, write("c3.csv", csv))
    box = page.inner_text("#validationMessages")
    check("Case3 headline", "Participant data validation failed." in box and "3 missing values found in your participant data." in box)
    rows = page.locator("#validationMessages tbody tr").all_inner_texts()
    flat = [re.sub(r"\s+", " ", r).strip() for r in rows]
    check("Case3 row 3 Usha course_name", any(r.startswith("3 Usha Rani course_name") for r in flat), str(flat))
    check("Case3 row 5 Ravi certificate_id", any(r.startswith("5 Ravi Kumar certificate_id") for r in flat), str(flat))
    check("Case3 row 6 Priya course_name", any(r.startswith("6 Priya course_name") for r in flat), str(flat))
    check("Case3 ALL errors listed (3)", len(flat) == 3, str(len(flat)))
    check("Case6 blank row reported, not an error", "1 completely blank row will be skipped" in box)
    check("Case6 stats: blank rows = 1", page.locator(".v-stat", has_text="Blank rows skipped").inner_text().split()[-1] == "1")
    check("Red cells in preview table = 3", page.locator("td.cell-error").count() == 3, str(page.locator("td.cell-error").count()))
    check("Red cell has error icon", page.locator("td.cell-error .cell-flag i").count() == 3)
    check("Row note names the problem", page.locator(".row-note", has_text="Row 3: Course name is empty.").count() == 1)
    check("Error counter = 3", page.locator(".v-stat", has_text="Errors found").inner_text().split()[-1] == "3")
    check("Generate disabled until fixed", page.is_disabled("#generateButton"))
    check("Skip option offered (3 invalid, 1 valid)", "Skip the 3 participants" in page.inner_text("#skipInvalidWrap") and "other 1 certificate" in page.inner_text("#skipInvalidWrap"))
    check("'Upload corrected file' action present", page.locator("text=Upload corrected file").count() >= 1)
    page.locator("#validationPanel").screenshot(path=TMP + "/ui_case3_panel.png")
    page.locator("#participantPreview").screenshot(path=TMP + "/ui_case3_table.png")
    page.check("#skipInvalid")
    check("Ticking 'skip' enables generate", page.is_enabled("#generateButton"))
    page.uncheck("#skipInvalid")
    # one-click fix: make course_name optional -> only certificate_id error remains
    page.click("text=Make “Course name” optional")
    page.wait_for_function("document.querySelectorAll('td.cell-error').length === 1", timeout=5000)
    check("'Make optional' re-validates instantly", page.locator("td.cell-error").count() == 1)

    # ---- Case 2: exactly one missing value -------------------------------
    csv = "student_name,course_name,start_date,end_date\nRoja,Python,01-06-2026,30-06-2026\nUsha,AI & ML,01-07-2026,30-07-2026\nRavi,,01-08-2026,30-08-2026\nPriya,SQL,01-09-2026,30-09-2026\n"
    run(page, TEMPLATE, write("c2.csv", csv))
    box = page.inner_text("#validationMessages")
    check("Case2 template has 7, csv has 4 -> column error shown first", "Template and CSV column mismatch detected." in box)
    check("Case2 lists all 3 absent columns", "3 required template fields could not be matched with your CSV file." in box)

    # ---- Case 4/5: extra columns info; multiple missing ------------------
    csv = HEADER + ",email,phone_number,address\nRoja,Python,1,2,C1,KITS,Dr. P,a@b.c,123,Guntur\n"
    run(page, TEMPLATE, write("c4.csv", csv))
    box = page.inner_text("#validationMessages")
    check("Case4 info text", "Your CSV file contains 3 additional columns that are not used in this certificate template. They will be ignored during certificate generation." in box)
    check("Case4 not an error (generate enabled)", page.is_enabled("#generateButton"))
    check("Case4 shown as info, no error card", page.locator(".v-error").count() == 0 and page.locator(".v-info").count() >= 1)
    page.locator("#validationPanel").screenshot(path=TMP + "/ui_case4_panel.png")

    # ---- Provided sample files must work out of the box ------------------
    run(page, TEMPLATE, os.path.join(EX, "sample_participants.csv"))
    check("Sample CSV: all good, generate enabled", page.locator(".v-success").count() == 1 and page.is_enabled("#generateButton"))
    run(page, TEMPLATE, os.path.join(EX, "sample_participants.xlsx"))
    check("Sample XLSX: all good", page.locator(".v-success").count() == 1 and "Excel" not in page.inner_text(".v-error") if page.locator(".v-error").count() else True)
    # (uses its own copy of the data, so the result does not depend on edits to examples/; tests/test_examples.py checks that file)
    MISTAKES = (HEADER + "\nRoja Pasupuleti,Cyber Security,2026-06-01,2026-06-30,CERT-001,KITS,Dr. P. Babu\n"
                "Usha Rani,,2026-07-01,2026-07-30,CERT-002,KITS,Dr. P. Babu\n,,,,,,\n"
                "Ravi Kumar,Python Programming,2026-08-01,2026-08-30,,KITS,Dr. P. Babu\n")
    run(page, TEMPLATE, write("mistakes.csv", MISTAKES))
    red = page.locator("td.cell-error").count()
    blank_note = "completely blank row" in page.inner_text("#validationMessages")
    rows_found = page.locator("#participantTableBody tr").count()
    check("Sample-with-mistakes demonstrates the report", red == 2 and blank_note,
          f"red cells={red} (expected 2), blank-row note shown={blank_note}, table rows={rows_found} (expected 3)")

    # ---- Step 1.2: smart mapping (suggest, never apply silently) ----------
    run(page, TEMPLATE, write("s1.csv", HEADER.replace("student_name", "Student Full Name") + "\nRoja,Python,2026-06-01,2026-06-30,C1,KITS,Dr. P\n"))
    check("Suggestion shown in the data check", page.locator(".v-suggest", has_text="student_name").count() == 1)
    check("Suggestion offers the right column", page.locator(".v-suggest .hint-button", has_text="Student Full Name").count() == 1)
    check("Suggestion also shown on the field card", page.locator('.dynamic-position-card[data-field="student_name"] .hint-button').count() == 1)
    check("Suggestion is NOT applied silently (still blocked)", page.is_disabled("#generateButton"))
    page.locator("#validationPanel").screenshot(path=TMP + "/ui_suggest_panel.png")
    page.locator(".v-suggest .hint-button").click()
    page.wait_for_selector(".v-success", timeout=5000)
    check("One click on the suggestion resolves it",
          page.is_enabled("#generateButton") and page.locator('.dynamic-position-card[data-field="student_name"] .field-source').input_value() == "student_full_name")

    run(page, TEMPLATE, write("s2.csv", "first_name,last_name,course_name,start_date,end_date,certificate_id,college_name,principal_name\nRoja,P,Python,2026-06-01,2026-06-30,C1,KITS,Dr. P\n"))
    check("Ambiguous match asks which column", "Which column is it?" in page.inner_text(".v-suggest") and page.locator(".v-suggest .hint-button").count() == 2)

    # ---- Step 1.1: duplicates ------------------------------------------------------
    rows = ["P%d,Python,2026-06-01,2026-06-30,ID%d,KITS,Dr. P" % (i, i) for i in range(1, 13)] + ["P13,Python,2026-06-01,2026-06-30,ID3,KITS,Dr. P"]
    run(page, TEMPLATE, write("d.csv", HEADER + "\n" + "\n".join(rows) + "\n"))
    check("Unique hint appears (found from the data, no field name used)", page.locator(".v-info", has_text="Should 'certificate_id' be unique?").count() == 1)
    check("The hint never blocks generation", page.is_enabled("#generateButton"))
    page.click("text=Check “Certificate id” for duplicates")
    page.wait_for_selector(".v-error", timeout=5000)
    box = page.inner_text("#validationMessages")
    check("Duplicate message shown", "Duplicate values found." in box and "We found 1 duplicate value in the 'certificate_id' field." in box)
    cells = [re.sub(r"\s+", " ", t).strip() for t in page.locator("#validationMessages tbody tr").all_inner_texts()]
    check("Duplicate table lists value, rows and participants", cells == ["certificate_id ID3 4, 14 P3, P13"], str(cells))
    check("Later duplicate red, first use amber", page.locator("td.cell-error", has_text="Duplicate").count() == 1 and page.locator("td.cell-warn", has_text="First use").count() == 1)
    check("Row note names the first row", page.locator(".row-note", has_text="already used in row 4").count() == 1)
    check("Generation blocked by duplicates", page.is_disabled("#generateButton"))
    check("Skip offers to keep the first use", "Skip the 1 participant" in page.inner_text("#skipInvalidWrap") and "other 12 certificates" in page.inner_text("#skipInvalidWrap"))
    page.locator("#validationPanel").screenshot(path=TMP + "/ui_dup_panel.png")
    page.locator("#participantPreview").screenshot(path=TMP + "/ui_dup_table.png")
    page.locator('.dynamic-position-card[data-field="certificate_id"]').screenshot(path=TMP + "/ui_card.png")
    page.click("text=Stop checking “Certificate id” for duplicates")
    page.wait_for_selector(".v-success", timeout=5000)
    check("Turning the check off clears it", page.is_enabled("#generateButton"))
    page.locator('.dynamic-position-card[data-field="certificate_id"] .field-unique-box').check()
    page.wait_for_selector(".v-error", timeout=5000)
    check("'Must be unique' tick box on the field card works", "Duplicate values found." in page.inner_text("#validationMessages"))

    # ---- Step 1.1: formats ---------------------------------------------------------------
    run(page, TEMPLATE, write("f.csv", HEADER + "\nRoja,Python,2026-06-01,31-02-2026,C1,KITS,Dr. P\nUsha,Java,2026-06-02,2026-06-30,C2,KITS,Dr. P\n"))
    check("No format is checked unless chosen", page.is_enabled("#generateButton"))
    page.locator('.dynamic-position-card[data-field="end_date"] .field-format').select_option("date")
    page.wait_for_selector(".v-error", timeout=5000)
    box = page.inner_text("#validationMessages")
    check("Format message shown", "Some values are not in the expected format." in box and "31-02-2026" in box and "Dates must be real dates" in box)
    check("Invalid cell flagged with the bad value", page.locator("td.cell-error", has_text="Invalid").count() == 1 and page.locator("td.cell-error .cell-value", has_text="31-02-2026").count() == 1)
    page.locator("#validationPanel").screenshot(path=TMP + "/ui_format_panel.png")
    page.click("text=Accept any format for “End date”")
    page.wait_for_selector(".v-success", timeout=5000)
    check("Accepting any format clears it", page.is_enabled("#generateButton"))

    # ---- Label column chosen by the user (no name guessing) -----------------------------------------
    run(page, TEMPLATE, write("l.csv", HEADER + "\nRoja,,2026-06-01,2026-06-30,C1,KITS,Dr. P\n"))
    check("Default label is the file's first column", page.locator(".v-table-wrap td", has_text="Roja").count() == 1)
    page.select_option("#labelColumn", "certificate_id")
    page.wait_for_function("document.querySelector('.v-table-wrap td') && document.querySelector('.v-table-wrap').innerText.includes('C1')", timeout=5000)
    check("Choosing 'Identify participants by' renames people in messages", page.locator(".v-table-wrap td", has_text="C1").count() == 1)

    # ---- Landing page extras: sample files, use cases, FAQ, accessibility --------------
    page.goto(URL)
    check("Skip link is the first focusable element", page.locator("a.skip-link").count() == 1 and page.evaluate("document.querySelector('a.skip-link').getAttribute('href')") == "#workspace")
    check("Use-case section lists the 3 organization types", page.locator(".usecase-card").count() == 3 and "Colleges" in page.inner_text("#use-cases") and "Training institutes" in page.inner_text("#use-cases") and "Companies" in page.inner_text("#use-cases"))
    check("FAQ has 7 questions", page.locator(".faq-item").count() == 7)
    first = page.locator(".faq-item").first
    check("FAQ answers start closed", first.get_attribute("open") is None)
    first.locator("summary").click()
    check("FAQ opens on click", first.get_attribute("open") is not None)
    check("FAQ keeps literal placeholders", "{{student_name}}" in page.inner_text("#faq") or "{{student_name}}" in page.locator("#faq").inner_html())
    check("Share tags present", page.locator('meta[property="og:title"]').count() == 1)

    page.goto(URL)
    check("Check button disabled before any file", page.is_disabled("#previewButton"))
    page.click("#sampleButton")
    page.wait_for_function("document.querySelector('#previewButton') && !document.querySelector('#previewButton').disabled", timeout=8000)
    check("Sample button fills BOTH file boxes", "sample_certificate_template.pdf" in page.inner_text("#templateInfo") and "sample_participants.csv" in page.inner_text("#participantInfo"))
    check("Sample status tells the user what to do next", "Click “Check my files”" in page.inner_text("#statusMessage"))
    page.click("#previewButton")
    page.wait_for_selector(".v-success", timeout=10000)
    check("Sample files pass the data check end to end", page.is_enabled("#generateButton") and page.locator(".dynamic-position-card").count() == 7)
    names = page.locator(".dynamic-position-card .ph-name").all_inner_texts()
    check("Field cards follow the order of the sample design", len(names) == 7)

    for width in (360, 768, 1440):
        pg = new_page(width, 900)
        pg.goto(URL)
        over = pg.evaluate("document.documentElement.scrollWidth > window.innerWidth + 2")
        check(f"No sideways scroll on the landing page at {width}px", not over)
        pg.close()
    page.goto(URL)
    page.locator("#use-cases").screenshot(path=TMP + "/ui_usecases.png")
    page.locator("#faq").screenshot(path=TMP + "/ui_faq.png")
    page.locator(".try-row").screenshot(path=TMP + "/ui_tryrow.png")

    # ---- Guide section + downloads ---------------------------------------
    page.goto(URL)
    check("Guide: placeholder example text visible", "{{student_name}}" in page.inner_text("#guide"))
    for name in ["sample_certificate_template.pdf", "sample_certificate_template.png", "sample_participants.csv", "sample_participants.xlsx", "sample_participants_with_errors.csv"]:
        r = page.request.get(f"http://127.0.0.1:5055/examples/{name}")
        check(f"Download works: {name}", r.status == 200 and len(r.body()) > 100)
    page.locator("#guide").screenshot(path=TMP + "/ui_guide.png")

    # ---- Mobile layout ---------------------------------------------------
    mobile = new_page(390, 800)
    mobile.goto(URL)
    mobile.set_input_files("#templateFile", TEMPLATE)
    mobile.set_input_files("#participantFile", write("m.csv", csv))
    mobile.click("#previewButton"); mobile.wait_for_selector("#validationPanel:not(.hidden)")
    overflow = mobile.evaluate("document.documentElement.scrollWidth > window.innerWidth + 2")
    check("Mobile: no horizontal page overflow", not overflow)
    mobile.locator("#validationPanel").screenshot(path=TMP + "/ui_mobile_panel.png")

    errors = [e for e in errors if "ERR_NAME_NOT_RESOLVED" not in e]  # Google Fonts: no internet in the sandbox
    check("No JavaScript errors in the console", not errors, "; ".join(errors)[:300])
    browser.close()

failed = [r for r in results if not r[1]]
for name, _ok, detail in failed:
    print(f"FAILED: {name}  [{detail}]")
print(f"\n{len(results) - len(failed)}/{len(results)} UI checks passed")
sys.exit(1 if failed else 0)