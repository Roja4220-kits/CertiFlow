"""Validation through the real Flask routes (upload -> validate -> bulk).

Run:  python -m unittest tests.test_validation_api -v
Template detection is replaced by a fixed field list so these tests do not depend
on PDF/OCR engines; everything else (routes, file parsing, validation) is real.
"""
import io
import os
import unittest
from unittest.mock import patch

from tests import testenv  # noqa: F401  (must be imported before app)

import app as app_module  # noqa: E402

FONT = testenv.FONT


def upload(client, template_fields, csv_text, filename="people.csv"):
    with patch.object(app_module, "validate_template", lambda path: None), \
         patch.object(app_module, "extract_pdf_template_fields", lambda path: list(template_fields)):
        response = client.post("/api/preview-upload", data={
            "template": (io.BytesIO(b"%PDF-1.4 dummy"), "template.pdf"),
            "participants": (io.BytesIO(csv_text.encode("utf-8")), filename),
        }, content_type="multipart/form-data")
    return response


class ApiTestCase(unittest.TestCase):
    def setUp(self):
        app_module.app.config["TESTING"] = True
        self.client = app_module.app.test_client()

    def msg(self, report, code):
        return next((m for m in report["messages"] if m["code"] == code), None)


class TestValidationEndpoints(ApiTestCase):

    def test_case1_missing_column_reported_with_exact_message(self):
        r = upload(self.client, ["student_name", "course_name", "start_date", "end_date",
                                 "certificate_id", "college_name", "principal_name"],
                   "student_name,course_name,start_date,end_date,certificate_id,college_name\n"
                   "Roja,Python,1,2,C1,KITS\n")
        self.assertEqual(r.status_code, 200)
        report = r.get_json()["report"]
        self.assertEqual(report["missing_columns"], ["principal_name"])
        self.assertIn("Your certificate template contains 7 fields, but your CSV file "
                      "contains only 6 matching fields.", report["text"])
        self.assertFalse(report["can_generate"])
        self.assertEqual(r.get_json()["invalid"], 1)  # row is not generatable

    def test_case2_and_3_cell_errors_have_row_participant_field(self):
        r = upload(self.client, ["student_name", "course_name", "certificate_id"],
                   "student_name,course_name,certificate_id\n"
                   "Roja,Python,CERT001\nUsha,,CERT002\nRavi,Machine Learning,\nPriya,,CERT004\n")
        report = r.get_json()["report"]
        self.assertEqual([(i["row"], i["participant"], i["field"]) for i in report["cell_issues"]],
                         [(3, "Usha", "course_name"), (4, "Ravi", "certificate_id"), (5, "Priya", "course_name")])
        rows = {p["row"]: p for p in r.get_json()["participants"]}
        self.assertFalse(rows[3]["valid"])
        self.assertEqual(rows[3]["error"], "Missing value(s): course_name")
        self.assertTrue(rows[2]["valid"])

    def test_case4_extra_columns_not_an_error(self):
        r = upload(self.client, ["student_name"], "student_name,email,phone,address\nRoja,a@b.c,1,x\n")
        report = r.get_json()["report"]
        self.assertTrue(report["can_generate"])
        self.assertIn("3 additional columns", self.msg(report, "extra_columns")["summary"])

    def test_case6_blank_rows(self):
        r = upload(self.client, ["student_name"], "student_name\nRoja\n\n,\nUsha\n")
        body = r.get_json()
        self.assertEqual(body["total"], 2)
        self.assertEqual(body["blank_rows_skipped"], 2)
        self.assertTrue(body["report"]["can_generate"])

    def test_validate_endpoint_mapping_and_optional(self):
        r = upload(self.client, ["student_name", "course_name", "principal_name"],
                   "full name,subject\nRoja,Python\nUsha,\n")
        upload_id = r.get_json()["upload_id"]
        self.assertFalse(r.get_json()["report"]["can_generate"])

        def validate(**extra):
            return self.client.post("/api/validate", json={"upload_id": upload_id, **extra}).get_json()["report"]

        mapped = validate(mapping={"student_name": "full_name", "course_name": "subject", "principal_name": ""})
        self.assertEqual(mapped["missing_columns"], ["principal_name"])
        self.assertEqual(mapped["cell_issue_count"], 1)   # Usha has no subject
        fixed = validate(mapping={"student_name": "full_name", "course_name": "subject"},
                         optional_fields=["principal_name", "course_name"])
        self.assertTrue(fixed["can_generate"])

    def test_bulk_is_blocked_while_columns_missing(self):
        r = upload(self.client, ["student_name", "course_name"], "student_name\nRoja\n")
        resp = self.client.post("/api/certificates/bulk", json={
            "upload_id": r.get_json()["upload_id"], "positions": {}})
        self.assertEqual(resp.status_code, 400)
        body = resp.get_json()
        self.assertEqual(body["report"]["missing_columns"], ["course_name"])
        self.assertIn("Template and CSV column mismatch detected.", body["error"])

    def test_bulk_is_blocked_with_empty_required_values_unless_skip(self):
        r = upload(self.client, ["student_name", "course_name"],
                   "student_name,course_name\nRoja,Python\nRavi,\n")
        resp = self.client.post("/api/certificates/bulk", json={"upload_id": r.get_json()["upload_id"]})
        self.assertEqual(resp.status_code, 400)
        self.assertIn("1 missing value", resp.get_json()["error"])

    def test_duplicate_headers_rejected_at_upload(self):
        r = upload(self.client, ["student_name"], "Student Name,student-name\nA,B\n")
        self.assertEqual(r.status_code, 400)
        self.assertIn("Duplicate column names", r.get_json()["error"])

    def test_excel_upload_uses_excel_wording(self):
        from openpyxl import Workbook
        wb = Workbook(); ws = wb.active
        ws.append(["student_name"]); ws.append(["Roja"])
        buf = io.BytesIO(); wb.save(buf)
        with patch.object(app_module, "validate_template", lambda p: None), \
             patch.object(app_module, "extract_pdf_template_fields", lambda p: ["student_name", "course_name"]):
            r = self.client.post("/api/preview-upload", data={
                "template": (io.BytesIO(b"x"), "t.pdf"), "participants": (io.BytesIO(buf.getvalue()), "p.xlsx")},
                content_type="multipart/form-data")
        self.assertIn("Template and Excel column mismatch detected.", r.get_json()["report"]["text"])


