"""Read participant files (CSV / XLSX / text PDF) into clean, numbered rows."""
import csv
from datetime import date, datetime

from fieldnames import normalize_field_name

DEFAULT_MAX_ROWS = 5000


def cell_to_text(value):
    if value is None:
        return ""
    if isinstance(value, datetime):
        return value.strftime("%Y-%m-%d")
    if isinstance(value, date):
        return value.isoformat()
    if isinstance(value, float) and value.is_integer():
        return str(int(value))  # Excel stores 101 as 101.0
    return str(value).strip()


def _read_csv(file_path):
    for encoding in ("utf-8-sig", "cp1252"):
        try:
            with open(file_path, "r", encoding=encoding, newline="") as handle:
                sample = handle.read(8192)
                handle.seek(0)
                try:
                    dialect = csv.Sniffer().sniff(sample, delimiters=",;\t")
                    return list(csv.reader(handle, dialect))
                except csv.Error:
                    handle.seek(0)
                    return list(csv.reader(handle))
        except UnicodeDecodeError:
            continue
    raise ValueError("The CSV file could not be read. Please save it as UTF-8 CSV.")


def _read_rows(file_path, extension):
    if extension == ".csv":
        return _read_csv(file_path)
    if extension == ".xlsx":
        from openpyxl import load_workbook
        workbook = load_workbook(file_path, read_only=True, data_only=True)
        try:
            return list(workbook.active.iter_rows(values_only=True))
        finally:
            workbook.close()
    if extension == ".pdf":
        from pypdf import PdfReader
        reader = PdfReader(file_path)
        if reader.is_encrypted:
            raise ValueError("Password-protected participant PDFs are not supported.")
        content = "\n".join(page.extract_text() or "" for page in reader.pages)
        return [next(csv.reader([line])) for line in content.splitlines() if line.strip()]
    raise ValueError("Participants must be CSV, XLSX, or text-based PDF.")


def read_participant_file(file_path, extension, max_rows=DEFAULT_MAX_ROWS):
    """Return {'headers', 'original_headers', 'participants', 'blank_rows_skipped'}.

    * Row numbers are spreadsheet row numbers (header = row 1, first person = 2).
    * Completely blank rows are skipped and counted, never turned into errors.
    * Blank header cells are ignored (common trailing empty Excel columns).
    """
    rows = _read_rows(file_path, extension)
    if len(rows) < 2:
        raise ValueError(
            "The file must contain a header row and at least one participant row."
        )

    columns, seen = [], {}  # (index, normalised name)
    original_headers = {}
    for index, raw in enumerate(rows[0]):
        if raw is None or not str(raw).strip():
            continue
        name = normalize_field_name(raw)
        if not name:
            raise ValueError(
                f"Column {index + 1} heading '{raw}' has no usable letters or digits."
            )
        if name in seen:
            raise ValueError(
                f"Duplicate column names: '{seen[name]}' and '{str(raw).strip()}' "
                f"both become '{name}'. Please rename one of them."
            )
        seen[name] = str(raw).strip()
        original_headers[name] = str(raw).strip()
        columns.append((index, name))
    if not columns:
        raise ValueError("The header row is empty.")

    participants, blank_rows = [], 0
    for row_number, row in enumerate(rows[1:], start=2):
        fields = {
            name: cell_to_text(row[index]) if index < len(row) else ""
            for index, name in columns
        }
        if not any(fields.values()):
            blank_rows += 1
            continue
        participants.append({"row": row_number, "fields": fields})

    if not participants:
        raise ValueError("No participant records were found (all rows are empty).")
    if len(participants) > max_rows:
        raise ValueError(
            f"The file has {len(participants)} participants. The limit is {max_rows} per batch; "
            "please split the file."
        )
    return {
        "headers": [name for _, name in columns],
        "original_headers": original_headers,
        "participants": participants,
        "blank_rows_skipped": blank_rows,
    }