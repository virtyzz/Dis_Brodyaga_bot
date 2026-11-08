import asyncio
import sqlite3
import tempfile
import threading
import unittest
from contextlib import closing
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock, patch

import database as db
import subscriptions as sub
from coords_handler import SERVERS, get_all_locations


class SubscriptionReliabilityTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.db_patch = patch.object(db, "DB_PATH", str(Path(self.temp.name) / "audit.db"))
        self.db_patch.start()
        db.init_database()
        self.server = SERVERS[0]
        self.location = get_all_locations()[0]

    def tearDown(self):
        self.db_patch.stop()
        self.temp.cleanup()

    def report(self):
        db.add_trader_report(self.server, self.location, 1, 2, 9)

    def interaction(self, uid, send=None):
        return SimpleNamespace(
            user=SimpleNamespace(id=uid, send=send or AsyncMock()),
            response=SimpleNamespace(defer=AsyncMock(), is_done=Mock(return_value=False)),
            edit_original_response=AsyncMock(),
        )

    async def test_constructor_has_no_database_io(self):
        with patch.object(sub, "get_subscriptions", side_effect=AssertionError("Blocking database read")):
            view = sub.SubscriptionView(1, current=[self.server])
        self.assertEqual(view.selected, {self.server})

    async def test_factory_reads_database_off_event_loop(self):
        loop_thread = threading.get_ident()
        def load(uid):
            self.assertNotEqual(threading.get_ident(), loop_thread)
            return {self.server}
        with patch.object(sub, "get_subscriptions", side_effect=load):
            view = await sub.SubscriptionView.create(1)
        self.assertEqual(view.selected, {self.server})

    async def test_slow_dm_does_not_block_another_users_unsubscribe(self):
        db.set_subscriptions(2, [self.server])
        started, release = asyncio.Event(), asyncio.Event()
        async def slow_dm(**kwargs):
            started.set()
            await release.wait()
        saving = asyncio.create_task(sub.SubscriptionView(1, [self.server]).save(
            self.interaction(1, slow_dm)
        ))
        try:
            await asyncio.wait_for(started.wait(), 1)
            await asyncio.wait_for(sub.SubscriptionView(2).unsubscribe(self.interaction(2)), 1)
            self.assertEqual(db.get_subscriptions(2), set())
            self.assertFalse(saving.done())
        finally:
            release.set()
            await saving

    async def test_daily_reset_cancels_remaining_rows_in_loaded_batch(self):
        for user in (1, 2):
            db.set_subscriptions(user, [self.server])
        self.report()
        async def reset(**kwargs):
            await asyncio.to_thread(db.archive_reports)
        first = SimpleNamespace(send=AsyncMock(side_effect=reset))
        second = SimpleNamespace(send=AsyncMock())
        bot = SimpleNamespace(get_user=lambda uid: {1: first, 2: second}[uid])
        await sub.send_pending_notifications(bot)
        first.send.assert_awaited_once()
        second.send.assert_not_awaited()

    async def test_unsubscribe_during_user_fetch_prevents_send(self):
        db.set_subscriptions(1, [self.server])
        self.report()
        user = SimpleNamespace(send=AsyncMock())
        async def fetch(uid):
            await asyncio.to_thread(db.set_subscriptions, uid, [])
            return user
        bot = SimpleNamespace(get_user=lambda uid: None, fetch_user=fetch)
        await sub.send_pending_notifications(bot)
        user.send.assert_not_awaited()

    async def test_overlapping_workers_do_not_send_same_row(self):
        db.set_subscriptions(1, [self.server])
        self.report()
        started, release = asyncio.Event(), asyncio.Event()
        async def slow_send(**kwargs):
            started.set()
            await release.wait()
        user = SimpleNamespace(send=AsyncMock(side_effect=slow_send))
        bot = SimpleNamespace(get_user=lambda uid: user)
        first = asyncio.create_task(sub.send_pending_notifications(bot))
        try:
            await asyncio.wait_for(started.wait(), 1)
            await asyncio.wait_for(sub.send_pending_notifications(bot), 1)
            self.assertEqual(user.send.await_count, 1)
        finally:
            release.set()
            await first

    async def test_database_error_reading_queue_does_not_escape_worker(self):
        with patch.object(sub, "get_pending_notifications", side_effect=sqlite3.OperationalError("busy")):
            with self.assertLogs("subscriptions", level="ERROR"):
                await sub.send_pending_notifications(SimpleNamespace())

    async def test_database_error_claiming_one_row_does_not_block_others(self):
        for user in (1, 2):
            db.set_subscriptions(user, [self.server])
        self.report()
        actual_claim = sub.claim_notification
        failed = False
        def claim(nid):
            nonlocal failed
            if not failed:
                failed = True
                raise sqlite3.OperationalError("busy")
            return actual_claim(nid)
        user = SimpleNamespace(send=AsyncMock())
        bot = SimpleNamespace(get_user=lambda uid: user)
        with patch.object(sub, "claim_notification", side_effect=claim):
            with self.assertLogs("subscriptions", level="ERROR"):
                await sub.send_pending_notifications(bot)
        self.assertEqual(user.send.await_count, 1)
        await sub.send_pending_notifications(bot)
        self.assertEqual(user.send.await_count, 2)

    async def test_database_failure_in_settings_preserves_selection(self):
        view = sub.SubscriptionView(1, [self.server])
        interaction = self.interaction(1)
        with patch.object(sub, "set_subscriptions", side_effect=sqlite3.OperationalError("busy")):
            with self.assertLogs("subscriptions", level="ERROR"):
                await view.save(interaction)
        shown = interaction.edit_original_response.call_args.kwargs["view"]
        self.assertEqual(shown.selected, {self.server})
        self.assertIn("База данных временно недоступна", str(shown.to_components()))
        self.assertEqual(db.get_subscriptions(1), set())

    async def test_retry_reuses_same_message_nonce(self):
        db.set_subscriptions(1, [self.server])
        self.report()
        notification_id = db.get_pending_notifications()[0][0]
        user = SimpleNamespace(send=AsyncMock(side_effect=[OSError("ambiguous send"), None]))
        bot = SimpleNamespace(get_user=lambda uid: user)
        with self.assertLogs("subscriptions", level="WARNING"):
            await sub.send_pending_notifications(bot)
        # Advance the queue's due time without sleeping through the backoff.
        with closing(sqlite3.connect(db.DB_PATH)) as conn, conn:
            conn.execute("UPDATE trader_notifications SET next_attempt_at = 0 WHERE id = ?",
                         (notification_id,))
        await sub.send_pending_notifications(bot)
        self.assertEqual(user.send.await_count, 2)
        self.assertEqual([call.kwargs["nonce"] for call in user.send.await_args_list],
                         [f"b{notification_id}", f"b{notification_id}"])


if __name__ == "__main__":
    unittest.main()