@unittest.skipUnless(FONT, "needs a .ttf font on this computer")
class TestFontsAndHardening(ApiTestCase):

    def test_font_upload_list_and_validation(self):
        r = upload(self.client, ["student_name"], "student_name\nRoja\n")
        upload_id = r.get_json()["upload_id"]
        with open(FONT, "rb") as handle:
            ok = self.client.post("/api/fonts", data={"upload_id": upload_id, "font": (handle, "My Font.ttf")},
                                  content_type="multipart/form-data")
        self.assertEqual(ok.status_code, 201, ok.get_json())
        font_id = ok.get_json()["id"]
        self.assertRegex(font_id, r"^custom:[0-9a-f]{32}$")
        listed = self.client.get(f"/api/fonts?upload_id={upload_id}").get_json()["fonts"]
        self.assertIn(font_id, [f["id"] for f in listed])
        # another visitor (different upload) must not see this font
        other = self.client.get("/api/fonts?upload_id=" + "0" * 32).get_json()["fonts"]
        self.assertNotIn(font_id, [f["id"] for f in other])
        # junk is rejected
        bad = self.client.post("/api/fonts", data={"upload_id": upload_id, "font": (io.BytesIO(b"not a font"), "x.ttf")},
                               content_type="multipart/form-data")
        self.assertEqual(bad.status_code, 400)
        wrong_ext = self.client.post("/api/fonts", data={"upload_id": upload_id, "font": (io.BytesIO(b"x"), "x.exe")},
                                     content_type="multipart/form-data")
        self.assertEqual(wrong_ext.status_code, 400)
        # the font id is accepted as a field's font_name
        cfg = app_module.normalize_position({"font_name": font_id}, "student_name")
        self.assertEqual(cfg["font_name"], font_id)
        with self.assertRaises(ValueError):
            app_module.normalize_position({"font_name": "custom:../../etc/passwd"}, "student_name")

    def test_font_upload_requires_existing_upload(self):
        resp = self.client.post("/api/fonts", data={"upload_id": "f" * 32, "font": (io.BytesIO(b"x"), "x.ttf")},
                                content_type="multipart/form-data")
        self.assertEqual(resp.status_code, 400)

    def test_security_headers_health_and_unknown_job(self):
        resp = self.client.get("/api/health")
        self.assertEqual(resp.status_code, 200)
        self.assertEqual(resp.headers["X-Content-Type-Options"], "nosniff")
        self.assertEqual(resp.headers["Cache-Control"], "no-store")
        self.assertEqual(self.client.get("/api/unknown").status_code, 404)

    def test_rate_limit_returns_429(self):
        app_module.app.config["TESTING"] = False
        app_module._hits.clear()
        try:
            codes = [self.client.post("/api/preview-upload").status_code for _ in range(22)]
        finally:
            app_module.app.config["TESTING"] = True
            app_module._hits.clear()
        self.assertEqual(codes[0], 400)
        self.assertEqual(codes[-1], 429)


