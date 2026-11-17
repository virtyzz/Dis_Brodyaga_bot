import os
import tempfile
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path
from unittest.mock import AsyncMock, patch
from types import SimpleNamespace

os.environ.setdefault("DISCORD_BOT_TOKEN", "local-tests-only")
import database as db
import main
from coords_handler import SERVERS, get_all_locations


class CommentTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.path = patch.object(db, "DB_PATH", str(Path(self.temp.name) / "test.db"))
        self.path.start()
        db.init_database()
        self.server, self.location = SERVERS[0], get_all_locations()[0]
        db.add_trader_report(self.server, self.location, 1, 2, 9)

    def tearDown(self):
        self.path.stop()
        self.temp.cleanup()

    async def test_shared_persistent_comments_are_scoped_to_location_and_server(self):
        cycle, rows = db.get_trader_comments(self.server, self.location)
        self.assertEqual(rows, [])
        self.assertTrue(db.add_trader_comment(self.server, self.location, 10, "  Второй этаж  ", cycle))
        db.init_database()
        self.assertEqual(db.get_trader_comments(self.server, self.location)[1][0][:2], (10, "Второй этаж"))
        self.assertEqual(db.get_trader_comments(SERVERS[1], self.location)[1], [])
        self.assertEqual(db.get_trader_comments(self.server, "other")[1], [])

    async def test_reset_clears_comments_and_rejects_stale_form(self):
        cycle, _ = db.get_trader_comments(self.server, self.location)
        db.add_trader_comment(self.server, self.location, 10, "Внутри", cycle)
        db.ensure_current_cycle(datetime.now(timezone.utc) + timedelta(days=1))
        self.assertEqual(db.get_trader_comments(self.server, self.location)[1], [])
        self.assertFalse(db.add_trader_comment(self.server, self.location, 10, "Внутри", cycle))

    async def test_invalid_text_and_missing_report_are_not_saved(self):
        cycle, _ = db.get_trader_comments(self.server, self.location)
        for text in ("   ", "a" * 601):
            with self.assertRaises(ValueError):
                db.add_trader_comment(self.server, self.location, 10, text, cycle)
        self.assertFalse(db.add_trader_comment(self.server, "missing", 10, "test", cycle))

    async def test_worst_case_report_page_fits_and_has_comment_counts(self):
        reports = {server: {} for server in SERVERS}
        for location in get_all_locations()[:15]:
            reports[self.server][location] = {"x": 1, "y": 2, "users": ["player"]}
        view = main.TraderLocationsV2View(1, page=1, notice="Сохранено", reports=reports)
        self.assertEqual(view.total_children_count, 40)
        buttons = [item for item in view.walk_children() if isinstance(item, main.TraderCommentsButton)]
        self.assertEqual(len(buttons), 5)
        self.assertTrue(all(button.label == "(0)" and str(button.emoji) == "💬" for button in buttons))

    async def test_comment_count_button_uses_saved_comments(self):
        db.register_user(9, "player")
        cycle, _ = db.get_trader_comments(self.server, self.location)
        for author in (10, 11):
            db.add_trader_comment(self.server, self.location, author, "Внутри", cycle)
        view = await main.TraderLocationsV2View.create(1)
        button = next(item for item in view.walk_children() if isinstance(item, main.TraderCommentsButton))
        self.assertEqual(button.label, "(2)")
        db.ensure_current_cycle(datetime.now(timezone.utc) + timedelta(days=1))
        self.assertEqual(db.get_trader_comment_counts(), {})

    async def test_comments_pagination_fits_with_maximum_escaped_text(self):
        comments = [(10, "*_" * 300, 123) for _ in range(8)]
        view = main.TraderCommentsView(1, self.server, self.location, "cycle", comments, page=1)
        self.assertLessEqual(view.content_length(), 4000)
        self.assertLessEqual(view.total_children_count, 40)
        text = str(view.to_components())
        self.assertIn("Назад", text)
        self.assertIn("Вперёд", text)
        self.assertIsInstance(main.TraderCommentModal(view), main.discord.ui.Modal)

    async def test_modal_acknowledges_before_saving_and_returns_shared_comments(self):
        view = await main.TraderCommentsView.create(1, self.server, self.location)
        modal = main.TraderCommentModal(view)
        modal.body._value = "В комнате справа"
        event = SimpleNamespace(
            user=SimpleNamespace(id=1),
            followup=SimpleNamespace(send=AsyncMock()),
            edit_original_response=AsyncMock(),
        )
        async def acknowledged(*args, **kwargs):
            return True
        with patch.object(main, "acknowledge", AsyncMock(side_effect=acknowledged)) as ack:
            await modal.on_submit(event)
        ack.assert_awaited_once()
        self.assertEqual(db.get_trader_comments(self.server, self.location)[1][0][1], "В комнате справа")
        event.edit_original_response.assert_awaited_once()
        self.assertIsInstance(event.edit_original_response.call_args.kwargs["view"], main.TraderCommentsView)

    async def test_other_user_cannot_use_private_comment_view(self):
        view = await main.TraderCommentsView.create(1, self.server, self.location)
        event = SimpleNamespace(user=SimpleNamespace(id=2), response=SimpleNamespace(send_message=AsyncMock()))
        self.assertFalse(await view.interaction_check(event))
        event.response.send_message.assert_awaited_once()

    async def test_confirmation_hidden_only_for_users_own_exact_report(self):
        db.register_user(9, "same-name")
        db.register_user(10, "same-name")
        own = await main.TraderLocationsV2View.create(9)
        other = await main.TraderLocationsV2View.create(10)
        self.assertFalse(any(isinstance(item, main.TraderLocationConfirmButton) for item in own.walk_children()))
        self.assertTrue(any(isinstance(item, main.TraderLocationConfirmButton) for item in other.walk_children()))
        reports = {server: {} for server in SERVERS}
        for server, location in ((self.server, self.location), (self.server, "other"), (SERVERS[1], self.location)):
            reports[server][location] = {"x": 1, "y": 2, "users": ["player"]}
        view = main.TraderLocationsV2View(9, reports=reports, confirmed_locations=db.get_user_trader_locations(9))
        buttons = [item for item in view.walk_children() if isinstance(item, main.TraderLocationConfirmButton)]
        self.assertEqual({(item.server, item.location_name) for item in buttons}, {(self.server, "other"), (SERVERS[1], self.location)})
        db.ensure_current_cycle(datetime.now(timezone.utc) + timedelta(days=1))
        self.assertEqual(db.get_user_trader_locations(9), set())
