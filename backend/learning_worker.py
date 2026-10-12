"""Run durable evaluation jobs and periodic incomplete-record maintenance."""
import argparse
import logging
from pathlib import Path
import signal
import time
from threading import Event

from dotenv import load_dotenv
from sqlalchemy.exc import SQLAlchemyError

from config import Settings
from diagnostics import log_failure
from evaluation_worker import Worker
from learning_lifecycle import archive_incomplete, backfill_outcomes
from study_sessions import expire_sessions
from review_records import maintain_review_records
from market_engine import maintain_markets

logger = logging.getLogger(__name__)


def maintain(worker, stopped, once=False):
    """Keep deadlines and prices independent of model latency."""
    maintenance_at = 0
    while not stopped.is_set():
        count = 0
        try:
            count = maintain_markets(worker.factory)
            if time.monotonic() >= maintenance_at:
                expire_sessions(worker.factory)
                maintain_review_records(worker.factory)
                archive_incomplete(worker.factory)
                backfill_outcomes(worker.factory)
                worker.backfill_pending()
                maintenance_at = time.monotonic() + 30
        except SQLAlchemyError as error:
            log_failure(logger, "Learning maintenance data service unavailable; retrying.", error)
        if once:
            return
        # Drain full batches promptly; do not make every 100 users wait 30 seconds.
        stopped.wait(0.1 if count >= 100 else 1)


def main():
    load_dotenv(Path(__file__).resolve().parent / ".env")
    parser = argparse.ArgumentParser()
    parser.add_argument("--once", action="store_true")
    parser.add_argument("--mode", choices=("evaluation", "maintenance"), default="evaluation")
    args = parser.parse_args()
    worker = Worker(Settings.from_env())
    stopped = Event()
    signal.signal(signal.SIGTERM, lambda *_: stopped.set())
    signal.signal(signal.SIGINT, lambda *_: stopped.set())
    try:
        if args.mode == "maintenance":
            maintain(worker, stopped, args.once)
            return
        while not stopped.is_set():
            worked = False
            try:
                worked = worker.run_once()
            except SQLAlchemyError as error:
                log_failure(logger, "Learning data service unavailable; retrying.", error)
            if args.once:
                break
            if not worked:
                stopped.wait(2)
    finally:
        worker.close()


if __name__ == "__main__":
    main()
