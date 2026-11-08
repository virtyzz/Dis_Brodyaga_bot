import os
import sqlite3
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock, patch

import discord
import subscriptions as sub
from coords_handler import SERVERS, get_all_locations

os.environ.setdefault("DISCORD_BOT_TOKEN", "local-tests-only")
import main


def interaction(user_id=7):
    return SimpleNamespace(
        user=SimpleNamespace(id=user_id), guild=None,
        response=SimpleNamespace(is_done=Mock(return_value=False), defer=AsyncMock()),
        edit_original_response=AsyncMock(),
    )


class NotificationActionTests(unittest.IsolatedAsyncioTestCase):
    async def test_alert_has_persistent_photo_map_and_subscriptions_without_old_notice(self):
        view = sub.TraderNotificationView(SERVERS[0], get_all_locations()[0], 123, 456)
        buttons = [item for item in view.walk_children() if isinstance(item, discord.ui.Button)]
        self.assertEqual([button.label for button in buttons], ["Фото", "На карте", "Подписки"])
        self.assertTrue(view.is_persistent())
        text = str(view.to_components())
        self.assertNotIn("Это отметка игрока", text)
        self.assertNotIn("главного меню", text)

    async def test_reporter_is_plain_escaped_text_below_coordinates(self):
        view = sub.TraderNotificationView(
            SERVERS[0], get_all_locations()[0], 123, 456,
            reporter_name="**player**\n<@123456789012345678> @everyone",
        )
        text = view.children[0].children[0].content
        self.assertIn("**Координаты:** X: 123, Y: 456\n**Сообщил:**", text)
        self.assertIn(r"\*\*player\*\*", text)
        self.assertNotIn("<@123456789012345678>", text)
        self.assertNotIn("@everyone", text)

    async def test_management_opens_clicking_users_menu_in_new_dm_response(self):
        event = interaction(91)
        menu = sub.SubscriptionView(91)
        with patch.object(sub.SubscriptionView, "create", AsyncMock(return_value=menu)) as create:
            with patch.object(sub, "set_subscriptions") as write:
                await sub.NotificationSubscriptionsButton().callback(event)
        create.assert_awaited_once_with(91)
        write.assert_not_called()
        event.response.defer.assert_awaited_once_with(ephemeral=False, thinking=True)
        event.edit_original_response.assert_awaited_once_with(view=menu)

    async def test_photo_sends_exact_notifications_location(self):
        location = get_all_locations()[1]
        event = interaction()
        with tempfile.TemporaryDirectory() as temp:
            photo = Path(temp) / "photo.png"
            photo.write_bytes(b"test-photo")
            captured = []
            async def send(**kwargs):
                captured.append((kwargs["content"], kwargs["attachments"][0].fp.read()))
            event.edit_original_response.side_effect = send
            with patch.object(sub, "get_screenshot_path", return_value=str(photo)) as lookup:
                await sub.NotificationPhotoButton(location).callback(event)
            lookup.assert_called_once_with(location)
        self.assertEqual(captured, [(location, b"test-photo")])

    async def test_missing_photo_shows_message(self):
        event = interaction()
        with patch.object(sub, "get_screenshot_path", return_value=None):
            await sub.NotificationPhotoButton(get_all_locations()[0]).callback(event)
        self.assertIn("нет изображения", event.edit_original_response.call_args.kwargs["content"])

    async def test_menu_database_error_is_recoverable(self):
        event = interaction()
        with patch.object(sub.SubscriptionView, "create", AsyncMock(side_effect=sqlite3.OperationalError("locked"))):
            with self.assertLogs(sub.logger, level="ERROR"):
                await sub.NotificationSubscriptionsButton().callback(event)
        self.assertIn("временно недоступна", event.edit_original_response.call_args.kwargs["content"])

    async def test_restart_restores_all_notification_button_ids(self):
        client = main.BrodyagaBot(command_prefix="!", intents=discord.Intents.none())
        try:
            sub.register_notification_views(client)
            ids = [sub.NotificationSubscriptionsButton().custom_id]
            ids.extend(sub.NotificationPhotoButton(location).custom_id for location in get_all_locations())
            for custom_id in ids:
                event = SimpleNamespace(message=SimpleNamespace(id=123, interaction_metadata=None),
                                        data={"component_type": 2, "custom_id": custom_id})
                self.assertTrue(main.has_component_handler(client, event))
        finally:
            for view in client.persistent_views:
                view.stop()
            await client.close()


if __name__ == "__main__":
    unittest.main()
