import secrets
from datetime import datetime, timezone

from flask_sqlalchemy import SQLAlchemy
from sqlalchemy.ext.mutable import MutableDict

db = SQLAlchemy()


def utcnow():
    return datetime.now(timezone.utc)


def new_token():
    """Unguessable public id for a job (job ids in URLs must not be enumerable)."""
    return secrets.token_urlsafe(16)


class JobStatus:
    PENDING = "pending"
    QUEUED = "queued"
    PROCESSING = "processing"
    COMPLETED = "completed"
    FAILED = "failed"


class RecipientStatus:
    PENDING = "pending"
    PROCESSING = "processing"
    SUCCESS = "success"
    FAILED = "failed"


class CertificateJob(db.Model):
    """One bulk certificate generation run."""

    __tablename__ = "certificate_jobs"

    id = db.Column(db.Integer, primary_key=True)
    token = db.Column(db.String(40), unique=True, index=True, default=new_token)
    status = db.Column(db.String(20), nullable=False, default=JobStatus.PENDING, index=True)
    total_recipients = db.Column(db.Integer, nullable=False, default=0)
    successful = db.Column(db.Integer, nullable=False, default=0)
    failed = db.Column(db.Integer, nullable=False, default=0)
    error_message = db.Column(db.Text, nullable=True)
    created_at = db.Column(db.DateTime, nullable=False, default=utcnow)
    updated_at = db.Column(db.DateTime, nullable=False, default=utcnow, onupdate=utcnow)

    recipients = db.relationship(
        "CertificateRecipient", back_populates="job",
        cascade="all, delete-orphan", order_by="CertificateRecipient.id", lazy=True,
    )

    @property
    def progress_percentage(self):
        if not self.total_recipients:
            return 0
        return round(((self.successful or 0) + (self.failed or 0)) / self.total_recipients * 100, 2)

    def __repr__(self):
        return f"<CertificateJob id={self.id}, status={self.status}, total={self.total_recipients}>"


class CertificateRecipient(db.Model):
    """One participant and their generated certificate.

    participant_data holds the template fields plus two bookkeeping keys:
    "_row" (spreadsheet row number) and "_label" (participant name for display).
    """

    __tablename__ = "certificate_recipients"

    id = db.Column(db.Integer, primary_key=True)
    job_id = db.Column(db.Integer, db.ForeignKey("certificate_jobs.id"), nullable=False, index=True)
    certificate_id = db.Column(db.String(100), unique=True, nullable=False, index=True)
    participant_data = db.Column(MutableDict.as_mutable(db.JSON), nullable=False, default=dict)
    status = db.Column(db.String(20), nullable=False, default=RecipientStatus.PENDING, index=True)
    file_path = db.Column(db.String(500), nullable=True)
    error_message = db.Column(db.Text, nullable=True)
    created_at = db.Column(db.DateTime, nullable=False, default=utcnow)
    updated_at = db.Column(db.DateTime, nullable=False, default=utcnow, onupdate=utcnow)

    job = db.relationship("CertificateJob", back_populates="recipients")

    def to_dict(self):
        data = self.participant_data or {}
        return {
            "certificate_id": self.certificate_id, "status": self.status,
            "file": self.file_path, "error": self.error_message,
            "row": data.get("_row"), "participant": data.get("_label"),
        }

    def __repr__(self):
        return f"<CertificateRecipient id={self.id}, certificate_id={self.certificate_id}, status={self.status}>"


def configure_sqlite():
    """Use WAL + a busy timeout so background workers and web requests share SQLite safely."""
    import sqlite3
    from sqlalchemy import event
    from sqlalchemy.engine import Engine

    @event.listens_for(Engine, "connect")
    def _pragmas(dbapi_connection, _record):
        if isinstance(dbapi_connection, sqlite3.Connection):
            cursor = dbapi_connection.cursor()
            cursor.execute("PRAGMA journal_mode=WAL")
            cursor.execute("PRAGMA busy_timeout=15000")
            cursor.close()


def ensure_schema():
    """Add columns introduced after the first release to an existing SQLite file."""
    from sqlalchemy import inspect, text

    inspector = inspect(db.engine)
    if "certificate_jobs" not in inspector.get_table_names():
        return
    existing = {c["name"] for c in inspector.get_columns("certificate_jobs")}
    with db.engine.begin() as connection:
        if "token" not in existing:
            connection.execute(text("ALTER TABLE certificate_jobs ADD COLUMN token VARCHAR(40)"))
        if "error_message" not in existing:
            connection.execute(text("ALTER TABLE certificate_jobs ADD COLUMN error_message TEXT"))
        for (job_id,) in connection.execute(
            text("SELECT id FROM certificate_jobs WHERE token IS NULL")
        ).fetchall():
            connection.execute(
                text("UPDATE certificate_jobs SET token=:t WHERE id=:i"),
                {"t": new_token(), "i": job_id},
            )