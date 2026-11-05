import json
import logging
from contextlib import contextmanager
from contextvars import ContextVar
from uuid import uuid4

_trace: ContextVar[tuple[str | None, str | None]] = ContextVar(
    "multisearch_trace", default=(None, None)
)
_logger = logging.getLogger("uvicorn.error.multisearch.telemetry")


def trace_id() -> str | None:
    return _trace.get()[0]


def redact_http_client_logging() -> None:
    for name in ("httpx", "httpx2", "httpcore"):
        logging.getLogger(name).setLevel(logging.WARNING)


@contextmanager
def bind_trace(identifier: str | None, request_id: str | None = None):
    previous = _trace.get()
    reset = _trace.set((identifier or previous[0] or uuid4().hex, request_id or previous[1]))
    try:
        yield
    finally:
        _trace.reset(reset)


def trace_event(
    stage: str,
    *,
    status: str,
    elapsed_ms: int | None = None,
    provider: str | None = None,
) -> None:
    identifier = trace_id()
    if identifier is None or not _logger.isEnabledFor(logging.INFO):
        return
    fields = {"event": stage, "trace_id": identifier, "status": status}
    if elapsed_ms is not None:
        fields["elapsed_ms"] = elapsed_ms
    if provider is not None:
        fields["provider"] = provider
    _logger.info(json.dumps(fields, separators=(",", ":")))
