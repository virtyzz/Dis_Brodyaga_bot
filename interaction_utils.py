"""Acknowledge Discord clicks before changing persistent state."""

import logging
import time
from datetime import datetime

import discord

logger = logging.getLogger(__name__)


async def acknowledge(interaction, *, ephemeral=False, thinking=False) -> bool:
    if interaction.response.is_done():
        return True
    started = time.monotonic()
    try:
        await interaction.response.defer(ephemeral=ephemeral, thinking=thinking)
    except discord.HTTPException as error:
        if error.code not in (10062, 40060):
            raise
        created_at = getattr(interaction, "created_at", None)
        age = ((discord.utils.utcnow() - created_at).total_seconds()
               if isinstance(created_at, datetime) else None)
        logger.warning(
            "Discord rejected interaction acknowledgement: code=%s id=%s "
            "component=%s age_s=%s ack_wait_s=%.3f gateway_latency_s=%s; "
            "action cancelled, user must click again",
            error.code, getattr(interaction, "id", None),
            (interaction.data or {}).get("custom_id"), age,
            time.monotonic() - started,
            getattr(getattr(interaction, "client", None), "latency", None),
        )
        return False
    except discord.InteractionResponded:
        # Another handler won a simultaneous initial response. Do not replay
        # an action whose result may already be in progress elsewhere.
        logger.warning("Concurrent interaction response; duplicate action cancelled")
        return False
    return True
