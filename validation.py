"""Dynamic validation of template fields against participant data.

Pure Python: no field name is hard-coded. Template fields and file columns are
both discovered at run time and compared after normalisation.

Problems are kept strictly apart, each with its own message:
  * missing COLUMN   - a template field has no matching column in the file
  * missing VALUE    - the column exists, but a participant's cell is empty
  * duplicate VALUE  - a field the user marked "must be unique" repeats
  * invalid FORMAT   - a field the user gave a format (date/number/email) holds something else
Information (never an error): unused columns, blank rows, optional fields,
possible unique columns, suggested column matches.
"""
import re
from datetime import datetime

from fieldnames import normalize_field_name
from mapping import suggest_mappings

FORMATS = ("any", "date", "number", "email")
FORMAT_NAMES = {"date": "a date", "number": "a number", "email": "an email address"}
FORMAT_HINTS = {
    "date": "Dates must be real dates, for example 2026-07-31 or 31-07-2026.",
    "number": "Numbers may only contain digits (and a decimal point), for example 1250 or 99.5.",
    "email": "Email addresses look like name@example.com.",
}
_DATE_FORMATS = (
    "%Y-%m-%d", "%d-%m-%Y", "%d/%m/%Y", "%m/%d/%Y", "%Y/%m/%d", "%d.%m.%Y",
    "%d %b %Y", "%d %B %Y", "%b %d, %Y", "%B %d, %Y", "%Y-%m-%d %H:%M:%S",
)
_EMAIL = re.compile(r"[^@\s]+@[^@\s]+\.[^@\s]+")
_NUMBER = re.compile(r"[-+]?\d+(\.\d+)?")
UNIQUE_HINT_MIN_ROWS = 5
UNIQUE_HINT_RATIO = 0.9


def humanize(field):
    text = str(field).replace("_", " ").strip()
    return text[:1].upper() + text[1:]


def plural(count, noun):
    return f"{count} {noun}" if count == 1 else f"{count} {noun}s"


def is_valid_format(kind, value):
    """True when `value` (a non-empty string) matches the format `kind`."""
    value = str(value).strip()
    if kind == "date":
        for pattern in _DATE_FORMATS:
            try:
                datetime.strptime(value, pattern)
                return True
            except ValueError:
                continue
        return False
    if kind == "number":
        return bool(_NUMBER.fullmatch(value.replace(",", "").replace(" ", "")))
    if kind == "email":
        return bool(_EMAIL.fullmatch(value))
    return True


def _unique_normalized(values):
    seen, result = set(), []
    for value in values or []:
        name = normalize_field_name(value)
        if name and name not in seen:
            seen.add(name)
            result.append(name)
    return result


def label_columns(columns, preferred=None):
    """Columns to try, in order, when naming a participant in a message.

    The user's chosen column comes first; otherwise the file's own column order
    is used (no guessing from names).
    """
    preferred = normalize_field_name(preferred) if preferred else ""
    first = [preferred] if preferred in columns else []
    return first + [c for c in columns if c not in first]


def participant_label(values, columns, row):
    """The participant's name from the label column, or 'Row N' when that cell is empty
    (never another column's value: 'Participant: Python' would mislead)."""
    value = str(values.get(columns[0], "")).strip() if columns else ""
    return value or f"Row {row}"


