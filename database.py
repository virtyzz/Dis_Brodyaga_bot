import sqlite3
import os
import math
import json
import time
from activity_logging import audit_write
from contextlib import closing
from datetime import datetime, timedelta, timezone

# Определяем директорию для БД
DB_DIR = os.path.join(os.path.dirname(__file__), "data")
os.makedirs(DB_DIR, exist_ok=True)
DB_PATH = os.path.join(DB_DIR, "bot_database.db")
REPORT_COOLDOWN_SECONDS = 60


class ReportCooldownError(ValueError):
    def __init__(self, retry_after: int):
        self.retry_after = retry_after
        super().__init__(f"Подождите {retry_after} сек. перед сменой локации на этом сервере.")


def _utc_now(now=None):
    now = now or datetime.now(timezone.utc)
    return now.replace(tzinfo=timezone.utc) if now.tzinfo is None else now.astimezone(timezone.utc)


def _cycle_start(now=None):
    now = _utc_now(now)
    boundary = now.replace(hour=1, minute=0, second=0, microsecond=0)
    return boundary if now >= boundary else boundary - timedelta(days=1)


def _archive(cursor, now):
    cursor.execute("""
        INSERT INTO trader_history (server, location_name, x_coord, y_coord, reporter_id,
                                    report_date, is_first_reporter, archived_at)
        SELECT server, location_name, x_coord, y_coord, reporter_id, report_date,
               is_first_reporter, ? FROM trader_reports
    """, (_utc_now(now).replace(tzinfo=None),))
    for table in ("trader_reports", "location_checks", "trader_notification_events",
                  "trader_notifications", "report_cooldowns"):
        cursor.execute(f"DELETE FROM {table}")
    _queue_map_snapshot(cursor, now)


