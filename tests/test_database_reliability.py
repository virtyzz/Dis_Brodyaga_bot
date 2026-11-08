import sqlite3
import tempfile
import unittest
from concurrent.futures import ThreadPoolExecutor
from contextlib import closing
from datetime import datetime, timedelta, timezone
from pathlib import Path
from unittest.mock import patch

import database as db
from coords_handler import SERVERS, get_all_locations


class DatabaseReliabilityTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.path = patch.object(db, "DB_PATH", str(Path(self.temp.name) / "test.db"))
        self.path.start()
        self.now = datetime(2026, 11, 9, 2, tzinfo=timezone.utc)
        normalize = db._utc_now
        self.clock = patch.object(db, "_utc_now", side_effect=lambda now=None: normalize(now or self.now))
        self.clock.start()
        db.init_database()
        self.server = SERVERS[0]
        self.location, self.other = get_all_locations()[:2]
        db.set_subscriptions(1, [self.server])

    def tearDown(self):
        self.clock.stop()
        self.path.stop()
        self.temp.cleanup()

    def report(self, location=None, reporter=9):
        return db.add_trader_report(self.server, location or self.location, 1, 2, reporter)

    def test_alert_keeps_first_author_after_confirmations_vote_change_and_restart(self):
        db.register_user(9, "first_player")
        db.register_user(10, "confirming_player")
        self.report(reporter=9)
        self.report(reporter=10)
        self.assertEqual(len(db.get_pending_notifications()), 1)
        self.now += timedelta(seconds=db.REPORT_COOLDOWN_SECONDS)
        self.report(self.other, reporter=9)
        with closing(sqlite3.connect(db.DB_PATH)) as conn, conn:
            conn.execute("UPDATE users SET username = 'new_name' WHERE user_id = 9")
        db.init_database()
        first_alert = next(row for row in db.get_pending_notifications() if row[3] == self.location)
        self.assertEqual(first_alert[6], "first_player")

    def test_missed_reset_catches_up_once_and_preserves_subscriptions(self):
        self.report()
        self.now += timedelta(days=1, minutes=5)
        self.assertTrue(db.ensure_current_cycle())
        self.assertFalse(db.ensure_current_cycle())
        db.init_database()
        self.assertFalse(db.ensure_current_cycle())
        self.assertEqual(db.get_trader_report_summary(), [])
        self.assertEqual(db.get_subscriptions(1), {self.server})
        with closing(sqlite3.connect(db.DB_PATH)) as conn:
            self.assertEqual(conn.execute("SELECT COUNT(*) FROM trader_history").fetchone()[0], 1)

    def test_reset_boundary_is_01_utc(self):
        self.report()
        self.now = datetime(2026, 11, 10, 0, 59, 59, tzinfo=timezone.utc)
        self.assertFalse(db.ensure_current_cycle())
        self.now += timedelta(seconds=1)
        self.assertTrue(db.ensure_current_cycle())
        self.report()
        self.assertFalse(db.ensure_current_cycle())
        self.assertEqual(len(db.get_trader_report_summary()), 1)

    def test_concurrent_reset_archives_only_once(self):
        self.report()
        self.now += timedelta(days=1)
        with ThreadPoolExecutor(max_workers=4) as pool:
            results = list(pool.map(lambda _: db.ensure_current_cycle(), range(4)))
        self.assertEqual(results.count(True), 1)

    def test_report_write_catches_up_before_accepting_new_vote(self):
        self.report()
        self.now += timedelta(days=1)
        self.report(self.other)
        self.assertEqual([row[1] for row in db.get_trader_report_summary()], [self.other])

    def test_cooldown_rejects_changes_preserves_old_vote_and_survives_restart(self):
        self.report()
        db.init_database()
        with self.assertRaises(db.ReportCooldownError) as error:
            self.report(self.other)
        self.assertEqual(error.exception.retry_after, db.REPORT_COOLDOWN_SECONDS)
        self.assertEqual([row[1] for row in db.get_trader_report_summary()], [self.location])
        self.assertFalse(self.report())
        self.now += timedelta(seconds=db.REPORT_COOLDOWN_SECONDS)
        self.assertTrue(self.report(self.other))

    def test_concurrent_claim_has_only_one_winner(self):
        self.report()
        notification_id = db.get_pending_notifications()[0][0]
        with ThreadPoolExecutor(max_workers=4) as pool:
            results = list(pool.map(lambda _: db.claim_notification(notification_id), range(4)))
        self.assertEqual(results.count(True), 1)
        self.assertTrue(db.notification_is_current(notification_id))
        self.assertEqual(db.get_pending_notifications(), [])

    def test_claimed_notification_invalidated_by_reset_or_unsubscribe(self):
        self.report()
        notification_id = db.get_pending_notifications()[0][0]
        self.assertTrue(db.claim_notification(notification_id))
        db.set_subscriptions(1, [])
        self.assertFalse(db.notification_is_current(notification_id))
        db.set_subscriptions(1, [self.server])
        self.assertFalse(db.claim_notification(notification_id))
        self.now += timedelta(days=1)
        self.assertFalse(db.notification_is_current(notification_id))

    def test_claim_rejects_notification_for_withdrawn_location(self):
        self.report()
        notification_id = db.get_pending_notifications()[0][0]
        self.now += timedelta(seconds=db.REPORT_COOLDOWN_SECONDS)
        self.report(self.other)
        self.assertFalse(db.claim_notification(notification_id))

    def test_retry_backoff_persists_and_does_not_stop_after_three_attempts(self):
        self.report()
        notification_id = db.get_pending_notifications()[0][0]
        for delay in (30, 60, 120, 240):
            self.assertTrue(db.claim_notification(notification_id))
            db.finish_notification(notification_id, "pending")
            db.init_database()
            self.assertEqual(db.get_pending_notifications(), [])
            self.now += timedelta(seconds=delay)
            self.assertEqual(db.get_pending_notifications()[0][0], notification_id)

    def test_crashed_worker_lease_is_recoverable(self):
        self.report()
        notification_id = db.get_pending_notifications()[0][0]
        self.assertTrue(db.claim_notification(notification_id, lease_seconds=120))
        self.assertFalse(db.claim_notification(notification_id))
        self.now += timedelta(seconds=120)
        self.assertTrue(db.claim_notification(notification_id))

    def test_expired_notification_never_claimed(self):
        self.report()
        notification_id = db.get_pending_notifications()[0][0]
        self.now += timedelta(days=1)
        self.assertFalse(db.claim_notification(notification_id))
        self.assertEqual(db.get_pending_notifications(), [])

    def test_legacy_queue_schema_migrates_without_losing_current_data(self):
        self.report()
        with closing(sqlite3.connect(db.DB_PATH)) as conn, conn:
            conn.execute("DROP TABLE trader_notifications")
            conn.execute("""
                CREATE TABLE trader_notifications (
                    id INTEGER PRIMARY KEY AUTOINCREMENT, user_id BIGINT NOT NULL,
                    server TEXT NOT NULL, location_name TEXT NOT NULL,
                    x_coord INTEGER NOT NULL, y_coord INTEGER NOT NULL,
                    status TEXT NOT NULL DEFAULT 'pending', attempts INTEGER NOT NULL DEFAULT 0
                )
            """)
            conn.execute("""
                INSERT INTO trader_notifications (user_id, server, location_name, x_coord, y_coord)
                VALUES (1, ?, ?, 1, 2)
            """, (self.server, self.location))
            conn.execute("DELETE FROM bot_state")
        db.init_database()
        self.assertEqual(len(db.get_trader_report_summary()), 1)
        self.assertEqual(len(db.get_pending_notifications()), 1)

        # Legacy queued events do not contain a provable original author.
        # Preserve the row without attributing it to a later confirmation.
        self.assertEqual(db.get_pending_notifications()[0][6], "неизвестно")

    def test_legacy_old_reports_are_archived_on_upgrade(self):
        self.report()
        with closing(sqlite3.connect(db.DB_PATH)) as conn, conn:
            conn.execute("DELETE FROM bot_state")
        self.now += timedelta(days=1)
        db.init_database()
        self.assertEqual(db.get_trader_report_summary(), [])
        self.assertEqual(db.get_pending_notifications(), [])

    def test_atomic_registration(self):
        with ThreadPoolExecutor(max_workers=4) as pool:
            results = list(pool.map(lambda _: db.register_user(99, "player"), range(4)))
        self.assertEqual(results.count(True), 1)

    def test_concurrent_fresh_database_initialization(self):
        fresh_path = str(Path(self.temp.name) / "fresh.db")
        with patch.object(db, "DB_PATH", fresh_path):
            with ThreadPoolExecutor(max_workers=4) as pool:
                list(pool.map(lambda _: db.init_database(), range(4)))
            self.assertEqual(db.get_pending_notifications(), [])


if __name__ == "__main__":
    unittest.main()
