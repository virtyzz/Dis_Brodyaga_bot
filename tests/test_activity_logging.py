import asyncio
import io
import json
import logging
import os
import sqlite3
import tempfile
import unittest
from contextlib import closing
from datetime import datetime, timezone
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock, patch

import discord
import activity_logging as activity
import database as db
from coords_handler import SERVERS, get_all_locations

os.environ.setdefault("DISCORD_BOT_TOKEN", "local-tests-only")
import main
import web_api


def click(user_id=7, guild_id=8):
    return SimpleNamespace(
        id=100 + user_id, user=SimpleNamespace(id=user_id),
        guild=SimpleNamespace(id=guild_id, name="Community\nnext line") if guild_id else None,
        channel=SimpleNamespace(id=9, name="bot"),
        data={"custom_id": "select-point", "component_type": 3, "values": ["point-1"]},
        message=None, type=discord.InteractionType.component,
        response=SimpleNamespace(is_done=Mock(return_value=True)),
    )


class CaptureActivity:
    def setUp(self):
        super().setUp()
        self.stream = io.StringIO()
        self.handler = logging.StreamHandler(self.stream)
        self.old_handlers = activity.logger.handlers[:]
        activity.logger.handlers = [self.handler]
        self.context_token = activity.context.set(None)

    def tearDown(self):
        activity.context.reset(self.context_token)
        activity.logger.handlers = self.old_handlers
        self.handler.close()
        super().tearDown()

    def records(self):
        return [json.loads(line) for line in self.stream.getvalue().splitlines()]


class ActivityTests(CaptureActivity, unittest.TestCase):
    def test_daily_file_rotation_keeps_archives_and_separates_services(self):
        with tempfile.TemporaryDirectory() as directory, patch.dict(
            os.environ, {"BRODYAGA_LOG_DIR": str(Path(directory) / "logs")}
        ):
            bot_handler = activity.configure_activity_file("bot")
            api_handler = None
            try:
                self.assertIs(activity.configure_activity_file("bot"), bot_handler)
                activity.emit("before_midnight", action="Проверка")
                midnight = int(datetime(2026, 11, 15, tzinfo=timezone.utc).timestamp())
                bot_handler.rolloverAt = midnight
                old_archive = Path(bot_handler.baseFilename + ".2020-01-01")
                old_archive.write_text("old archive", encoding="utf-8")
                with patch("logging.handlers.time.time", return_value=midnight + 1):
                    activity.emit("after_midnight")
                archived = Path(bot_handler.baseFilename + ".2026-11-14")
                self.assertEqual(json.loads(archived.read_text(encoding="utf-8"))["event"], "before_midnight")
                self.assertEqual(json.loads(Path(bot_handler.baseFilename).read_text(encoding="utf-8"))["event"], "after_midnight")
                self.assertTrue(old_archive.exists())
                self.assertTrue(bot_handler.utc)
                self.assertEqual(bot_handler.interval, 86400)
                activity.logger.removeHandler(bot_handler)
                bot_handler.close()
                api_handler = activity.configure_activity_file("api")
                activity.emit("api_event")
                self.assertNotEqual(api_handler.baseFilename, bot_handler.baseFilename)
                self.assertEqual(json.loads(Path(api_handler.baseFilename).read_text(encoding="utf-8"))["event"], "api_event")
            finally:
                for handler in (bot_handler, api_handler):
                    if handler is not None:
                        activity.logger.removeHandler(handler)
                        handler.close()

    def test_file_survives_reopening_without_duplicate_handlers(self):
        with tempfile.TemporaryDirectory() as directory, patch.dict(
            os.environ, {"BRODYAGA_LOG_DIR": directory}
        ):
            handler = activity.configure_activity_file("bot")
            activity.emit("first_process")
            activity.logger.removeHandler(handler)
            handler.close()
            handler = activity.configure_activity_file("bot")
            try:
                activity.emit("second_process")
                records = [json.loads(line) for line in Path(handler.baseFilename).read_text(encoding="utf-8").splitlines()]
                self.assertEqual([record["event"] for record in records], ["first_process", "second_process"])
            finally:
                activity.logger.removeHandler(handler)
                handler.close()

    def test_single_json_line_and_stable_ids_without_message_secrets(self):
        event = click()
        event.content = "message-secret"
        event.token = "interaction-secret"
        activity.emit("interaction_received", details=activity.discord_context(event))
        record, = self.records()
        self.assertEqual(record["guild_name"], "Community\nnext line")
        self.assertEqual(record["user_id"], "7")
        self.assertEqual(record["guild_id"], "8")
        self.assertEqual(record["source"], "guild")
        self.assertNotIn("secret", self.stream.getvalue())
        self.assertEqual(activity.discord_context(click(guild_id=None))["source"], "dm")

    def test_nested_components_v2_resolve_labels_without_logging_message_content(self):
        event = click()
        event.message = SimpleNamespace(components=[SimpleNamespace(to_dict=lambda: {
            "type": 17, "components": [{"type": 10, "content": "private text"},
                {"type": 9, "accessory": {"type": 3, "custom_id": "select-point",
                 "placeholder": "Выберите точку", "options": [
                     {"value": "point-1", "label": "Березино"}]}}]})])
        metadata = activity.component_metadata(event)
        self.assertEqual(metadata["action"], "Выберите точку")
        self.assertEqual(metadata["selected_labels"], ["Березино"])
        self.assertNotIn("private text", str(metadata))

    def test_write_results_are_after_commit_and_errors_are_not_success(self):
        with tempfile.TemporaryDirectory() as directory:
            path = str(Path(directory) / "test.db")
            with patch.object(db, "DB_PATH", path):
                db.init_database()
                activity.bind_discord(click())
                db.register_user(7, "player")
                with closing(sqlite3.connect(path)) as connection:
                    self.assertEqual(connection.execute("SELECT user_id FROM users").fetchone()[0], 7)
                db.set_subscriptions(7, (server for server in SERVERS[:2]))
                self.assertEqual(db.get_subscriptions(7), set(SERVERS[:2]))
                db.add_trader_report(SERVERS[0], get_all_locations()[0], 1, 2, 7)
                self.assertFalse(db.add_location_check(SERVERS[0], get_all_locations()[0], 7))
                with self.assertRaises(db.ReportCooldownError):
                    db.add_trader_report(SERVERS[0], get_all_locations()[1], 1, 2, 7)
            with patch.object(db.sqlite3, "connect", side_effect=sqlite3.OperationalError("private error text")):
                with self.assertRaises(sqlite3.OperationalError):
                    db.register_user(7, "player")
        records = self.records()
        self.assertTrue(all(record["request_id"] == "107" for record in records))
        self.assertEqual(records[3]["applied"], False)
        self.assertEqual(records[-2]["error_type"], "ReportCooldownError")
        self.assertEqual(records[-1]["outcome"], "error")
        self.assertNotIn("private error text", self.stream.getvalue())

    def test_http_result_excludes_oauth_query_and_unknown_paths(self):
        handler = object.__new__(web_api.ApiHandler)
        handler.command = "GET"
        handler.path = "/auth/callback?code=secret-code&state=secret-state"
        handler.begin_activity()
        handler.log_request(302)
        handler.path = "/arbitrary-secret-path"
        handler.log_request(404)
        handler.path = "/health"
        handler.log_request(200)
        records = self.records()
        self.assertEqual(len(records), 2)
        self.assertEqual(records[0]["source"], "map")
        self.assertEqual(records[0]["action"], "GET /auth/callback")
        self.assertEqual(records[1]["action"], "GET unknown")
        self.assertNotIn("secret", self.stream.getvalue())

    def test_authenticated_map_report_has_same_request_id_as_http_result(self):
        import time
        session = {"user_id": "7", "username": "player", "csrf": "csrf-secret",
                   "expires": time.time() + 60}
        handler = object.__new__(web_api.ApiHandler)
        handler.command = "POST"
        handler.path = "/reports"
        handler.read_json = Mock(return_value={"server": SERVERS[0], "location_id": 1})
        handler.send_json = lambda status, payload: handler.log_request(status)
        with tempfile.TemporaryDirectory() as directory, patch.object(
            db, "DB_PATH", str(Path(directory) / "test.db")
        ), patch.object(web_api, "SESSION_SECRET", "signing-secret"), patch.dict(
            web_api.SESSIONS, {"test-session": session}
        ):
            db.init_database()
            signature = web_api.sign_session("test-session")
            handler.headers = {"Cookie": f"{web_api.COOKIE_NAME}=test-session.{signature}",
                               "X-CSRF-Token": "csrf-secret"}
            handler.begin_activity()
            handler.create_report()
        records = self.records()
        self.assertEqual([record["event"] for record in records],
                         ["action_result", "action_result", "http_result"])
        self.assertEqual(len({record["request_id"] for record in records}), 1)
        self.assertTrue(all(record["source"] == "map" and record["user_id"] == "7" for record in records))
        self.assertEqual(records[-1]["status"], 200)
        self.assertNotIn("secret", self.stream.getvalue())
        self.assertNotIn("test-session", self.stream.getvalue())
        handler.begin_activity()
        self.assertIsNone(activity.context.get()["user_id"])


