import json
import logging
import sys
from contextvars import ContextVar
from datetime import datetime, timezone
from time import perf_counter
from uuid import uuid4

from fastapi import Request, Response
from opentelemetry import trace
from starlette.middleware.base import RequestResponseEndpoint


LOG_FIELDS = (
    "event",
    "dependency",
    "source",
    "apod_date",
    "is_today",
    "request_id",
    "http_method",
    "http_path",
    "http_status_code",
    "duration_ms",
    "client_ip",
)

request_id_context: ContextVar[str | None] = ContextVar("request_id", default=None)


class JsonFormatter(logging.Formatter):
    def format(self, record: logging.LogRecord) -> str:
        payload = {
            "timestamp": datetime.fromtimestamp(record.created, timezone.utc).isoformat(timespec="milliseconds").replace("+00:00", "Z"),
            "level": record.levelname,
            "service": "apod",
            "logger": record.name,
            "message": record.getMessage(),
        }
        for field in LOG_FIELDS:
            if hasattr(record, field):
                payload[field] = getattr(record, field)
        if "request_id" not in payload and (request_id := request_id_context.get()) is not None:
            payload["request_id"] = request_id

        span_context = trace.get_current_span().get_span_context()
        if span_context.is_valid:
            payload["trace_id"] = format(span_context.trace_id, "032x")
            payload["span_id"] = format(span_context.span_id, "016x")

        if record.exc_info:
            payload["exception"] = self.formatException(record.exc_info)
        return json.dumps(payload, default=str, separators=(",", ":"))


def configure_logging() -> None:
    app_logger = logging.getLogger("app")
    app_logger.setLevel(logging.INFO)
    app_logger.propagate = False

    if any(handler.get_name() == "cosmofy-json" for handler in app_logger.handlers):
        return

    handler = logging.StreamHandler(sys.stdout)
    handler.set_name("cosmofy-json")
    handler.setFormatter(JsonFormatter())
    app_logger.addHandler(handler)


async def log_requests(request: Request, call_next: RequestResponseEndpoint) -> Response:
    request_id = request.headers.get("x-request-id") or uuid4().hex
    request_id = request_id[:128]
    request_id_token = request_id_context.set(request_id)
    started_at = perf_counter()
    client_ip = request.client.host if request.client else None

    try:
        try:
            response = await call_next(request)
        except Exception:
            logger.exception(
                msg="http request failed",
                extra={
                    "event": "http.request.failed",
                    "request_id": request_id,
                    "http_method": request.method,
                    "http_path": request.url.path,
                    "http_status_code": 500,
                    "duration_ms": round((perf_counter() - started_at) * 1000, 3),
                    "client_ip": client_ip,
                },
            )
            raise

        response.headers["x-request-id"] = request_id
        logger.info(
            msg="http request completed",
            extra={
                "event": "http.request.completed",
                "request_id": request_id,
                "http_method": request.method,
                "http_path": request.url.path,
                "http_status_code": response.status_code,
                "duration_ms": round((perf_counter() - started_at) * 1000, 3),
                "client_ip": client_ip,
            },
        )
        return response
    finally:
        request_id_context.reset(request_id_token)


logger = logging.getLogger(__name__)
