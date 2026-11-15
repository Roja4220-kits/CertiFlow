"""The 7 requested validation scenarios, run against REAL files.

Run:  python -m unittest tests.test_validation -v     (no pytest needed)
"""
import os
import sys
import tempfile
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from participants import read_participant_file  # noqa: E402
from validation import build_report  # noqa: E402


def load_csv(text):
    handle = tempfile.NamedTemporaryFile("w", suffix=".csv", delete=False, encoding="utf-8", newline="")
    handle.write(text)
    handle.close()
    try:
        return read_participant_file(handle.name, ".csv")
    finally:
        os.unlink(handle.name)


def validate(template_fields, csv_text, **kwargs):
    data = load_csv(csv_text)
    return build_report(
        template_fields, data["headers"], data["participants"],
        blank_rows_skipped=data["blank_rows_skipped"], **kwargs,
    )


def message(report, code):
    return next((m for m in report["messages"] if m["code"] == code), None)


class TestScenarios(unittest.TestCase):

    def test_1_template_has_7_fields_csv_has_6(self):
        report = validate(
            ["student_name", "course_name", "start_date", "end_date",
             "certificate_id", "college_name", "principal_name"],
            "student_name,course_name,start_date,end_date,certificate_id,college_name\n"
            "Roja,Python,01-06-2026,30-06-2026,C1,KITS\n",
        )
        self.assertFalse(report["can_generate"])
        self.assertEqual(report["missing_columns"], ["principal_name"])
        self.assertEqual(report["cell_issue_count"], 0)  # a column problem, not a cell problem
        self.assertEqual(report["text"], (
            "Template and CSV column mismatch detected.\n\n"
            "Your certificate template contains 7 fields, but your CSV file contains only 6 matching fields.\n\n"
            "Missing field:\n- principal_name\n\n"
            "Please add the missing column to your CSV file or manually map this field "
            "before generating certificates."
        ))

    def test_2_one_participant_missing_value(self):
        report = validate(
            ["student_name", "course_name", "start_date", "end_date"],
            "student_name,course_name,start_date,end_date\n"
            "Roja,Python,01-06-2026,30-06-2026\n"
            "Usha,AI & ML,01-07-2026,30-07-2026\n"
            "Ravi,,01-08-2026,30-08-2026\n"
            "Priya,SQL,01-09-2026,30-09-2026\n",
        )
        self.assertFalse(report["can_generate"])
        self.assertEqual(report["missing_columns"], [])
        self.assertEqual(len(report["cell_issues"]), 1)
        issue = report["cell_issues"][0]
        self.assertEqual((issue["row"], issue["participant"], issue["field"]), (4, "Ravi", "course_name"))
        self.assertEqual(report["text"], (
            "Participant data validation failed.\n\n"
            "We found 1 missing value in your uploaded file.\n\n"
            "- Row: 4 (including the header row)\n- Participant: Ravi\n"
            "- Missing field: course_name\n- Problem: Course name is empty.\n\n"
            "Please provide the missing course name before generating the certificate."
        ))

    def test_3_multiple_participants_missing_values(self):
        report = validate(
            ["student_name", "course_name", "certificate_id"],
            "student_name,course_name,certificate_id\n"
            "Roja,Python,CERT001\nUsha,,CERT002\nRavi,Machine Learning,\nPriya,,CERT004\n",
        )
        found = [(i["row"], i["participant"], i["field"]) for i in report["cell_issues"]]
        self.assertEqual(found, [(3, "Usha", "course_name"), (4, "Ravi", "certificate_id"),
                                 (5, "Priya", "course_name")])  # ALL errors, not only the first
        self.assertIn("3 missing values found in your participant data.", report["text"])
        self.assertIn("| 3 | Usha | course_name |", report["text"])
        self.assertIn("| 4 | Ravi | certificate_id |", report["text"])
        self.assertIn("| 5 | Priya | course_name |", report["text"])
        self.assertEqual(report["invalid_rows"], 3)
        self.assertEqual(report["valid_rows"], 1)

    def test_4_extra_columns_are_information_not_errors(self):
        report = validate(
            ["student_name", "course_name", "certificate_id"],
            "student_name,course_name,certificate_id,email,phone_number,address\n"
            "Roja,Python,C1,r@x.com,123,Guntur\n",
        )
        self.assertTrue(report["can_generate"])
        self.assertEqual(report["extra_columns"], ["email", "phone_number", "address"])
        info = message(report, "extra_columns")
        self.assertEqual(info["level"], "info")
        self.assertEqual(info["summary"], (
            "Your CSV file contains 3 additional columns that are not used in this "
            "certificate template. They will be ignored during certificate generation."))
        self.assertFalse([m for m in report["messages"] if m["level"] == "error"])

    def test_5_multiple_template_fields_missing(self):
        report = validate(
            ["student_name", "course_name", "start_date", "end_date",
             "principal_name", "college_name", "certificate_id"],
            "student_name,course_name,certificate_id\nRoja,Python,C1\n",
        )
        self.assertFalse(report["can_generate"])
        self.assertFalse(report["can_generate_valid_rows"])
        self.assertEqual(report["missing_columns"],
                         ["start_date", "end_date", "principal_name", "college_name"])
        self.assertIn("4 required template fields could not be matched with your CSV file.", report["text"])
        self.assertIn("Missing fields:\n- start_date\n- end_date\n- principal_name\n- college_name", report["text"])

    def test_6_blank_rows_are_skipped_without_errors(self):
        report = validate(
            ["student_name", "course_name"],
            "student_name,course_name\nRoja,Python\n,\n\n   ,  \nUsha,Java\n,\n",
        )
        self.assertEqual(report["total_rows"], 2)           # no empty certificates
        self.assertEqual(report["blank_rows_skipped"], 4)
        self.assertEqual(report["cell_issue_count"], 0)     # blank rows are not errors
        self.assertTrue(report["can_generate"])
        self.assertIn("4 completely blank rows will be skipped", message(report, "blank_rows")["summary"])

    def test_7_missing_cells_vs_missing_column_are_different(self):
        csv_text = "student_name,course_name\nRoja,Python\nUsha,\nRavi,Java\nPriya,\n"
        report = validate(["student_name", "course_name"], csv_text)
        self.assertEqual([(i["row"], i["participant"], i["field"]) for i in report["cell_issues"]],
                         [(3, "Usha", "course_name"), (5, "Priya", "course_name")])
        self.assertEqual(report["missing_columns"], [])
        self.assertIsNone(message(report, "missing_columns"))
        self.assertIsNotNone(message(report, "missing_values"))
        # and the opposite: column missing -> a column message, never per-cell messages
        other = validate(["student_name", "course_name", "email"], csv_text)
        self.assertIsNotNone(message(other, "missing_columns"))
        self.assertEqual(other["missing_columns"], ["email"])
        self.assertTrue(all(i["field"] != "email" for i in other["cell_issues"]))


