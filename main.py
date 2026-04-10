import discord
from discord.ext import commands, tasks
import os
import pytz
from datetime import datetime, time

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
if not TOKEN:
    # Попытка загрузить из файла .env если существует
    env_file = os.path.join(os.path.dirname(__file__), ".env")
    if os.path.exists(env_file):
        with open(env_file, "r", encoding="utf-8") as f:
            for line in f:
                if line.startswith("DISCORD_BOT_TOKEN="):
                    TOKEN = line.strip().split("=", 1)[1]
                    break

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
# user_id -> {"step": int, "server": str, "location_base": str}
user_states = {}

# Московский часовой пояс
MSK = pytz.timezone("Europe/Moscow")


@bot.event
async def on_ready():
    """Событие при запуске бота"""
    print(f"Бот запущен: {bot.user.name} (ID: {bot.user.id})")

    # Инициализация базы данных
    init_database()
    print("База данных инициализирована")

    # Запуск задачи архивирования
    archive_daily.start()


@tasks.loop(time=time(hour=0, minute=0, tzinfo=MSK))
async def archive_daily():
    """Ежедневное архивирование данных в 00:00 MSK"""
    print("Выполняется ежедневное архивирование данных...")
    archive_reports()
    print("Архивирование завершено")


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
    """Главное меню с кнопками"""

    def __init__(self):
        super().__init__(timeout=None)

    @discord.ui.button(label="📍 Где торговец?", style=discord.ButtonStyle.primary)
    async def check_trader(
        self, interaction: discord.Interaction, button: discord.ui.Button
    ):
        ensure_user_registered(interaction.user)
        await show_trader_locations(interaction)

    @discord.ui.button(label="📢 Сообщить о торговце", style=discord.ButtonStyle.success)
    async def report_trader(
        self, interaction: discord.Interaction, button: discord.ui.Button
    ):
        ensure_user_registered(interaction.user)
        await start_report_flow(interaction)

    @discord.ui.button(label="ℹ️ Помощь", style=discord.ButtonStyle.secondary)
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
            value="Данные сбрасываются ежедневно в 00:00 по московскому времени и сохраняются в архив.",
            inline=False,
        )
        await interaction.response.send_message(embed=embed, ephemeral=True)


async def show_trader_locations(interaction: discord.Interaction):
    """Показ местоположения торговца по всем серверам"""
    reports = get_trader_reports()

    if not reports:
        embed = discord.Embed(
            title="📍 Где торговец?",
            description="На данный момент нет сообщений о местоположении торговца ни на одном сервере.",
            color=discord.Color.orange(),
        )
        await interaction.response.send_message(embed=embed, ephemeral=True)
        return

    # Группировка отчетов по серверам и локациям
    # Структура: {server: {location: {"x": x, "y": y, "users": [username1, ...]}}}
    reports_grouped = {}
    for server, location_name, x, y, is_first, username in reports:
        if server not in reports_grouped:
            reports_grouped[server] = {}
        if location_name not in reports_grouped[server]:
            reports_grouped[server][location_name] = {"x": x, "y": y, "users": []}
        if username not in reports_grouped[server][location_name]["users"]:
            reports_grouped[server][location_name]["users"].append(username)

    # Собираем embeds и файлы для каждого сервера
    embeds = []
    files = []
    for server in SERVERS:
        if server in reports_grouped:
            embed = discord.Embed(
                title=f"📍 {server}",
                color=discord.Color.green(),
            )

            for location_name, data in reports_grouped[server].items():
                # Первый пользователь жирным
                users = data["users"]
                users_formatted = ", ".join([f"**{users[0]}**"] + users[1:])

                embed.add_field(
                    name=f"**{location_name}**",
                    value=f"📍 Координаты: X: {data['x']}, Y: {data['y']}\n👥 Сообщили: {users_formatted}",
                    inline=False,
                )

                # Прикрепляем первый доступный скриншот
                screenshot = get_screenshot_path(location_name)
                if screenshot and os.path.exists(screenshot):
                    f = discord.File(screenshot, filename="screenshot.png")
                    embed.set_image(url="attachment://screenshot.png")
                    files.append(f)
                    break  # Только один скриншот на сервер

            embeds.append(embed)
        else:
            embed = discord.Embed(
                title=f"📍 {server}",
                description="Нет данных о торговце на этом сервере.",
                color=discord.Color.orange(),
            )
            embeds.append(embed)

    # Отправляем первый embed
    if files:
        await interaction.response.send_message(
            embed=embeds[0], files=files[:1], ephemeral=True
        )
    else:
        await interaction.response.send_message(embed=embeds[0], ephemeral=True)

    # Отправляем остальные сервера отдельными сообщениями
    for i in range(1, len(embeds)):
        await interaction.followup.send(embed=embeds[i], ephemeral=True)


async def start_report_flow(interaction: discord.Interaction):
    """Начало процесса сообщения о торговце"""
    # Сохраняем состояние пользователя
    user_states[interaction.user.id] = {"step": 1, "server": None, "location_base": None}

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
            title="✅ Спасибо!",
            description=f"Ваше сообщение о торговце в локации `{location_name}` на сервере `{server}` записано!",
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