def build_report(
    template_fields, available_fields, participants, mapping=None,
    optional_fields=None, blank_rows_skipped=0, source_label="CSV file",
    max_issues=500, unique_fields=None, field_formats=None, label_column=None,
):
    """Validate everything and return a structured report.

    template_fields  : placeholders found in the template
    available_fields : columns found in the participant file
    participants     : [{'row': int, 'fields': {column: value}}]
    mapping          : optional {template_field: column}; '' = "not in my file"
    optional_fields  : template fields that may stay empty
    unique_fields    : template fields whose values must all differ
    field_formats    : {template_field: 'date'|'number'|'email'|'any'}
    label_column     : file column used to name participants in messages
    """
    fields = _unique_normalized(template_fields)
    columns = _unique_normalized(available_fields)
    column_set = set(columns)
    optional = set(_unique_normalized(optional_fields))
    unique = _unique_normalized(unique_fields)
    formats = {
        normalize_field_name(k): str(v).lower()
        for k, v in (field_formats or {}).items() if normalize_field_name(k)
    }
    mapping = {
        normalize_field_name(k): normalize_field_name(v)
        for k, v in (mapping or {}).items() if normalize_field_name(k)
    }
    short = source_label.split()[0]

    column_for = {}
    for field in fields:
        column = mapping[field] if field in mapping else field
        column_for[field] = column if column in column_set else None

    matched = [f for f in fields if column_for[f]]
    unmatched = [f for f in fields if not column_for[f]]
    missing_columns = [f for f in unmatched if f not in optional]
    optional_blank = [f for f in unmatched if f in optional]
    used_columns = {column_for[f] for f in matched}
    extra_columns = [c for c in columns if c not in used_columns]
    checked = [f for f in matched if f not in optional]

    # Suggested matches (only a hint). A field the user explicitly set to
    # "not in my file" is left alone.
    asking = [f for f in unmatched if mapping.get(f, None) != ""]
    suggestions = suggest_mappings(asking, [c for c in columns if c not in used_columns])

    label_cols = label_columns(columns, label_column)

    def label_of(person):
        return participant_label(person["fields"], label_cols, person["row"])

    # -- missing cell values ---------------------------------------------------
    issues, empty_rows, issue_count = [], set(), 0
    for person in participants:
        row, values = person["row"], person["fields"]
        for field in checked:
            column = column_for[field]
            if str(values.get(column, "")).strip():
                continue
            issue_count += 1
            empty_rows.add(row)
            if len(issues) < max_issues:
                issues.append({
                    "row": row, "participant": label_of(person), "field": field,
                    "column": column, "problem": f"{humanize(field)} is empty.",
                })

    # -- duplicate values (first occurrence is kept, later ones are the problem)
    duplicate_groups, duplicate_issues, duplicate_rows = [], [], set()
    group_count = 0
    unique_checked = [f for f in unique if column_for.get(f)]
    for field in unique_checked:
        column = column_for[field]
        seen = {}
        for person in participants:
            value = str(person["fields"].get(column, "")).strip()
            if value:
                seen.setdefault(value.casefold(), []).append((person["row"], value, person))
        for occurrences in seen.values():
            if len(occurrences) < 2:
                continue
            group_count += 1
            first_row = occurrences[0][0]
            labels = [label_of(o[2]) for o in occurrences]
            if len(duplicate_groups) < max_issues:
                duplicate_groups.append({
                    "field": field, "column": column, "value": occurrences[0][1],
                    "rows": [o[0] for o in occurrences], "participants": labels,
                })
            for (row, value, _person), label in list(zip(occurrences, labels))[1:]:
                duplicate_rows.add(row)
                if len(duplicate_issues) < max_issues:
                    duplicate_issues.append({
                        "row": row, "participant": label, "field": field, "column": column,
                        "value": value, "first_row": first_row,
                        "problem": f"{humanize(field)} '{value}' is already used in row {first_row}.",
                    })

    # -- invalid formats ---------------------------------------------------------
    format_checked = {
        f: k for f, k in formats.items() if k in FORMAT_NAMES and column_for.get(f)
    }
    format_issues, format_rows, format_count = [], set(), 0
    for person in participants:
        for field, kind in format_checked.items():
            column = column_for[field]
            value = str(person["fields"].get(column, "")).strip()
            if not value or is_valid_format(kind, value):
                continue
            format_count += 1
            format_rows.add(person["row"])
            if len(format_issues) < max_issues:
                format_issues.append({
                    "row": person["row"], "participant": label_of(person), "field": field,
                    "column": column, "value": value, "expected": kind,
                    "problem": f"'{value}' is not {FORMAT_NAMES[kind]}.",
                })

    # -- columns that look like IDs but repeat (a hint, not an error) ----------
    unique_hints = []
    for field in matched:
        if field in unique_checked:
            continue
        values = [str(p["fields"].get(column_for[field], "")).strip().casefold() for p in participants]
        values = [v for v in values if v]
        distinct = len(set(values))
        if len(values) >= UNIQUE_HINT_MIN_ROWS and distinct < len(values) \
                and distinct / len(values) >= UNIQUE_HINT_RATIO:
            unique_hints.append({"field": field, "repeats": len(values) - distinct})

    bad_rows = empty_rows | duplicate_rows | format_rows
    total = len(participants)
    valid_rows = total - len(bad_rows)
    problem_count = issue_count + len(duplicate_rows) + format_count
    messages = []

    # -- 1. missing columns ---------------------------------------------------
    if missing_columns:
        if len(missing_columns) == 1:
            summary = (
                f"Your certificate template contains {plural(len(fields), 'field')}, "
                f"but your {source_label} contains only {plural(len(matched), 'matching field')}."
            )
            help_text = (
                f"Please add the missing column to your {source_label} or manually "
                "map this field before generating certificates."
            )
        else:
            summary = (
                f"{len(missing_columns)} required template fields could not be "
                f"matched with your {source_label}."
            )
            help_text = (
                f"Please add the missing columns to your {source_label} or manually "
                "map these fields before generating certificates."
            )
        messages.append({
            "level": "error", "code": "missing_columns",
            "title": f"Template and {short} column mismatch detected.",
            "summary": summary,
            "items_label": "Missing field:" if len(missing_columns) == 1 else "Missing fields:",
            "items": missing_columns, "fields": missing_columns, "help": help_text,
        })

    # -- 2. missing cell values -----------------------------------------------
    if issue_count:
        if issue_count == 1:
            summary = "We found 1 missing value in your uploaded file."
            help_text = (
                f"Please provide the missing {humanize(issues[0]['field']).lower()} "
                "before generating the certificate."
            )
        else:
            summary = f"{issue_count} missing values found in your participant data."
            help_text = (
                "Please fix these values in your file and upload it again, mark the "
                "field as optional, or choose to skip these participants."
            )
        messages.append({
            "level": "error", "code": "missing_values",
            "title": "Participant data validation failed.",
            "summary": summary, "issues": issues, "issue_count": issue_count,
            "fields": sorted({i["field"] for i in issues}), "help": help_text,
        })

    # -- 3. duplicate values --------------------------------------------------
    if group_count:
        if group_count == 1:
            summary = f"We found 1 duplicate value in the '{duplicate_groups[0]['field']}' field."
        else:
            summary = f"{group_count} duplicate values found in your participant data."
        messages.append({
            "level": "error", "code": "duplicate_values",
            "title": "Duplicate values found.",
            "summary": summary, "groups": duplicate_groups, "group_count": group_count,
            "fields": sorted({g["field"] for g in duplicate_groups}),
            "help": (
                "These fields must be unique. Correct the repeated values and upload the "
                "file again, turn off 'Must be unique' for the field, or skip the repeated "
                "rows (the first row with each value is kept)."
            ),
        })

    # -- 4. invalid formats ---------------------------------------------------
    if format_count:
        kinds = []
        for item in format_issues:
            if item["expected"] not in kinds:
                kinds.append(item["expected"])
        summary = ("We found 1 value that is not in the expected format." if format_count == 1
                   else f"{format_count} values are not in the expected format.")
        messages.append({
            "level": "error", "code": "invalid_format",
            "title": "Some values are not in the expected format.",
            "summary": summary, "issues": format_issues, "issue_count": format_count,
            "fields": sorted({i["field"] for i in format_issues}),
            "help": "Please correct these values and upload the file again, or change the "
                    "expected format of the field. " + " ".join(FORMAT_HINTS[k] for k in kinds),
        })

    # -- 5. information -------------------------------------------------------
    if extra_columns:
        k = len(extra_columns)
        messages.append({
            "level": "info", "code": "extra_columns",
            "title": "Extra columns will be ignored",
            "summary": (
                f"Your {source_label} contains {plural(k, 'additional column')} that "
                f"{'is' if k == 1 else 'are'} not used in this certificate template. "
                f"{'It' if k == 1 else 'They'} will be ignored during certificate generation."
            ),
            "items_label": "Ignored columns:", "items": extra_columns,
        })
    if blank_rows_skipped:
        messages.append({
            "level": "info", "code": "blank_rows",
            "title": "Blank rows skipped",
            "summary": (
                f"{plural(blank_rows_skipped, 'completely blank row')} will be skipped. "
                "No certificates will be created for them."
            ),
        })
    if optional_blank:
        messages.append({
            "level": "info", "code": "optional_blank",
            "title": "Optional fields without a column",
            "summary": "These optional fields have no column and will be left blank.",
            "items_label": "Left blank:", "items": optional_blank,
        })
    for hint in unique_hints:
        messages.append({
            "level": "info", "code": "unique_hint",
            "title": f"Should '{hint['field']}' be unique?",
            "summary": (
                f"Almost every value in this column is different, but {plural(hint['repeats'], 'value')} "
                "repeat. If each participant needs their own value (like an ID), turn on "
                "'Must be unique' for this field to review the repeats."
            ),
            "fields": [hint["field"]],
        })
    if not missing_columns and not problem_count:
        messages.append({
            "level": "success", "code": "all_good",
            "title": "Everything looks good",
            "summary": f"All {plural(total, 'participant')} are ready for certificate generation.",
        })

    report = {
        "template_fields": fields,
        "template_field_count": len(fields),
        "matched_fields": matched,
        "matched_count": len(matched),
        "missing_columns": missing_columns,
        "optional_unmatched": optional_blank,
        "extra_columns": extra_columns,
        "suggestions": suggestions,
        "column_for": column_for,
        "label_column": label_cols[0] if label_cols else "",
        "unique_fields_checked": unique_checked,
        "field_formats_checked": format_checked,
        "total_rows": total,
        "valid_rows": valid_rows,
        "invalid_rows": len(bad_rows),
        "invalid_row_numbers": sorted(bad_rows),
        "cell_issues": issues,
        "cell_issue_count": issue_count,
        "duplicate_groups": duplicate_groups,
        "duplicate_group_count": group_count,
        "duplicate_issues": duplicate_issues,
        "duplicate_row_count": len(duplicate_rows),
        "format_issues": format_issues,
        "format_issue_count": format_count,
        "unique_hints": unique_hints,
        "problem_count": problem_count,
        "issues_truncated": (issue_count > len(issues) or format_count > len(format_issues)
                             or group_count > len(duplicate_groups)),
        "blank_rows_skipped": blank_rows_skipped,
        "messages": messages,
        "can_generate": not missing_columns and not problem_count and total > 0,
        "can_generate_valid_rows": not missing_columns and valid_rows > 0,
    }
    report["text"] = render_text(report)
    return report