class TestExtraBehaviour(unittest.TestCase):

    def test_name_matching_is_normalised(self):
        report = validate(["student_name", "course_name"],
                          "Student Name,Course-Name\nRoja,Python\n")
        self.assertTrue(report["can_generate"])

    def test_manual_mapping_resolves_missing_column(self):
        csv_text = "full name,subject\nRoja,Python\n"
        self.assertFalse(validate(["student_name", "course_name"], csv_text)["can_generate"])
        mapped = validate(["student_name", "course_name"], csv_text,
                          mapping={"student_name": "full_name", "course_name": "subject"})
        self.assertTrue(mapped["can_generate"])
        self.assertEqual(mapped["extra_columns"], [])

    def test_optional_field_may_be_empty_or_missing(self):
        csv_text = "student_name,course_name\nRoja,\nUsha,Java\n"
        report = validate(["student_name", "course_name", "principal_name"], csv_text,
                          optional_fields=["course_name", "principal_name"])
        self.assertTrue(report["can_generate"])
        self.assertEqual(report["cell_issue_count"], 0)
        self.assertEqual(report["optional_unmatched"], ["principal_name"])

    def test_skip_option_available_only_when_columns_complete(self):
        report = validate(["student_name", "course_name"],
                          "student_name,course_name\nRoja,Python\nUsha,\n")
        self.assertFalse(report["can_generate"])
        self.assertTrue(report["can_generate_valid_rows"])

    def test_excel_file_numbers_dates_and_blank_rows(self):
        from datetime import datetime
        from openpyxl import Workbook
        workbook = Workbook()
        sheet = workbook.active
        sheet.append(["Student Name", "Roll Number", "Start Date", None])
        sheet.append(["Roja", 101.0, datetime(2026, 6, 1), None])
        sheet.append([None, None, None, None])
        sheet.append(["Usha", 102, datetime(2026, 7, 1), None])
        handle = tempfile.NamedTemporaryFile(suffix=".xlsx", delete=False)
        handle.close()
        workbook.save(handle.name)
        try:
            data = read_participant_file(handle.name, ".xlsx")
        finally:
            os.unlink(handle.name)
        self.assertEqual(data["headers"], ["student_name", "roll_number", "start_date"])
        self.assertEqual(data["participants"][0]["fields"]["roll_number"], "101")
        self.assertEqual(data["participants"][0]["fields"]["start_date"], "2026-06-01")
        self.assertEqual(data["blank_rows_skipped"], 1)
        self.assertEqual([p["row"] for p in data["participants"]], [2, 4])

    def test_duplicate_headers_after_normalisation_rejected(self):
        with self.assertRaises(ValueError) as ctx:
            load_csv("Student Name,student-name\nA,B\n")
        self.assertIn("student_name", str(ctx.exception))

    def test_unicode_headers_and_values_survive(self):
        data = load_csv("పేరు,కోర్సు\nరోజా,పైథాన్\n")
        self.assertEqual(data["headers"], ["పేరు", "కోర్సు"])
        self.assertEqual(data["participants"][0]["fields"]["పేరు"], "రోజా")

    def test_issue_list_is_capped_but_count_is_exact(self):
        rows = "".join(f"P{i},\n" for i in range(600))
        report = validate(["student_name", "course_name"], "student_name,course_name\n" + rows)
        self.assertEqual(report["cell_issue_count"], 600)
        self.assertEqual(len(report["cell_issues"]), 500)
        self.assertTrue(report["issues_truncated"])


if __name__ == "__main__":
    unittest.main(verbosity=2)
