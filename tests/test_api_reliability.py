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

    def test_checks_are_scoped_to_selected_server_without_private_fields(self):
        handler = self.handler()
        handler.path = "/checks?server=cherno-3"
        with patch.object(web_api, "get_location_check_summary", return_value=[("Test point", "2026-11-18", 2)]) as summary:
            handler.send_checks()
        summary.assert_called_once_with("cherno-3")
        handler.send_json.assert_called_once_with(HTTPStatus.OK, {
            "ok": True, "server": "cherno-3", "locations": ["Test point"]})

    def test_checks_reject_missing_unknown_and_duplicate_servers(self):
        for query in ["", "?server=unknown", "?server=cherno-1&server=cherno-2"]:
            handler = self.handler()
            handler.path = "/checks" + query
            with patch.object(web_api, "get_location_check_summary") as summary:
                handler.send_checks()
            summary.assert_not_called()
            self.assertEqual(handler.send_json.call_args.args[0], HTTPStatus.BAD_REQUEST)

    def test_checks_database_error_is_not_an_empty_success(self):
        handler = self.handler()
        handler.path = "/checks?server=cherno-1"
        with patch.object(web_api, "get_location_check_summary", side_effect=sqlite3.OperationalError("locked")):
            with self.assertLogs(web_api.LOGGER, level="ERROR"):
                handler.send_checks()
        self.assertEqual(handler.send_json.call_args.args[0], HTTPStatus.SERVICE_UNAVAILABLE)

    def test_create_check_uses_authenticated_actor_and_selected_server(self):
        handler = self.handler()
        handler.read_json.return_value = {"server": "cherno-4", "location_id": 1}
        with patch.object(web_api, "register_user"), patch.object(web_api, "add_location_check", return_value=True) as save:
            handler.create_check()
        save.assert_called_once_with("cherno-4", web_api.get_all_locations()[0], 7)
        self.assertEqual(handler.send_json.call_args.args[0], HTTPStatus.OK)

    def test_create_check_rejects_missing_auth_csrf_invalid_point_and_found_conflict(self):
        for case, expected in [("auth", 401), ("csrf", 403), ("point", 400), ("found", 409), ("database", 503)]:
            with self.subTest(case=case):
                handler = self.handler()
                if case == "auth":
                    handler.get_session.return_value = None
                if case == "csrf":
                    handler.headers = {}
                if case == "point":
                    handler.read_json.return_value = {"server": "cherno-1", "location_id": True}
                with patch.object(web_api, "register_user"), patch.object(web_api, "add_location_check", return_value=False, side_effect=sqlite3.OperationalError("locked") if case == "database" else None) as save:
                    if case == "database":
                        with self.assertLogs(web_api.LOGGER, level="ERROR"):
                            handler.create_check()
                    else:
                        handler.create_check()
                self.assertEqual(handler.send_json.call_args.args[0], expected)
                if case in {"auth", "csrf", "point"}:
                    save.assert_not_called()

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
