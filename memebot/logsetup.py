"""Logging with secret redaction.

httpx logs every request URL at INFO, and Telegram bot URLs contain the bot token
(https://api.telegram.org/bot<TOKEN>/...). We silence httpx/httpcore INFO logs and also run every
record through a filter that masks bot tokens and any configured secret value.
"""
from __future__ import annotations

import logging
import logging.handlers
import re
from pathlib import Path

_TOKEN_RE = re.compile(r"\b\d{6,12}:[A-Za-z0-9_-]{30,}\b")
_BOT_URL_RE = re.compile(r"(api\.telegram\.org/(?:file/)?bot)[^/\s]+")
_API_KEY_RE = re.compile(r"(?i)(x-api-key|api[_-]?key|authorization)(['\"]?\s*[:=]\s*['\"]?)([^\s'\",}]+)")


class RedactFilter(logging.Filter):
    def __init__(self, secrets: list[str] | None = None):
        super().__init__()
        self.secrets = sorted({s for s in (secrets or []) if s and len(s) >= 6}, key=len, reverse=True)

    def redact(self, text: str) -> str:
        for s in self.secrets:
            text = text.replace(s, "***")
        text = _BOT_URL_RE.sub(r"\1***", text)
        text = _TOKEN_RE.sub("***", text)
        text = _API_KEY_RE.sub(lambda m: m.group(1) + m.group(2) + "***", text)
        return text

    def filter(self, record: logging.LogRecord) -> bool:
        try:
            msg = record.getMessage()
        except Exception:
            msg = str(record.msg)
        red = self.redact(msg)
        if record.exc_info:
            # Render the traceback now so it is redacted too.
            fmt = logging.Formatter()
            red = red + "\n" + self.redact(fmt.formatException(record.exc_info))
            record.exc_info = None
            record.exc_text = None
        record.msg = red
        record.args = None
        return True


def setup_logging(data_dir: Path | None, secrets: list[str], level: int = logging.INFO) -> RedactFilter:
    flt = RedactFilter(secrets)
    root = logging.getLogger()
    root.setLevel(level)
    for h in list(root.handlers):
        root.removeHandler(h)
    fmt = logging.Formatter("%(asctime)s %(levelname)s %(name)s: %(message)s")
    sh = logging.StreamHandler()
    sh.setFormatter(fmt)
    sh.addFilter(flt)
    root.addHandler(sh)
    if data_dir is not None:
        fh = logging.handlers.RotatingFileHandler(Path(data_dir) / "memebot.log", maxBytes=5_000_000,
                                                  backupCount=5, encoding="utf-8")
        fh.setFormatter(fmt)
        fh.addFilter(flt)
        root.addHandler(fh)
    for noisy in ("httpx", "httpcore", "telegram.ext.Updater", "apscheduler", "uvicorn.access"):
        logging.getLogger(noisy).setLevel(logging.WARNING)
    return flt
