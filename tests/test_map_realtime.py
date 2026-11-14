import hashlib
import hmac
import json
import sqlite3
import tempfile
import threading
import time
import unittest
from contextlib import closing
from datetime import datetime, timedelta, timezone
from pathlib import Path
from unittest.mock import patch

import database as db
import map_realtime
from coords_handler import SERVERS, get_all_locations


class MapOutboxTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.path = str(Path(self.temp.name) / "test.db")
        patcher = patch.object(db, "DB_PATH", self.path)
        patcher.start()
        self.addCleanup(patcher.stop)
        db.init_database()
        self.server = SERVERS[0]
        self.location = get_all_locations()[0]

    def snapshot(self):
        with closing(sqlite3.connect(self.path)) as conn:
            return db.read_map_snapshot(conn)

    def test_confirmations_and_reset_are_versioned_without_user_data(self):
        first = self.snapshot()
        db.register_user(1, "private-name")
        db.add_trader_report(self.server, self.location, 1, 2, 1)
        second = self.snapshot()
        db.add_trader_report(self.server, self.location, 1, 2, 1)
        self.assertEqual(second, self.snapshot(), "unchanged confirmation must not publish")
        db.add_trader_report(self.server, self.location, 1, 2, 2)
        third = self.snapshot()
        self.assertEqual(third["reports"][0]["confirmations"], 2)
        self.assertEqual(third["revision"], first["revision"] + 2)
        self.assertNotIn("private-name", json.dumps(third))
        db.archive_reports()
        reset = self.snapshot()
        self.assertEqual(reset["reports"], [])
        self.assertEqual(reset["revision"], third["revision"] + 1)
        db.init_database()
        self.assertEqual(reset, self.snapshot(), "restart must retain epoch and revision")

    def test_failed_transaction_cannot_publish(self):
        db.add_trader_report(self.server, self.location, 1, 2, 1)
        before = self.snapshot()
        with self.assertRaises(db.ReportCooldownError):
            db.add_trader_report(self.server, get_all_locations()[1], 1, 2, 1)
        self.assertEqual(before, self.snapshot())

    def test_daily_reset_is_queued(self):
        db.add_trader_report(self.server, self.location, 1, 2, 1)
        before = self.snapshot()
        future = datetime.now(timezone.utc) + timedelta(days=1)
        db.ensure_current_cycle(future)
        after = self.snapshot()
        self.assertEqual(after["reports"], [])
        self.assertGreater(after["revision"], before["revision"])

    def test_publisher_reads_cross_process_commits_and_recovers_after_failure(self):
        delivered = []
        published = threading.Event()
        attempts = []

        def send(url, secret, snapshot):
            attempts.append(snapshot)
            if len(attempts) == 1:
                raise OSError("offline")
            delivered.append(snapshot)
            published.set()

        with patch.object(map_realtime, "deliver_snapshot", side_effect=send):
            publisher = map_realtime.MapPublisher("https://map/internal", "x" * 32)
            publisher.start()
            try:
                deadline = time.monotonic() + 2
                while not attempts and time.monotonic() < deadline:
                    time.sleep(0.01)
                db.add_trader_report(self.server, self.location, 1, 2, 1)
                self.assertTrue(published.wait(5))
                self.assertEqual(delivered[-1]["reports"][0]["confirmations"], 1)
                published.clear()
                db.add_trader_report(self.server, self.location, 1, 2, 2)
                self.assertTrue(published.wait(2))
                self.assertEqual(delivered[-1]["reports"][0]["confirmations"], 2)
                published.clear()
                db.register_user(99, "unrelated")
                self.assertFalse(published.wait(0.5))
            finally:
                publisher.stop()

    def test_http_signature_covers_exact_body_and_timestamp(self):
        captured = []

        class Response:
            def __enter__(self):
                return self
            def __exit__(self, *args):
                pass
            def read(self):
                return b'{"ok":true}'

        def open_request(request, timeout):
            captured.append(request)
            return Response()

        with patch.object(map_realtime.urllib.request, "urlopen", side_effect=open_request):
            map_realtime.deliver_snapshot("https://map/internal", "secret", self.snapshot())
        request = captured[0]
        self.assertTrue(request.get_header("User-agent").startswith("BrodyagaBot/"))
        stamp = request.get_header("X-brodyaga-timestamp")
        expected = hmac.new(b"secret", stamp.encode() + b"." + request.data, hashlib.sha256).hexdigest()
        self.assertEqual(request.get_header("X-brodyaga-signature"), expected)

    def test_multiple_targets_have_independent_publishers(self):
        targets = [{"url": "https://prod/internal", "secret": "p" * 32},
                   {"url": "https://dev/internal", "secret": "d" * 32}]
        with patch.dict(map_realtime.os.environ, {"BRODYAGA_PUSH_TARGETS": json.dumps(targets)}):
            with patch.object(map_realtime.MapPublisher, "start") as start:
                publishers = map_realtime.start_map_publisher()
        self.assertEqual(start.call_count, 2)
        self.assertEqual([(p.url, p.secret) for p in publishers.publishers],
                         [(t["url"], t["secret"]) for t in targets])
        self.assertIsNot(publishers.publishers[0].stop_event, publishers.publishers[1].stop_event)
        with patch.object(map_realtime.MapPublisher, "stop") as stop:
            publishers.stop()
        self.assertEqual(stop.call_count, 2)

    def test_invalid_target_configuration_starts_nothing_and_does_not_expose_keys(self):
        values = [[], {"url": "https://prod", "secret": "private"},
                  [{"url": "https://prod", "secret": "private"}],
                  [{"url": "https://same", "secret": "x" * 32}] * 2]
        for value in values:
            with patch.dict(map_realtime.os.environ, {"BRODYAGA_PUSH_TARGETS": json.dumps(value)}):
                with patch.object(map_realtime.MapPublisher, "start") as start:
                    with self.assertRaises(ValueError) as error:
                        map_realtime.start_map_publisher()
                start.assert_not_called()
                self.assertNotIn("private", str(error.exception))

    def test_dev_outage_does_not_block_production_delivery(self):
        production_delivered = threading.Event()

        def send(url, secret, snapshot):
            if url == "https://dev":
                raise OSError("dev offline")
            production_delivered.set()

        publishers = map_realtime.MapPublishers([("https://dev", "d" * 32), ("https://prod", "p" * 32)])
        with patch.object(map_realtime, "deliver_snapshot", side_effect=send):
            publishers.start()
            try:
                self.assertTrue(production_delivered.wait(2))
            finally:
                publishers.stop()


if __name__ == "__main__":
    unittest.main()
