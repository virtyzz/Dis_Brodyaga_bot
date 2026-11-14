"""Safe HTTP timings: never serialize URLs, headers, payloads or exception text."""

import asyncio
import atexit
import functools
import json
import logging
import math
import platform
import queue
import sys
import time
import uuid
from collections import deque
from contextvars import ContextVar
from datetime import datetime, timezone
from logging.handlers import QueueHandler, QueueListener

import aiohttp
import discord
from activity_logging import bind_discord, context, configure_activity_file


ack_context = ContextVar("ack_diagnostics", default=None)
logger = logging.getLogger("brodyaga.technical")
logger.setLevel(logging.INFO)
logger.propagate = False
if not logger.handlers:
    handler = logging.StreamHandler(sys.stdout)
    handler.setFormatter(logging.Formatter("%(message)s"))
    logger.addHandler(handler)

_lag_samples = deque(maxlen=60)
_listener = None
_output_handlers = []


def configure_file():
    global _listener, _output_handlers
    if _listener is not None:
        return
    configure_activity_file("bot", target_logger=logger, category="technical")
    _output_handlers = logger.handlers[:]
    pending = queue.SimpleQueue()
    logger.handlers = [QueueHandler(pending)]
    _listener = QueueListener(pending, *_output_handlers, respect_handler_level=True)
    _listener.start()
    # A separate writer keeps stdout/file I/O out of acknowledgement timing.
    emit("diagnostics_enabled", python_version=platform.python_version(),
         discord_version=discord.__version__, aiohttp_version=aiohttp.__version__)


def shutdown():
    global _listener, _output_handlers
    if _listener is not None:
        _listener.stop()
        _listener = None
        logger.handlers = [handler for handler in _output_handlers if not hasattr(handler, "baseFilename")]
        for handler in _output_handlers:
            if hasattr(handler, "baseFilename"):
                handler.close()
        _output_handlers = []


atexit.register(shutdown)


def emit(event, **fields):
    logger.info(json.dumps({**(context.get() or {}), **fields, "event": event,
                           "technical_schema_version": 1,
                           "timestamp": datetime.now(timezone.utc).isoformat()},
                          ensure_ascii=False, allow_nan=False, separators=(",", ":")))


def milliseconds(seconds):
    return round(seconds * 1000, 3) if isinstance(seconds, (float, int)) and math.isfinite(seconds) else None


def loop_metrics():
    recent = [value for stamp, value in _lag_samples if time.monotonic() - stamp <= 10]
    return {"loop_lag_max_10s_ms": max(recent) if recent else None}


def error_metadata(error):
    result = {"error_type": type(error).__name__}
    for attribute, field in (("code", "discord_error_code"), ("status", "http_status")):
        value = getattr(error, attribute, None)
        if isinstance(value, int):
            result[field] = value
    return result


def mark_ack_error(error):
    state = ack_context.get()
    if state is not None:
        state.update(error_metadata(error))


def interaction_age_ms(interaction):
    created = getattr(interaction, "created_at", None)
    if isinstance(created, datetime) and created.tzinfo is not None:
        return milliseconds((datetime.now(timezone.utc) - created).total_seconds())
    return None


def trace_acknowledgement(callback):
    @functools.wraps(callback)
    async def traced(interaction, **kwargs):
        bind_discord(interaction)
        if interaction.response.is_done():
            emit("ack_already_done", age_ms=interaction_age_ms(interaction))
            return await callback(interaction, **kwargs)
        state = {"started": time.monotonic(), "attempts": 0}
        token = ack_context.set(state)
        outcome = "error"
        try:
            emit("ack_start", age_ms=interaction_age_ms(interaction),
                 component=(getattr(interaction, "data", None) or {}).get("custom_id"),
                 message_id=str(interaction.message.id) if getattr(interaction, "message", None) is not None else None,
                 gateway_latency_ms=milliseconds(getattr(getattr(interaction, "client", None), "latency", None)),
                 ephemeral=kwargs.get("ephemeral", False), thinking=kwargs.get("thinking", False),
                 **loop_metrics())
            result = await callback(interaction, **kwargs)
            outcome = "acknowledged" if result else "rejected"
            return result
        except asyncio.CancelledError:
            outcome = "cancelled"
            raise
        except Exception as error:
            mark_ack_error(error)
            raise
        finally:
            try:
                emit("ack_result", outcome=outcome, age_ms=interaction_age_ms(interaction),
                     duration_ms=milliseconds(time.monotonic() - state["started"]),
                     http_attempts=state["attempts"], **loop_metrics(),
                     **{key: state[key] for key in ("error_type", "discord_error_code", "http_status") if key in state})
            finally:
                ack_context.reset(token)
    return traced


async def watch_event_loop():
    _lag_samples.clear()
    while True:
        started = time.monotonic()
        await asyncio.sleep(1)
        lag = milliseconds(max(0, time.monotonic() - started - 1))
        _lag_samples.append((time.monotonic(), lag))
        if lag >= 250:
            # Background task must not inherit a user's identity.
            token = context.set({"source": "internal"})
            try:
                emit("event_loop_lag", lag_ms=lag)
            finally:
                context.reset(token)


def operation(url):
    path = url.path
    if "/interactions/" in path and path.endswith("/callback"):
        return "interaction_callback"
    if "/webhooks/" in path:
        return "webhook_api"
    if "/channels/" in path:
        return "channel_api"
    return "discord_api"


