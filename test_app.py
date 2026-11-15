"""End-to-end tests (needs the real PyMuPDF, Flask-SQLAlchemy and reportlab).

Run:  pytest -q          (or: python -m unittest test_app -v)
These exercise PDF generation, the database, background jobs and downloads.
"""
import csv
import os
import time
import unittest
from io import BytesIO, StringIO

from reportlab.lib.pagesizes import A4, landscape
from reportlab.pdfgen import canvas

from tests import testenv  # noqa: F401

NEEDS_REAL_LIBS = "end-to-end tests need the real: " + ", ".join(testenv.STUBBED)

import app as app_module  # noqa: E402
from models import db  # noqa: E402

FONT = testenv.FONT


def placeholder_pdf():
    buffer = BytesIO()
    width, height = landscape(A4)
    pdf = canvas.Canvas(buffer, pagesize=(width, height))
    pdf.setFont("Helvetica-Bold", 24)
    pdf.drawCentredString(width / 2, height - 100, "TEST CERTIFICATE")
    pdf.setFont("Helvetica", 16)
    pdf.drawString(100, height - 200, "{{ student_name }}")
    pdf.drawString(100, height - 250, "{{ roll_number }}")
    pdf.drawString(100, height - 300, "{{ course_name }}")
    pdf.save()
    return buffer.getvalue()


def participants_csv(rows, header=("Student Name", "Roll Number", "Course Name", "College Name")):
    buffer = StringIO()
    writer = csv.writer(buffer)
    writer.writerow(header)
    writer.writerows(rows)
    return buffer.getvalue().encode("utf-8")


GOOD = [["Roja", "101", "Python", "KITS"], ["Usha", "102", "Java", "KITS"]]


@unittest.skipIf(testenv.STUBBED, NEEDS_REAL_LIBS)
class AppTestCase(unittest.TestCase):
    sync = True

    def setUp(self):
        app_module.app.config.update(TESTING=True, SYNC_JOBS=self.sync)
        with app_module.app.app_context():
            db.session.remove()
            db.drop_all()
            db.create_all()
        self.client = app_module.app.test_client()

    def tearDown(self):
        with app_module.app.app_context():
            db.session.remove()

    def upload(self, rows=GOOD, header=None, template=None, name="people.csv", data=None):
        payload = data if data is not None else (
            participants_csv(rows) if header is None else participants_csv(rows, header))
        response = self.client.post("/api/preview-upload", data={
            "template": (BytesIO(template or placeholder_pdf()), "template.pdf"),
            "participants": (BytesIO(payload), name),
        }, content_type="multipart/form-data")
        self.assertEqual(response.status_code, 200, response.get_json())
        return response.get_json()

    def bulk(self, upload_id, **extra):
        return self.client.post("/api/certificates/bulk", json={"upload_id": upload_id, **extra})


class TestBasics(AppTestCase):
    def test_health_dashboard_and_guide(self):
        self.assertEqual(self.client.get("/api/health").status_code, 200)
        page = self.client.get("/dashboard")
        self.assertEqual(page.status_code, 200)
        self.assertIn(b"{{student_name}}", page.data)

    def test_placeholders_detected_and_matched(self):
        data = self.upload()
        self.assertEqual(sorted(data["template_fields"]), ["course_name", "roll_number", "student_name"])
        self.assertIn("college_name", data["extra_fields"])
        self.assertTrue(data["report"]["can_generate"])

    def test_unsupported_formats_rejected(self):
        for template_name, part_name in (("t.txt", "p.csv"), ("t.pdf", "p.txt")):
            response = self.client.post("/api/preview-upload", data={
                "template": (BytesIO(placeholder_pdf()), template_name),
                "participants": (BytesIO(participants_csv(GOOD)), part_name),
            }, content_type="multipart/form-data")
            self.assertEqual(response.status_code, 400)


