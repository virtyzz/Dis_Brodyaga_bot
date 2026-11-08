"""Private Components V2 subscription settings and queued trader alerts."""

import asyncio
import logging
import sqlite3
from weakref import WeakValueDictionary

import discord
from interaction_utils import acknowledge

from coords_handler import (
    SERVERS, get_server_display_name, get_brodyaga_map_url,
    get_all_locations, get_screenshot_path,
)
from database import (
    get_subscriptions, set_subscriptions,
    get_pending_notifications, finish_notification,
    claim_notification, notification_is_current,
)

logger = logging.getLogger(__name__)
_user_locks = WeakValueDictionary()
DM_TIMEOUT = 30


def _user_lock(user_id: int) -> asyncio.Lock:
    lock = _user_locks.get(user_id)
    if lock is None:
        lock = asyncio.Lock()
        _user_locks[user_id] = lock
    return lock


def server_list(servers) -> str:
    return ", ".join(get_server_display_name(s) for s in SERVERS if s in servers)


class SubscriptionView(discord.ui.LayoutView):
    def __init__(self, user_id: int, selected=None, notice: str | None = None,
                 retry: bool = False, *, current=()):
        super().__init__(timeout=300)
        self.user_id = user_id
        # Construct only from a snapshot; create() loads SQLite off the event
        # loop before constructing Discord UI components on the event loop.
        current = set(current)
        self.current = current
        self.selected = set(current if selected is None else selected)
        container = discord.ui.Container(
            discord.ui.TextDisplay("# Подписки"),
            discord.ui.TextDisplay(
                f"**Активные серверы:** {server_list(current) or 'нет'}\n\n"
                "Выберите серверы и нажмите «Сохранить». Уведомления будут "
                "приходить в личные сообщения. При сохранении подписки бот "
                "отправит проверочное сообщение.\n"
                "Уведомляем о первой отметке каждой новой локации; повторные "
                "подтверждения не рассылаются. Сброс отметок — в 04:00 МСК.\n"
                "Чтобы отключить все уведомления, нажмите «Отписаться от всех»."
            ),
        )
        if notice:
            container.add_item(discord.ui.TextDisplay(notice))
        select = discord.ui.Select(
            placeholder="Выберите серверы для уведомлений",
            min_values=0,
            max_values=len(SERVERS),
            options=[discord.SelectOption(
                label=get_server_display_name(server), value=server,
                default=server in self.selected,
            ) for server in SERVERS],
        )

        async def select_servers(interaction: discord.Interaction):
            if not await acknowledge(interaction):
                return
            try:
                await interaction.edit_original_response(
                    view=await SubscriptionView.create(self.user_id, select.values)
                )
            except sqlite3.Error:
                self.selected = set(select.values)
                logger.exception("Cannot reload subscriptions for user %s", self.user_id)
                await self._database_error(interaction)

        select.callback = select_servers
        container.add_item(discord.ui.ActionRow(select))
        save = discord.ui.Button(
            label="Проверить снова" if retry else "Сохранить",
            style=discord.ButtonStyle.success,
        )
        unsubscribe = discord.ui.Button(
            label="Отписаться от всех", style=discord.ButtonStyle.danger,
        )
        cancel = discord.ui.Button(
            label="Отмена", style=discord.ButtonStyle.secondary,
            disabled=self.selected == self.current,
        )
        save.callback = self.save
        unsubscribe.callback = self.unsubscribe
        cancel.callback = self.cancel
        container.add_item(discord.ui.ActionRow(save, unsubscribe, cancel))
        self.add_item(container)

    @classmethod
    async def create(cls, user_id: int, selected=None, notice: str | None = None,
                     retry: bool = False):
        current = await asyncio.to_thread(get_subscriptions, user_id)
        return cls(user_id, selected, notice, retry, current=current)

    async def _database_error(self, interaction: discord.Interaction):
        await interaction.edit_original_response(view=SubscriptionView(
            self.user_id, self.selected,
            "**База данных временно недоступна.**\n"
            "Не удалось подтвердить сохранение изменений. "
            "Попробуйте ещё раз чуть позже.", retry=True, current=self.current,
        ))

    async def interaction_check(self, interaction: discord.Interaction) -> bool:
        if interaction.user.id != self.user_id:
            await interaction.response.send_message("Это не ваше меню.", ephemeral=True)
            return False
        return True

    async def save(self, interaction: discord.Interaction):
        if not await acknowledge(interaction):
            return
        try:
            await self._save(interaction)
        except sqlite3.Error:
            logger.exception("Cannot save subscriptions for user %s", self.user_id)
            await self._database_error(interaction)

    async def _save(self, interaction: discord.Interaction):
        async with _user_lock(self.user_id):
            if not self.selected:
                await asyncio.to_thread(set_subscriptions, self.user_id, [])
                await interaction.edit_original_response(view=await SubscriptionView.create(
                    self.user_id, notice="Все подписки отключены."
                ))
                return
            try:
                await asyncio.wait_for(interaction.user.send(
                    view=SubscriptionConfirmationView(self.selected),
                    allowed_mentions=discord.AllowedMentions.none(),
                ), timeout=DM_TIMEOUT)
            except discord.Forbidden:
                await interaction.edit_original_response(view=await SubscriptionView.create(
                    self.user_id, self.selected,
                    "**Не удалось отправить вам личное сообщение.**\n"
                    "1. Откройте меню Discord-сервера, на котором находится бот.\n"
                    "2. В настройках конфиденциальности разрешите личные сообщения "
                    "от участников сервера.\n"
                    "3. Если бот заблокирован — разблокируйте его.\n"
                    "4. Нажмите «Проверить снова».\n\n"
                    "Изменения пока не сохранены. Выбранные серверы сохранены "
                    "в этом меню; прежние подписки продолжают действовать.", retry=True,
                ))
                return
            except (discord.HTTPException, OSError, asyncio.TimeoutError):
                await interaction.edit_original_response(view=await SubscriptionView.create(
                    self.user_id, self.selected,
                    "**Discord временно не смог отправить сообщение.**\n"
                    "Нажмите «Проверить снова» чуть позже. "
                    "Изменения пока не сохранены, прежние подписки действуют.", retry=True,
                ))
                return
            await asyncio.to_thread(set_subscriptions, self.user_id, self.selected)
            await interaction.edit_original_response(view=await SubscriptionView.create(
                self.user_id, notice="Подписки сохранены. Проверочное сообщение отправлено в личку."
            ))

    async def unsubscribe(self, interaction: discord.Interaction):
        if not await acknowledge(interaction):
            return
        try:
            async with _user_lock(self.user_id):
                await asyncio.to_thread(set_subscriptions, self.user_id, [])
                await interaction.edit_original_response(view=await SubscriptionView.create(
                    self.user_id, notice="Все подписки отключены."
                ))
        except sqlite3.Error:
            logger.exception("Cannot unsubscribe user %s", self.user_id)
            await self._database_error(interaction)

    async def cancel(self, interaction: discord.Interaction):
        # Acknowledge before rebuilding the view (which reads SQLite).
        # Discord invalidates an interaction if its initial response is late.
        if not await acknowledge(interaction):
            return
        try:
            await interaction.edit_original_response(view=await SubscriptionView.create(
                self.user_id, notice="Несохранённые изменения отменены."
            ))
        except sqlite3.Error:
            logger.exception("Cannot reload subscriptions for user %s", self.user_id)
            await self._database_error(interaction)