def create_http_trace():
    trace = aiohttp.TraceConfig()
    last_response_at = None

    async def start(session, ctx, params):
        ctx.started = time.monotonic()
        ctx.operation = operation(params.url)
        ctx.http_id = uuid.uuid4().hex
        ctx.ack = ack_context.get()
        ctx.timings = {}
        ctx.marks = {}
        ctx.reused = False
        ctx.dns_cache = None
        ctx.idle_ms = milliseconds(ctx.started - last_response_at) if last_response_at is not None else None
        ctx.attempt = None
        if ctx.ack is not None:
            ctx.ack["attempts"] += 1
            ctx.attempt = ctx.ack["attempts"]
            emit("discord_http_start", operation=ctx.operation, http_id=ctx.http_id,
                 attempt=ctx.attempt, previous_response_ago_ms=ctx.idle_ms,
                 since_previous_http_headers_ms=milliseconds(ctx.started - ctx.ack["last_headers_at"]) if "last_headers_at" in ctx.ack else None,
                 since_ack_start_ms=milliseconds(ctx.started - ctx.ack["started"]))

    def stage(name, kind):
        async def callback(session, ctx, params):
            now = time.monotonic()
            if kind == "start":
                ctx.marks[name] = now
            elif name in ctx.marks:
                ctx.timings[name + "_ms"] = milliseconds(now - ctx.marks.pop(name))
            if ctx.ack is not None:
                emit("discord_http_stage", http_id=ctx.http_id, attempt=ctx.attempt,
                     stage=name + "_" + kind, elapsed_ms=milliseconds(now - ctx.started))
        return callback

    async def reuse(session, ctx, params):
        ctx.reused = True
        if ctx.ack is not None:
            emit("discord_http_stage", http_id=ctx.http_id, attempt=ctx.attempt,
                 stage="connection_reused", elapsed_ms=milliseconds(time.monotonic() - ctx.started))

    def dns_cache(hit):
        async def callback(session, ctx, params):
            ctx.dns_cache = "hit" if hit else "miss"
            if ctx.ack is not None:
                emit("discord_http_stage", http_id=ctx.http_id, attempt=ctx.attempt,
                     stage="dns_cache_" + ctx.dns_cache,
                     elapsed_ms=milliseconds(time.monotonic() - ctx.started))
        return callback

    async def headers_sent(session, ctx, params):
        ctx.sent_at = time.monotonic()
        if ctx.ack is not None:
            emit("discord_http_stage", http_id=ctx.http_id, attempt=ctx.attempt,
                 stage="headers_sent", elapsed_ms=milliseconds(ctx.sent_at - ctx.started))

    async def finish(session, ctx, params):
        nonlocal last_response_at
        now = time.monotonic()
        last_response_at = now
        if ctx.ack is not None:
            ctx.ack["last_headers_at"] = now
        fields = {"http_id": ctx.http_id, "operation": ctx.operation, "attempt": ctx.attempt,
                  "headers_total_ms": milliseconds(now - ctx.started),
                  "connection_reused": ctx.reused, "previous_response_ago_ms": ctx.idle_ms,
                  "dns_cache": ctx.dns_cache, "unfinished_phases": sorted(ctx.marks),
                  **ctx.timings}
        if hasattr(ctx, "sent_at"):
            fields["response_wait_ms"] = milliseconds(now - ctx.sent_at)
        response = getattr(params, "response", None)
        if response is not None:
            fields["status"] = response.status
            # Numeric retry hints only; never log the complete response headers.
            for name, field in (("Retry-After", "retry_after_ms"),
                                ("X-RateLimit-Reset-After", "rate_reset_after_ms")):
                try:
                    fields[field] = milliseconds(float(response.headers[name]))
                except (KeyError, TypeError, ValueError):
                    pass
        error = getattr(params, "exception", None)
        if error is not None:
            fields["error_type"] = type(error).__name__
            errno = getattr(error, "errno", None)
            if isinstance(errno, int):
                fields["errno"] = errno
        if ctx.ack is not None or error is not None or (response is not None and response.status >= 400) or now - ctx.started >= 0.5:
            emit("discord_http_result", **fields, **loop_metrics())

    async def body_read(session, ctx, params):
        if ctx.ack is not None:
            emit("discord_http_stage", http_id=ctx.http_id, attempt=ctx.attempt,
                 stage="response_body_read", elapsed_ms=milliseconds(time.monotonic() - ctx.started))

    trace.on_request_start.append(start)
    trace.on_request_end.append(finish)
    trace.on_request_exception.append(finish)
    trace.on_connection_reuseconn.append(reuse)
    trace.on_dns_cache_hit.append(dns_cache(True))
    trace.on_dns_cache_miss.append(dns_cache(False))
    trace.on_request_headers_sent.append(headers_sent)
    trace.on_response_chunk_received.append(body_read)
    for name, start_signal, end_signal in (
        ("connection_queue", trace.on_connection_queued_start, trace.on_connection_queued_end),
        ("connection_create", trace.on_connection_create_start, trace.on_connection_create_end),
        ("dns", trace.on_dns_resolvehost_start, trace.on_dns_resolvehost_end),
    ):
        start_signal.append(stage(name, "start"))
        end_signal.append(stage(name, "end"))
    return trace
