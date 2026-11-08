"""Private Components V2 subscription settings and queued trader alerts."""

import asyncio
import logging

import discord

from coords_handler import SERVERS, get_server_display_name, get_brodyaga_map_url
from database import (
    get_subscriptions, set_subscriptions,
    get_pending_notifications, finish_notification,
)

logger = logging.getLogger(__name__)
_save_lock = asyncio.Lock()


def server_list(servers) -> str:
    return ", ".join(get_server_display_name(s) for s in SERVERS if s in servers)


class SubscriptionView(discord.ui.LayoutView):
    def __init__(self, user_id: int, selected=None, notice: str | None = None,
                 retry: bool = False):
        super().__init__(timeout=300)
        self.user_id = user_id
        current = get_subscriptions(user_id)
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
            await interaction.response.defer()
            await interaction.edit_original_response(
                view=SubscriptionView(self.user_id, select.values)
            )

        select.callback = select_servers
        container.add_item(discord.ui.ActionRow(select))
        save = discord.ui.Button(
            label="Проверить снова" if retry else "Сохранить",
            style=discord.ButtonStyle.success,
        )
        unsubscribe = discord.ui.Button(
            label="Отписаться от всех", style=discord.ButtonStyle.danger,
        )
        cancel = discord.ui.Button(label="Отмена", style=discord.ButtonStyle.secondary)
        save.callback = self.save
        unsubscribe.callback = self.unsubscribe
        cancel.callback = self.cancel
        container.add_item(discord.ui.ActionRow(save, unsubscribe, cancel))
        self.add_item(container)

    async def interaction_check(self, interaction: discord.Interaction) -> bool:
        if interaction.user.id != self.user_id:
            await interaction.response.send_message("Это не ваше меню.", ephemeral=True)
            return False
        return True

    async def save(self, interaction: discord.Interaction):
        await interaction.response.defer()
        async with _save_lock:
            if not self.selected:
                set_subscriptions(self.user_id, [])
                await interaction.edit_original_response(view=SubscriptionView(
                    self.user_id, notice="Все подписки отключены."
                ))
                return
            try:
                await interaction.user.send(
                    view=SubscriptionConfirmationView(self.selected),
                    allowed_mentions=discord.AllowedMentions.none(),
                )
            except discord.Forbidden:
                await interaction.edit_original_response(view=SubscriptionView(
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
            except discord.HTTPException:
                await interaction.edit_original_response(view=SubscriptionView(
                    self.user_id, self.selected,
                    "**Discord временно не смог отправить сообщение.**\n"
                    "Нажмите «Проверить снова» чуть позже. "
                    "Изменения пока не сохранены, прежние подписки действуют.", retry=True,
                ))
                return
            set_subscriptions(self.user_id, self.selected)
            await interaction.edit_original_response(view=SubscriptionView(
                self.user_id, notice="Подписки сохранены. Проверочное сообщение отправлено в личку."
            ))

    async def unsubscribe(self, interaction: discord.Interaction):
        await interaction.response.defer()
        async with _save_lock:
            set_subscriptions(self.user_id, [])
            await interaction.edit_original_response(view=SubscriptionView(
                self.user_id, notice="Все подписки отключены."
            ))

    async def cancel(self, interaction: discord.Interaction):
        # Acknowledge before rebuilding the view (which reads SQLite).
        # Discord invalidates an interaction if its initial response is late.
        await interaction.response.defer()
        await interaction.edit_original_response(view=SubscriptionView(
            self.user_id, notice="Несохранённые изменения отменены."
        ))


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


class TraderNotificationView(discord.ui.LayoutView):
    def __init__(self, server: str, location: str, x: int, y: int):
        super().__init__(timeout=None)
        container = discord.ui.Container(discord.ui.TextDisplay(
            "# Сообщили о бродяге\n"
            f"**Сервер:** {get_server_display_name(server)}\n"
            f"**Локация:** {location}\n"
            f"**Координаты:** X: {x}, Y: {y}\n\n"
            "Это отметка игрока. Управление уведомлениями — в разделе "
            "«Подписки» главного меню бота."
        ))
        map_url = get_brodyaga_map_url(location)
        if map_url:
            container.add_item(discord.ui.ActionRow(discord.ui.Button(
                label="На карте", style=discord.ButtonStyle.link, url=map_url,
            )))
        self.add_item(container)


async def send_pending_notifications(bot):
    """Bounded delivery batch; a closed DM never interrupts other recipients."""
    for notification_id, user_id, server, location, x, y in get_pending_notifications():
        # A previous send may have yielded while the user unsubscribed.
        if server not in get_subscriptions(user_id):
            finish_notification(notification_id, "cancelled")
            continue
        try:
            user = bot.get_user(user_id) or await bot.fetch_user(user_id)
            await user.send(
                view=TraderNotificationView(server, location, x, y),
                allowed_mentions=discord.AllowedMentions.none(),
            )
        except (discord.Forbidden, discord.NotFound):
            finish_notification(notification_id, "failed")
            logger.info("Trader notification unavailable for user %s", user_id)
        except (discord.HTTPException, OSError, asyncio.TimeoutError):
            finish_notification(notification_id, "pending")
            logger.warning("Temporary trader notification failure for user %s", user_id)
        else:
            finish_notification(notification_id, "sent")
