"""Versioned JSON activity records for stdout and daily rotating files."""

import functools
import inspect
import json
import logging
import os
import sys
import uuid
from contextvars import ContextVar
from datetime import datetime, timezone
from logging.handlers import TimedRotatingFileHandler
from pathlib import Path


context = ContextVar("activity_context", default=None)
logger = logging.getLogger("brodyaga.activity")
logger.setLevel(logging.INFO)
logger.propagate = False
if not logger.handlers:
    handler = logging.StreamHandler(sys.stdout)
    handler.setFormatter(logging.Formatter("%(message)s"))
    logger.addHandler(handler)


def configure_activity_file(service, *, target_logger=None, category="activity"):
    """One writer per file; retain daily archives across container replacement."""
    if service not in {"bot", "api"}:
        raise ValueError("Unknown logging service")
    if category not in {"activity", "technical"}:
        raise ValueError("Unknown logging category")
    target_logger = logger if target_logger is None else target_logger
    directory = Path(os.getenv("BRODYAGA_LOG_DIR", str(Path(__file__).resolve().parent / "logs")))
    directory.mkdir(parents=True, exist_ok=True)
    path = (directory / f"{service}-{category}.jsonl").resolve()
    for handler in target_logger.handlers:
        if isinstance(handler, TimedRotatingFileHandler) and handler.baseFilename == str(path):
            return handler
    handler = TimedRotatingFileHandler(
        path, when="midnight", interval=1, backupCount=0,
        encoding="utf-8", utc=True,
    )
    handler.setFormatter(logging.Formatter("%(message)s"))
    target_logger.addHandler(handler)
    return handler


def identifier(value):
    return str(value) if value is not None else None


def discord_context(subject):
    """Only metadata; never include message text or the full interaction payload."""
    user = getattr(subject, "user", None) or getattr(subject, "author", None)
    guild = getattr(subject, "guild", None)
    guild_id = getattr(subject, "guild_id", None) or getattr(guild, "id", None)
    channel = getattr(subject, "channel", None)
    return {
        "source": "guild" if guild_id is not None else "dm",
        "request_id": identifier(getattr(subject, "id", None)),
        "user_id": identifier(getattr(user, "id", None)),
        "username": str(user) if user is not None else None,
        "guild_id": identifier(guild_id),
        "guild_name": getattr(guild, "name", None),
        "channel_id": identifier(getattr(subject, "channel_id", None) or getattr(channel, "id", None)),
        "channel_name": getattr(channel, "name", None),
    }


def bind_discord(subject):
    return context.set(discord_context(subject))


def emit(event, *, details=None, **fields):
    record = {
        **(context.get() or {"source": "internal"}),
        **(details or {}), **fields,
        "schema_version": 1,
        "event_id": uuid.uuid4().hex,
        "timestamp": datetime.now(timezone.utc).isoformat(),
        "event": event,
    }
    logger.info(json.dumps(record, ensure_ascii=False, separators=(",", ":")))


def component_metadata(interaction):
    """Resolve human-readable controls, including nested Components V2 layouts."""
    data = interaction.data or {}
    custom_id = data.get("custom_id")
    result = {"custom_id": custom_id, "component_type": data.get("component_type")}
    values = data.get("values", [])
    if values:
        result["selected_values"] = values

    def walk(components):
        for component in components:
            if component.get("custom_id") == custom_id and custom_id is not None:
                result["action"] = component.get("label") or component.get("placeholder") or custom_id
                options = {item["value"]: item.get("label") for item in component.get("options", [])}
                if values:
                    result["selected_labels"] = [options.get(value, value) for value in values]
                return True
            children = component.get("components", [])
            accessory = component.get("accessory")
            if accessory:
                children = [*children, accessory]
            if walk(children):
                return True
        return False

    message = getattr(interaction, "message", None)
    walk([item.to_dict() for item in getattr(message, "components", [])])
    result.setdefault("action", custom_id or "unknown_component")
    return result


def audit_write(action, actor, result_field=None):
    """Record results only after the transaction has committed; re-raise errors."""
    def decorate(callback):
        signature = inspect.signature(callback)

        @functools.wraps(callback)
        def wrapped(*args, **kwargs):
            arguments = signature.bind(*args, **kwargs).arguments
            fields = {"action": action, "user_id": identifier(arguments[actor])}
            for argument, field in (("server", "game_server"), ("location_name", "location_name")):
                if argument in arguments:
                    fields[field] = arguments[argument]
            if "servers" in arguments:
                # Preserve iterables for the callback and record a deterministic list.
                arguments["servers"] = sorted(set(arguments["servers"]))
                fields["game_servers"] = arguments["servers"]
            try:
                result = callback(**arguments)
            except Exception as error:
                emit("action_result", **fields, outcome="error", error_type=type(error).__name__)
                raise
            if result_field:
                fields[result_field] = result
            emit("action_result", **fields, outcome="success")
            return result
        return wrapped
    return decorate
