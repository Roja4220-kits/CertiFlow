"""Test environment: isolated data folder, and stand-ins ONLY for libraries that are not installed.

On a normal machine (PyMuPDF + Flask-SQLAlchemy installed) nothing is stubbed.
"""
import os
import sys
import tempfile
import types
from unittest.mock import MagicMock

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if ROOT not in sys.path:
    sys.path.insert(0, ROOT)

os.environ.setdefault("CERTIFLOW_DATA_DIR", tempfile.mkdtemp(prefix="certiflow_test_"))
os.environ["CERTIFLOW_DISABLE_BACKGROUND"] = "1"

STUBBED = []

try:
    import pymupdf  # noqa: F401
except ImportError:
    try:
        import fitz  # noqa: F401
    except ImportError:
        sys.modules["fitz"] = types.ModuleType("fitz")
        STUBBED.append("PyMuPDF")

try:
    import flask_sqlalchemy  # noqa: F401
    import sqlalchemy  # noqa: F401
except ImportError:
    stub = types.ModuleType("models")
    stub.db = MagicMock()
    stub.CertificateJob = MagicMock()
    stub.CertificateRecipient = MagicMock()
    stub.configure_sqlite = lambda: None
    stub.ensure_schema = lambda: None

    class JobStatus:
        PENDING, QUEUED, PROCESSING, COMPLETED, FAILED = "pending", "queued", "processing", "completed", "failed"

    class RecipientStatus:
        PENDING, PROCESSING, SUCCESS, FAILED = "pending", "processing", "success", "failed"

    stub.JobStatus, stub.RecipientStatus = JobStatus, RecipientStatus
    sys.modules["models"] = stub
    STUBBED.append("Flask-SQLAlchemy")


# A real TrueType font for the font-upload tests (any of these will do).
_FONT_CANDIDATES = [
    "/usr/share/fonts/truetype/dejavu/DejaVuSans.ttf",
    "/Library/Fonts/Arial.ttf", "/System/Library/Fonts/Supplemental/Arial.ttf",
    r"C:\Windows\Fonts\arial.ttf", r"C:\Windows\Fonts\segoeui.ttf", r"C:\Windows\Fonts\calibri.ttf",
]
FONT = next((p for p in _FONT_CANDIDATES if os.path.exists(p)), None)