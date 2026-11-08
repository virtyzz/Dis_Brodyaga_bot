import os
import unittest
from datetime import timedelta
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock, patch

import discord
import interaction_utils

os.environ.setdefault("DISCORD_BOT_TOKEN", "local-tests-only")
import main
import subscriptions
from coords_handler import SERVERS, get_all_locations


def expired_click(code=10062):
    error = discord.NotFound(SimpleNamespace(status=404, reason="Not Found"),
                             {"code": code, "message": "Unknown interaction"})
    return SimpleNamespace(
        id=1234, created_at=discord.utils.utcnow() - timedelta(seconds=4),
        data={"custom_id": "confirmation"}, client=SimpleNamespace(latency=.1),
        user=SimpleNamespace(id=1, send=AsyncMock()),
        response=SimpleNamespace(is_done=Mock(return_value=False), defer=AsyncMock(side_effect=error)),
        edit_original_response=AsyncMock(), followup=SimpleNamespace(send=AsyncMock()),
    )


class AcknowledgementTests(unittest.IsolatedAsyncioTestCase):
    async def test_expired_acknowledgement_logs_diagnostics_without_retry(self):
        event = expired_click()
        with self.assertLogs(interaction_utils.logger, level="WARNING") as logs:
            self.assertFalse(await interaction_utils.acknowledge(event))
        text = " ".join(logs.output)
        self.assertIn("code=10062", text)
        self.assertIn("age_s=", text)
        self.assertIn("ack_wait_s=", text)
        event.response.defer.assert_awaited_once()

    async def test_expired_confirmation_does_not_write_report(self):
        event = expired_click()
        view = main.TraderConfirmationView(1, SERVERS[0], get_all_locations()[0], 0)
        with patch.object(main, "add_trader_report") as write:
            with self.assertLogs(interaction_utils.logger, level="WARNING"):
                await view._confirm_callback(event)
        write.assert_not_called()
        event.edit_original_response.assert_not_awaited()
        event.followup.send.assert_not_awaited()

    async def test_expired_save_does_not_send_dm_or_change_subscription(self):
        event = expired_click()
        with patch.object(subscriptions, "set_subscriptions") as write:
            with self.assertLogs(interaction_utils.logger, level="WARNING"):
                await subscriptions.SubscriptionView(1, [SERVERS[0]]).save(event)
        write.assert_not_called()
        event.user.send.assert_not_awaited()

    async def test_other_discord_errors_are_not_hidden(self):
        event = expired_click(10008)
        with self.assertRaises(discord.NotFound):
            await interaction_utils.acknowledge(event)

    async def test_acknowledged_interaction_can_continue_without_second_response(self):
        event = expired_click()
        event.response.is_done.return_value = True
        self.assertTrue(await interaction_utils.acknowledge(event))
        event.response.defer.assert_not_awaited()

    async def test_handler_lookup_never_reads_deprecated_message_interaction(self):
        class Message:
            id = 50
            interaction_metadata = SimpleNamespace(id=60)

            @property
            def interaction(self):
                raise AssertionError("Deprecated property accessed")

        event = expired_click()
        event.message = Message()
        event.data["component_type"] = 2
        store = SimpleNamespace(_views={60: {(2, "confirmation"): object()}}, _dynamic_items={})
        client = SimpleNamespace(_connection=SimpleNamespace(_view_store=store))
        self.assertTrue(main.has_component_handler(client, event))


if __name__ == "__main__":
    unittest.main()
