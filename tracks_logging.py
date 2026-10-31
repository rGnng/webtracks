"""Daily, gzip-compressed logging for WebTracks."""

import gzip
import logging
import os
import shutil
from logging.handlers import TimedRotatingFileHandler


def _compress_archive(path):
    if not os.path.isfile(path) or path.endswith(".gz"):
        return
    target = path + ".gz"
    try:
        if os.path.exists(target):
            os.remove(path)
            return
        with open(path, "rb") as source, gzip.open(target, "wb") as dest:
            shutil.copyfileobj(source, dest)
        os.remove(path)
    except OSError:
        try:
            if os.path.exists(target):
                os.remove(target)
        except OSError:
            pass


class CompressedTimedRotatingFileHandler(TimedRotatingFileHandler):
    """Rotate at midnight and gzip the previous day's file."""

    def __init__(self, filename, backup_count=30):
        super().__init__(
            filename,
            when="midnight",
            interval=1,
            backupCount=backup_count,
            encoding="utf-8",
            delay=True,
            utc=False,
        )
        self._compress_archives()

    def _compress_archives(self):
        directory = os.path.dirname(self.baseFilename) or "."
        prefix = os.path.basename(self.baseFilename) + "."
        try:
            names = os.listdir(directory)
        except OSError:
            return
        for name in names:
            if name.startswith(prefix) and not name.endswith(".gz"):
                _compress_archive(os.path.join(directory, name))

    def doRollover(self):
        super().doRollover()
        self._compress_archives()


def configure_logging(logs_dir, level="INFO"):
    """(Re)configure runtime and operation loggers under ``logs_dir``."""

    os.makedirs(logs_dir, exist_ok=True)
    level_value = getattr(logging, str(level or "INFO").upper(), logging.INFO)

    runtime = logging.getLogger("webtracks.runtime")
    runtime.setLevel(level_value)
    runtime.propagate = False
    for handler in list(runtime.handlers):
        runtime.removeHandler(handler)
        handler.close()

    operations = logging.getLogger("webtracks.operations")
    operations.setLevel(logging.INFO)
    operations.propagate = False
    for handler in list(operations.handlers):
        operations.removeHandler(handler)
        handler.close()

    runtime_handler = CompressedTimedRotatingFileHandler(
        os.path.join(logs_dir, "runtime.log")
    )
    runtime_handler.setLevel(level_value)
    runtime_handler.setFormatter(logging.Formatter(
        "%(asctime)s %(levelname)s %(message)s",
        datefmt="%Y-%m-%d %H:%M:%S",
    ))
    runtime.addHandler(runtime_handler)

    operation_handler = CompressedTimedRotatingFileHandler(
        os.path.join(logs_dir, "operations.log")
    )
    operation_handler.setLevel(logging.INFO)
    operation_handler.setFormatter(logging.Formatter("%(message)s"))
    operations.addHandler(operation_handler)

    return runtime, operations
