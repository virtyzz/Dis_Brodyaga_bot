import hashlib
import hmac
import json
import sqlite3
import tempfile
import time
import unittest
from contextlib import closing
from pathlib import Path
from unittest.mock import Mock, patch

import database
import map_realtime
import web_api


class RecoveryEndpointTests(unittest.TestCase):
    def setUp(self):
        temp = tempfile.TemporaryDirectory()
        self.addCleanup(temp.cleanup)
        path = str(Path(temp.name) / "test.db")
        db_patch = patch.object(database, "DB_PATH", path)
        db_patch.start()
        self.addCleanup(db_patch.stop)
        database.init_database()
        self.secrets = ["production-secret-32-characters-long", "development-secret-32-characters-long"]
        env = patch.dict(map_realtime.os.environ, {"BRODYAGA_PUSH_TARGETS": json.dumps([
            {"url": "https://prod/internal", "secret": self.secrets[0]},
            {"url": "https://dev/internal", "secret": self.secrets[1]},
        ])})
        env.start()
        self.addCleanup(env.stop)

    def request(self, secret=None, stamp=None, context=map_realtime.RECOVERY_CONTEXT):
        stamp = str(int(time.time())) if stamp is None else stamp
        handler = object.__new__(web_api.ApiHandler)
        handler.path = "/internal/map-snapshot"
        handler.begin_activity = Mock()
        handler.send_json = Mock()
        signature = hmac.new((secret or self.secrets[0]).encode(), context + stamp.encode(), hashlib.sha256).hexdigest()
        handler.headers = {"X-Brodyaga-Timestamp": stamp, "X-Brodyaga-Signature": signature}
        handler.do_GET()
        return handler.send_json.call_args.args

    def test_each_environment_can_recover_exact_public_snapshot(self):
        with closing(sqlite3.connect(database.DB_PATH)) as conn:
            expected = database.read_map_snapshot(conn)
        for secret in self.secrets:
            status, body = self.request(secret)
            self.assertEqual(status, 200)
            self.assertEqual(body, expected)

    def test_wrong_expired_and_push_signatures_are_rejected_before_reading(self):
        with patch.object(database, "read_map_snapshot") as read:
            self.assertEqual(self.request(secret="wrong")[0], 401)
            self.assertEqual(self.request(stamp="1")[0], 401)
            self.assertEqual(self.request(stamp="invalid")[0], 401)
            self.assertEqual(self.request(context=b"")[0], 401)
        read.assert_not_called()

    def test_non_ascii_signature_is_rejected(self):
        handler = object.__new__(web_api.ApiHandler)
        handler.headers = {"X-Brodyaga-Timestamp": str(int(time.time())), "X-Brodyaga-Signature": "я" * 64}
        handler.send_json = Mock()
        handler.send_map_snapshot()
        self.assertEqual(handler.send_json.call_args.args[0], 401)

    def test_database_outage_returns_retryable_error(self):
        with patch.object(database, "ensure_current_cycle", side_effect=sqlite3.OperationalError("locked")), \
             self.assertLogs(web_api.LOGGER, level="ERROR"):
            self.assertEqual(self.request()[0], 503)