def render_text(report):
    """Plain-text version of every message (used by API errors and tests)."""
    blocks = []
    for m in report["messages"]:
        lines = [m["title"], "", m["summary"]]
        if m.get("items"):
            lines += ["", m.get("items_label", "")] + [f"- {item}" for item in m["items"]]

        if m["code"] == "missing_values":
            issues, count = m["issues"], m["issue_count"]
            if count == 1:
                i = issues[0]
                lines += [
                    "", f"- Row: {i['row']} (including the header row)",
                    f"- Participant: {i['participant']}",
                    f"- Missing field: {i['field']}",
                    f"- Problem: {i['problem']}",
                ]
            else:
                lines += ["", "| Row | Participant | Missing field |", "|---|---|---|"]
                lines += [f"| {i['row']} | {i['participant']} | {i['field']} |" for i in issues]
                if count > len(issues):
                    lines.append(f"...and {count - len(issues)} more.")
        elif m["code"] == "duplicate_values":
            lines += ["", "| Field | Value | Rows | Participants |", "|---|---|---|---|"]
            lines += [
                f"| {g['field']} | {g['value']} | {', '.join(map(str, g['rows']))} | {', '.join(g['participants'])} |"
                for g in m["groups"]
            ]
            if m["group_count"] > len(m["groups"]):
                lines.append(f"...and {m['group_count'] - len(m['groups'])} more.")
        elif m["code"] == "invalid_format":
            lines += ["", "| Row | Participant | Field | Value | Expected |", "|---|---|---|---|---|"]
            lines += [
                f"| {i['row']} | {i['participant']} | {i['field']} | {i['value']} | {i['expected']} |"
                for i in m["issues"]
            ]
            if m["issue_count"] > len(m["issues"]):
                lines.append(f"...and {m['issue_count'] - len(m['issues'])} more.")

        if m.get("help"):
            lines += ["", m["help"]]
        blocks.append("\n".join(lines))
    return "\n\n".join(blocks)