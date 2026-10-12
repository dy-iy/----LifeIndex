"""Own the durable workers inside the single API process."""
import fcntl
import logging
import os
from pathlib import Path
from threading import Event, Lock, Thread

from diagnostics import log_failure

logger = logging.getLogger("uvicorn.error")


class WorkerRuntime:
    def __init__(self, settings, *, jobs=None, lock_directory=None, retry_seconds=2):
        self.settings = settings
        self.stopped = Event()
        self._mutex = Lock()
        self._states = {}
        self._threads = []
        self._lock_fd = None
        self._started = False
        self._lock_directory = Path(lock_directory or Path(__file__).resolve().parent)
        self._retry_seconds = retry_seconds
        self._jobs = jobs

    def _default_jobs(self):
        from evaluation_worker import Worker as EvaluationWorker
        from learning_worker import maintain
        from schedule_worker import Worker as ScheduleWorker

        return {
            "schedule": (lambda: ScheduleWorker(self.settings), self._poll),
            "evaluation": (lambda: EvaluationWorker(self.settings), self._poll),
            "maintenance": (lambda: EvaluationWorker(self.settings), maintain),
        }

    @staticmethod
    def _poll(worker, stopped):
        while not stopped.is_set():
            if not worker.run_once():
                stopped.wait(2)

    def _state(self, name, value):
        with self._mutex:
            self._states[name] = value

    def status(self):
        with self._mutex:
            return dict(self._states)

    def start(self):
        if self._started:
            raise RuntimeError("Background workers have already been started")
        # Lock the deployment directory itself: no lock file to create or unlink.
        # The kernel releases this lock on process death, including forced exits.
        descriptor = os.open(self._lock_directory, os.O_RDONLY | os.O_DIRECTORY)
        try:
            fcntl.flock(descriptor, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            os.close(descriptor)
            raise RuntimeError("Lifefund background workers already run in this deployment; use one API process") from None
        except BaseException:
            os.close(descriptor)
            raise
        self._lock_fd = descriptor
        self._started = True
        try:
            jobs = self._default_jobs() if self._jobs is None else self._jobs
            for name, (factory, run) in jobs.items():
                self._state(name, "starting")
                thread = Thread(target=self._supervise, args=(name, factory, run),
                                name=f"lifefund-{name}", daemon=False)
                thread.start()
                self._threads.append(thread)
        except BaseException:
            self.stop()
            raise

    def _supervise(self, name, factory, run):
        try:
            while not self.stopped.is_set():
                worker = None
                try:
                    worker = factory()
                    self._state(name, "running")
                    logger.info("Lifefund background worker started: %s", name)
                    run(worker, self.stopped)
                    if not self.stopped.is_set():
                        raise RuntimeError("Background worker loop exited unexpectedly")
                except Exception as error:
                    self._state(name, "restarting")
                    log_failure(logger, "Lifefund background worker unavailable; restarting: %s", error, name)
                finally:
                    if worker is not None:
                        try:
                            worker.close()
                        except Exception as error:
                            log_failure(logger, "Lifefund background worker cleanup failed: %s", error, name)
                if not self.stopped.is_set():
                    self.stopped.wait(self._retry_seconds)
        finally:
            self._state(name, "stopped")
            logger.info("Lifefund background worker stopped: %s", name)

    def stop(self):
        self.stopped.set()
        # Finish already claimed work before releasing the singleton lock. PM2 or
        # systemd sets the hard shutdown deadline; persisted leases recover after
        # forced termination. Never release the lock while a worker is still live.
        for thread in self._threads:
            thread.join()
        if self._lock_fd is not None:
            os.close(self._lock_fd)
            self._lock_fd = None
