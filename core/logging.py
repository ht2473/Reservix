"""Форматирование журналов."""

from __future__ import annotations

import datetime as dt
import json
import logging
import traceback
from typing import Any

#: Штатные атрибуты записи журнала. Всё, что не входит в перечень, считается
#: дополнительным контекстом и переносится в результат как есть.
_RESERVED_ATTRS = frozenset(
    {
        "args",
        "asctime",
        "created",
        "exc_info",
        "exc_text",
        "filename",
        "funcName",
        "levelname",
        "levelno",
        "lineno",
        "module",
        "msecs",
        "message",
        "msg",
        "name",
        "pathname",
        "process",
        "processName",
        "relativeCreated",
        "stack_info",
        "taskName",
        "thread",
        "threadName",
    }
)


class JsonFormatter(logging.Formatter):
    """Преобразует запись журнала в одну строку JSON."""

    def format(self, record: logging.LogRecord) -> str:
        payload: dict[str, Any] = {
            "timestamp": dt.datetime.fromtimestamp(record.created, tz=dt.UTC).isoformat(),
            "level": record.levelname,
            "logger": record.name,
            "message": record.getMessage(),
        }

        if record.exc_info:
            exc_type, exc_value, exc_tb = record.exc_info
            payload["exception"] = {
                "type": exc_type.__name__ if exc_type else "",
                "message": str(exc_value),
                "traceback": "".join(traceback.format_exception(exc_type, exc_value, exc_tb))[
                    -4000:
                ],
            }

        if record.stack_info:
            payload["stack"] = self.formatStack(record.stack_info)

        # Место возникновения записи полезно только для предупреждений и ошибок:
        # для информационных сообщений оно лишь увеличивает объём журнала.
        if record.levelno >= logging.WARNING:
            payload["source"] = f"{record.pathname}:{record.lineno}"
            payload["function"] = record.funcName

        for key, value in record.__dict__.items():
            if key in _RESERVED_ATTRS or key.startswith("_"):
                continue
            payload[key] = self._coerce(value)

        return json.dumps(payload, ensure_ascii=False, default=str)

    @staticmethod
    def _coerce(value: Any) -> Any:
        """Приводит значение к типу, сериализуемому в JSON."""
        if isinstance(value, str | int | float | bool | type(None)):
            return value
        if isinstance(value, dt.datetime | dt.date):
            return value.isoformat()
        if isinstance(value, list | tuple | set):
            return [JsonFormatter._coerce(item) for item in value]
        if isinstance(value, dict):
            return {str(k): JsonFormatter._coerce(v) for k, v in value.items()}
        return str(value)


def get_logger(name: str) -> logging.Logger:
    """Возвращает журнал приложения в общем пространстве имён."""
    return logging.getLogger(f"reservix.{name}" if not name.startswith("reservix") else name)
