"""Backup and finish expired active timers, preserving IDs and all paused segments."""
import argparse
from datetime import datetime, timezone
import json
import os
from pathlib import Path
import sys
import uuid

BACKEND = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(BACKEND))
from dotenv import load_dotenv
from sqlalchemy import select
from sqlalchemy.orm import sessionmaker
from config import Settings
from db import make_engine
from models import StudySession
from study_sessions import TIMING_GRACE, activate_review_tracking, close_at_cutoff, lock_reader, session_segments


def repair_expired(factory, backup_dir=None, *, now=None, session_id=None):
    now = now or datetime.now(timezone.utc)
    query = select(StudySession.id, StudySession.user_id).where(
        StudySession.phase.in_(["running", "paused"]), StudySession.ends_at <= now - TIMING_GRACE)
    if session_id:
        query = query.where(StudySession.id == session_id)
    with factory() as db:
        candidates = db.execute(query.order_by(StudySession.ends_at, StudySession.id)).all()
    if backup_dir is None:
        return len(candidates)
    backup_dir = Path(backup_dir)
    backup_dir.mkdir(parents=True, exist_ok=True, mode=0o700)
    if backup_dir.is_symlink() or backup_dir.stat().st_mode & 0o077:
        raise ValueError("Backup directory must be private (mode 700) and not a symlink")
    repaired = 0
    for identifier, owner in candidates:
        with factory.begin() as db:
            lock_reader(db, owner)
            item = db.get(StudySession, identifier)
            if not item or item.phase == "finished" or item.ends_at > now - TIMING_GRACE:
                continue
            segments = session_segments(db, owner, identifier)
            snapshot = {"session": {column.name: getattr(item, column.name) for column in StudySession.__table__.columns},
                        "segments": [{column.name: getattr(row, column.name) for column in row.__table__.columns} for row in segments],
                        "backed_up_at": now}
            path = backup_dir / f"{identifier}-{uuid.uuid4().hex}.json"
            # Exclusive creation, durable backup, then mutation in the same owner lock.
            with path.open("x", encoding="utf-8") as output:
                os.chmod(path, 0o600)
                json.dump(snapshot, output, ensure_ascii=False, indent=2, default=str)
                output.flush()
                os.fsync(output.fileno())
            close_at_cutoff(item, segments, "repair")
            activate_review_tracking(db, item, segments)
            db.flush()
            repaired += 1
    return repaired


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--apply", action="store_true")
    parser.add_argument("--backup-dir", type=Path)
    parser.add_argument("--session-id", type=uuid.UUID)
    args = parser.parse_args()
    if args.apply and args.backup_dir is None:
        parser.error("--apply requires --backup-dir")
    load_dotenv(BACKEND / ".env")
    engine = make_engine(Settings.from_env().database_url)
    if engine is None:
        parser.error("DATABASE_URL is required")
    try:
        count = repair_expired(sessionmaker(engine), args.backup_dir if args.apply else None, session_id=args.session_id)
        print(json.dumps({"mode": "apply" if args.apply else "dry-run", "expired_timers": count}))
    finally:
        engine.dispose()


if __name__ == "__main__":
    main()
