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

    cursor.execute("""
        CREATE TABLE IF NOT EXISTS location_checks (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            server VARCHAR(20) NOT NULL,
            location_name VARCHAR(50) NOT NULL,
            checker_id BIGINT NOT NULL,
            checked_at TIMESTAMP NOT NULL,
            status VARCHAR(20) NOT NULL DEFAULT 'not_found',
            UNIQUE(server, location_name, checker_id),
            FOREIGN KEY (checker_id) REFERENCES users(user_id)
        )
    """)
    _migrate_location_checks_schema(cursor)
    cursor.execute("""
        CREATE INDEX IF NOT EXISTS idx_location_checks_server_location
        ON location_checks(server, location_name)
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
        cursor.execute(
            "DELETE FROM location_checks WHERE server = ? AND location_name = ?",
            (server, location_name),
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

    # A confirmed trader report supersedes all "not found" checks for this point.
    cursor.execute(
        "DELETE FROM location_checks WHERE server = ? AND location_name = ?",
        (server, location_name),
    )

    conn.commit()
    conn.close()
    return is_first


def _migrate_location_checks_schema(cursor):
    """Upgrade the first release's one-check-per-server constraint safely."""
    cursor.execute("PRAGMA index_list(location_checks)")
    for index in cursor.fetchall():
        if not index[2]:
            continue
        cursor.execute(f'PRAGMA index_info("{index[1]}")')
        columns = [item[2] for item in cursor.fetchall()]
        if columns != ["server", "checker_id"]:
            continue

        cursor.execute("""
            CREATE TABLE location_checks_new (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                server VARCHAR(20) NOT NULL,
                location_name VARCHAR(50) NOT NULL,
                checker_id BIGINT NOT NULL,
                checked_at TIMESTAMP NOT NULL,
                status VARCHAR(20) NOT NULL DEFAULT 'not_found',
                UNIQUE(server, location_name, checker_id),
                FOREIGN KEY (checker_id) REFERENCES users(user_id)
            )
        """)
        cursor.execute("""
            INSERT INTO location_checks_new
                (id, server, location_name, checker_id, checked_at, status)
            SELECT id, server, location_name, checker_id, checked_at, status
            FROM location_checks
        """)
        cursor.execute("DROP TABLE location_checks")
        cursor.execute("ALTER TABLE location_checks_new RENAME TO location_checks")
        break


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


def get_trader_report_summary():
    """Return public aggregate reports without Discord user data."""
    conn = sqlite3.connect(DB_PATH, timeout=10)
    cursor = conn.cursor()
    cursor.execute("""
        SELECT server, location_name, x_coord, y_coord, COUNT(*)
        FROM trader_reports
        GROUP BY server, location_name, x_coord, y_coord
        ORDER BY server, location_name
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


def add_location_check(server: str, location_name: str, checker_id: int):
    """Save or refresh a player's 'trader not found' check for one exact point."""
    conn = sqlite3.connect(DB_PATH, timeout=10)
    cursor = conn.cursor()
    cursor.execute(
        "SELECT 1 FROM trader_reports WHERE server = ? AND location_name = ? LIMIT 1",
        (server, location_name),
    )
    if cursor.fetchone():
        conn.close()
        return False
    cursor.execute(
        """
        INSERT INTO location_checks (server, location_name, checker_id, checked_at, status)
        VALUES (?, ?, ?, ?, 'not_found')
        ON CONFLICT(server, location_name, checker_id)
        DO UPDATE SET checked_at = excluded.checked_at, status = excluded.status
        """,
        (server, location_name, checker_id, datetime.now()),
    )
    conn.commit()
    conn.close()
    return True


def remove_location_check(server: str, location_name: str, checker_id: int) -> bool:
    """Withdraw the player's check for a point."""
    conn = sqlite3.connect(DB_PATH, timeout=10)
    cursor = conn.cursor()
    cursor.execute(
        """
        DELETE FROM location_checks
        WHERE server = ? AND location_name = ? AND checker_id = ?
        """,
        (server, location_name, checker_id),
    )
    removed = cursor.rowcount > 0
    conn.commit()
    conn.close()
    return removed


def get_location_check_summary(server: str):
    """Return (location_name, latest_check_time, number_of_checkers) by location."""
    conn = sqlite3.connect(DB_PATH, timeout=10)
    cursor = conn.cursor()
    cursor.execute(
        """
        SELECT location_name, MAX(checked_at), COUNT(DISTINCT checker_id)
        FROM location_checks
        WHERE server = ? AND status = 'not_found'
        GROUP BY location_name
        """,
        (server,),
    )
    rows = cursor.fetchall()
    conn.close()
    return rows


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
    cursor.execute("DELETE FROM location_checks")

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