class TestGeneration(AppTestCase):
    def test_bulk_generation_downloads_and_zip(self):
        up = self.upload()
        response = self.bulk(up["upload_id"])
        self.assertEqual(response.status_code, 200, response.get_json())
        data = response.get_json()
        self.assertEqual((data["total"], data["successful"], data["failed"]), (2, 2, 0))
        self.assertEqual(data["status"], "completed")
        self.assertEqual(data["progress_percentage"], 100)
        self.assertEqual([r["participant"] for r in data["results"]], ["Roja", "Usha"])
        self.assertTrue(all("file" not in r for r in data["results"]))      # no server paths leak
        pdf = self.client.get(data["results"][0]["download_url"])
        self.assertEqual(pdf.status_code, 200)
        self.assertTrue(pdf.data.startswith(b"%PDF"))
        zipped = self.client.get(data["download_url"])
        self.assertEqual((zipped.status_code, zipped.mimetype), (200, "application/zip"))

    def test_blocked_when_required_value_missing_then_skip_works(self):
        up = self.upload(rows=[["Roja", "101", "Python", "KITS"], ["Ravi", "103", "", "KITS"]])
        blocked = self.bulk(up["upload_id"])
        self.assertEqual(blocked.status_code, 400)
        self.assertEqual(blocked.get_json()["report"]["cell_issues"][0]["participant"], "Ravi")
        skipped = self.bulk(up["upload_id"], on_invalid="skip")
        self.assertEqual(skipped.status_code, 200)
        body = skipped.get_json()
        self.assertEqual((body["total"], body["successful"]), (1, 1))
        self.assertEqual(body["skipped"], [{"row": 3, "participant": "Ravi"}])

    def test_optional_field_allows_empty_value(self):
        up = self.upload(rows=[["Roja", "101", "", "KITS"]])
        response = self.bulk(up["upload_id"], optional_fields=["course_name"])
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.get_json()["successful"], 1)

    def test_blocked_when_column_missing_unless_mapped(self):
        up = self.upload(rows=[["Roja", "Python"]], header=("Full Name", "Subject"))
        self.assertEqual(self.bulk(up["upload_id"]).status_code, 400)
        mapped = self.bulk(up["upload_id"], mapping={"student_name": "full_name", "course_name": "subject"},
                           optional_fields=["roll_number"])
        self.assertEqual(mapped.status_code, 200, mapped.get_json())

    def test_one_failure_does_not_stop_the_batch(self):
        original = app_module.generate_certificate

        def flaky(**kwargs):
            if kwargs["participant_data"].get("student_name") == "Roja":
                raise RuntimeError("simulated failure")
            return original(**kwargs)

        app_module.generate_certificate = flaky
        try:
            body = self.bulk(self.upload()["upload_id"]).get_json()
        finally:
            app_module.generate_certificate = original
        self.assertEqual((body["successful"], body["failed"]), (1, 1))
        self.assertEqual([r["status"] for r in body["results"]], ["failed", "success"])

    def test_preview_certificate_returns_pdf(self):
        up = self.upload()
        response = self.client.post("/api/preview-certificate", json={
            "upload_id": up["upload_id"],
            "fields": {"student_name": "Preview", "roll_number": "1", "course_name": "X"},
            "positions": {"student_name": {"font_size": 20, "align": "center", "font_name": "hebo", "color": "#112233"}},
        })
        self.assertEqual(response.status_code, 200)
        self.assertTrue(response.data.startswith(b"%PDF"))

    @unittest.skipUnless(FONT, "needs a .ttf font on this computer")
    def test_uploaded_font_is_used_for_generation(self):
        up = self.upload()
        with open(FONT, "rb") as handle:
            font = self.client.post("/api/fonts", data={"upload_id": up["upload_id"], "font": (handle, "Test.ttf")},
                                    content_type="multipart/form-data").get_json()
        positions = {"student_name": {"font_name": font["id"], "font_size": 22, "align": "center", "color": "#000000"}}
        body = self.bulk(up["upload_id"], positions=positions).get_json()
        self.assertEqual((body["successful"], body["failed"]), (2, 0), body)


class TestBackgroundJobs(AppTestCase):
    sync = False

    def test_job_runs_in_background_and_can_be_polled(self):
        up = self.upload()
        response = self.bulk(up["upload_id"])
        self.assertEqual(response.status_code, 202)
        job = response.get_json()
        self.assertIn(job["status"], ("queued", "processing", "completed"))
        for _ in range(100):
            status = self.client.get(job["status_url"]).get_json()
            if status["status"] in ("completed", "failed"):
                break
            time.sleep(0.2)
        self.assertEqual(status["status"], "completed", status)
        results = self.client.get(job["status_url"] + "?results=1").get_json()["results"]
        self.assertEqual([r["status"] for r in results], ["success", "success"])

    def test_job_ids_are_unguessable_tokens(self):
        self.assertEqual(self.client.get("/api/jobs/1").status_code, 404)
        self.assertEqual(self.client.get("/api/jobs/999999/download").status_code, 404)


if __name__ == "__main__":
    unittest.main(verbosity=2)