import asyncio
import io
import json
import logging
import os
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock, patch

import aiohttp
from aiohttp import web
import discord
from discord.webhook.async_ import AsyncWebhookAdapter

import activity_logging as activity
import discord_diagnostics as diagnostics
from interaction_utils import acknowledge


class DiagnosticsTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.stream = io.StringIO()
        self.handler = logging.StreamHandler(self.stream)
        self.old_handlers = diagnostics.logger.handlers[:]
        diagnostics.logger.handlers = [self.handler]
        self.token = activity.context.set(None)

    async def asyncTearDown(self):
        diagnostics.shutdown()
        diagnostics.logger.handlers = self.old_handlers
        activity.context.reset(self.token)
        self.handler.close()

    def records(self):
        return [json.loads(line) for line in self.stream.getvalue().splitlines()]

    def interaction(self, defer):
        return SimpleNamespace(
            id=123, user=SimpleNamespace(id=7), guild=None, channel=None,
            created_at=discord.utils.utcnow(), message=SimpleNamespace(id=77),
            data={"custom_id": "main_menu_check_trader"},
            client=SimpleNamespace(latency=.12),
            response=SimpleNamespace(is_done=Mock(return_value=False), defer=defer),
        )

    async def test_real_http_retry_connection_reuse_and_expired_ack_are_correlated_and_redacted(self):
        count = 0

        async def respond(request):
            nonlocal count
            count += 1
            response = web.json_response({"message": "response-secret", "code": 10062},
                                         status=500 if count == 1 else 404)
            response.headers["Content-Type"] = "application/json"
            return response

        app = web.Application()
        app.router.add_post("/api/v10/interactions/123/url-secret/callback", respond)
        runner = web.AppRunner(app, access_log=None)
        await runner.setup()
        # Port selected by the OS; no external Discord requests.
        import socket
        sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        sock.bind(("127.0.0.1", 0))
        port = sock.getsockname()[1]
        site = web.SockSite(runner, sock)
        await site.start()
        try:
            async with aiohttp.ClientSession(trace_configs=[diagnostics.create_http_trace()]) as session:
                route = SimpleNamespace(method="POST", webhook_id=123, webhook_token="url-secret",
                                        url=f"http://localhost:{port}/api/v10/interactions/123/url-secret/callback?query-secret=1")
                adapter = AsyncWebhookAdapter()

                async def defer(**kwargs):
                    await adapter.request(route, session, payload={"content": "payload-secret"}, auth_token="authorization-secret")

                with self.assertLogs("interaction_utils", level="WARNING"):
                    self.assertFalse(await acknowledge(self.interaction(defer)))
        finally:
            await runner.cleanup()
        records = self.records()
        results = [record for record in records if record["event"] == "discord_http_result"]
        self.assertEqual([record["status"] for record in results], [500, 404])
        self.assertEqual([record["attempt"] for record in results], [1, 2])
        self.assertFalse(results[0]["connection_reused"])
        self.assertTrue(results[1]["connection_reused"])
        stages = {record.get("stage") for record in records}
        self.assertIn("connection_create_end", stages)
        self.assertIn("dns_end", stages)
        self.assertIn("headers_sent", stages)
        self.assertIn("response_body_read", stages)
        summary = records[-1]
        self.assertEqual(summary["event"], "ack_result")
        self.assertEqual(summary["http_attempts"], 2)
        self.assertEqual(summary["discord_error_code"], 10062)
        self.assertEqual(summary["outcome"], "rejected")
        self.assertTrue(all(record["request_id"] == "123" for record in records))
        self.assertNotIn("secret", self.stream.getvalue())
        self.assertIsNone(diagnostics.ack_context.get())

    async def test_success_and_transport_failure_always_emit_ack_summary(self):
        self.assertTrue(await acknowledge(self.interaction(AsyncMock())))
        self.assertEqual(self.records()[-1]["outcome"], "acknowledged")
        event = self.interaction(AsyncMock(side_effect=OSError("exception-secret")))
        with self.assertRaises(OSError):
            await acknowledge(event)
        self.assertEqual(self.records()[-1]["error_type"], "OSError")
        self.assertNotIn("exception-secret", self.stream.getvalue())
        self.assertIsNone(diagnostics.ack_context.get())

    async def test_request_exception_is_redacted_and_nonfinite_latency_is_safe(self):
        trace = diagnostics.create_http_trace()
        ctx = trace.trace_config_ctx()
        from yarl import URL
        state = {"started": diagnostics.time.monotonic(), "attempts": 0}
        token = diagnostics.ack_context.set(state)
        try:
            for callback in trace.on_request_start:
                await callback(None, ctx, SimpleNamespace(url=URL("https://discord.com/api/v10/interactions/1/url-secret/callback")))
            for callback in trace.on_request_exception:
                await callback(None, ctx, SimpleNamespace(exception=OSError(54, "exception-secret")))
        finally:
            diagnostics.ack_context.reset(token)
        self.assertEqual(self.records()[-1]["errno"], 54)
        self.assertNotIn("secret", self.stream.getvalue())
        event = self.interaction(AsyncMock())
        event.client.latency = float("inf")
        self.assertTrue(await acknowledge(event))
        self.assertIsNone(self.records()[-2]["gateway_latency_ms"])

    async def test_file_writer_drains_queue_and_uses_daily_technical_file(self):
        with tempfile.TemporaryDirectory() as directory, patch.dict(os.environ, {"BRODYAGA_LOG_DIR": directory}):
            diagnostics.configure_file()
            diagnostics.configure_file()
            diagnostics.emit("test_diagnostic", note="Проверка\nстроки")
            diagnostics.shutdown()
            path = Path(directory) / "bot-technical.jsonl"
            records = [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines()]
            self.assertEqual([record["event"] for record in records], ["diagnostics_enabled", "test_diagnostic"])
            self.assertFalse((Path(directory) / "bot-activity.jsonl").exists())

    async def test_bot_installs_trace_and_stops_watchdog_on_close(self):
        os.environ.setdefault("DISCORD_BOT_TOKEN", "local-tests-only")
        import main
        client = main.BrodyagaBot(command_prefix="!", intents=discord.Intents.none())
        try:
            self.assertIsInstance(client.http.http_trace, aiohttp.TraceConfig)
            with patch.object(main, "init_database"), patch.object(main, "ensure_current_cycle"):
                await client.setup_hook()
            task = client._diagnostic_task
            self.assertFalse(task.done())
        finally:
            await client.close()
        self.assertTrue(task.done())
