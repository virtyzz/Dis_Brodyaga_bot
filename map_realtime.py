"""Durable, coalescing publisher for writes from both bot and map API."""
import hashlib
import hmac
import json
import logging
import os
import sqlite3
import threading
import time
import urllib.request
from contextlib import closing
from datetime import timedelta

import database

LOGGER = logging.getLogger(__name__)


def deliver_snapshot(url, secret, snapshot):
    body = json.dumps(snapshot, ensure_ascii=False, separators=(",", ":")).encode("utf-8")
    stamp = str(int(time.time()))
    signature = hmac.new(secret.encode(), stamp.encode() + b"." + body, hashlib.sha256).hexdigest()
    request = urllib.request.Request(url, data=body, method="POST", headers={
        "Content-Type": "application/json", "X-Brodyaga-Timestamp": stamp,
        "X-Brodyaga-Signature": signature,
    })
    with urllib.request.urlopen(request, timeout=5) as response:
        result = json.load(response)
    if result.get("ok") is not True:
        raise ValueError("Map did not acknowledge the snapshot")


class MapPublisher:
    def __init__(self, url, secret):
        self.url, self.secret = url, secret
        self.stop_event = threading.Event()
        self.thread = threading.Thread(target=self.run, name="map-realtime", daemon=True)

    def start(self):
        self.thread.start()

    def stop(self):
        self.stop_event.set()
        self.thread.join(timeout=6)

    def run(self):
        # One cheap local SQLite metadata check for all visitors. Read the
        # aggregate only after commits, on retries, or to repair a lost cache.
        with closing(sqlite3.connect(database.DB_PATH, timeout=10)) as conn:
            observed = None
            due = 0.0
            failures = 0
            next_reset = 0.0
            acknowledged = None
            while not self.stop_event.is_set():
                try:
                    if time.time() >= next_reset:
                        database.ensure_current_cycle()
                        next_reset = (database._cycle_start() + timedelta(days=1)).timestamp()
                    version = conn.execute("PRAGMA data_version").fetchone()[0]
                    if time.monotonic() >= due or (not failures and version != observed):
                        snapshot = database.read_map_snapshot(conn)
                        identity = (snapshot["epoch"], snapshot["revision"]) if snapshot else None
                        if snapshot is not None and (identity != acknowledged or time.monotonic() >= due):
                            deliver_snapshot(self.url, self.secret, snapshot)
                            acknowledged = identity
                            due = time.monotonic() + 30
                        observed = version
                        failures = 0
                except Exception:
                    failures += 1
                    if failures == 1 or failures % 10 == 0:
                        LOGGER.exception("Map snapshot delivery failed; saved state will be retried")
                    due = time.monotonic() + min(30, 2 ** min(failures, 5))
                self.stop_event.wait(0.2)


def start_map_publisher():
    url = os.getenv("BRODYAGA_PUSH_URL", "").strip()
    secret = os.getenv("BRODYAGA_PUSH_SECRET", "")
    if not url and not secret:
        LOGGER.warning("Map realtime disabled: configure BRODYAGA_PUSH_URL and BRODYAGA_PUSH_SECRET")
        return None
    if not url or len(secret) < 32:
        raise ValueError("BRODYAGA_PUSH_URL and BRODYAGA_PUSH_SECRET (at least 32 characters) are required")
    publisher = MapPublisher(url, secret)
    publisher.start()
    return publisher