if __name__ == "__main__":
    unittest.main(verbosity=2)


class TestStep11And12Endpoints(ApiTestCase):
    """Duplicate / format checks, label column and suggestions through the real routes."""

    CSV = ("student_name,certificate_id,start_date\n"
           "Roja,C1,2026-06-01\nUsha,C2,31-02-2026\nRavi,c1,2026-06-03\n")
    FIELDS = ["student_name", "certificate_id", "start_date"]

    def validate(self, upload_id, **extra):
        resp = self.client.post("/api/validate", json={"upload_id": upload_id, **extra})
        self.assertEqual(resp.status_code, 200, resp.get_json())
        return resp.get_json()["report"]

    def test_validate_accepts_unique_formats_and_label(self):
        upload_id = upload(self.client, self.FIELDS, self.CSV).get_json()["upload_id"]
        plain = self.validate(upload_id)
        self.assertTrue(plain["can_generate"])                       # nothing is checked unless asked
        report = self.validate(upload_id, unique_fields=["certificate_id"],
                               field_formats={"start_date": "date"}, label_column="certificate_id")
        self.assertEqual(report["duplicate_group_count"], 1)
        self.assertEqual(report["format_issues"][0]["value"], "31-02-2026")
        self.assertEqual(report["format_issues"][0]["participant"], "C2")      # label column honoured
        self.assertEqual(report["invalid_row_numbers"], [3, 4])

    def test_unknown_format_name_is_rejected(self):
        upload_id = upload(self.client, self.FIELDS, self.CSV).get_json()["upload_id"]
        resp = self.client.post("/api/validate", json={"upload_id": upload_id, "field_formats": {"start_date": "zodiac"}})
        self.assertEqual(resp.status_code, 400)
        self.assertIn("Unknown format", resp.get_json()["error"])

    def test_bulk_is_blocked_by_duplicates_and_formats(self):
        upload_id = upload(self.client, self.FIELDS, self.CSV).get_json()["upload_id"]
        resp = self.client.post("/api/certificates/bulk", json={
            "upload_id": upload_id, "unique_fields": ["certificate_id"], "field_formats": {"start_date": "date"}})
        self.assertEqual(resp.status_code, 400)
        body = resp.get_json()
        self.assertIn("Duplicate values found.", body["error"])
        self.assertIn("Some values are not in the expected format.", body["error"])
        self.assertEqual(body["report"]["problem_count"], 2)

    def test_upload_response_row_errors_and_suggestion(self):
        body = upload(self.client, ["student_name", "course_name"],
                      "student_full_name,course_name\nRoja,Python\n").get_json()
        self.assertEqual(body["report"]["suggestions"]["student_name"]["candidates"][0]["column"], "student_full_name")
        self.assertFalse(body["report"]["can_generate"])      # a suggestion is never applied silently
        mapped = self.client.post("/api/validate", json={
            "upload_id": body["upload_id"], "mapping": {"student_name": "student_full_name"}}).get_json()["report"]
        self.assertTrue(mapped["can_generate"])