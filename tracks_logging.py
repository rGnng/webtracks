"""Daily, gzip-compressed logging for WebTracks."""

import datetime
import gzip
import json
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


def _iso_time(created):
    return datetime.datetime.fromtimestamp(
        created
    ).astimezone().isoformat(timespec="seconds")


class JsonRuntimeFormatter(logging.Formatter):
    def format(self, record):
        payload = {
            "time": _iso_time(record.created),
            "level": record.levelname,
            "logger": record.name,
            "message": record.getMessage(),
        }
        if record.exc_info:
            payload["exception"] = self.formatException(record.exc_info)
        return json.dumps(
            payload, ensure_ascii=False, separators=(",", ":")
        )


def _text_value(value):
    if isinstance(value, (dict, list, tuple)):
        text = json.dumps(value, ensure_ascii=False, separators=(",", ":"))
    else:
        text = str(value)
    if not text or any(char.isspace() for char in text) or "=" in text:
        return json.dumps(text, ensure_ascii=False)
    return text


class OperationTextFormatter(logging.Formatter):
    def format(self, record):
        event = getattr(record, "event", None)
        if not isinstance(event, dict):
            return super().format(record)
        timestamp = event.get("time") or _iso_time(record.created)
        fields = " ".join(
            "%s=%s" % (key, _text_value(value))
            for key, value in event.items()
            if key != "time"
        )
        return ("%s %s %s" % (
            timestamp, record.levelname, fields
        )).rstrip()


class OperationJsonFormatter(logging.Formatter):
    def format(self, record):
        event = getattr(record, "event", None)
        if isinstance(event, dict):
            return json.dumps(
                event, ensure_ascii=False, separators=(",", ":")
            )
        return super().format(record)


def configure_logging(logs_dir, level="INFO", log_format="text"):
    """(Re)configure runtime and operation loggers under ``logs_dir``."""

    os.makedirs(logs_dir, exist_ok=True)
    level_value = getattr(logging, str(level or "INFO").upper(), logging.INFO)
    log_format = str(log_format or "text").strip().lower()
    if log_format not in ("text", "json"):
        log_format = "text"

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
    if log_format == "json":
        runtime_handler.setFormatter(JsonRuntimeFormatter())
    else:
        runtime_handler.setFormatter(logging.Formatter(
            "%(asctime)s %(levelname)s %(message)s",
            datefmt="%Y-%m-%d %H:%M:%S",
        ))
    runtime.addHandler(runtime_handler)

    operation_handler = CompressedTimedRotatingFileHandler(
        os.path.join(logs_dir, "operations.log")
    )
    operation_handler.setLevel(logging.INFO)
    if log_format == "json":
        operation_handler.setFormatter(OperationJsonFormatter())
    else:
        operation_handler.setFormatter(
            OperationTextFormatter("%(message)s")
        )
    operations.addHandler(operation_handler)

    return runtime, operations