class SubscriptionConfirmationView(discord.ui.LayoutView):
    def __init__(self, servers):
        super().__init__(timeout=None)
        self.add_item(discord.ui.Container(discord.ui.TextDisplay(
            "# Проверка уведомлений\n"
            "Личные сообщения доступны. Вы сохраняете подписки на серверы: "
            f"**{server_list(servers)}**.\n"
            "Статус сохранения указан в меню подписок. "
            "Изменить выбор или отписаться можно через «Подписки» в главном меню."
        )))


class NotificationSubscriptionsButton(discord.ui.Button):
    def __init__(self):
        super().__init__(label="Подписки", style=discord.ButtonStyle.secondary,
                         custom_id="trader_notification_subscriptions")

    async def callback(self, interaction: discord.Interaction):
        if not await acknowledge(interaction, ephemeral=interaction.guild is not None, thinking=True):
            return
        try:
            menu = await SubscriptionView.create(interaction.user.id)
        except sqlite3.Error:
            logger.exception("Cannot open subscriptions from notification")
            await interaction.edit_original_response(
                content="База данных временно недоступна. Нажмите «Подписки» ещё раз чуть позже."
            )
            return
        # A thinking response is a new message: retain the original alert and
        # its photo/map buttons while opening this user's subscription menu.
        await interaction.edit_original_response(view=menu)


