"""The example files shipped in examples/ must behave exactly as the page describes.

Run:  python -m unittest tests.test_examples -v
If one fails, your copy was edited. Restore all examples with:  python scripts/make_examples.py data
"""
import os
import sys
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from participants import read_participant_file  # noqa: E402
from validation import build_report  # noqa: E402

EX = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "examples")
FIELDS = ["student_name", "course_name", "start_date", "end_date",
          "certificate_id", "college_name", "principal_name"]
HOWTO = "  (your copy was changed - restore it with: python scripts/make_examples.py data)"


def load(name):
    path = os.path.join(EX, name)
    data = read_participant_file(path, os.path.splitext(name)[1])
    return data, build_report(FIELDS, data["headers"], data["participants"],
                              blank_rows_skipped=data["blank_rows_skipped"])


class TestShippedExamples(unittest.TestCase):
    def test_good_csv_is_clean(self):
        data, report = load("sample_participants.csv")
        self.assertEqual(len(data["participants"]), 3, "sample_participants.csv should have 3 people" + HOWTO)
        self.assertTrue(report["can_generate"], report["text"] + HOWTO)

    def test_good_xlsx_is_clean(self):
        data, report = load("sample_participants.xlsx")
        self.assertEqual(len(data["participants"]), 3, "sample_participants.xlsx should have 3 people" + HOWTO)
        self.assertTrue(report["can_generate"], report["text"] + HOWTO)

    def test_file_with_mistakes_has_exactly_the_documented_mistakes(self):
        data, report = load("sample_participants_with_errors.csv")
        detail = (f"found {len(data['participants'])} people, {data['blank_rows_skipped']} blank rows skipped, "
                  f"rows: {[(p['row'], p['fields']['student_name']) for p in data['participants']]}")
        self.assertEqual(len(data["participants"]), 3, detail + HOWTO)
        self.assertEqual(data["blank_rows_skipped"], 1, detail + HOWTO)
        self.assertEqual([(i["row"], i["participant"], i["field"]) for i in report["cell_issues"]],
                         [(3, "Usha Rani", "course_name"), (5, "Ravi Kumar", "certificate_id")], detail + HOWTO)

    def test_all_five_example_files_exist(self):
        for name in ("sample_certificate_template.pdf", "sample_certificate_template.png",
                     "sample_participants.csv", "sample_participants.xlsx", "sample_participants_with_errors.csv"):
            self.assertTrue(os.path.getsize(os.path.join(EX, name)) > 100, name + " is missing or empty")


if __name__ == "__main__":
    unittest.main(verbosity=2)
