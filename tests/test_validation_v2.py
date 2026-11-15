"""Step 1.1 (duplicates, formats) and 1.2 (smart mapping) of the roadmap.

Run:  python -m unittest tests.test_validation_v2 -v
"""
import os
import sys
import tempfile
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from mapping import similarity, suggest_mappings  # noqa: E402
from participants import read_participant_file  # noqa: E402
from validation import build_report, is_valid_format  # noqa: E402


def validate(template_fields, csv_text, **kwargs):
    handle = tempfile.NamedTemporaryFile("w", suffix=".csv", delete=False, encoding="utf-8", newline="")
    handle.write(csv_text)
    handle.close()
    try:
        data = read_participant_file(handle.name, ".csv")
    finally:
        os.unlink(handle.name)
    return build_report(template_fields, data["headers"], data["participants"],
                        blank_rows_skipped=data["blank_rows_skipped"], **kwargs)


def message(report, code):
    return next((m for m in report["messages"] if m["code"] == code), None)


class TestDuplicates(unittest.TestCase):
    CSV = ("student_name,certificate_id\nRoja,CERT001\nUsha,CERT002\nRavi,cert001\nPriya,CERT004\nAnil,CERT002\nSita,\nGita,\n")

    def test_duplicates_found_ignoring_case_and_blanks(self):
        report = validate(["student_name", "certificate_id"], self.CSV, unique_fields=["certificate_id"],
                          optional_fields=["certificate_id"])   # blank IDs are allowed here: only repeats matter
        groups = {g["value"].casefold(): g for g in report["duplicate_groups"]}
        self.assertEqual(set(groups), {"cert001", "cert002"})          # blanks are NOT duplicates
        self.assertEqual(groups["cert001"]["rows"], [2, 4])
        self.assertEqual(groups["cert001"]["participants"], ["Roja", "Ravi"])
        self.assertEqual(groups["cert002"]["rows"], [3, 6])
        self.assertEqual(report["duplicate_group_count"], 2)
        self.assertFalse(report["can_generate"])
        self.assertIn("2 duplicate values found in your participant data.", report["text"])
        self.assertIn("| certificate_id | CERT001 | 2, 4 | Roja, Ravi |", report["text"])

    def test_first_occurrence_is_kept_later_ones_are_the_problem(self):
        report = validate(["student_name", "certificate_id"], self.CSV, unique_fields=["certificate_id"],
                          optional_fields=["certificate_id"])
        self.assertEqual(report["invalid_row_numbers"], [4, 6])        # not rows 2 and 3
        self.assertEqual([(i["row"], i["first_row"]) for i in report["duplicate_issues"]], [(4, 2), (6, 3)])
        self.assertTrue(report["can_generate_valid_rows"])             # "skip" can generate the rest

    def test_single_duplicate_has_its_own_wording(self):
        report = validate(["student_name", "certificate_id"],
                          "student_name,certificate_id\nA,1\nB,2\nC,1\n", unique_fields=["certificate_id"])
        self.assertIn("We found 1 duplicate value in the 'certificate_id' field.", report["text"])

    def test_nothing_checked_unless_the_user_asks(self):
        report = validate(["student_name", "certificate_id"], self.CSV,
                          optional_fields=["certificate_id"])   # no unique_fields given
        self.assertEqual(report["duplicate_group_count"], 0)
        self.assertTrue(report["can_generate"])

    def test_any_field_can_be_unique_nothing_is_hardcoded(self):
        report = validate(["email"], "email\na@x.com\nb@x.com\nA@X.COM\n", unique_fields=["email"])
        self.assertEqual(report["duplicate_group_count"], 1)

    def test_unique_check_on_unmatched_field_is_ignored_not_crashing(self):
        report = validate(["student_name", "badge"], "student_name\nA\nB\n", unique_fields=["badge"])
        self.assertEqual(report["duplicate_group_count"], 0)
        self.assertEqual(report["missing_columns"], ["badge"])

    def test_hint_for_id_like_column_with_repeats(self):
        rows = "".join(f"P{i},ID{i}\n" for i in range(12)) + "P99,ID3\n"
        report = validate(["student_name", "certificate_id"], "student_name,certificate_id\n" + rows)
        hint = message(report, "unique_hint")
        self.assertEqual(hint["fields"], ["certificate_id"])
        self.assertEqual(hint["level"], "info")
        self.assertTrue(report["can_generate"])                        # a hint never blocks
        again = validate(["student_name", "certificate_id"], "student_name,certificate_id\n" + rows,
                         unique_fields=["certificate_id"])
        self.assertIsNone(message(again, "unique_hint"))               # no hint once the user checks it

    def test_no_hint_for_ordinary_repeating_columns(self):
        rows = "".join(f"P{i},{'Python' if i % 2 else 'Java'}\n" for i in range(12))
        report = validate(["student_name", "course_name"], "student_name,course_name\n" + rows)
        self.assertIsNone(message(report, "unique_hint"))


