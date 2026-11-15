"""Field-name normalisation shared by every module (no heavy dependencies)."""
import re
import unicodedata


def normalize_field_name(value):
    """'Student Name', 'student-name', ' STUDENT_NAME ' -> 'student_name'.

    Letters/digits of any script are kept; returns '' if nothing is left.
    """
    value = str(value or "").strip().lstrip("\ufeff").lower()
    value = re.sub(r"[\s\-]+", "_", value)
    value = "".join(
        ch for ch in value
        if ch == "_" or ch.isalnum() or unicodedata.category(ch).startswith("M")
    )
    return re.sub(r"_+", "_", value).strip("_")