class AsyncActivityTests(CaptureActivity, unittest.IsolatedAsyncioTestCase):
    async def test_context_isolated_between_users_and_copied_to_database_threads(self):
        async def action(event):
            from interaction_utils import acknowledge
            await acknowledge(event)
            await asyncio.sleep(0)
            await asyncio.to_thread(activity.emit, "thread_result")
        await asyncio.gather(action(click(7, 8)), action(click(10, None)))
        records = {record["user_id"]: record for record in self.records()}
        self.assertEqual(records["7"]["source"], "guild")
        self.assertEqual(records["7"]["request_id"], "107")
        self.assertEqual(records["10"]["source"], "dm")
        self.assertIsNone(records["10"]["guild_id"])
        self.assertIsNone(activity.context.get())

    async def test_received_click_logged_once_even_if_already_acknowledged(self):
        await main.on_interaction(click())
        record, = self.records()
        self.assertEqual(record["event"], "interaction_received")

    async def test_prefix_command_is_logged_with_source_and_context_is_reset(self):
        event = click()
        event.author = event.user
        event.content = "!start private-argument"
        ctx = SimpleNamespace(prefix="!", message=event, command=SimpleNamespace(qualified_name="start"))
        client = main.BrodyagaBot(command_prefix="!", intents=discord.Intents.none())
        try:
            async def invoke(_ctx):
                await asyncio.to_thread(activity.emit, "test_command_work")
            with patch("discord.ext.commands.Bot.invoke", side_effect=invoke):
                await client.invoke(ctx)
        finally:
            await client.close()
        received, work = self.records()
        self.assertEqual(received["action"], "start")
        self.assertEqual(received["request_id"], work["request_id"])
        self.assertIsNone(activity.context.get())
        self.assertNotIn("private-argument", self.stream.getvalue())
