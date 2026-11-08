import asyncio
import os
import sqlite3
import threading
import unittest
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock, patch

os.environ.setdefault("DISCORD_BOT_TOKEN", "local-tests-only")
import main
from coords_handler import SERVERS, get_all_locations


def interaction(user_id=1):
    done = {"value": False}

    def defer(**kwargs):
        done["value"] = True

    return SimpleNamespace(
        user=SimpleNamespace(id=user_id),
        response=SimpleNamespace(
            is_done=Mock(side_effect=lambda: done["value"]),
            defer=AsyncMock(side_effect=defer), send_message=AsyncMock(),
            edit_message=AsyncMock(),
        ),
        followup=SimpleNamespace(send=AsyncMock()),
        edit_original_response=AsyncMock(),
    )


class UIReliabilityTests(unittest.IsolatedAsyncioTestCase):
    async def test_ten_reporters_and_remaining_count(self):
        names = [f"player{i}" for i in range(15)]
        text = main.format_reporters(names)
        self.assertIn("player9", text)
        self.assertNotIn("player10", text)
        self.assertIn("ещё 5 игроков", text)
        self.assertIn("**player0**", text)

    async def test_five_cards_of_long_escaped_names_fit_discord_budget(self):
        reports = {server: {} for server in SERVERS}
        for location in get_all_locations()[:5]:
            reports[SERVERS[0]][location] = {
                "x": 12345, "y": 12345,
                "users": ["*_~`|\\" * 16 + str(i) for i in range(130)],
            }
        view = main.TraderLocationsV2View(1, notice="Отметка сохранена", reports=reports)
        self.assertLessEqual(view.content_length(), 4000)
        self.assertLessEqual(view.total_children_count, 40)
        self.assertIn("ещё 120 игроков", str(view.to_components()))

    async def test_view_constructors_do_not_read_database(self):
        with patch.object(main, "get_grouped_trader_reports", side_effect=AssertionError("sync DB")), \
             patch.object(main, "get_location_check_summary", side_effect=AssertionError("sync DB")), \
             patch.object(main, "get_trader_report_summary", side_effect=AssertionError("sync DB")), \
             patch.object(main, "get_server_trader_statuses", side_effect=AssertionError("sync DB")):
            main.TraderLocationsV2View(1)
            main.LocationStatusV2View(1, SERVERS[0])
            main.SearchServerSelectV2View(1, True)

    async def test_slow_database_read_does_not_block_event_loop(self):
        started = threading.Event()
        release = threading.Event()

        def blocked_read():
            started.set()
            if not release.wait(2):
                raise AssertionError("event loop did not release blocked DB read")
            return {server: {} for server in SERVERS}

        with patch.object(main, "get_grouped_trader_reports", side_effect=blocked_read):
            task = asyncio.create_task(main.TraderLocationsV2View.create(1))
            try:
                for _ in range(100):
                    if started.is_set():
                        break
                    await asyncio.sleep(.005)
                self.assertTrue(started.is_set())
                self.assertFalse(task.done())
            finally:
                release.set()
                await task

    async def test_report_acknowledged_before_write_and_cooldown_is_friendly(self):
        event = interaction()
        view = main.TraderReportConfirmationView(1, SERVERS[0], get_all_locations()[0])

        def blocked_by_cooldown(*args):
            event.response.defer.assert_awaited_once()
            raise main.ReportCooldownError(20)

        with patch.object(main, "add_trader_report", side_effect=blocked_by_cooldown):
            await view._confirm_callback(event)
        self.assertIn("20", event.followup.send.call_args.args[0])
        event.edit_original_response.assert_not_awaited()

    async def test_daily_task_keeps_running_after_database_error(self):
        with patch.object(main, "ensure_current_cycle", side_effect=sqlite3.OperationalError("locked")):
            with self.assertLogs(main.logger, level="ERROR"):
                await main.archive_daily.coro()

    async def test_delivery_task_keeps_running_after_database_error(self):
        with patch.object(main, "send_pending_notifications", AsyncMock(side_effect=sqlite3.OperationalError("locked"))):
            with self.assertLogs(main.logger, level="ERROR"):
                await main.subscription_notifications.coro()


if __name__ == "__main__":
    unittest.main()