class TestFormats(unittest.TestCase):
    def test_date_validator(self):
        for good in ("2026-07-31", "31-07-2026", "31/07/2026", "07/31/2026", "31 Jul 2026", "July 31, 2026"):
            self.assertTrue(is_valid_format("date", good), good)
        for bad in ("31-02-2026", "2026-13-01", "tomorrow", "31-07", "20260731", "1/1/"):
            self.assertFalse(is_valid_format("date", bad), bad)

    def test_number_and_email_validators(self):
        for good in ("101", "99.5", "-3", "1,250", "1 250"):
            self.assertTrue(is_valid_format("number", good), good)
        for bad in ("12a", "ten", "1.2.3", "--1"):
            self.assertFalse(is_valid_format("number", bad), bad)
        self.assertTrue(is_valid_format("email", "roja@kits.ac.in"))
        for bad in ("roja@", "roja.kits.ac.in", "a b@c.com", "@x.com"):
            self.assertFalse(is_valid_format("email", bad), bad)

    def test_format_report_lists_every_bad_value(self):
        csv_text = "student_name,start_date,fee\nRoja,2026-06-01,1500\nUsha,31-02-2026,1500\nRavi,2026-06-03,abc\nPriya,,200\n"
        report = validate(["student_name", "start_date", "fee"], csv_text,
                          field_formats={"start_date": "date", "fee": "number"})
        found = [(i["row"], i["participant"], i["field"], i["value"]) for i in report["format_issues"]]
        self.assertEqual(found, [(3, "Usha", "start_date", "31-02-2026"), (4, "Ravi", "fee", "abc")])
        self.assertEqual(report["format_issue_count"], 2)
        self.assertEqual(report["cell_issue_count"], 1)   # Priya's EMPTY date: a missing value, not a wrong format
        self.assertEqual([i["participant"] for i in report["cell_issues"]], ["Priya"])
        self.assertIn("2 values are not in the expected format.", report["text"])
        self.assertIn("| 3 | Usha | start_date | 31-02-2026 | date |", report["text"])
        self.assertIn("Dates must be real dates", report["text"])
        self.assertFalse(report["can_generate"])

    def test_any_format_and_unknown_format_names_do_nothing(self):
        report = validate(["student_name"], "student_name\nRoja\n", field_formats={"student_name": "any"})
        self.assertEqual(report["format_issue_count"], 0)

    def test_empty_required_value_and_bad_format_are_separate_messages(self):
        csv_text = "student_name,start_date\nRoja,31-02-2026\nUsha,\n"
        report = validate(["student_name", "start_date"], csv_text, field_formats={"start_date": "date"})
        self.assertIsNotNone(message(report, "missing_values"))
        self.assertIsNotNone(message(report, "invalid_format"))
        self.assertEqual(report["invalid_row_numbers"], [2, 3])

    def test_optional_field_still_checked_when_filled(self):
        report = validate(["student_name", "email"], "student_name,email\nRoja,not-an-email\nUsha,\n",
                          optional_fields=["email"], field_formats={"email": "email"})
        self.assertEqual(report["format_issue_count"], 1)
        self.assertEqual(report["cell_issue_count"], 0)                    # optional: empty is fine


