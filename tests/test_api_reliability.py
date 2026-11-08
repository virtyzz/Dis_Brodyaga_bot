import sqlite3
import unittest
from http import HTTPStatus
from unittest.mock import Mock, patch

import web_api
from database import ReportCooldownError
from coords_handler import SERVERS


class ApiReliabilityTests(unittest.TestCase):
    def handler(self):
        handler = object.__new__(web_api.ApiHandler)
        handler.headers = {"X-CSRF-Token": "test"}
        handler.get_session = Mock(return_value={"user_id": "7", "username": "tester", "csrf": "test"})
        handler.read_json = Mock(return_value={"location_id": 1, "server": SERVERS[0]})
        handler.send_json = Mock()
        return handler

    def test_shared_cooldown_returns_429(self):
        handler = self.handler()
        with patch.object(web_api, "register_user"), patch.object(
            web_api, "add_trader_report", side_effect=ReportCooldownError(45)
        ):
            handler.create_report()
        status, payload = handler.send_json.call_args.args
        self.assertEqual(status, HTTPStatus.TOO_MANY_REQUESTS)
        self.assertEqual(payload["retry_after"], 45)

    def test_locked_database_returns_503_instead_of_dropped_connection(self):
        handler = self.handler()
        with patch.object(web_api, "register_user", side_effect=sqlite3.OperationalError("database is locked")):
            with self.assertLogs(web_api.LOGGER, level="ERROR"):
                handler.create_report()
        self.assertEqual(handler.send_json.call_args.args[0], HTTPStatus.SERVICE_UNAVAILABLE)

    def test_read_failure_returns_503(self):
        handler = self.handler()
        with patch.object(web_api, "get_trader_report_summary", side_effect=sqlite3.OperationalError("database is locked")):
            with self.assertLogs(web_api.LOGGER, level="ERROR"):
                handler.send_reports()
        self.assertEqual(handler.send_json.call_args.args[0], HTTPStatus.SERVICE_UNAVAILABLE)


if __name__ == "__main__":
    unittest.main()
