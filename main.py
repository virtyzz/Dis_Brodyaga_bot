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
    archive_reports,
    has_trader_report_for_server,
)
from coords_handler import (
    get_all_locations,
    get_location_coords,
    get_screenshot_path,
    get_location_groups,
    get_unique_base_locations,
    get_locations_by_base,
    SERVERS,
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

# Московский часовой пояс
MSK = pytz.timezone("Europe/Moscow")


def escape_markdown(text: str) -> str:
    """Экранирование спецсимволов Discord markdown"""
    return re.sub(r'([*_~`|\\])', r'\\\1', text)


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

    view = MainMenuView()
    embed = discord.Embed(
        title="🎒 Бродячий Торговец",
        description="Выберите действие:",
        color=discord.Color.gold(),
    )
    embed.add_field(
        name="📍 Где торговец?",
        value="Узнать текущее местоположение торговца на всех серверах",
        inline=False,
    )
    embed.add_field(
        name="📢 Сообщить о торговце",
        value="Сообщить, где вы видели бродячего торговца",
        inline=False,
    )
    embed.add_field(
        name="ℹ️ Помощь",
        value="Информация о боте и командах",
        inline=False,
    )

    await interaction.response.send_message(embed=embed, view=view)


class MainMenuView(discord.ui.View):
    """Главное меню с кнопками (persistent — работает после перезапуска)"""

    def __init__(self):
        super().__init__(timeout=None)

    @discord.ui.button(label="📍 Где торговец?", style=discord.ButtonStyle.primary, custom_id="main_menu_check_trader")
    async def check_trader(
        self, interaction: discord.Interaction, button: discord.ui.Button
    ):
        ensure_user_registered(interaction.user)
        await show_trader_locations(interaction)

    @discord.ui.button(label="📢 Сообщить о торговце", style=discord.ButtonStyle.success, custom_id="main_menu_report_trader")
    async def report_trader(
        self, interaction: discord.Interaction, button: discord.ui.Button
    ):
        ensure_user_registered(interaction.user)
        await start_report_flow(interaction)

    @discord.ui.button(label="🔗 Поделиться или добавить себе", style=discord.ButtonStyle.secondary, custom_id="main_menu_share_bot")
    async def share_bot(
        self, interaction: discord.Interaction, button: discord.ui.Button
    ):
        await interaction.response.send_message(
            "Ссылка для добавления и отправки друзьям:\n"
            "https://discord.com/oauth2/authorize?client_id=1492221304173236406",
            ephemeral=True,
        )

    @discord.ui.button(label="ℹ️ Помощь", style=discord.ButtonStyle.secondary, custom_id="main_menu_help")
    async def help_info(
        self, interaction: discord.Interaction, button: discord.ui.Button
    ):
        embed = discord.Embed(
            title="ℹ️ Помощь",
            description="**Бот Бродячий Торговец** помогает отслеживать местоположение бродячего торговца в DayZ.",
            color=discord.Color.blue(),
        )
        embed.add_field(
            name="📍 Где торговец?",
            value="Показывает текущие сообщения о местоположении торговца на всех серверах (cherno-1, cherno-2, cherno-3, cherno-4).",
            inline=False,
        )
        embed.add_field(
            name="📢 Сообщить о торговце",
            value="Позволяет сообщить, где вы видели торговца. Выберите сервер, локацию и конкретное здание.",
            inline=False,
        )
        embed.add_field(
            name="🔄 Обновление данных",
            value="Данные сбрасываются ежедневно в 00:00 по московскому времени.",
            inline=False,
        )
        await interaction.response.send_message(embed=embed, ephemeral=True)


async def show_trader_locations(interaction: discord.Interaction):
    """Показ местоположения торговца с кнопками скриншотов"""
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

    # Отправляем каждый сервер отдельно
    first_server = True
    for server in SERVERS:
        if server in reports_grouped:
            embed = discord.Embed(
                title=f"📍 {server}",
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
                        label=f"📷 {loc_name}",
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
                    label="🗺️ Посмотреть на карте",
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
                title=f"📍 {server}",
                description="Нет данных о торговце на этом сервере.",
                color=discord.Color.orange(),
            )
            if first_server:
                await interaction.response.send_message(embed=embed, ephemeral=True)
                first_server = False
            else:
                await interaction.followup.send(embed=embed, ephemeral=True)


async def start_report_flow(interaction: discord.Interaction):
    """Начало процесса сообщения о торговце"""
    # Очищаем просроченные состояния
    clean_expired_user_states()

    # Сохраняем состояние пользователя с таймстампом
    user_states[interaction.user.id] = {
        "step": 1,
        "server": None,
        "location_base": None,
        "timestamp": time.time(),
    }

    # Шаг 1: Выбор сервера
    view = ServerSelectView(interaction.user.id)
    embed = discord.Embed(
        title="📢 Сообщить о торговце",
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
            discord.SelectOption(label=server, value=server, emoji="🌐")
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
                "⏰ Время вышло. Начните заново, нажав 📢 Сообщить о торговце.",
                ephemeral=True,
            )
            return

        server = select.values[0]
        user_states[interaction.user.id]["server"] = server
        user_states[interaction.user.id]["step"] = 2

        # Шаг 2: Выбор локации
        base_locations = get_unique_base_locations()
        view = make_location_select_view(self.user_id, server, base_locations)

        embed = discord.Embed(
            title="📢 Сообщить о торговце",
            description=f"**Шаг 2/3:** Выберите локацию на сервере `{server}`:",
            color=discord.Color.green(),
        )

        await interaction.response.edit_message(embed=embed, view=view)


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
                            "⏰ Время вышло. Начните заново, нажав 📢 Сообщить о торговце.",
                            ephemeral=True,
                        )
                        return

                    location_base = interaction.data["values"][0]
                    user_states[interaction.user.id]["location_base"] = location_base
                    user_states[interaction.user.id]["step"] = 3

                    location_variants = get_locations_by_base(location_base)

                    if len(location_variants) == 1:
                        location_name = location_variants[0]
                        await process_report(interaction, location_name)
                    else:
                        view = BuildingSelectView(user_id, server, location_variants)
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

    def __init__(self, user_id: int, server: str, locations: list):
        super().__init__(timeout=300)
        self.user_id = user_id
        self.server = server
        self.locations = locations
        self.current_index = 0

    def _build_embed(self):
        location_name = self.locations[self.current_index]
        embed = discord.Embed(
            title="📢 Выберите здание",
            description=f"**{location_name}**\n({self.current_index + 1}/{len(self.locations)})",
            color=discord.Color.green(),
        )
        return embed

    def _update_buttons(self):
        # Удаляем все кнопки и пересоздаём
        self.clear_items()

        btn_prev = discord.ui.Button(
            label="⬅️ Назад",
            style=discord.ButtonStyle.secondary,
            disabled=self.current_index == 0,
        )
        btn_prev.callback = self._prev_callback
        self.add_item(btn_prev)

        btn_select = discord.ui.Button(
            label="✅ Выбрать это",
            style=discord.ButtonStyle.success,
        )
        btn_select.callback = self._select_callback
        self.add_item(btn_select)

        btn_next = discord.ui.Button(
            label="Вперёд ➡️",
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
            description=f"Вы **первый** сообщили о торговце в локации `{location_name}` на сервере `{server}`!",
            color=discord.Color.green(),
        )
    else:
        embed = discord.Embed(
            title="✅ Данные обновлены!",
            description=f"Ваше сообщение о торговце в локации `{location_name}` на сервере `{server}` обновлено!",
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

    view = MainMenuView()
    embed = discord.Embed(
        title="🎒 Бродячий Торговец",
        description="Выберите действие:",
        color=discord.Color.gold(),
    )
    await ctx.send(embed=embed, view=view)


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