class TestSmartMapping(unittest.TestCase):
    def test_exact_normalised_match_needs_no_suggestion(self):
        report = validate(["student_name"], "Student Name\nRoja\n")
        self.assertEqual(report["suggestions"], {})
        self.assertTrue(report["can_generate"])

    def test_close_column_is_suggested_but_not_applied(self):
        report = validate(["student_name", "course_name"], "student_full_name,course_name\nRoja,Python\n")
        self.assertEqual(report["missing_columns"], ["student_name"])      # still blocked until confirmed
        self.assertFalse(report["can_generate"])
        suggestion = report["suggestions"]["student_name"]
        self.assertEqual(suggestion["status"], "suggested")
        self.assertEqual(suggestion["candidates"][0]["column"], "student_full_name")

    def test_accepting_the_suggestion_through_mapping_resolves_it(self):
        report = validate(["student_name", "course_name"], "student_full_name,course_name\nRoja,Python\n",
                          mapping={"student_name": "student_full_name"})
        self.assertTrue(report["can_generate"])
        self.assertEqual(report["suggestions"], {})

    def test_two_plausible_columns_are_ambiguous(self):
        report = validate(["student_name"], "first_name,last_name\nRoja,P\n")
        self.assertEqual(report["suggestions"]["student_name"]["status"], "ambiguous")
        self.assertEqual({c["column"] for c in report["suggestions"]["student_name"]["candidates"]},
                         {"first_name", "last_name"})

    def test_same_column_wanted_by_two_fields_is_ambiguous_for_both(self):
        result = suggest_mappings(["student_name", "guest_name"], ["name"])
        self.assertEqual({f: r["status"] for f, r in result.items()},
                         {"student_name": "ambiguous", "guest_name": "ambiguous"})

    def test_columns_already_used_are_never_suggested(self):
        # 'college_name' is an exact match for its own field, so it must not be offered for principal_name
        report = validate(["college_name", "principal_name"], "college_name\nKITS\n")
        self.assertEqual(report["suggestions"], {})

    def test_unrelated_columns_are_not_guessed(self):
        report = validate(["principal_name"], "email,phone_number,address\na,b,c\n")
        self.assertEqual(report["suggestions"], {})

    def test_user_choice_of_not_in_my_file_stops_the_nagging(self):
        report = validate(["student_name"], "student_full_name\nRoja\n", mapping={"student_name": ""})
        self.assertEqual(report["suggestions"], {})

    def test_abbreviations(self):
        self.assertGreaterEqual(similarity("department", "dept"), 0.5)
        self.assertLess(similarity("certificate_id", "id"), 0.5)            # too weak to ever be offered
        self.assertEqual(similarity("principal_name", "email"), 0.0)

    def test_case1_text_is_unchanged_by_suggestions(self):
        report = validate(
            ["student_name", "course_name", "start_date", "end_date", "certificate_id", "college_name", "principal_name"],
            "student_name,course_name,start_date,end_date,certificate_id,college_name\nA,B,C,D,E,F\n")
        self.assertNotIn("suggest", report["text"].lower())


class TestLabelColumn(unittest.TestCase):
    CSV = "roll_no,student_name,course_name\nR1,Roja,Python\nR2,Usha,\n"

    def test_default_is_the_first_column_no_name_guessing(self):
        report = validate(["student_name", "course_name"], self.CSV)
        self.assertEqual(report["cell_issues"][0]["participant"], "R2")
        self.assertEqual(report["label_column"], "roll_no")

    def test_user_can_choose_the_label_column(self):
        report = validate(["student_name", "course_name"], self.CSV, label_column="Student Name")
        self.assertEqual(report["cell_issues"][0]["participant"], "Usha")

    def test_falls_back_when_the_label_cell_is_empty(self):
        report = validate(["student_name", "course_name"], "student_name,course_name\n,Python\nUsha,\n",
                          label_column="student_name")
        # an empty name cell is shown as "Row N", never as another column's value ("Python")
        self.assertEqual([(i["row"], i["participant"]) for i in report["cell_issues"]],
                         [(2, "Row 2"), (3, "Usha")])


if __name__ == "__main__":
    unittest.main(verbosity=2)