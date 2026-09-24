import discord
from discord.ext import commands, tasks
import os
import re
import time
import pytz
from datetime import datetime, time as dt_time

from database import (
    init_database,
    register_user,
    is_user_registered,
    add_trader_report,
    get_trader_reports,
    get_trader_report_summary,
    archive_reports,
    has_trader_report_for_server,
    add_location_check,
    remove_location_check,
    get_user_checked_locations,
    get_location_check_summary,
)
from coords_handler import (
    get_all_locations,
    get_location_coords,
    get_screenshot_path,
    get_location_groups,
    get_unique_base_locations,
    get_locations_by_base,
    SERVERS,
    get_server_display_name,
    get_brodyaga_map_url,
)

# Загрузка токена из переменных окружения или файла .env
TOKEN = os.getenv("DISCORD_BOT_TOKEN")
MAP_URL = os.getenv("MAP_URL", "")
if not TOKEN:
    # Попытка загрузить из файла .env если существует
    env_file = os.path.join(os.path.dirname(__file__), ".env")
    if os.path.exists(env_file):
        with open(env_file, "r", encoding="utf-8") as f:
            for line in f:
                line = line.strip()
                if line.startswith("DISCORD_BOT_TOKEN="):
                    TOKEN = line.split("=", 1)[1]
                elif line.startswith("MAP_URL="):
                    MAP_URL = line.split("=", 1)[1]

if not TOKEN:
    print("ВНИМАНИЕ: Токен бота не найден!")
    print("Установите переменную окружения DISCORD_BOT_TOKEN или создайте файл .env")
    TOKEN = "YOUR_TOKEN_HERE"

# Настройка intents
intents = discord.Intents.default()
intents.message_content = True
intents.members = True

bot = commands.Bot(command_prefix="!", intents=intents)

# Хранилище состояний для многошагового процесса сообщения
# user_id -> {"step": int, "server": str, "location_base": str, "timestamp": float}
user_states = {}

# Таймаут состояния (5 минут в секундах)
USER_STATE_TIMEOUT = 300
REPORT_CODE_PREFIX = "73"

# Московский часовой пояс
MSK = pytz.timezone("Europe/Moscow")


def escape_markdown(text: str) -> str:
    """Экранирование спецсимволов Discord markdown"""
    return re.sub(r'([*_~`|\\])', r'\\\1', text)


def decode_map_report_code(content: str):
    """Return (server, location_name) for a valid 10-digit map report code."""
    match = re.fullmatch(r"\s*(\d{10})\s*", content)
    if not match:
        return None

    code = match.group(1)
    if not code.startswith(REPORT_CODE_PREFIX):
        return None

    location_id = int(code[2:6])
    server_id = int(code[6:8])
    checksum = int(code[8:10])
    expected_checksum = (location_id * 17 + server_id * 31 + 73) % 100
    locations = get_all_locations()

    if checksum != expected_checksum or not 1 <= server_id <= len(SERVERS):
        return None
    if not 1 <= location_id <= len(locations):
        return None

    return SERVERS[server_id - 1], locations[location_id - 1]


@bot.event
async def on_message(message):
    """Accept a copied report code from the map in the bot's direct messages."""
    if message.author.bot:
        return

    report = decode_map_report_code(message.content)
    if report:
        if message.guild is not None:
            await message.reply("Отправьте код боту в личном сообщении.")
            return

        server, location_name = report
        coords = get_location_coords(location_name)
        if not coords:
            await message.reply("Не удалось найти локацию для этого кода.")
            return

        x, y = coords
        is_first = add_trader_report(server, location_name, x, y, message.author.id)
        status = "Вы первый сообщили" if is_first else "Ваше сообщение обновлено"
        await message.reply(
            f"✅ {status}: `{location_name}` на сервере `{get_server_display_name(server)}`.\n"
            f"Координаты: X: {x}, Y: {y}"
        )
        return

    await bot.process_commands(message)


@bot.event
async def on_ready():
    """Событие при запуске бота"""
    print(f"Бот запущен: {bot.user.name} (ID: {bot.user.id})")

    # Инициализация базы данных
    init_database()
    print("База данных инициализирована")

    # Запуск задачи архивирования (00:00 MSK = 21:00 UTC)
    archive_daily.start()
    print("Задача ежедневного архивирования запущена")

    # Регистрация persistent views (работают после перезапуска)
    bot.add_view(MainMenuView())
    print("Persistent views зарегистрированы")


@tasks.loop(minutes=1)
async def archive_daily():
    """Ежедневное архивирование данных в 00:00 MSK (21:00 UTC)"""
    now_utc = datetime.utcnow()
    print(f"[Archive check] UTC time: {now_utc.strftime('%H:%M:%S')}")
    if now_utc.hour == 21 and now_utc.minute < 3:
        if not hasattr(archive_daily, "last_run") or archive_daily.last_run != now_utc.date():
            print("Выполняется ежедневное архивирование данных...")
            archive_reports()
            print("Архивирование завершено")
            archive_daily.last_run = now_utc.date()


def clean_expired_user_states():
    """Очистка просроченных состояний пользователей"""
    expired = [
        uid for uid, state in user_states.items()
        if time.time() - state.get("timestamp", 0) > USER_STATE_TIMEOUT
    ]
    for uid in expired:
        del user_states[uid]
    if expired:
        print(f"Очищены состояния: {len(expired)} пользователей")


def ensure_user_registered(user: discord.User) -> bool:
    """
    Проверка регистрации пользователя и регистрация если нужно.
    Возвращает True если пользователь новый.
    """
    if not is_user_registered(user.id):
        register_user(user.id, str(user))
        return True
    return False


async def send_main_menu(interaction: discord.Interaction):
    """Отправка главного меню с кнопками"""
    ensure_user_registered(interaction.user)
    await interaction.response.send_message(embed=build_main_menu_embed(), view=MainMenuView())


def build_main_menu_embed() -> discord.Embed:
    """Build the stable main menu; live search figures belong to the search view."""
    embed = discord.Embed(
        title="🕵️ Бродячий торговец",
        description="Выберите действие:",
        color=discord.Color.gold(),
    )
    return embed


