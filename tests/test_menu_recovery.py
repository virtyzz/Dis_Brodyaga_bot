import os
import unittest
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock, patch

import discord

os.environ.setdefault("DISCORD_BOT_TOKEN", "local-tests-only")
import main


def component(custom_id, message_id=123):
    return SimpleNamespace(
        type=discord.InteractionType.component,
        data={"component_type": 2, "custom_id": custom_id},
        message=SimpleNamespace(id=message_id, interaction_metadata=None, interaction=None),
        user=SimpleNamespace(id=7),
        response=SimpleNamespace(is_done=Mock(return_value=False), defer=AsyncMock()),
        edit_original_response=AsyncMock(),
    )


class MenuRecoveryTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.client = main.BrodyagaBot(command_prefix="!", intents=discord.Intents.none())

    async def asyncTearDown(self):
        for view in self.client.persistent_views:
            view.stop()
        await self.client.close()

    async def initialize(self):
        with patch.object(main, "init_database") as initialize, patch.object(main, "ensure_current_cycle"):
            await self.client.setup_hook()
        initialize.assert_called_once()

    async def test_startup_registers_all_old_main_buttons_before_ready(self):
        old = main.MainMenuV2View()
        custom_ids = [item.custom_id for item in old.walk_children()
                      if isinstance(item, discord.ui.Button)]
        await self.initialize()
        for custom_id in custom_ids:
            self.assertTrue(main.has_component_handler(self.client, component(custom_id)))

    async def test_fresh_process_dispatches_old_message_to_current_callback(self):
        await self.initialize()
        view = self.client.persistent_views[0]
        button = next(item for item in view.walk_children()
                      if isinstance(item, discord.ui.Button) and item.custom_id == "main_menu_share_bot")
        called = AsyncMock()
        button.callback = called
        event = component(button.custom_id, message_id=99999)
        self.client._connection._view_store.dispatch_view(2, button.custom_id, event)
        import asyncio
        await asyncio.sleep(0)
        called.assert_awaited_once_with(event)

    async def test_active_private_view_is_not_recovered_or_answered_twice(self):
        view = discord.ui.View(timeout=300)
        button = discord.ui.Button(label="Сохранить")
        view.add_item(button)
        self.client._connection.store_view(view, 123)
        try:
            event = component(button.custom_id)
            with patch.object(main, "bot", self.client):
                await main.on_interaction(event)
            event.response.defer.assert_not_awaited()
            event.edit_original_response.assert_not_awaited()
        finally:
            view.stop()

    async def test_expired_private_view_recovers_to_menu_without_replaying_save(self):
        view = discord.ui.View(timeout=300)
        button = discord.ui.Button(label="Сохранить")
        view.add_item(button)
        self.client._connection.store_view(view, 123)
        view.stop()
        event = component(button.custom_id)
        with patch.object(main, "bot", self.client):
            await main.on_interaction(event)
        event.response.defer.assert_awaited_once_with(ephemeral=True, thinking=True)
        menu = event.edit_original_response.call_args.kwargs["view"]
        self.assertTrue(menu.is_persistent())
        self.assertIn("Предыдущее окно устарело", str(menu.to_components()))
        self.assertIn("main_menu_subscriptions", str(menu.to_components()))

    async def test_unknown_buttons_after_restart_get_private_menu(self):
        await self.initialize()
        event = component("random-old-component-id")
        with patch.object(main, "bot", self.client):
            await main.on_interaction(event)
        event.edit_original_response.assert_awaited_once()

    async def test_slash_commands_and_already_answered_clicks_are_untouched(self):
        event = component("unknown")
        with patch.object(main, "bot", self.client):
            event.type = discord.InteractionType.application_command
            await main.on_interaction(event)
            event.type = discord.InteractionType.component
            event.response.is_done.return_value = True
            await main.on_interaction(event)
        event.response.defer.assert_not_awaited()


if __name__ == "__main__":
    unittest.main()
