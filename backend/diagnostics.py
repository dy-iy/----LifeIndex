"""Log exception locations without provider payloads, credentials or locals."""
import logging
import traceback


def log_failure(logger: logging.Logger, message: str, error: BaseException, *args):
    # Exception values and source lines can contain prompts, tokens or SQL data.
    # Keep the full frame locations and exception type, without formatting either.
    frames = " -> ".join(
        f"{frame.f_code.co_filename}:{lineno}:{frame.f_code.co_name}"
        for frame, lineno in traceback.walk_tb(error.__traceback__)
    )
    logger.error(message + " error_type=%s stack=%s", *args, type(error).__name__, frames)