class MainMenuView(discord.ui.View):
    """Главное меню с кнопками (persistent — работает после перезапуска)"""

    def __init__(self):
        super().__init__(timeout=None)

    @discord.ui.button(label="Начать поиск", style=discord.ButtonStyle.success, custom_id="main_menu_start_search", row=0)
    async def start_search(
        self, interaction: discord.Interaction, button: discord.ui.Button
    ):
        ensure_user_registered(interaction.user)
        await start_location_status(interaction, show_progress=True)

    @discord.ui.button(label="Где торговец?", style=discord.ButtonStyle.primary, custom_id="main_menu_check_trader", row=0)
    async def check_trader(
        self, interaction: discord.Interaction, button: discord.ui.Button
    ):
        ensure_user_registered(interaction.user)
        await show_trader_locations(interaction)

    @discord.ui.button(label="Как это работает", style=discord.ButtonStyle.secondary, custom_id="main_menu_help", row=1)
    async def help_info(
        self, interaction: discord.Interaction, button: discord.ui.Button
    ):
        await show_help(interaction)

    @discord.ui.button(label="Добавить или поделиться", style=discord.ButtonStyle.secondary, custom_id="main_menu_share_bot", row=1)
    async def share_bot(
        self, interaction: discord.Interaction, button: discord.ui.Button
    ):
        await interaction.response.send_message(
            "Ссылка для добавления и отправки друзьям:\n"
            "https://discord.com/oauth2/authorize?client_id=1492221304173236406&permissions=274878023680&integration_type=0&scope=bot+applications.commands",
            ephemeral=True,
        )

class HelpInfoV2View(discord.ui.LayoutView):
    """Compact Components V2 help screen shown from the main menu."""

    def __init__(self):
        super().__init__(timeout=300)

        container = discord.ui.Container(
            discord.ui.TextDisplay("# ℹ️ Как это работает"),
            discord.ui.TextDisplay(
                "Бот помогает отслеживать Бродячего торговца на четырёх серверах Chernarus YourWorld PVE."
            ),
        )
        container.add_item(
            discord.ui.TextDisplay(
                "\n**🕵️ Где торговец?**\n"
                "Показывает подтверждённые находки сразу по всем серверам: "
                "локацию, ник игрока, который её отметил, и время сообщения."
            )
        )
        container.add_item(
            discord.ui.TextDisplay(
                "\n**🔎 Начать поиск**\n"
                "Выберите сервер и проверяйте локации. Для каждой точки можно отметить: "
                "торговец не найден или найден. Перед отправкой находки бот попросит подтверждение."
            )
        )
        container.add_item(
            discord.ui.TextDisplay(
                "\n**🔄 Обновление данных**\n"
                "Поиски и сообщения сбрасываются ежедневно в 00:00 по московскому времени."
            )
        )
        self.add_item(container)


async def show_help(interaction: discord.Interaction):
    await interaction.response.send_message(view=HelpInfoV2View(), ephemeral=True)


async def show_trader_locations(interaction: discord.Interaction):
    """Show trader reports in one navigable Components V2 result screen."""
    await interaction.response.send_message(
        view=TraderLocationsV2View(interaction.user.id), ephemeral=True
    )


async def show_trader_locations_detail(
    interaction: discord.Interaction, selected_server: str | None = None
):
    """Show reports for one server or, by default, all servers."""
    reports = get_trader_reports()

    if not reports:
        embed = discord.Embed(
            title="📍 Где торговец?",
            description="На данный момент нет сообщений о местоположении торговца ни на одном сервере.",
            color=discord.Color.orange(),
        )
        await interaction.response.send_message(embed=embed, ephemeral=True)
        return

    # Группировка: {server: {location: {"x": x, "y": y, "users": [...]}}}
    reports_grouped = {}
    for server, location_name, x, y, is_first, username in reports:
        if server not in reports_grouped:
            reports_grouped[server] = {}
        if location_name not in reports_grouped[server]:
            reports_grouped[server][location_name] = {"x": x, "y": y, "users": []}
        if username not in reports_grouped[server][location_name]["users"]:
            reports_grouped[server][location_name]["users"].append(username)

    servers_to_show = [selected_server] if selected_server else SERVERS
    first_server = True
    for server in servers_to_show:
        if server in reports_grouped:
            embed = discord.Embed(
                title=f"📍 {get_server_display_name(server)}",
                color=discord.Color.green(),
            )

            locations = reports_grouped[server]
            # Находим локацию с максимумом голосов
            max_votes = 0
            winner = None
            for loc_name, data in locations.items():
                votes = len(data["users"])
                if votes > max_votes:
                    max_votes = votes
                    winner = loc_name

            is_clear_winner = max_votes > 1
            if is_clear_winner:
                ties = sum(1 for d in locations.values() if len(d["users"]) == max_votes)
                if ties > 1:
                    is_clear_winner = False

            for loc_name, data in locations.items():
                votes = len(data["users"])
                users = [escape_markdown(u) for u in data["users"]]
                users_formatted = ", ".join([f"**{users[0]}**"] + users[1:])
                vote_label = f"({votes} {'голос' if votes == 1 else 'голоса' if votes < 5 else 'голосов'})"
                prefix = "🏆 " if (is_clear_winner and loc_name == winner) else ""

                embed.add_field(
                    name=f"{prefix}**{loc_name}** {vote_label}",
                    value=f"📍 Координаты: X: {data['x']}, Y: {data['y']}\n👥 Сообщили: {users_formatted}",
                    inline=False,
                )

            # Кнопки для скриншотов и ссылка на карту
            view = discord.ui.View(timeout=120)
            for loc_name in locations.keys():
                screenshot = get_screenshot_path(loc_name)
                if screenshot and os.path.exists(screenshot):
                    btn = discord.ui.Button(
                        label=loc_name,
                        style=discord.ButtonStyle.secondary,
                    )

                    def make_callback(sc_path, loc):
                        async def callback(inter: discord.Interaction):
                            if inter.user.id != interaction.user.id:
                                await inter.response.send_message("Это не ваше сообщение!", ephemeral=True)
                                return
                            f = discord.File(sc_path, filename="loc.png")
                            e = discord.Embed(title=f"📷 {loc}", color=discord.Color.blue())
                            e.set_image(url="attachment://loc.png")
                            await inter.response.send_message(embed=e, file=f, ephemeral=True)
                        return callback

                    btn.callback = make_callback(screenshot, loc_name)
                    view.add_item(btn)

            # Кнопка-ссылка на карту
            if MAP_URL and MAP_URL.strip():
                view.add_item(discord.ui.Button(
                    label="Посмотреть на карте",
                    style=discord.ButtonStyle.link,
                    url=MAP_URL.strip(),
                ))

            if first_server:
                if view.children:
                    await interaction.response.send_message(embed=embed, view=view, ephemeral=True)
                else:
                    await interaction.response.send_message(embed=embed, ephemeral=True)
                first_server = False
            else:
                if view.children:
                    await interaction.followup.send(embed=embed, view=view, ephemeral=True)
                else:
                    await interaction.followup.send(embed=embed, ephemeral=True)
        else:
            embed = discord.Embed(
                title=f"📍 {get_server_display_name(server)}",
                description="Нет данных о торговце на этом сервере.",
                color=discord.Color.orange(),
            )
            if first_server:
                await interaction.response.send_message(embed=embed, ephemeral=True)
                first_server = False
            else:
                await interaction.followup.send(embed=embed, ephemeral=True)


