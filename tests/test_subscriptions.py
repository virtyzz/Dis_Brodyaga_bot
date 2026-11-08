import os
import sqlite3
import tempfile
import unittest
from contextlib import closing
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

import discord
import database as db
from coords_handler import SERVERS, get_all_locations
from subscriptions import SubscriptionView, send_pending_notifications


class SubscriptionTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.path_patch = patch.object(db, "DB_PATH", str(Path(self.temp.name) / "test.db"))
        self.path_patch.start()
        db.init_database()
        self.server = SERVERS[0]
        self.location = get_all_locations()[0]

    def tearDown(self):
        self.path_patch.stop()
        self.temp.cleanup()

    def report(self, location=None, user=9, server=None):
        return db.add_trader_report(
            server or self.server, location or self.location, 1, 2, user
        )

    def interaction(self, send_error=None):
        return SimpleNamespace(
            user=SimpleNamespace(id=1, send=AsyncMock(side_effect=send_error)),
            response=SimpleNamespace(defer=AsyncMock(), send_message=AsyncMock(),
                                     edit_message=AsyncMock()),
            edit_original_response=AsyncMock(),
        )

    async def test_selection_persists_and_validates(self):
        db.set_subscriptions(1, SERVERS[:2])
        db.init_database()
        self.assertEqual(db.get_subscriptions(1), set(SERVERS[:2]))
        with self.assertRaises(ValueError):
            db.set_subscriptions(1, ["unknown"])
        self.assertEqual(db.get_subscriptions(1), set(SERVERS[:2]))

    async def test_only_matching_subscribers_and_no_duplicate_confirmations(self):
        db.set_subscriptions(1, [self.server])
        db.set_subscriptions(2, [SERVERS[1]])
        db.set_subscriptions(9, [self.server])
        self.report()
        self.report()
        self.report(user=10)
        self.assertEqual({row[1] for row in db.get_pending_notifications()}, {1, 9})
        self.assertEqual(len(db.get_pending_notifications()), 2)

    async def test_vote_moving_away_and_back_does_not_rebroadcast(self):
        db.set_subscriptions(1, [self.server])
        self.report()
        self.report(get_all_locations()[1])
        self.report()
        self.assertEqual(len(db.get_pending_notifications()), 2)

    async def test_daily_reset_preserves_subscriptions_and_allows_new_alert(self):
        db.set_subscriptions(1, [self.server])
        self.report()
        db.archive_reports()
        self.assertEqual(db.get_pending_notifications(), [])
        self.assertEqual(db.get_subscriptions(1), {self.server})
        self.report()
        self.assertEqual(len(db.get_pending_notifications()), 1)

    async def test_existing_reports_and_late_subscription_are_not_broadcast(self):
        self.report()
        # Simulate upgrading a database with existing trader reports.
        with closing(sqlite3.connect(db.DB_PATH)) as conn, conn:
            conn.execute("DELETE FROM trader_notification_events")
        db.init_database()
        db.set_subscriptions(1, [self.server])
        self.report(user=10)
        self.assertEqual(db.get_pending_notifications(), [])

    async def test_pending_queue_survives_reinitialization(self):
        db.set_subscriptions(1, [self.server])
        self.report()
        before = db.get_pending_notifications()
        db.init_database()
        self.assertEqual(db.get_pending_notifications(), before)

    async def test_unsubscribe_cancels_queued_messages(self):
        db.set_subscriptions(1, SERVERS[:2])
        self.report()
        self.report(server=SERVERS[1])
        db.set_subscriptions(1, [SERVERS[1]])
        self.assertEqual([row[2] for row in db.get_pending_notifications()], [SERVERS[1]])
        db.set_subscriptions(1, [])
        self.assertEqual(db.get_pending_notifications(), [])

    async def test_dm_success_activates_selection(self):
        interaction = self.interaction()
        await SubscriptionView(1, SERVERS[:2]).save(interaction)
        interaction.user.send.assert_awaited_once()
        self.assertEqual(db.get_subscriptions(1), set(SERVERS[:2]))

    async def test_closed_dm_preserves_old_subscription_and_retry_selection(self):
        db.set_subscriptions(1, [SERVERS[1]])
        error = discord.Forbidden(SimpleNamespace(status=403, reason="Forbidden"),
                                  {"code": 50007, "message": "Cannot send messages"})
        interaction = self.interaction(error)
        await SubscriptionView(1, [self.server]).save(interaction)
        self.assertEqual(db.get_subscriptions(1), {SERVERS[1]})
        view = interaction.edit_original_response.call_args.kwargs["view"]
        self.assertEqual(view.selected, {self.server})
        payload = str(view.to_components())
        self.assertIn("Проверить снова", payload)
        self.assertIn("разблокируйте", payload)

    async def test_temporary_dm_error_does_not_activate(self):
        error = discord.HTTPException(SimpleNamespace(status=503, reason="Unavailable"), "Retry")
        interaction = self.interaction(error)
        await SubscriptionView(1, [self.server]).save(interaction)
        self.assertEqual(db.get_subscriptions(1), set())
        self.assertIn("временно", str(interaction.edit_original_response.call_args.kwargs["view"].to_components()))

    async def test_empty_selection_unsubscribes_without_dm(self):
        db.set_subscriptions(1, [self.server])
        interaction = self.interaction()
        await SubscriptionView(1, []).save(interaction)
        interaction.user.send.assert_not_awaited()
        self.assertEqual(db.get_subscriptions(1), set())

    async def test_private_menu_rejects_other_users(self):
        interaction = self.interaction()
        interaction.user.id = 2
        self.assertFalse(await SubscriptionView(1).interaction_check(interaction))
        interaction.response.send_message.assert_awaited_once_with(
            "Это не ваше меню.", ephemeral=True
        )

    async def test_delivery_continues_after_closed_dm_and_does_not_resend(self):
        db.set_subscriptions(1, [self.server])
        db.set_subscriptions(2, [self.server])
        self.report()
        error = discord.Forbidden(SimpleNamespace(status=403, reason="Forbidden"), "Closed")
        closed = SimpleNamespace(send=AsyncMock(side_effect=error))
        available = SimpleNamespace(send=AsyncMock())
        bot = SimpleNamespace(get_user=lambda uid: {1: closed, 2: available}[uid])
        await send_pending_notifications(bot)
        await send_pending_notifications(bot)
        closed.send.assert_awaited_once()
        available.send.assert_awaited_once()
        self.assertEqual(db.get_pending_notifications(), [])

    async def test_temporary_delivery_errors_stop_after_three_attempts(self):
        db.set_subscriptions(1, [self.server])
        self.report()
        user = SimpleNamespace(send=AsyncMock(side_effect=OSError("Offline")))
        bot = SimpleNamespace(get_user=lambda uid: user)
        for _ in range(4):
            await send_pending_notifications(bot)
        self.assertEqual(user.send.await_count, 3)
        self.assertEqual(db.get_pending_notifications(), [])

    async def test_main_menu_layout_and_component_limits(self):
        with patch.dict(os.environ, {"DISCORD_BOT_TOKEN": "test-placeholder"}):
            from main import MainMenuV2View
        view = MainMenuV2View()
        rows = [item for item in view.children[0].children
                if isinstance(item, discord.ui.ActionRow)]
        self.assertEqual([[button.label for button in row.children] for row in rows], [
            ["Начать поиск", "Где торговец?"],
            ["Подписки", "Справка"],
            ["Добавить себе или поделиться"],
        ])
        self.assertTrue(view.is_persistent())
        settings = SubscriptionView(1, SERVERS)
        self.assertLessEqual(settings.total_children_count, 40)
        self.assertLessEqual(view.total_children_count, 40)


if __name__ == "__main__":
    unittest.main()
