import os

from sqlalchemy import create_engine, text
from sqlalchemy.orm import sessionmaker

from .database import Base

DATABASE_URL = os.environ["DATABASE_URL"]

engine = create_engine(
    DATABASE_URL,
    pool_pre_ping=True,
)

SessionLocal = sessionmaker(
    autocommit=False,
    autoflush=False,
    bind=engine,
)


def init_db(bind=None):
    """Create any missing tables and add columns introduced since release."""
    target = bind or engine
    Base.metadata.create_all(bind=target)

    # create_all does not alter existing tables; add new nullable columns.
    with target.begin() as conn:
        for column, ddl in (
            ("audio_file_path", "VARCHAR"),
            ("audio_original_filename", "VARCHAR"),
            ("audio_size_bytes", "INTEGER"),
            ("owner_email", "VARCHAR"),
        ):
            conn.execute(
                text(
                    f"ALTER TABLE meetings "
                    f"ADD COLUMN IF NOT EXISTS {column} {ddl}"
                )
            )
        conn.execute(text("ALTER TABLE users ADD COLUMN IF NOT EXISTS name VARCHAR"))
        conn.execute(text("ALTER TABLE transcripts ADD COLUMN IF NOT EXISTS speaker_voices JSON"))
        conn.execute(text("ALTER TABLE voice_profiles ADD COLUMN IF NOT EXISTS email VARCHAR"))
        for ddl in (
            "ALTER TABLE users ADD COLUMN IF NOT EXISTS timezone VARCHAR",
            "ALTER TABLE users ADD COLUMN IF NOT EXISTS email_notifications BOOLEAN NOT NULL DEFAULT FALSE",
            "ALTER TABLE meetings ADD COLUMN IF NOT EXISTS timezone VARCHAR",
            "ALTER TABLE meetings ADD COLUMN IF NOT EXISTS series_id VARCHAR",
            "ALTER TABLE meetings ADD COLUMN IF NOT EXISTS recurrence VARCHAR",
            "ALTER TABLE meetings ADD COLUMN IF NOT EXISTS source VARCHAR",
            "ALTER TABLE meetings ADD COLUMN IF NOT EXISTS consent_at TIMESTAMP",
            "ALTER TABLE action_items ADD COLUMN IF NOT EXISTS due_phrase VARCHAR",
            "CREATE INDEX IF NOT EXISTS ix_meetings_series_id ON meetings (series_id)",
            "ALTER TABLE meetings ADD COLUMN IF NOT EXISTS folder_id VARCHAR REFERENCES folders(id)",
            "CREATE INDEX IF NOT EXISTS ix_meetings_folder_id ON meetings (folder_id)",
            "ALTER TABLE summaries ADD COLUMN IF NOT EXISTS follow_up TEXT",
            "ALTER TABLE ask_history ADD COLUMN IF NOT EXISTS folder_id VARCHAR",
            "CREATE INDEX IF NOT EXISTS ix_ask_history_folder_id ON ask_history (folder_id)",
        ):
            conn.execute(text(ddl))
        conn.execute(
            text(
                "CREATE INDEX IF NOT EXISTS ix_meetings_owner_email "
                "ON meetings (owner_email)"
            )
        )


def get_db():
    """Provide a database session and close it after use."""
    db = SessionLocal()
    try:
        yield db
    finally:
        db.close()
