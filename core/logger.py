import logging
import os
import re
import sys
import tempfile

REDACTED = "[REDACTED]"
LOG_FORMAT = "%(asctime)s | %(levelname)s | %(message)s"
DEFAULT_LOG_FILE = "logs/primebot.log"
LOG_FILE_ENV = "PRIMEBOT_LOG_FILE"
TELEGRAM_URL_TOKEN_RE = re.compile(r"(/bot)([^/\s]+)(/)", re.IGNORECASE)
BOT_TOKEN_SHAPE_RE = re.compile(r"\b\d{5,}:[A-Za-z0-9_-]{5,}\b")
DEPENDENCY_LOGGERS = (
    "httpx",
    "httpcore",
    "telegram",
    "telegram.ext",
    "telegram.request",
    "telegram._bot",
    "telegram._request",
    "telegram.ext._application",
    "telegram.ext._updater",
)


def _configured_tokens():
    token = os.getenv("BOT_TOKEN")

    if token:
        return (token,)

    return ()


def redact_text(value):
    if value is None:
        return None

    text = str(value)
    text = TELEGRAM_URL_TOKEN_RE.sub(r"\1" + REDACTED + r"\3", text)

    for token in _configured_tokens():
        text = text.replace(token, REDACTED)

    return BOT_TOKEN_SHAPE_RE.sub(REDACTED, text)


class SecretRedactionFilter(logging.Filter):

    def filter(self, record):
        try:
            record.msg = redact_text(record.getMessage())
            record.args = ()
        except Exception:
            record.msg = redact_text(record.msg)

        if record.exc_text:
            record.exc_text = redact_text(record.exc_text)

        return True


class RedactingFormatter(logging.Formatter):

    def format(self, record):
        return redact_text(super().format(record))


redaction_filter = SecretRedactionFilter()


def _running_under_unittest():
    return "unittest" in sys.modules and any(
        arg == "discover" or "unittest" in os.path.basename(arg).lower()
        for arg in sys.argv
    )


def get_log_file_path():
    configured = os.getenv(LOG_FILE_ENV)

    if configured:
        return configured

    if _running_under_unittest():
        return os.path.join(
            tempfile.gettempdir(),
            "primebot-test-logs",
            f"primebot-{os.getpid()}.log",
        )

    return DEFAULT_LOG_FILE


def create_primebot_file_handler(log_path=None):
    path = log_path or get_log_file_path()
    directory = os.path.dirname(path)

    if directory:
        os.makedirs(directory, exist_ok=True)

    return logging.FileHandler(
        path,
        encoding="utf-8",
        errors="backslashreplace",
    )


def configure_dependency_loggers():
    for name in DEPENDENCY_LOGGERS:
        logging.getLogger(name).setLevel(logging.WARNING)


def _install_redaction(handler):
    if not any(
        isinstance(existing, SecretRedactionFilter)
        for existing in handler.filters
    ):
        handler.addFilter(redaction_filter)

    if not isinstance(handler.formatter, RedactingFormatter):
        current = handler.formatter
        fmt = getattr(current, "_fmt", LOG_FORMAT)
        datefmt = getattr(current, "datefmt", None)
        handler.setFormatter(RedactingFormatter(fmt=fmt, datefmt=datefmt))


def configure_logging():
    configure_dependency_loggers()

    root_logger = logging.getLogger()
    root_logger.setLevel(logging.INFO)

    if not any(
        getattr(handler, "_primebot_managed", False)
        for handler in root_logger.handlers
    ):
        handlers = [
            create_primebot_file_handler(),
            logging.StreamHandler(),
        ]

        for handler in handlers:
            handler._primebot_managed = True
            handler.setFormatter(RedactingFormatter(LOG_FORMAT))
            _install_redaction(handler)
            root_logger.addHandler(handler)

    for handler in root_logger.handlers:
        _install_redaction(handler)

    primebot_logger = logging.getLogger("PrimeBot2")
    primebot_logger.addFilter(redaction_filter)

    for handler in primebot_logger.handlers:
        _install_redaction(handler)

    return primebot_logger


logger = configure_logging()