def get_grouped_trader_reports() -> dict[str, dict[str, dict]]:
    """Return current reports grouped by server and exact location."""
    grouped: dict[str, dict[str, dict]] = {server: {} for server in SERVERS}
    for server, location_name, x, y, is_first, username in get_trader_reports():
        location = grouped.setdefault(server, {}).setdefault(
            location_name, {"x": x, "y": y, "users": []}
        )
        if username not in location["users"]:
            location["users"].append(username)
    return grouped


def format_trader_server_status(locations: dict[str, dict]) -> str:
    """Format all current reports for one server, retaining reporter nicknames."""
    if not locations:
        return "❔ Сообщений о торговце пока нет."

    reports = sorted(
        locations.items(), key=lambda item: (-len(item[1]["users"]), item[0])
    )
    lines = []
    if len(reports) == 1:
        lines.append("🕵️ **Торговец найден**")
    else:
        lines.append("⚠️ **Сообщения расходятся**")

    for location_name, data in reports:
        users = [escape_markdown(username) for username in data["users"]]
        users_text = ", ".join([f"**{users[0]}**"] + users[1:])
        confirmations = len(users)
        word = (
            "подтверждение" if confirmations == 1
            else "подтверждения" if confirmations < 5 else "подтверждений"
        )
        lines.append(
            f"**{location_name}** · {confirmations} {word}\n"
            f"📍 X: {data['x']}, Y: {data['y']}\n"
            f"👥 Сообщили: {users_text}"
        )
    return "\n\n".join(lines)


class TraderLocationConfirmSelect(discord.ui.Select):
    """Let a player choose an already reported point to confirm."""

    def __init__(self, user_id: int, server: str, locations: list[str]):
        self.user_id = user_id
        self.server = server
        super().__init__(
            placeholder="Я тоже видел торговца в...",
            options=[discord.SelectOption(label=location, value=location) for location in locations],
        )

    async def callback(self, interaction: discord.Interaction):
        if interaction.user.id != self.user_id:
            await interaction.response.send_message("Это не ваше меню.", ephemeral=True)
            return
        await interaction.response.edit_message(
            view=TraderReportConfirmationView(
                self.user_id, self.server, self.values[0]
            )
        )


class TraderLocationMapSelect(discord.ui.Select):
    """Offer exact DayZ-Map links for the reported locations of one server."""

    def __init__(self, user_id: int, locations: list[str]):
        self.user_id = user_id
        super().__init__(
            placeholder="Открыть точку на карте...",
            options=[discord.SelectOption(label=location, value=location) for location in locations],
        )

    async def callback(self, interaction: discord.Interaction):
        if interaction.user.id != self.user_id:
            await interaction.response.send_message("Это не ваше меню.", ephemeral=True)
            return
        location_name = self.values[0]
        map_url = get_brodyaga_map_url(location_name)
        if not map_url:
            await interaction.response.send_message(
                f"Не удалось подготовить ссылку на карту: {location_name}.", ephemeral=True
            )
            return
        view = discord.ui.View(timeout=300)
        view.add_item(
            discord.ui.Button(
                label="Открыть на карте", style=discord.ButtonStyle.link, url=map_url
            )
        )
        await interaction.response.send_message(location_name, view=view, ephemeral=True)


class TraderLocationPhotoButton(discord.ui.Button):
    """Open one trader-location screenshot without leaving the result page."""

    def __init__(self, user_id: int, location_name: str):
        super().__init__(label="Фото", style=discord.ButtonStyle.secondary)
        self.user_id = user_id
        self.location_name = location_name

    async def callback(self, interaction: discord.Interaction):
        if interaction.user.id != self.user_id:
            await interaction.response.send_message("Это не ваше меню.", ephemeral=True)
            return
        screenshot = get_screenshot_path(self.location_name)
        if not screenshot:
            await interaction.response.send_message(
                f"Для {self.location_name} нет изображения.", ephemeral=True
            )
            return
        await interaction.response.send_message(
            content=self.location_name,
            file=discord.File(screenshot, filename="location-preview.png"),
            ephemeral=True,
        )


class TraderLocationConfirmButton(discord.ui.Button):
    """Start the confirmation flow for the exact card on the current page."""

    def __init__(self, user_id: int, server: str, location_name: str, return_page: int):
        super().__init__(label="Подтвердить", style=discord.ButtonStyle.success)
        self.user_id = user_id
        self.server = server
        self.location_name = location_name
        self.return_page = return_page

    async def callback(self, interaction: discord.Interaction):
        if interaction.user.id != self.user_id:
            await interaction.response.send_message("Это не ваше меню.", ephemeral=True)
            return
        await interaction.response.edit_message(
            view=TraderReportConfirmationView(
                self.user_id, self.server, self.location_name, self.return_page
            )
        )


class TraderReportConfirmationView(discord.ui.LayoutView):
    """Confirmation before a report is added from the public status screen."""

    def __init__(self, user_id: int, server: str, location_name: str, return_page: int = 0):
        super().__init__(timeout=300)
        self.user_id = user_id
        self.server = server
        self.location_name = location_name
        self.return_page = return_page

        container = discord.ui.Container(
            discord.ui.TextDisplay("# Подтвердить торговца")
        )
        container.add_item(
            discord.ui.TextDisplay(
                f"Подтвердить торговца в **{location_name}** на сервере "
                f"**{get_server_display_name(server)}**?"
            )
        )
        container.add_item(
            discord.ui.TextDisplay(
                "> Ваше подтверждение станет видно другим игрокам."
            )
        )
        confirm_button = discord.ui.Button(
            label="Да, подтвердить", style=discord.ButtonStyle.success
        )
        cancel_button = discord.ui.Button(
            label="Отмена", style=discord.ButtonStyle.secondary
        )
        confirm_button.callback = self._confirm_callback
        cancel_button.callback = self._cancel_callback
        container.add_item(discord.ui.ActionRow(confirm_button, cancel_button))
        self.add_item(container)

    async def interaction_check(self, interaction: discord.Interaction) -> bool:
        if interaction.user.id != self.user_id:
            await interaction.response.send_message("Это не ваше меню.", ephemeral=True)
            return False
        return True

    async def _confirm_callback(self, interaction: discord.Interaction):
        coords = get_location_coords(self.location_name)
        if not coords:
            notice = f"Не удалось найти координаты: {self.location_name}"
        else:
            is_first = add_trader_report(
                self.server, self.location_name, coords[0], coords[1], self.user_id
            )
            notice = (
                f"Торговец подтверждён: {self.location_name}"
                if is_first
                else f"Ваше сообщение обновлено: {self.location_name}"
            )
        await interaction.response.edit_message(
            view=TraderLocationsV2View(self.user_id, self.return_page, notice)
        )

    async def _cancel_callback(self, interaction: discord.Interaction):
        await interaction.response.edit_message(
            view=TraderLocationsV2View(self.user_id, self.return_page)
        )