def _queue_map_snapshot(cursor, now=None):
    """A coalescing outbox: public state and revision commit with the vote."""
    reports = [dict(zip(("server", "location_name", "x", "y", "confirmations"), row))
               for row in cursor.execute("""
        SELECT server, location_name, x_coord, y_coord, COUNT(*) FROM trader_reports
        GROUP BY server, location_name, x_coord, y_coord ORDER BY server, location_name
    """)]
    payload = json.dumps({"reports": reports, "expires_at":
        (_cycle_start(now) + timedelta(days=1)).timestamp()}, ensure_ascii=False, separators=(",", ":"))
    cursor.execute("""
        INSERT INTO map_reports_outbox (id, epoch, revision, payload) VALUES (1, ?, 1, ?)
        ON CONFLICT(id) DO UPDATE SET revision = revision + 1, payload = excluded.payload
        WHERE payload != excluded.payload
    """, (str(time.time_ns() // 1000), payload))


def read_map_snapshot(conn):
    row = conn.execute("SELECT epoch, revision, payload FROM map_reports_outbox WHERE id = 1").fetchone()
    if row is None:
        return None
    return {"type": "brodyaga.reports", "epoch": row[0], "revision": row[1], **json.loads(row[2])}


def _ensure_cycle(cursor, now=None):
    now = _utc_now(now)
    cycle = _cycle_start(now).isoformat()
    row = cursor.execute("SELECT value FROM bot_state WHERE key = 'cycle_start'").fetchone()
    if row is None:
        # During the upgrade infer stale legacy data before initializing the
        # persisted boundary; never broadcast yesterday's existing reports.
        oldest = cursor.execute("""
            SELECT MIN(stamp) FROM (
                SELECT report_date AS stamp FROM trader_reports
                UNION ALL SELECT checked_at AS stamp FROM location_checks
            )
        """).fetchone()[0]
        stale = False
        if oldest:
            try:
                stale = _utc_now(datetime.fromisoformat(str(oldest))) < _cycle_start(now)
            except ValueError:
                stale = True
        if stale:
            _archive(cursor, now)
        cursor.execute("INSERT INTO bot_state (key, value) VALUES ('cycle_start', ?)", (cycle,))
        return stale
    if row[0] < cycle:
        _archive(cursor, now)
        cursor.execute("UPDATE bot_state SET value = ? WHERE key = 'cycle_start'", (cycle,))
        return True
    return False


def ensure_current_cycle(now=None) -> bool:
    """Catch up a missed reset once, atomically across processes and restarts."""
    with closing(sqlite3.connect(DB_PATH, timeout=10)) as conn, conn:
        conn.execute("BEGIN IMMEDIATE")
        return _ensure_cycle(conn.cursor(), now)


def init_database():
    """Initialize and safely migrate persistent bot state."""
    with closing(sqlite3.connect(DB_PATH, timeout=10)) as conn, conn:
        cursor = conn.cursor()
        try:
            conn.execute("PRAGMA journal_mode=WAL")
        except sqlite3.OperationalError as error:
            # Bot and HTTP service can initialize the shared file together.
            # Another connection may be changing journal mode right now;
            # BEGIN IMMEDIATE below still serializes the mandatory migration.
            if "locked" not in str(error).lower():
                raise
        cursor.execute("BEGIN IMMEDIATE")

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

        cursor.execute("""
            CREATE TABLE IF NOT EXISTS server_subscriptions (
                user_id BIGINT NOT NULL,
                server VARCHAR(20) NOT NULL,
                PRIMARY KEY (user_id, server)
            )
        """)
        cursor.execute("""
            CREATE TABLE IF NOT EXISTS trader_notification_events (
                server VARCHAR(20) NOT NULL,
                location_name VARCHAR(50) NOT NULL,
                PRIMARY KEY (server, location_name)
            )
        """)
        # Existing reports are already known, so enabling this feature must not
        # cause an old location to be broadcast on the next confirmation.
        cursor.execute("""
            INSERT OR IGNORE INTO trader_notification_events (server, location_name)
            SELECT DISTINCT server, location_name FROM trader_reports
        """)
        cursor.execute("""
            CREATE TABLE IF NOT EXISTS trader_notifications (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                user_id BIGINT NOT NULL,
                server VARCHAR(20) NOT NULL,
                location_name VARCHAR(50) NOT NULL,
                x_coord INTEGER NOT NULL,
                y_coord INTEGER NOT NULL,
                status VARCHAR(20) NOT NULL DEFAULT 'pending',
                attempts INTEGER NOT NULL DEFAULT 0
            )
        """)
        cursor.execute("""
            CREATE INDEX IF NOT EXISTS idx_trader_notifications_status
            ON trader_notifications(status, id)
        """)
        columns = {row[1] for row in cursor.execute("PRAGMA table_info(trader_notifications)")}
        for name, definition in (
            ("cycle_start", "TEXT NOT NULL DEFAULT ''"),
            ("expires_at", "REAL NOT NULL DEFAULT 0"),
            ("next_attempt_at", "REAL NOT NULL DEFAULT 0"),
            ("claimed_until", "REAL NOT NULL DEFAULT 0"),
            ("reporter_id", "BIGINT"),
            ("reporter_name", "TEXT NOT NULL DEFAULT ''"),
        ):
            if name not in columns:
                cursor.execute(f"ALTER TABLE trader_notifications ADD COLUMN {name} {definition}")
        cursor.execute("CREATE TABLE IF NOT EXISTS bot_state (key TEXT PRIMARY KEY, value TEXT NOT NULL)")
        cursor.execute("""
            CREATE TABLE IF NOT EXISTS map_reports_outbox (
                id INTEGER PRIMARY KEY CHECK (id = 1), epoch TEXT NOT NULL,
                revision INTEGER NOT NULL, payload TEXT NOT NULL
            )
        """)
        cursor.execute("""
            CREATE TABLE IF NOT EXISTS report_cooldowns (
                reporter_id BIGINT NOT NULL, server TEXT NOT NULL,
                last_changed_at REAL NOT NULL, PRIMARY KEY (reporter_id, server)
            )
        """)
        _ensure_cycle(cursor)
        _queue_map_snapshot(cursor)
        cycle = _cycle_start()
        cursor.execute("""
            UPDATE trader_notifications SET cycle_start = ?, expires_at = ?
            WHERE cycle_start = ''
        """, (cycle.isoformat(), (cycle + timedelta(days=1)).timestamp()))
        cursor.execute("CREATE INDEX IF NOT EXISTS idx_subscriptions_server ON server_subscriptions(server)")
        cursor.execute("CREATE INDEX IF NOT EXISTS idx_reports_server_location ON trader_reports(server, location_name)")
        cursor.execute("CREATE INDEX IF NOT EXISTS idx_reports_server_reporter ON trader_reports(server, reporter_id)")

def get_subscriptions(user_id: int) -> set[str]:
    with closing(sqlite3.connect(DB_PATH, timeout=10)) as conn, conn:
        return {row[0] for row in conn.execute(
            "SELECT server FROM server_subscriptions WHERE user_id = ?", (user_id,)
        )}


@audit_write("subscriptions_set", "user_id")
def set_subscriptions(user_id: int, servers):
    """Replace a user's selection atomically; remove obsolete pending DMs."""
    from coords_handler import SERVERS
    servers = set(servers)
    if not servers.issubset(SERVERS):
        raise ValueError("Unknown subscription server")
    with closing(sqlite3.connect(DB_PATH, timeout=10)) as conn, conn:
        conn.execute("DELETE FROM server_subscriptions WHERE user_id = ?", (user_id,))
        conn.executemany(
            "INSERT INTO server_subscriptions (user_id, server) VALUES (?, ?)",
            [(user_id, server) for server in servers],
        )
        conn.execute("""
            DELETE FROM trader_notifications WHERE user_id = ? AND NOT EXISTS (
                SELECT 1 FROM server_subscriptions s
                WHERE s.user_id = trader_notifications.user_id
                AND s.server = trader_notifications.server
            )
        """, (user_id,))


def _notification_valid(cursor, notification_id, now):
    return cursor.execute("""
        SELECT 1 FROM trader_notifications n
        WHERE n.id = ? AND n.cycle_start = ? AND n.expires_at > ?
        AND EXISTS (SELECT 1 FROM server_subscriptions s
                    WHERE s.user_id = n.user_id AND s.server = n.server)
        AND EXISTS (SELECT 1 FROM trader_reports r
                    WHERE r.server = n.server AND r.location_name = n.location_name)
    """, (notification_id, _cycle_start(now).isoformat(), now.timestamp())).fetchone() is not None


def get_pending_notifications(limit: int = 20):
    with closing(sqlite3.connect(DB_PATH, timeout=10)) as conn, conn:
        conn.execute("BEGIN IMMEDIATE")
        now = _utc_now()
        _ensure_cycle(conn.cursor(), now)
        return conn.execute("""
            SELECT n.id, n.user_id, n.server, n.location_name, n.x_coord, n.y_coord,
                   COALESCE(NULLIF(n.reporter_name, ''), 'неизвестно')
            FROM trader_notifications n
            WHERE ((n.status = 'pending' AND n.next_attempt_at <= ?)
                   OR (n.status = 'sending' AND n.claimed_until <= ?))
            AND n.expires_at > ? AND n.cycle_start = ?
            AND EXISTS (SELECT 1 FROM server_subscriptions s
                        WHERE s.user_id = n.user_id AND s.server = n.server)
            AND EXISTS (SELECT 1 FROM trader_reports r
                        WHERE r.server = n.server AND r.location_name = n.location_name)
            ORDER BY n.next_attempt_at, n.id LIMIT ?
        """, (now.timestamp(), now.timestamp(), now.timestamp(),
              _cycle_start(now).isoformat(), limit)).fetchall()


def claim_notification(notification_id: int, lease_seconds: int = 120) -> bool:
    """Claim one due row atomically; a crashed worker's lease can be retried."""
    with closing(sqlite3.connect(DB_PATH, timeout=10)) as conn, conn:
        conn.execute("BEGIN IMMEDIATE")
        cursor, now = conn.cursor(), _utc_now()
        _ensure_cycle(cursor, now)
        if not _notification_valid(cursor, notification_id, now):
            return False
        cursor.execute("""
            UPDATE trader_notifications SET status = 'sending', claimed_until = ?
            WHERE id = ? AND ((status = 'pending' AND next_attempt_at <= ?)
                              OR (status = 'sending' AND claimed_until <= ?))
        """, (now.timestamp() + lease_seconds, notification_id, now.timestamp(), now.timestamp()))
        return cursor.rowcount == 1


def notification_is_current(notification_id: int) -> bool:
    with closing(sqlite3.connect(DB_PATH, timeout=10)) as conn, conn:
        conn.execute("BEGIN IMMEDIATE")
        cursor, now = conn.cursor(), _utc_now()
        _ensure_cycle(cursor, now)
        return _notification_valid(cursor, notification_id, now) and cursor.execute(
            "SELECT 1 FROM trader_notifications WHERE id = ? AND status = 'sending' AND claimed_until > ?",
            (notification_id, now.timestamp()),
        ).fetchone() is not None


def finish_notification(notification_id: int, status: str):
    """Transient delivery errors back off until this location's cycle expires."""
    if status not in {"pending", "sent", "failed", "cancelled"}:
        raise ValueError("Invalid notification status")
    with closing(sqlite3.connect(DB_PATH, timeout=10)) as conn, conn:
        conn.execute("BEGIN IMMEDIATE")
        cursor, now = conn.cursor(), _utc_now()
        _ensure_cycle(cursor, now)
        row = cursor.execute(
            "SELECT attempts, expires_at FROM trader_notifications WHERE id = ?", (notification_id,)
        ).fetchone()
        if row is None:
            return
        delay = min(1800, 30 * (2 ** min(row[0], 6)))
        next_attempt = min(now.timestamp() + delay, row[1])
        if status == 'pending' and row[1] <= now.timestamp():
            status = 'cancelled'
        cursor.execute("""
            UPDATE trader_notifications SET attempts = attempts + 1, status = ?,
                next_attempt_at = ?, claimed_until = 0 WHERE id = ?
        """, (status, next_attempt, notification_id))


@audit_write("user_register", "user_id", "created")
def register_user(user_id: int, username: str):
    """Atomic registration, safe for simultaneous bot and HTTP requests."""
    with closing(sqlite3.connect(DB_PATH, timeout=10)) as conn, conn:
        cursor = conn.execute(
            "INSERT OR IGNORE INTO users (user_id, username, registration_date) VALUES (?, ?, ?)",
            (user_id, username, datetime.now()),
        )
        return cursor.rowcount == 1


def is_user_registered(user_id: int) -> bool:
    """Проверка, зарегистрирован ли пользователь"""
    conn = sqlite3.connect(DB_PATH, timeout=10)
    cursor = conn.cursor()
    cursor.execute("SELECT user_id FROM users WHERE user_id = ?", (user_id,))
    result = cursor.fetchone() is not None
    conn.close()
    return result


@audit_write("trader_report", "reporter_id", "is_first_reporter")
def add_trader_report(
    server: str, location_name: str, x_coord: int, y_coord: int, reporter_id: int
):
    """One vote per player/server, with atomic cooldown and notification enqueue."""
    from coords_handler import SERVERS, get_all_locations
    if server not in SERVERS or location_name not in get_all_locations():
        raise ValueError("Unknown report server or location")
    with closing(sqlite3.connect(DB_PATH, timeout=10)) as conn, conn:
        cursor = conn.cursor()
        cursor.execute("BEGIN IMMEDIATE")
        now = _utc_now()
        _ensure_cycle(cursor, now)
        existing = cursor.execute(
            "SELECT location_name FROM trader_reports WHERE server = ? AND reporter_id = ?",
            (server, reporter_id),
        ).fetchone()
        if existing and existing[0] == location_name:
            cursor.execute(
                "UPDATE trader_reports SET report_date = ?, x_coord = ?, y_coord = ? WHERE server = ? AND reporter_id = ?",
                (now.replace(tzinfo=None), x_coord, y_coord, server, reporter_id),
            )
            cursor.execute("DELETE FROM location_checks WHERE server = ? AND location_name = ?", (server, location_name))
            _queue_map_snapshot(cursor, now)
            return False

        previous = cursor.execute(
            "SELECT last_changed_at FROM report_cooldowns WHERE reporter_id = ? AND server = ?",
            (reporter_id, server),
        ).fetchone()
        if previous:
            remaining = previous[0] + REPORT_COOLDOWN_SECONDS - now.timestamp()
            if remaining > 0:
                raise ReportCooldownError(math.ceil(remaining))
        cursor.execute(
            "DELETE FROM trader_reports WHERE server = ? AND reporter_id = ?", (server, reporter_id)
        )
        is_first = cursor.execute(
            "SELECT 1 FROM trader_reports WHERE server = ? AND location_name = ? LIMIT 1", (server, location_name)
        ).fetchone() is None
        cursor.execute("""
            INSERT INTO trader_reports (server, location_name, x_coord, y_coord, reporter_id,
                                        report_date, is_first_reporter) VALUES (?, ?, ?, ?, ?, ?, ?)
        """, (server, location_name, x_coord, y_coord, reporter_id, now.replace(tzinfo=None), is_first))
        cursor.execute("""
            INSERT INTO report_cooldowns (reporter_id, server, last_changed_at) VALUES (?, ?, ?)
            ON CONFLICT(reporter_id, server) DO UPDATE SET last_changed_at = excluded.last_changed_at
        """, (reporter_id, server, now.timestamp()))
        cursor.execute("DELETE FROM location_checks WHERE server = ? AND location_name = ?", (server, location_name))
        cursor.execute(
            "INSERT OR IGNORE INTO trader_notification_events (server, location_name) VALUES (?, ?)",
            (server, location_name),
        )
        if cursor.rowcount:
            cycle = _cycle_start(now)
            # Capture the event's author now. Later votes or profile changes
            # must not replace the first reporter in a delayed/retried alert.
            reporter = cursor.execute(
                "SELECT username FROM users WHERE user_id = ?", (reporter_id,)
            ).fetchone()
            reporter_name = reporter[0] if reporter and reporter[0] else "неизвестно"
            cursor.execute("""
                INSERT INTO trader_notifications
                    (user_id, server, location_name, x_coord, y_coord, cycle_start, expires_at,
                     reporter_id, reporter_name)
                SELECT user_id, server, ?, ?, ?, ?, ?, ?, ? FROM server_subscriptions WHERE server = ?
            """, (location_name, x_coord, y_coord, cycle.isoformat(),
                  (cycle + timedelta(days=1)).timestamp(), reporter_id, reporter_name, server))
        _queue_map_snapshot(cursor, now)
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
    ensure_current_cycle()
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
    ensure_current_cycle()
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
    ensure_current_cycle()
    conn = sqlite3.connect(DB_PATH, timeout=10)
    cursor = conn.cursor()
    cursor.execute(
        "SELECT COUNT(*) FROM trader_reports WHERE server = ? AND location_name = ?",
        (server, location_name),
    )
    count = cursor.fetchone()[0]
    conn.close()
    return count > 0


@audit_write("location_check", "checker_id", "applied")
def add_location_check(server: str, location_name: str, checker_id: int):
    """Save a not-found check under the same write lock as trader reports."""
    from coords_handler import SERVERS, get_all_locations
    if server not in SERVERS or location_name not in get_all_locations():
        raise ValueError("Unknown check server or location")
    with closing(sqlite3.connect(DB_PATH, timeout=10)) as conn, conn:
        cursor = conn.cursor()
        cursor.execute("BEGIN IMMEDIATE")
        now = _utc_now()
        _ensure_cycle(cursor, now)
        if cursor.execute(
            "SELECT 1 FROM trader_reports WHERE server = ? AND location_name = ? LIMIT 1",
            (server, location_name),
        ).fetchone():
            return False
        cursor.execute("""
            INSERT INTO location_checks (server, location_name, checker_id, checked_at, status)
            VALUES (?, ?, ?, ?, 'not_found')
            ON CONFLICT(server, location_name, checker_id)
            DO UPDATE SET checked_at = excluded.checked_at, status = excluded.status
        """, (server, location_name, checker_id, now.replace(tzinfo=None)))
        return True


@audit_write("location_check_remove", "checker_id", "removed")
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


def get_user_checked_locations(server: str, checker_id: int) -> set[str]:
    """Return exact locations currently marked by one player on one server."""
    ensure_current_cycle()
    conn = sqlite3.connect(DB_PATH, timeout=10)
    cursor = conn.cursor()
    cursor.execute(
        """
        SELECT location_name FROM location_checks
        WHERE server = ? AND checker_id = ? AND status = 'not_found'
        """,
        (server, checker_id),
    )
    locations = {row[0] for row in cursor.fetchall()}
    conn.close()
    return locations


def get_location_check_summary(server: str):
    """Return (location_name, latest_check_time, number_of_checkers) by location."""
    ensure_current_cycle()
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
    """Explicit administrative reset; scheduled resets use ensure_current_cycle."""
    with closing(sqlite3.connect(DB_PATH, timeout=10)) as conn, conn:
        cursor = conn.cursor()
        cursor.execute("BEGIN IMMEDIATE")
        now = _utc_now()
        _archive(cursor, now)
        cursor.execute("""
            INSERT INTO bot_state (key, value) VALUES ('cycle_start', ?)
            ON CONFLICT(key) DO UPDATE SET value = excluded.value
        """, (_cycle_start(now).isoformat(),))


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
