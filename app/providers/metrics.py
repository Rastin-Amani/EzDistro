"""Provider call observability.

Every provider call can be recorded as a ProviderCallRecord:
provider, model, operation, latency, request size, token usage, retry count,
success/failure, error category.

Prompt/content text is NEVER part of a record — only request size is captured.
Observers decide where records go: debug logs (default), job_events (opt-in),
or tests.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from typing import Any, Protocol

from app.providers.base import LLMResult

logger = logging.getLogger("provider.metrics")

ERROR_NONE = "none"
ERROR_TRANSIENT = "transient"
ERROR_PERMANENT = "permanent"


@dataclass
class ProviderCallRecord:
    provider: str
    model: str
    operation: str
    latency_ms: int
    success: bool
    project_id: str = ""
    error_category: str = ERROR_NONE
    error_message: str = ""
    retries: int = 0
    request_chars: int = 0  # request SIZE only — never content
    prompt_tokens: int | None = None
    completion_tokens: int | None = None

    def to_dict(self) -> dict[str, Any]:
        return {
            "provider": self.provider,
            "model": self.model,
            "operation": self.operation,
            "latency_ms": self.latency_ms,
            "success": self.success,
            "error_category": self.error_category,
            "error_message": self.error_message[:300] if self.error_message else "",
            "retries": self.retries,
            "request_chars": self.request_chars,
            "prompt_tokens": self.prompt_tokens,
            "completion_tokens": self.completion_tokens,
        }


class CallObserver(Protocol):
    def on_call(self, record: ProviderCallRecord) -> None: ...


class LoggingObserver:
    """Debug-level logging. Content is never logged (size only)."""

    def __init__(self, log: logging.Logger | None = None) -> None:
        self._log = log or logger

    def on_call(self, record: ProviderCallRecord) -> None:
        if record.success:
            self._log.debug(
                "provider_call.ok",
                provider=record.provider,
                model=record.model,
                operation=record.operation,
                latency_ms=record.latency_ms,
                retries=record.retries,
                request_chars=record.request_chars,
                prompt_tokens=record.prompt_tokens,
                completion_tokens=record.completion_tokens,
            )
        else:
            self._log.warning(
                "provider_call.failed",
                provider=record.provider,
                model=record.model,
                operation=record.operation,
                latency_ms=record.latency_ms,
                error_category=record.error_category,
                error_message=record.error_message[:300],
                retries=record.retries,
            )


class EventObserver:
    """Writes records into job_events (opt-in via PROVIDER_EVENTS_ENABLED)."""

    def __init__(self, events: Any, project: str, job: str = "") -> None:
        self._events = events
        self._project = project
        self._job = job

    def on_call(self, record: ProviderCallRecord) -> None:
        self._events.add(
            project=self._project,
            job=self._job,
            event_type="provider_call",
            message=f"{record.provider}:{record.model} {record.operation}",
            metadata=record.to_dict(),
        )


class NoopObserver:
    def on_call(self, record: ProviderCallRecord) -> None:
        pass


class ChainedObserver:
    """Fan a record out to multiple observers."""

    def __init__(self, observers: list[CallObserver]) -> None:
        self._observers = observers

    def on_call(self, record: ProviderCallRecord) -> None:
        import contextlib

        for observer in self._observers:
            with contextlib.suppress(Exception):
                observer.on_call(record)


class ProjectScopedObserver:
    """Stamps the project id onto records before delegating (metrics attribution)."""

    def __init__(self, project_id: str, inner: CallObserver) -> None:
        self._project_id = project_id
        self._inner = inner

    def on_call(self, record: ProviderCallRecord) -> None:
        import dataclasses

        self._inner.on_call(dataclasses.replace(record, project_id=self._project_id))


class MetricMixin:
    """Mixin for adapters: records every observed operation.

    Subclasses set `provider_name` / `model_name` class attributes and call
    `self._observed(operation, request_chars, fn, retry_counter)` around their
    provider calls. `retry_counter` is a local `list[int]` filled by
    `with_retry(..., retry_counter=...)` — per-call retry counts stay correct
    under concurrent use.
    """

    provider_name: str = ""
    model_name: str = ""

    def __init__(self, *args: Any, **kwargs: Any) -> None:
        super().__init__(*args, **kwargs)
        self._observer: CallObserver = NoopObserver()

    def _set_observer(self, observer: CallObserver) -> None:
        self._observer = observer

    async def _observed(
        self, operation: str, request_chars: int, fn: Any, retry_counter: list[int] | None = None
    ) -> Any:
        import time

        started = time.monotonic()
        try:
            result = await fn()
            latency_ms = int((time.monotonic() - started) * 1000)
            retries = retry_counter[0] if retry_counter else 0
            self._observer.on_call(
                self._make_record(
                    operation,
                    latency_ms,
                    True,
                    retries=retries,
                    request_chars=request_chars,
                    result=result,
                )
            )
            return result
        except Exception as exc:
            latency_ms = int((time.monotonic() - started) * 1000)
            from app.providers.base import ProviderError

            category = ERROR_PERMANENT
            if isinstance(exc, ProviderError):
                category = ERROR_TRANSIENT if exc.retryable else ERROR_PERMANENT
            self._observer.on_call(
                ProviderCallRecord(
                    provider=self.provider_name,
                    model=self.model_name,
                    operation=operation,
                    latency_ms=latency_ms,
                    success=False,
                    error_category=category,
                    error_message=str(exc),
                    retries=retry_counter[0] if retry_counter else 0,
                    request_chars=request_chars,
                )
            )
            raise

    def _make_record(
        self,
        operation: str,
        latency_ms: int,
        success: bool,
        *,
        retries: int,
        request_chars: int,
        result: Any = None,
    ) -> ProviderCallRecord:
        usage: dict[str, int] = {}
        if result is not None:
            usage = getattr(result, "usage", None) or {}
        return ProviderCallRecord(
            provider=self.provider_name,
            model=self.model_name,
            operation=operation,
            latency_ms=latency_ms,
            success=success,
            retries=retries,
            request_chars=request_chars,
            prompt_tokens=usage.get("prompt_tokens") if usage else None,
            completion_tokens=usage.get("completion_tokens") if usage else None,
        )

    async def _observed_stream(
        self, fn: Any, operation: str, request_chars: int, retry_counter: list[int]
    ) -> Any:
        """Observe an async-generator provider call (streaming)."""
        import time

        started = time.monotonic()
        chunks: list[str] = []
        try:
            async for delta in fn():
                chunks.append(delta)
                yield delta
            self._observer.on_call(
                self._make_record(
                    operation,
                    int((time.monotonic() - started) * 1000),
                    True,
                    retries=len(retry_counter),
                    request_chars=request_chars,
                    result=LLMResult(
                        text="".join(chunks), provider=self.provider_name, model=self.model_name
                    ),
                )
            )
        except Exception as exc:
            from app.providers.base import ProviderError

            category = (
                ERROR_TRANSIENT
                if isinstance(exc, ProviderError) and exc.retryable
                else ERROR_PERMANENT
            )
            self._observer.on_call(
                ProviderCallRecord(
                    provider=self.provider_name,
                    model=self.model_name,
                    operation=operation,
                    latency_ms=int((time.monotonic() - started) * 1000),
                    success=False,
                    error_category=category,
                    error_message=str(exc),
                    retries=len(retry_counter),
                    request_chars=request_chars,
                )
            )
            raise