class TraderLocationsV2View(discord.ui.LayoutView):
    """Show every server immediately, with direct actions for each report."""

    PAGE_SIZE = 5

    def __init__(self, user_id: int, page: int = 0, notice: str | None = None):
        super().__init__(timeout=300)
        self.user_id = user_id
        self.page = page
        self.notice = notice
        self._build_layout()

    def _build_layout(self):
        reports = get_grouped_trader_reports()
        cards = []
        for server in SERVERS:
            locations = reports[server]
            if not locations:
                cards.append(("empty", server, None, None))
                continue
            conflicting = len(locations) > 1
            for location_name, data in sorted(
                locations.items(), key=lambda item: (-len(item[1]["users"]), item[0])
            ):
                cards.append(("report", server, location_name, (data, conflicting)))

        pages = max(1, (len(cards) + self.PAGE_SIZE - 1) // self.PAGE_SIZE)
        self.page = max(0, min(self.page, pages - 1))
        visible_cards = cards[self.page * self.PAGE_SIZE:(self.page + 1) * self.PAGE_SIZE]

        container = discord.ui.Container(discord.ui.TextDisplay("# 🕵️ Где торговец?"))
        if self.notice:
            container.add_item(discord.ui.TextDisplay(f"> {self.notice}"))

        for index, (card_type, server, location_name, data) in enumerate(visible_cards):
            server_name = get_server_display_name(server)
            if card_type == "empty":
                content = f"## {server_name}\nСообщений о торговце пока нет."
            else:
                location_data, conflicting = data
                users = [escape_markdown(username) for username in location_data["users"]]
                users_text = ", ".join([f"**{users[0]}**"] + users[1:])
                confirmations = len(users)
                word = (
                    "подтверждение" if confirmations == 1
                    else "подтверждения" if confirmations < 5 else "подтверждений"
                )
                conflict_notice = "\nСообщения расходятся" if conflicting else ""
                content = (
                    f"## {server_name}\n"
                    f"**{location_name}** · {confirmations} {word}{conflict_notice}\n"
                    f"X: {location_data['x']}, Y: {location_data['y']}\n"
                    f"Сообщили: {users_text}"
                )
            container.add_item(discord.ui.TextDisplay(content))

            if card_type == "report":
                map_url = get_brodyaga_map_url(location_name)
                actions = [TraderLocationPhotoButton(self.user_id, location_name)]
                if map_url:
                    actions.append(
                        discord.ui.Button(
                            label="На карте", style=discord.ButtonStyle.link, url=map_url
                        )
                    )
                actions.append(
                    TraderLocationConfirmButton(
                        self.user_id, server, location_name, self.page
                    )
                )
                container.add_item(discord.ui.ActionRow(*actions))

            if index < len(visible_cards) - 1:
                container.add_item(
                    discord.ui.Separator(spacing=discord.SeparatorSpacing.small)
                )

        navigation = discord.ui.ActionRow(
            self._navigation_button("Назад", -1, self.page <= 0),
            self._refresh_button(),
            self._navigation_button("Вперёд", 1, self.page >= pages - 1),
        )
        container.add_item(navigation)
        self.add_item(container)

    def _navigation_button(self, label: str, step: int, disabled: bool):
        button = discord.ui.Button(label=label, style=discord.ButtonStyle.secondary, disabled=disabled)

        async def callback(interaction: discord.Interaction):
            if interaction.user.id != self.user_id:
                await interaction.response.send_message("Это не ваше меню.", ephemeral=True)
                return
            await interaction.response.edit_message(
                view=TraderLocationsV2View(self.user_id, self.page + step)
            )

        button.callback = callback
        return button

    def _refresh_button(self):
        button = discord.ui.Button(label="Обновить", style=discord.ButtonStyle.primary)

        async def callback(interaction: discord.Interaction):
            if interaction.user.id != self.user_id:
                await interaction.response.send_message("Это не ваше меню.", ephemeral=True)
                return
            await interaction.response.edit_message(
                view=TraderLocationsV2View(self.user_id, self.page)
            )

        button.callback = callback
        return button


async def start_report_flow(interaction: discord.Interaction):
    """Начало процесса сообщения о торговце"""
    # Очищаем просроченные состояния
    clean_expired_user_states()

    # Сохраняем состояние пользователя с таймстампом
    user_states[interaction.user.id] = {
        "flow": "report",
        "step": 1,
        "server": None,
        "location_base": None,
        "timestamp": time.time(),
    }

    # Шаг 1: Выбор сервера
    view = ServerSelectView(interaction.user.id)
    embed = discord.Embed(
        title="📢 Нашёл торговца",
        description="**Шаг 1/3:** Выберите сервер, где вы видели торговца:",
        color=discord.Color.green(),
    )
    await interaction.response.send_message(embed=embed, view=view, ephemeral=True)


class ServerSelectView(discord.ui.View):
    """Выбор сервера"""

    def __init__(self, user_id: int):
        super().__init__(timeout=300)  # 5 минут таймаут
        self.user_id = user_id

    @discord.ui.select(
        placeholder="Выберите сервер...",
        options=[
            discord.SelectOption(
                label=get_server_display_name(server), value=server, emoji="🌐"
            )
            for server in SERVERS
        ],
    )
    async def select_server(
        self, interaction: discord.Interaction, select: discord.ui.Select
    ):
        if interaction.user.id != self.user_id:
            await interaction.response.send_message(
                "Это не ваше меню!", ephemeral=True
            )
            return

        # Проверяем таймаут
        state = user_states.get(interaction.user.id)
        if state and time.time() - state.get("timestamp", 0) > USER_STATE_TIMEOUT:
            del user_states[interaction.user.id]
            await interaction.response.send_message(
                "⏰ Время вышло. Начните заново, нажав 📢 Нашёл торговца.",
                ephemeral=True,
            )
            return

        server = select.values[0]
        user_states[interaction.user.id]["server"] = server
        user_states[interaction.user.id]["step"] = 2

        # Шаг 2: Выбор локации
        base_locations = get_unique_base_locations()
        view = make_location_select_view(self.user_id, server, base_locations)

        flow = user_states[interaction.user.id].get("flow", "report")
        action = "🔎 Проверить локацию" if flow == "search" else "📢 Нашёл торговца"
        embed = discord.Embed(
            title=action,
            description=(
                f"**Шаг 2/3:** Выберите локацию на сервере "
                f"`{get_server_display_name(server)}`:"
            ),
            color=discord.Color.green(),
        )

        await interaction.response.edit_message(embed=embed, view=view)


async def start_search_flow(interaction: discord.Interaction):
    """Show per-server search progress, then start the detailed selection flow."""
    clean_expired_user_states()
    user_states[interaction.user.id] = {
        "flow": "search",
        "step": 1,
        "server": None,
        "location_base": None,
        "timestamp": time.time(),
    }
    embed = build_search_overview_embed()
    await interaction.response.send_message(
        embed=embed, view=ServerSelectView(interaction.user.id), ephemeral=True
    )


def build_search_overview_embed() -> discord.Embed:
    """Show search progress for every server where it is useful to players."""
    reports = {(server, location) for server, location, *_rest in get_trader_report_summary()}
    trader_statuses = get_server_trader_statuses()
    total = len(get_all_locations())
    embed = discord.Embed(
        title="🔎 Начать поиск",
        description="Выберите сервер. В статусе можно отметить точку одной кнопкой.",
        color=discord.Color.green(),
    )
    for server in SERVERS:
        checked = sum(
            1
            for location, _checked_at, _count in get_location_check_summary(server)
            if (server, location) not in reports
        )
        trader_text, _select_description = trader_statuses[server]
        value = f"Проверено: **{checked}/{total} точек**"
        if trader_text:
            value = f"{trader_text}\n{value}"
        embed.add_field(
            name=get_server_display_name(server),
            value=value,
            # Discord раскладывает inline-поля в колонки. Статус сервера
            # должен быть отдельным читаемым блоком, а не частью таблицы.
            inline=False,
        )
    return embed


def get_server_trader_statuses() -> dict[str, tuple[str | None, str | None]]:
    """Build full and select-menu-friendly trader status text for every server."""
    reports_by_server = {server: [] for server in SERVERS}
    for server, location, _x, _y, confirmations in get_trader_report_summary():
        reports_by_server[server].append((location, confirmations))

    statuses = {}
    for server, reports in reports_by_server.items():
        if not reports:
            statuses[server] = (None, None)
            continue
        reports.sort(key=lambda item: (-item[1], item[0]))
        leader, confirmations = reports[0]
        leaders = [location for location, count in reports if count == confirmations]
        if len(reports) == 1:
            text = f"🕵️ Торговец: {leader} · {confirmations} сообщ."
            description = f"Торговец: {leader} · {confirmations} сообщ."
        elif len(leaders) > 1:
            text = f"🕵️ Несколько лидирующих точек · {len(leaders)}"
            description = f"Несколько лидирующих точек: {len(leaders)}"
        else:
            text = f"🕵️ Несколько точек · лидирует {leader}, {confirmations} сообщ."
            description = f"Несколько точек; лидер: {leader}"
        statuses[server] = (text, description)
    return statuses


class WithdrawCheckView(discord.ui.View):
    """Lets a player undo only their own fresh check."""

    def __init__(self, user_id: int, server: str, location_name: str):
        super().__init__(timeout=300)
        self.user_id = user_id
        self.server = server
        self.location_name = location_name

    @discord.ui.button(label="Ошибся / вернуться", style=discord.ButtonStyle.secondary)
    async def withdraw(self, interaction: discord.Interaction, _button: discord.ui.Button):
        if interaction.user.id != self.user_id:
            await interaction.response.send_message("Это не ваша отметка.", ephemeral=True)
            return
        removed = remove_location_check(self.server, self.location_name, self.user_id)
        for item in self.children:
            item.disabled = True
        text = "Отметка отменена." if removed else "Эта отметка уже была заменена или отменена."
        await interaction.response.edit_message(content=text, embed=None, view=self)


async def process_location_check(interaction: discord.Interaction, location_name: str):
    """Persist a 'not found' marker after the player chose the exact location."""
    state = user_states.get(interaction.user.id)
    if not state:
        await interaction.response.send_message("Время выбора истекло. Начните поиск заново.", ephemeral=True)
        return
    server = state["server"]
    saved = add_location_check(server, location_name, interaction.user.id)
    user_states.pop(interaction.user.id, None)

    if saved:
        embed = discord.Embed(
            title="✅ Локация отмечена",
            description=(
                f"В `{location_name}` на сервере `{get_server_display_name(server)}` торговца не нашли. "
                "Отметка попадёт в общую сводку поиска."
            ),
            color=discord.Color.green(),
        )
        view = WithdrawCheckView(interaction.user.id, server, location_name)
    else:
        embed = discord.Embed(
            title="📍 Торговец уже подтверждён",
            description=(
                f"Для `{location_name}` на сервере `{get_server_display_name(server)}` уже есть сообщение о торговце, "
                "поэтому отметка «не найден» не сохранена."
            ),
            color=discord.Color.orange(),
        )
        view = None

    await interaction.response.defer(ephemeral=True)
    await interaction.followup.send(embed=embed, view=view, ephemeral=True)


def make_location_select_view(user_id: int, server: str, locations: list):
    """Динамическое создание view с выбором локации"""

    class LocationSelectViewDynamic(discord.ui.View):
        def __init__(self):
            super().__init__(timeout=300)
            options = [
                discord.SelectOption(label=loc, value=loc)
                for loc in locations
            ]
            select = discord.ui.Select(
                placeholder="Выберите локацию...",
                options=options,
            )
            def make_callback():
                async def callback(interaction: discord.Interaction):
                    if interaction.user.id != user_id:
                        await interaction.response.send_message(
                            "Это не ваше меню!", ephemeral=True
                        )
                        return

                    # Проверяем таймаут
                    state = user_states.get(interaction.user.id)
                    if state and time.time() - state.get("timestamp", 0) > USER_STATE_TIMEOUT:
                        if interaction.user.id in user_states:
                            del user_states[interaction.user.id]
                        await interaction.response.send_message(
                            "⏰ Время вышло. Начните заново, нажав 📢 Нашёл торговца.",
                            ephemeral=True,
                        )
                        return

                    location_base = interaction.data["values"][0]
                    user_states[interaction.user.id]["location_base"] = location_base
                    user_states[interaction.user.id]["step"] = 3

                    location_variants = get_locations_by_base(location_base)

                    if len(location_variants) == 1:
                        location_name = location_variants[0]
                        if state.get("flow") == "search":
                            await process_location_check(interaction, location_name)
                        else:
                            await process_report(interaction, location_name)
                    else:
                        view = BuildingSelectView(user_id, server, location_variants, flow=state.get("flow", "report"))
                        view._update_buttons()
                        location_name = location_variants[0]
                        embed = view._build_embed()
                        screenshot = get_screenshot_path(location_name)
                        if screenshot and os.path.exists(screenshot):
                            file = discord.File(screenshot, filename="preview.png")
                            embed.set_image(url="attachment://preview.png")
                            await interaction.response.defer(ephemeral=True)
                            await interaction.followup.send(embed=embed, view=view, file=file, ephemeral=True)
                        else:
                            await interaction.response.edit_message(embed=embed, view=view)

                return callback

            select.callback = make_callback()
            self.add_item(select)

    return LocationSelectViewDynamic()


class BuildingSelectView(discord.ui.View):
    """Выбор конкретного здания с превью скриншота"""

    def __init__(self, user_id: int, server: str, locations: list, flow: str = "report"):
        super().__init__(timeout=300)
        self.user_id = user_id
        self.server = server
        self.locations = locations
        self.flow = flow
        self.current_index = 0

    def _build_embed(self):
        location_name = self.locations[self.current_index]
        embed = discord.Embed(
            title="🔎 Выберите здание" if self.flow == "search" else "📢 Выберите здание",
            description=f"**{location_name}**\n({self.current_index + 1}/{len(self.locations)})",
            color=discord.Color.green(),
        )
        return embed

    def _update_buttons(self):
        # Удаляем все кнопки и пересоздаём
        self.clear_items()

        btn_prev = discord.ui.Button(
            label="Назад",
            style=discord.ButtonStyle.secondary,
            disabled=self.current_index == 0,
        )
        btn_prev.callback = self._prev_callback
        self.add_item(btn_prev)

        btn_select = discord.ui.Button(
            label="Выбрать это",
            style=discord.ButtonStyle.success,
        )
        btn_select.callback = self._select_callback
        self.add_item(btn_select)

        btn_next = discord.ui.Button(
            label="Вперёд",
            style=discord.ButtonStyle.secondary,
            disabled=self.current_index == len(self.locations) - 1,
        )
        btn_next.callback = self._next_callback
        self.add_item(btn_next)

    async def _prev_callback(self, interaction: discord.Interaction):
        if interaction.user.id != self.user_id:
            return
        self.current_index -= 1
        self._update_buttons()
        await self._send_update(interaction)

    async def _next_callback(self, interaction: discord.Interaction):
        if interaction.user.id != self.user_id:
            return
        self.current_index += 1
        self._update_buttons()
        await self._send_update(interaction)

    async def _select_callback(self, interaction: discord.Interaction):
        if interaction.user.id != self.user_id:
            await interaction.response.send_message("Это не ваше меню!", ephemeral=True)
            return
        location_name = self.locations[self.current_index]
        if self.flow == "search":
            await process_location_check(interaction, location_name)
        else:
            await process_report(interaction, location_name)

    async def _send_update(self, interaction: discord.Interaction):
        location_name = self.locations[self.current_index]
        embed = self._build_embed()
        screenshot = get_screenshot_path(location_name)
        if screenshot and os.path.exists(screenshot):
            file = discord.File(screenshot, filename=f"preview.png")
            embed.set_image(url="attachment://preview.png")
            await interaction.response.defer(ephemeral=True)
            await interaction.followup.send(embed=embed, view=self, file=file, ephemeral=True)
        else:
            await interaction.response.edit_message(embed=embed, view=self)

    async def interaction_check(self, interaction: discord.Interaction) -> bool:
        if interaction.user.id != self.user_id:
            await interaction.response.send_message("Это не ваше меню!", ephemeral=True)
            return False
        return True

    async def on_timeout(self):
        for item in self.children:
            if hasattr(item, "disabled"):
                item.disabled = True


async def process_report(interaction: discord.Interaction, location_name: str):
    """Обработка и сохранение отчета о торговце"""
    server = user_states[interaction.user.id]["server"]

    # Получаем координаты
    coords = get_location_coords(location_name)
    if not coords:
        embed = discord.Embed(
            title="❌ Ошибка",
            description="Не удалось найти координаты для этой локации.",
            color=discord.Color.red(),
        )
        await interaction.response.edit_message(embed=embed, view=None)
        return

    x, y = coords

    # Добавляем отчет
    is_first = add_trader_report(
        server, location_name, x, y, interaction.user.id
    )

    # Очищаем состояние
    if interaction.user.id in user_states:
        del user_states[interaction.user.id]

    # Формируем сообщение
    if is_first:
        embed = discord.Embed(
            title="✅ Спасибо!",
            description=(
                f"Вы **первый** сообщили о торговце в локации `{location_name}` "
                f"на сервере `{get_server_display_name(server)}`!"
            ),
            color=discord.Color.green(),
        )
    else:
        embed = discord.Embed(
            title="✅ Данные обновлены!",
            description=(
                f"Ваше сообщение о торговце в локации `{location_name}` "
                f"на сервере `{get_server_display_name(server)}` обновлено!"
            ),
            color=discord.Color.blue(),
        )

    embed.add_field(
        name="📍 Координаты",
        value=f"X: {x}, Y: {y}",
        inline=False,
    )

    # Прикрепляем скриншот если есть
    screenshot = get_screenshot_path(location_name)
    if screenshot and os.path.exists(screenshot):
        file = discord.File(screenshot, filename=f"{location_name}.png")
        embed.set_image(url=f"attachment://{location_name}.png")
        await interaction.response.defer(ephemeral=True)
        await interaction.followup.send(embed=embed, file=file, ephemeral=True)
    else:
        await interaction.response.defer(ephemeral=True)
        await interaction.followup.send(embed=embed, ephemeral=True)


def humanize_check_time(value) -> str:
    """Format SQLite's timestamp as a compact Russian relative time."""
    try:
        checked_at = value if isinstance(value, datetime) else datetime.fromisoformat(str(value))
        seconds = max(0, int((datetime.now() - checked_at).total_seconds()))
    except (TypeError, ValueError):
        return "недавно"
    if seconds < 60:
        return "только что"
    minutes = seconds // 60
    if minutes < 60:
        return f"{minutes} мин назад"
    hours = minutes // 60
    return f"{hours} ч назад"


class LocationPhotoSelect(discord.ui.Select):
    def __init__(self, locations: list[str]):
        self.locations = locations
        super().__init__(
            placeholder="Показать фото локации...",
            options=[
                discord.SelectOption(label=location, value=str(index))
                for index, location in enumerate(locations)
            ],
        )

    async def callback(self, interaction: discord.Interaction):
        location = self.locations[int(self.values[0])]
        screenshot = get_screenshot_path(location)
        if not screenshot:
            await interaction.response.send_message(
                f"Для {location} нет изображения.", ephemeral=True
            )
            return
        file = discord.File(screenshot, filename="location-preview.png")
        await interaction.response.send_message(
            content=location, file=file, ephemeral=True
        )


class LocationStatusActionButton(discord.ui.Button):
    """A real status action for one location in the compact search layout."""

    def __init__(
        self, user_id: int, server: str, location_name: str, action: str,
        disabled: bool = False, label: str | None = None,
    ):
        labels = {"check": "Не найден", "cancel": "Отменить", "found": "Подтвердить"}
        styles = {
            "check": discord.ButtonStyle.success,
            "cancel": discord.ButtonStyle.danger,
            "found": discord.ButtonStyle.primary,
        }
        super().__init__(label=label or labels[action], style=styles[action], disabled=disabled)
        self.user_id = user_id
        self.server = server
        self.location_name = location_name
        self.action = action

    async def callback(self, interaction: discord.Interaction):
        if interaction.user.id != self.user_id:
            await interaction.response.send_message("Это не ваше меню.", ephemeral=True)
            return
        if self.action == "cancel":
            remove_location_check(self.server, self.location_name, self.user_id)
            notice = f"Отметка отменена: {self.location_name}"
        elif self.action == "check":
            if add_location_check(self.server, self.location_name, self.user_id):
                notice = f"Отметка сохранена: {self.location_name}"
            else:
                notice = f"Для {self.location_name} уже подтверждён торговец."
        else:
            # A first report changes the shared status for everyone, so ask for
            # an explicit confirmation before writing it to the database.
            if not has_trader_report_for_server(self.server, self.location_name):
                await interaction.response.edit_message(
                    view=TraderConfirmationView(
                        self.user_id,
                        self.server,
                        self.location_name,
                        return_page=self.view.page,
                    )
                )
                return
            coords = get_location_coords(self.location_name)
            if not coords:
                notice = f"Не удалось найти координаты: {self.location_name}"
            else:
                is_first = add_trader_report(
                    self.server, self.location_name, coords[0], coords[1], self.user_id
                )
                notice = (
                    f"Торговец подтверждён: {self.location_name}"
                    if is_first
                    else f"Сообщение о торговце обновлено: {self.location_name}"
                )
        await self.view.render(interaction, notice)


class TraderConfirmationView(discord.ui.LayoutView):
    """Ask before creating a new public trader report."""

    def __init__(
        self, user_id: int, server: str, location_name: str, return_page: int
    ):
        super().__init__(timeout=300)
        self.user_id = user_id
        self.server = server
        self.location_name = location_name
        self.return_page = return_page

        container = discord.ui.Container(
            discord.ui.TextDisplay("# Подтвердить торговца")
        )
        container.add_item(
            discord.ui.TextDisplay(
                f"Торговец найден в **{location_name}** на сервере "
                f"**{get_server_display_name(server)}**?"
            )
        )
        container.add_item(
            discord.ui.TextDisplay(
                "> После подтверждения отметка станет видна другим игрокам."
            )
        )

        confirm_button = discord.ui.Button(
            label="Да, подтвердить", style=discord.ButtonStyle.success
        )
        cancel_button = discord.ui.Button(
            label="Отмена", style=discord.ButtonStyle.secondary
        )
        confirm_button.callback = self._confirm_callback
        cancel_button.callback = self._cancel_callback
        container.add_item(discord.ui.ActionRow(confirm_button, cancel_button))
        self.add_item(container)

    async def interaction_check(self, interaction: discord.Interaction) -> bool:
        if interaction.user.id != self.user_id:
            await interaction.response.send_message("Это не ваше меню.", ephemeral=True)
            return False
        return True

    async def _confirm_callback(self, interaction: discord.Interaction):
        coords = get_location_coords(self.location_name)
        if not coords:
            notice = f"Не удалось найти координаты: {self.location_name}"
        else:
            is_first = add_trader_report(
                self.server, self.location_name, coords[0], coords[1], self.user_id
            )
            notice = (
                f"Торговец подтверждён: {self.location_name}"
                if is_first
                else f"Сообщение о торговце обновлено: {self.location_name}"
            )
        await interaction.response.edit_message(
            view=LocationStatusV2View(
                self.user_id, self.server, self.return_page, notice
            )
        )

    async def _cancel_callback(self, interaction: discord.Interaction):
        await interaction.response.edit_message(
            view=LocationStatusV2View(self.user_id, self.server, self.return_page)
        )


class LocationStatusV2View(discord.ui.LayoutView):
    """Compact search layout based on Test 3, with real location actions."""

    # Components V2 permits at most 40 nested components per message. Five
    # points leave room for their actions, group headers, navigation, and the
    # visual separators between points.
    PAGE_SIZE = 5

    def __init__(self, user_id: int, server: str, page: int = 0, notice: str | None = None):
        super().__init__(timeout=300)
        self.user_id = user_id
        self.server = server
        self.page = page
        self.notice = notice
        self._build_layout()

    def _build_layout(self):
        checks = {location: (checked_at, count) for location, checked_at, count in get_location_check_summary(self.server)}
        reports = {location: count for report_server, location, _x, _y, count in get_trader_report_summary() if report_server == self.server}
        locations = get_all_locations()
        found_locations = sorted(location for location in locations if location in reports)
        unverified_locations = sorted(
            location for location in locations if location not in reports and location not in checks
        )
        checked_locations = sorted(
            location for location in locations if location not in reports and location in checks
        )
        ordered_locations = (
            [("found", location) for location in found_locations]
            + [("unverified", location) for location in unverified_locations]
            + [("checked", location) for location in checked_locations]
        )
        pages = max(1, (len(ordered_locations) + self.PAGE_SIZE - 1) // self.PAGE_SIZE)
        self.page = max(0, min(self.page, pages - 1))
        page_locations = ordered_locations[self.page * self.PAGE_SIZE:(self.page + 1) * self.PAGE_SIZE]
        checked_count = len(checked_locations)
        user_checks = get_user_checked_locations(self.server, self.user_id)
        header = (
            f"# Поиск · {get_server_display_name(self.server)}\n"
            f"Проверено: **{checked_count}/{len(locations)}** · "
            f"Страница {self.page + 1}/{pages}"
        )
        container = discord.ui.Container(discord.ui.TextDisplay(header))
        if self.notice:
            container.add_item(discord.ui.TextDisplay(f"> {self.notice}"))

        group_titles = {
            "found": f"## 🕵️ Торговец найден · {len(found_locations)}",
            "unverified": f"## ❔ Ещё не проверяли · {len(unverified_locations)}",
            "checked": f"## ✅ Проверено, торговца нет · {len(checked_locations)}",
        }
        current_group = None
        for index, (group, location) in enumerate(page_locations):
            if group != current_group:
                container.add_item(discord.ui.TextDisplay(group_titles[group]))
                current_group = group
            if group == "found":
                status = f"Торговец подтверждён · сообщений: {reports[location]}"
            elif group == "checked":
                checked_at, count = checks[location]
                people = "игрок" if count == 1 else "игрока" if 2 <= count <= 4 else "игроков"
                status = f"Проверено {humanize_check_time(checked_at)} · {count} {people}"
            else:
                status = "Ещё не проверяли"
            container.add_item(discord.ui.TextDisplay(f"**{location}**\n{status}"))
            if group == "found":
                found_actions = [
                    LocationStatusActionButton(self.user_id, self.server, location, "found")
                ]
                map_url = get_brodyaga_map_url(location)
                if map_url:
                    found_actions.append(
                        discord.ui.Button(
                            label="На карте", style=discord.ButtonStyle.link, url=map_url
                        )
                    )
                actions = discord.ui.ActionRow(*found_actions)
            else:
                check_action = "cancel" if location in user_checks else "check"
                actions = discord.ui.ActionRow(
                    LocationStatusActionButton(self.user_id, self.server, location, check_action),
                    LocationStatusActionButton(
                        self.user_id, self.server, location, "found", label="Нашёл"
                    ),
                )
            container.add_item(actions)
            if index < len(page_locations) - 1:
                container.add_item(
                    discord.ui.Separator(spacing=discord.SeparatorSpacing.small)
                )

        container.add_item(
            discord.ui.ActionRow(LocationPhotoSelect([location for _group, location in page_locations]))
        )
        container.add_item(
            discord.ui.ActionRow(
                self._navigation_button("Назад", -1, self.page <= 0),
                self._refresh_button(),
                self._navigation_button("Вперёд", 1, self.page >= pages - 1),
            )
        )
        self.add_item(container)

    def _navigation_button(self, label: str, step: int, disabled: bool):
        button = discord.ui.Button(label=label, style=discord.ButtonStyle.secondary, disabled=disabled)

        async def callback(interaction: discord.Interaction):
            await self.render(interaction, page=self.page + step)

        button.callback = callback
        return button

    def _refresh_button(self):
        button = discord.ui.Button(label="Обновить", style=discord.ButtonStyle.primary)

        async def callback(interaction: discord.Interaction):
            await self.render(interaction)

        button.callback = callback
        return button

    async def render(self, interaction: discord.Interaction, notice: str | None = None, page: int | None = None):
        if interaction.user.id != self.user_id:
            await interaction.response.send_message("Это не ваше меню.", ephemeral=True)
            return
        await interaction.response.edit_message(
            view=LocationStatusV2View(
                self.user_id, self.server, self.page if page is None else page, notice
            )
        )


async def start_location_status(interaction: discord.Interaction, show_progress: bool = False):
    trader_statuses = get_server_trader_statuses()
    view = discord.ui.View(timeout=300)
    select = discord.ui.Select(
        placeholder="Выберите сервер...",
        options=[
            discord.SelectOption(
                label=get_server_display_name(server),
                value=server,
                description=trader_statuses[server][1],
                emoji="🌐",
            )
            if trader_statuses[server][1]
            else discord.SelectOption(
                label=get_server_display_name(server), value=server, emoji="🌐"
            )
            for server in SERVERS
        ],
    )

    async def select_server(select_interaction: discord.Interaction):
        if select_interaction.user.id != interaction.user.id:
            await select_interaction.response.send_message("Это не ваше меню.", ephemeral=True)
            return
        server = select.values[0]
        await select_interaction.response.edit_message(
            content=None,
            embed=None,
            view=LocationStatusV2View(interaction.user.id, server),
        )

    select.callback = select_server
    view.add_item(select)
    if show_progress:
        embed = build_search_overview_embed()
    else:
        embed = discord.Embed(
            title="🗺️ Поиск / статус",
            description="Выберите сервер, чтобы увидеть проверенные локации.",
            color=discord.Color.gold(),
        )
    await interaction.response.send_message(embed=embed, view=view, ephemeral=True)


# Обработка ошибок
@bot.event
async def on_command_error(ctx, error):
    """Обработка ошибок команд"""
    if isinstance(error, commands.CommandNotFound):
        await send_main_menu_from_command(ctx)
    else:
        print(f"Ошибка: {error}")
        await ctx.send(f"Произошла ошибка: {str(error)}")


async def send_main_menu_from_command(ctx):
    """Отправка главного меню из текстовой команды"""
    ensure_user_registered(ctx.author)

    await ctx.send(embed=build_main_menu_embed(), view=MainMenuView())


# Команда для вызова главного меню
@bot.command(name="start", help="Показать главное меню")
async def start_command(ctx):
    """Команда для вызова главного меню"""
    await send_main_menu_from_command(ctx)


# Slash команда (если настроены)
@bot.tree.command(name="menu", description="Показать главное меню бота")
async def menu_command(interaction: discord.Interaction):
    """Slash команда для главного меню"""
    await send_main_menu(interaction)


if __name__ == "__main__":
    bot.run(TOKEN)
