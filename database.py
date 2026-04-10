import sqlite3
import os
from datetime import datetime

# Определяем директорию для БД
DB_DIR = os.path.join(os.path.dirname(__file__), "data")
os.makedirs(DB_DIR, exist_ok=True)
DB_PATH = os.path.join(DB_DIR, "bot_database.db")


def init_database():
    """Инициализация базы данных и создание таблиц если они не существуют"""
    conn = sqlite3.connect(DB_PATH, timeout=10)
    cursor = conn.cursor()

    # Таблица пользователей
    cursor.execute("""
        CREATE TABLE IF NOT EXISTS users (
            user_id BIGINT PRIMARY KEY,
            username VARCHAR(100),
            registration_date TIMESTAMP
        )
    """)

    # Таблица текущих отчетов о торговце
    cursor.execute("""
        CREATE TABLE IF NOT EXISTS trader_reports (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            server VARCHAR(20),
            location_name VARCHAR(50),
            x_coord INTEGER,
            y_coord INTEGER,
            reporter_id BIGINT,
            report_date TIMESTAMP,
            is_first_reporter BOOLEAN DEFAULT FALSE,
            FOREIGN KEY (reporter_id) REFERENCES users(user_id)
        )
    """)

    # Таблица истории отчетов
    cursor.execute("""
        CREATE TABLE IF NOT EXISTS trader_history (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            server VARCHAR(20),
            location_name VARCHAR(50),
            x_coord INTEGER,
            y_coord INTEGER,
            reporter_id BIGINT,
            report_date TIMESTAMP,
            is_first_reporter BOOLEAN DEFAULT FALSE,
            archived_at TIMESTAMP,
            FOREIGN KEY (reporter_id) REFERENCES users(user_id)
        )
    """)

    conn.commit()
    conn.close()


def register_user(user_id: int, username: str):
    """Регистрация нового пользователя"""
    conn = sqlite3.connect(DB_PATH, timeout=10)
    cursor = conn.cursor()

    cursor.execute(
        "SELECT user_id FROM users WHERE user_id = ?", (user_id,)
    )
    if cursor.fetchone() is None:
        cursor.execute(
            "INSERT INTO users (user_id, username, registration_date) VALUES (?, ?, ?)",
            (user_id, username, datetime.now()),
        )
        conn.commit()
        conn.close()
        return True  # Новый пользователь
    conn.close()
    return False  # Уже зарегистрирован


def is_user_registered(user_id: int) -> bool:
    """Проверка, зарегистрирован ли пользователь"""
    conn = sqlite3.connect(DB_PATH, timeout=10)
    cursor = conn.cursor()
    cursor.execute("SELECT user_id FROM users WHERE user_id = ?", (user_id,))
    result = cursor.fetchone() is not None
    conn.close()
    return result


def add_trader_report(
    server: str, location_name: str, x_coord: int, y_coord: int, reporter_id: int
):
    """Добавление или обновление отчета (1 голос = 1 пользователь на сервере)"""
    conn = sqlite3.connect(DB_PATH, timeout=10)
    cursor = conn.cursor()

    # Проверяем, есть ли уже запись этого пользователя на этом сервере
    cursor.execute(
        "SELECT location_name FROM trader_reports WHERE server = ? AND reporter_id = ?",
        (server, reporter_id),
    )
    existing_user = cursor.fetchone()

    if existing_user and existing_user[0] == location_name:
        # Пользователь уже сообщал эту локацию — просто обновляем время
        cursor.execute(
            "UPDATE trader_reports SET report_date = ?, x_coord = ?, y_coord = ? WHERE server = ? AND reporter_id = ?",
            (datetime.now(), x_coord, y_coord, server, reporter_id),
        )
        conn.commit()
        conn.close()
        return False  # Не первый

    if existing_user:
        # Пользователь сообщал другую локацию — удаляем старый голос
        cursor.execute(
            "DELETE FROM trader_reports WHERE server = ? AND reporter_id = ?",
            (server, reporter_id),
        )

    # Проверяем, есть ли уже эта локация в БД
    cursor.execute(
        "SELECT COUNT(*) FROM trader_reports WHERE server = ? AND location_name = ?",
        (server, location_name),
    )
    count = cursor.fetchone()[0]
    is_first = count == 0

    # Вставляем новую запись
    cursor.execute(
        "INSERT INTO trader_reports (server, location_name, x_coord, y_coord, reporter_id, report_date, is_first_reporter) VALUES (?, ?, ?, ?, ?, ?, ?)",
        (server, location_name, x_coord, y_coord, reporter_id, datetime.now(), is_first),
    )

    conn.commit()
    conn.close()
    return is_first


def get_trader_reports():
    """Получение всех текущих отчетов о торговце"""
    conn = sqlite3.connect(DB_PATH, timeout=10)
    cursor = conn.cursor()
    cursor.execute("""
        SELECT tr.server, tr.location_name, tr.x_coord, tr.y_coord, tr.is_first_reporter, u.username
        FROM trader_reports tr
        JOIN users u ON tr.reporter_id = u.user_id
        ORDER BY tr.server, tr.report_date
    """)
    reports = cursor.fetchall()
    conn.close()
    return reports


def has_trader_report_for_server(server: str, location_name: str) -> bool:
    """Проверка, есть ли уже отчет для сервера и локации"""
    conn = sqlite3.connect(DB_PATH, timeout=10)
    cursor = conn.cursor()
    cursor.execute(
        "SELECT COUNT(*) FROM trader_reports WHERE server = ? AND location_name = ?",
        (server, location_name),
    )
    count = cursor.fetchone()[0]
    conn.close()
    return count > 0


def archive_reports():
    """Архивирование текущих отчетов в историю"""
    conn = sqlite3.connect(DB_PATH, timeout=10)
    cursor = conn.cursor()

    now = datetime.now()

    cursor.execute("""
        INSERT INTO trader_history (server, location_name, x_coord, y_coord, reporter_id, report_date, is_first_reporter, archived_at)
        SELECT server, location_name, x_coord, y_coord, reporter_id, report_date, is_first_reporter, ?
        FROM trader_reports
    """, (now,))

    cursor.execute("DELETE FROM trader_reports")

    conn.commit()
    conn.close()


def get_history_by_date(date_str: str):
    """Получение истории за определенную дату (формат: YYYY-MM-DD)"""
    conn = sqlite3.connect(DB_PATH, timeout=10)
    cursor = conn.cursor()
    cursor.execute("""
        SELECT th.server, th.location_name, th.x_coord, th.y_coord, th.is_first_reporter, u.username, th.archived_at
        FROM trader_history th
        JOIN users u ON th.reporter_id = u.user_id
        WHERE DATE(th.archived_at) = ?
        ORDER BY th.archived_at, th.server
    """, (date_str,))
    results = cursor.fetchall()
    conn.close()
    return results
