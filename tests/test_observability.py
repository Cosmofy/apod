import json
import logging

import pytest
from opentelemetry import trace
from opentelemetry.trace import NonRecordingSpan, SpanContext, TraceFlags

from app.observability import JsonFormatter, request_id_context


def format_log(**extra: object) -> dict[str, object]:
    record = logging.LogRecord(
        name="app.test",
        level=logging.INFO,
        pathname=__file__,
        lineno=1,
        msg="test message",
        args=(),
        exc_info=None,
    )
    for name, value in extra.items():
        setattr(record, name, value)
    return json.loads(JsonFormatter().format(record))


@pytest.mark.parametrize("trace_flags", [TraceFlags.DEFAULT, TraceFlags.SAMPLED])
def test_log_contains_active_trace_and_span_ids(trace_flags: TraceFlags) -> None:
    span = NonRecordingSpan(
        SpanContext(
            trace_id=0x1234,
            span_id=0x5678,
            is_remote=False,
            trace_flags=trace_flags,
        )
    )

    with trace.use_span(span, end_on_exit=False):
        payload = format_log()

    assert payload["trace_id"] == "00000000000000000000000000001234"
    assert payload["span_id"] == "0000000000005678"


def test_log_omits_trace_fields_without_valid_active_span() -> None:
    payload = format_log()

    assert "trace_id" not in payload
    assert "span_id" not in payload


def test_log_preserves_request_id_context_fallback() -> None:
    token = request_id_context.set("context-request-id")
    try:
        payload = format_log()
    finally:
        request_id_context.reset(token)

    assert payload["request_id"] == "context-request-id"


def test_explicit_request_id_still_takes_precedence() -> None:
    token = request_id_context.set("context-request-id")
    try:
        payload = format_log(request_id="record-request-id")
    finally:
        request_id_context.reset(token)

    assert payload["request_id"] == "record-request-id"
