from __future__ import annotations

import hashlib
import logging
from logging.handlers import RotatingFileHandler

from app.config import AppConfig

RESET = "\033[0m"
ACCOUNT_PALETTE = (
    "38;5;39",
    "38;5;45",
    "38;5;51",
    "38;5;75",
    "38;5;81",
    "38;5;87",
    "38;5;99",
    "38;5;111",
    "38;5;117",
    "38;5;141",
    "38;5;147",
    "38;5;153",
    "38;5;177",
    "38;5;183",
    "38;5;189",
    "38;5;203",
    "38;5;209",
    "38;5;215",
    "38;5;221",
    "38;5;222",
    "38;5;186",
    "38;5;191",
    "38;5;121",
    "38;5;114",
    "38;5;78",
    "38;5;49",
)
LEVEL_COLORS = {
    logging.DEBUG: "38;5;244",
    logging.INFO: "38;5;75",
    logging.WARNING: "38;5;221",
    logging.ERROR: "38;5;203",
    logging.CRITICAL: "38;5;197",
}
SUCCESS_COLOR = "38;5;120"
SUCCESS_HINTS = (
    "passed",
    "ready",
    "saved",
    "finished",
    "complete",
    "completed",
    "created",
    "collected",
    "exported",
    "posted",
    "successful",
)
FAILURE_HINTS = (
    "failed",
    "error",
    "could not",
    "unable",
    "warning",
    "rejected",
)


class ContextFilter(logging.Filter):
    def filter(self, record: logging.LogRecord) -> bool:
        if not hasattr(record, "account_name"):
            record.account_name = "SYSTEM"
        return True


class PrettyConsoleFormatter(logging.Formatter):
    def format(self, record: logging.LogRecord) -> str:
        timestamp = self.formatTime(record, self.datefmt)
        message = record.getMessage()
        is_success = self._is_success_message(record.levelno, message)
        level_label = "SUCCESS" if is_success else record.levelname
        level_color = SUCCESS_COLOR if is_success else LEVEL_COLORS.get(record.levelno, "38;5;252")
        level = self._colorize(level_color, f"{level_label:<8}")
        account_name = str(getattr(record, "account_name", "SYSTEM"))
        account = self._colorize(self._account_color(account_name), f"{account_name:<14}")
        logger_name = self._colorize("38;5;245", f"{record.name:<22}")

        if record.exc_info:
            message = f"{message}\n{self.formatException(record.exc_info)}"
        elif record.stack_info:
            message = f"{message}\n{self.formatStack(record.stack_info)}"

        return f"{timestamp} | {level} | {account} | {logger_name} | {message}"

    @staticmethod
    def _colorize(code: str, value: str) -> str:
        return f"\033[{code}m{value}{RESET}"

    @staticmethod
    def _account_color(account_name: str) -> str:
        if account_name == "SYSTEM":
            return "38;5;250"

        digest = hashlib.sha256(account_name.encode("utf-8")).digest()
        index = digest[0] % len(ACCOUNT_PALETTE)
        return ACCOUNT_PALETTE[index]

    @staticmethod
    def _is_success_message(levelno: int, message: str) -> bool:
        if levelno != logging.INFO:
            return False
        normalized = str(message or "").strip().lower()
        if not normalized:
            return False
        if any(hint in normalized for hint in FAILURE_HINTS):
            return False
        return any(hint in normalized for hint in SUCCESS_HINTS)


class AccountLoggerAdapter(logging.LoggerAdapter):
    def process(self, msg: object, kwargs: dict) -> tuple[object, dict]:
        extra = kwargs.setdefault("extra", {})
        extra.setdefault("account_name", self.extra["account_name"])
        return msg, kwargs


def configure_logging(config: AppConfig) -> logging.Logger:
    config.logs_dir.mkdir(parents=True, exist_ok=True)

    logger = logging.getLogger("parser_tiktok")
    logger.setLevel(logging.INFO)
    logger.handlers.clear()

    context_filter = ContextFilter()

    console_handler = logging.StreamHandler()
    console_handler.addFilter(context_filter)
    console_handler.setFormatter(
        PrettyConsoleFormatter(datefmt="%Y-%m-%d %H:%M:%S")
    )

    file_handler = RotatingFileHandler(
        config.logs_dir / "app.log",
        maxBytes=1_000_000,
        backupCount=3,
        encoding="utf-8",
    )
    file_handler.addFilter(context_filter)
    file_handler.setFormatter(
        logging.Formatter(
            fmt="%(asctime)s | %(levelname)-8s | %(account_name)-14s | %(name)s | %(message)s",
            datefmt="%Y-%m-%d %H:%M:%S",
        )
    )

    logger.addHandler(console_handler)
    logger.addHandler(file_handler)
    logger.propagate = False
    return logger


def get_account_logger(logger: logging.Logger, account_name: str) -> logging.LoggerAdapter:
    return AccountLoggerAdapter(logger, {"account_name": account_name})