class NotificationPhotoButton(discord.ui.Button):
    def __init__(self, location: str):
        super().__init__(label="Фото", style=discord.ButtonStyle.secondary,
                         custom_id=f"trader_notification_photo:{location}")
        self.location = location

    async def callback(self, interaction: discord.Interaction):
        if not await acknowledge(interaction, ephemeral=interaction.guild is not None, thinking=True):
            return
        screenshot = await asyncio.to_thread(get_screenshot_path, self.location)
        if not screenshot:
            await interaction.edit_original_response(content=f"Для {self.location} нет изображения.")
            return
        try:
            file = discord.File(screenshot, filename="location-preview.png")
            try:
                await interaction.edit_original_response(content=self.location, attachments=[file])
            finally:
                file.close()
        except OSError:
            logger.exception("Cannot read notification photo for %s", self.location)
            await interaction.edit_original_response(content="Не удалось открыть фото. Попробуйте чуть позже.")


def register_notification_views(bot):
    """Restore stateless shortcuts on alerts sent by previous bot processes."""
    controls = [NotificationSubscriptionsButton()]
    controls.extend(NotificationPhotoButton(location) for location in get_all_locations())
    for button in controls:
        view = discord.ui.View(timeout=None)
        view.add_item(button)
        bot.add_view(view)


class TraderNotificationView(discord.ui.LayoutView):
    def __init__(self, server: str, location: str, x: int, y: int):
        super().__init__(timeout=None)
        container = discord.ui.Container(discord.ui.TextDisplay(
            "# Сообщили о бродяге\n"
            f"**Сервер:** {get_server_display_name(server)}\n"
            f"**Локация:** {location}\n"
            f"**Координаты:** X: {x}, Y: {y}"
        ))
        actions = [NotificationPhotoButton(location)]
        map_url = get_brodyaga_map_url(location)
        if map_url:
            actions.append(discord.ui.Button(
                label="На карте", style=discord.ButtonStyle.link, url=map_url,
            ))
        actions.append(NotificationSubscriptionsButton())
        container.add_item(discord.ui.ActionRow(*actions))
        self.add_item(container)


async def send_pending_notifications(bot):
    """Bounded delivery batch; a closed DM never interrupts other recipients."""
    try:
        pending = await asyncio.to_thread(get_pending_notifications)
    except sqlite3.Error:
        logger.exception("Cannot read notification queue; will retry next pass")
        return
    for notification_id, user_id, server, location, x, y in pending:
        try:
            if not await asyncio.to_thread(claim_notification, notification_id):
                continue
            status = "pending"
            try:
                user = bot.get_user(user_id)
                if user is None:
                    user = await asyncio.wait_for(bot.fetch_user(user_id), timeout=DM_TIMEOUT)
                # Resolve a user before the final validity check: a fetch may
                # yield while a reset, unsubscribe or vote change cancels it.
                if not await asyncio.to_thread(notification_is_current, notification_id):
                    await asyncio.to_thread(finish_notification, notification_id, "cancelled")
                    continue
                await asyncio.wait_for(user.send(
                    view=TraderNotificationView(server, location, x, y),
                    allowed_mentions=discord.AllowedMentions.none(),
                    # Stable across retries. discord.py sets enforce_nonce,
                    # so Discord deduplicates recent ambiguous send attempts.
                    nonce=f"b{notification_id}",
                ), timeout=DM_TIMEOUT)
            except (discord.Forbidden, discord.NotFound):
                status = "failed"
                logger.info("Trader notification unavailable for user %s", user_id)
            except (discord.HTTPException, OSError, asyncio.TimeoutError):
                logger.warning("Temporary trader notification failure for user %s", user_id)
            else:
                status = "sent"
            await asyncio.to_thread(finish_notification, notification_id, status)
        except sqlite3.Error:
            # A failed acknowledgement is recovered through the row's lease.
            # Never stop the background loop because SQLite was briefly busy.
            logger.exception("Notification database failure for row %s; will retry", notification_id)
