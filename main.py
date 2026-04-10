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

    # Группировка отчетов по серверам
    reports_by_server = {}
    for server, location_name, x, y, is_first, username in reports:
        if server not in reports_by_server:
            reports_by_server[server] = []
        reports_by_server[server].append(
            {
                "location": location_name,
                "x": x,
                "y": y,
                "is_first": is_first,
                "username": username,
            }
        )

    # Создаем embed для каждого сервера
    embeds = []
    for server in SERVERS:
        if server in reports_by_server:
            embed = discord.Embed(
                title=f"📍 {server}",
                color=discord.Color.green(),
            )

            for report in reports_by_server[server]:
                screenshot = get_screenshot_path(report["location"])

                # Формируем список пользователей (первый жирным)
                # Собираем всех пользователей для этой локации
                all_users_for_location = [
                    r["username"]
                    for r in reports_by_server[server]
                    if r["location"] == report["location"]
                ]

                # Убираем дубликаты сохраняя порядок
                seen = set()
                unique_users = []
                for u in all_users_for_location:
                    if u not in seen:
                        unique_users.append(u)
                        seen.add(u)

                # Первый пользователь жирным
                users_formatted = ", ".join(
                    [f"**{unique_users[0]}**"] + unique_users[1:]
                )

                embed.add_field(
                    name=f"**{report['location']}**",
                    value=f"📍 Координаты: X: {report['x']}, Y: {report['y']}\n👥 Сообщили: {users_formatted}",
                    inline=False,
                )

                # Прикрепляем скриншот если есть
                if screenshot and os.path.exists(screenshot):
                    file = discord.File(screenshot, filename=f"{report['location']}.png")
                    embed.set_image(
                        url=f"attachment://{report['location']}.png"
                    )
                    embeds.append((embed, file))
                else:
                    embeds.append((embed, None))
        else:
            embed = discord.Embed(
                title=f"📍 {server}",
                description="Нет данных о торговце на этом сервере.",
                color=discord.Color.orange(),
            )
            embeds.append((embed, None))

    # Отправляем первый embed
    first_embed, first_file = embeds[0]
    if first_file:
        await interaction.response.send_message(embed=first_embed, file=first_file)
    else:
        await interaction.response.send_message(embed=first_embed)


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
        view = LocationSelectView(self.user_id, server)
        base_locations = get_unique_base_locations()
        options = [
            discord.SelectOption(label=loc, value=loc)
            for loc in base_locations
        ]

        # Обновляем select с локациями
        select_menu = LocationSelectView.create_select(options)

        embed = discord.Embed(
            title="📢 Сообщить о торговце",
            description=f"**Шаг 2/3:** Выберите локацию на сервере `{server}`:",
            color=discord.Color.green(),
        )

        await interaction.response.edit_message(embed=embed, view=view)


class LocationSelectView(discord.ui.View):
    """Выбор локации"""

    def __init__(self, user_id: int, server: str):
        super().__init__(timeout=300)
        self.user_id = user_id
        self.server = server

    @staticmethod
    def create_select(options):
        """Создание select меню с опциями"""
        select = discord.ui.Select(
            placeholder="Выберите локацию...",
            options=options,
        )
        return select

    @discord.ui.select(placeholder="Выберите локацию...")
    async def select_location(
        self, interaction: discord.Interaction, select: discord.ui.Select
    ):
        if interaction.user.id != self.user_id:
            await interaction.response.send_message(
                "Это не ваше меню!", ephemeral=True
            )
            return

        location_base = select.values[0]
        user_states[interaction.user.id]["location_base"] = location_base
        user_states[interaction.user.id]["step"] = 3

        # Проверяем, есть ли несколько вариантов этой локации
        location_variants = get_locations_by_base(location_base)

        if len(location_variants) == 1:
            # Только один вариант - сразу записываем
            location_name = location_variants[0]
            await process_report(interaction, location_name)
        else:
            # Несколько вариантов - выбор конкретного здания
            view = BuildingSelectView(self.user_id, self.server, location_variants)
            embed = discord.Embed(
                title="📢 Сообщить о торговце",
                description=f"**Шаг 3/3:** Выберите конкретное здание для `{location_base}`:",
                color=discord.Color.green(),
            )
            await interaction.response.edit_message(embed=embed, view=view)


class BuildingSelectView(discord.ui.View):
    """Выбор конкретного здания"""

    def __init__(self, user_id: int, server: str, locations: list):
        super().__init__(timeout=300)
        self.user_id = user_id
        self.server = server
        self.locations = locations

        # Добавляем кнопки для каждого здания
        for location in locations:
            button = discord.ui.Button(
                label=location,
                style=discord.ButtonStyle.secondary,
                custom_id=f"building_{location}",
            )
            button.callback = self.make_callback(location)
            self.add_item(button)

    def make_callback(self, location_name: str):
        """Создание callback для кнопки здания"""

        async def callback(interaction: discord.Interaction):
            if interaction.user.id != self.user_id:
                await interaction.response.send_message(
                    "Это не ваше меню!", ephemeral=True
                )
                return

            await process_report(interaction, location_name)

        return callback


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
        await interaction.response.edit_message(embed=embed, view=None, file=file)
    else:
        await interaction.response.edit_message(embed=embed, view=None)


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
