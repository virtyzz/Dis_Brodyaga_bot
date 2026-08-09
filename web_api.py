"""Authenticated public API for Brodyaga reports.

Run behind the map's HTTPS reverse proxy at /brodyaga-api.
"""
from __future__ import annotations

import hashlib
import hmac
import json
import logging
import os
import secrets
import time
import urllib.error
import urllib.parse
import urllib.request
from http import HTTPStatus
from http.cookies import SimpleCookie
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

from coords_handler import SERVERS, get_all_locations, get_location_coords
from database import add_trader_report, get_trader_report_summary, init_database, register_user


logging.basicConfig(level=logging.INFO)
LOGGER = logging.getLogger(__name__)
DISCORD_USER_AGENT = "DiscordBot (https://github.com/virtyzz/Dis_Brodyaga_bot, 1.0)"


CLIENT_ID = os.getenv("DISCORD_CLIENT_ID", "")
CLIENT_SECRET = os.getenv("DISCORD_CLIENT_SECRET", "")
REDIRECT_URIS = {
    uri.strip()
    for uri in os.getenv("DISCORD_REDIRECT_URIS", os.getenv("DISCORD_REDIRECT_URI", "")).split(",")
    if uri.strip()
}
MAP_PUBLIC_URL = os.getenv("MAP_PUBLIC_URL", "").rstrip("/")
MAP_ALLOWED_ORIGINS = {
    origin.strip().rstrip("/")
    for origin in os.getenv("MAP_ALLOWED_ORIGINS", MAP_PUBLIC_URL).split(",")
    if origin.strip()
}
SESSION_SECRET = os.getenv("BRODYAGA_SESSION_SECRET", "")
COOKIE_DOMAIN = os.getenv("BRODYAGA_SESSION_COOKIE_DOMAIN", "").strip()
CORS_ORIGINS = {
    origin.strip().rstrip("/")
    for origin in os.getenv("BRODYAGA_CORS_ORIGIN", MAP_PUBLIC_URL).split(",")
    if origin.strip()
}
COOKIE_NAME = "brodyaga_session"
STATE_TTL = 600
SESSION_TTL = 7 * 24 * 3600
REPORT_COOLDOWN = 30
OAUTH_STATES: dict[str, dict] = {}
SESSIONS: dict[str, dict] = {}
LAST_REPORTS: dict[tuple[str, str], float] = {}


def json_request(url: str, *, data: dict | None = None, headers: dict | None = None) -> dict:
    encoded = None if data is None else urllib.parse.urlencode(data).encode("utf-8")
    request_headers = {"User-Agent": DISCORD_USER_AGENT, **(headers or {})}
    request = urllib.request.Request(url, data=encoded, headers=request_headers)
    with urllib.request.urlopen(request, timeout=10) as response:
        return json.loads(response.read().decode("utf-8"))


def sign_session(session_id: str) -> str:
    return hmac.new(SESSION_SECRET.encode("utf-8"), session_id.encode("utf-8"), hashlib.sha256).hexdigest()


def is_allowed_return_url(value: str) -> bool:
    return any(value == origin or value.startswith(origin + "/") for origin in MAP_ALLOWED_ORIGINS)


def get_redirect_uri(return_to: str) -> str | None:
    parsed_return_to = urllib.parse.urlparse(return_to)
    return_origin = f"{parsed_return_to.scheme}://{parsed_return_to.netloc}"
    for redirect_uri in REDIRECT_URIS:
        parsed_redirect = urllib.parse.urlparse(redirect_uri)
        redirect_origin = f"{parsed_redirect.scheme}://{parsed_redirect.netloc}"
        if redirect_origin == return_origin:
            return redirect_uri
    return None


class ApiHandler(BaseHTTPRequestHandler):
    server_version = "BrodyagaApi/1.0"

    def do_GET(self) -> None:
        path = urllib.parse.urlparse(self.path).path
        if path == "/health":
            self.send_json(HTTPStatus.OK, {"ok": True})
        elif path == "/auth/session":
            session = self.get_session()
            self.send_json(HTTPStatus.OK, {"ok": True, "authenticated": bool(session), "csrf": session["csrf"] if session else None})
        elif path == "/auth/discord":
            self.start_oauth()
        elif path == "/auth/callback":
            self.finish_oauth()
        elif path == "/reports":
            self.send_reports()
        else:
            self.send_json(HTTPStatus.NOT_FOUND, {"ok": False, "error": "Not found"})

    def do_POST(self) -> None:
        if urllib.parse.urlparse(self.path).path == "/reports":
            self.create_report()
        else:
            self.send_json(HTTPStatus.NOT_FOUND, {"ok": False, "error": "Not found"})

    def do_OPTIONS(self) -> None:
        self.send_response(HTTPStatus.NO_CONTENT)
        self.send_cors_headers()
        self.send_header("Access-Control-Allow-Methods", "GET, POST, OPTIONS")
        self.send_header("Access-Control-Allow-Headers", "Content-Type, X-CSRF-Token")
        self.send_header("Access-Control-Max-Age", "600")
        self.end_headers()

    def start_oauth(self) -> None:
        if not all((CLIENT_ID, CLIENT_SECRET, REDIRECT_URIS, MAP_PUBLIC_URL, SESSION_SECRET)):
            self.send_json(HTTPStatus.SERVICE_UNAVAILABLE, {"ok": False, "error": "OAuth is not configured"})
            return
        query = urllib.parse.parse_qs(urllib.parse.urlparse(self.path).query)
        return_to = query.get("return_to", [MAP_PUBLIC_URL])[0]
        if not is_allowed_return_url(return_to):
            self.send_json(HTTPStatus.BAD_REQUEST, {"ok": False, "error": "Invalid return URL"})
            return
        redirect_uri = get_redirect_uri(return_to)
        if not redirect_uri:
            self.send_json(HTTPStatus.BAD_REQUEST, {"ok": False, "error": "OAuth callback is not configured for this origin"})
            return
        state = secrets.token_urlsafe(32)
        OAUTH_STATES[state] = {"return_to": return_to, "redirect_uri": redirect_uri, "expires": time.time() + STATE_TTL}
        params = urllib.parse.urlencode({"client_id": CLIENT_ID, "response_type": "code", "redirect_uri": redirect_uri, "scope": "identify", "state": state})
        self.redirect(f"https://discord.com/oauth2/authorize?{params}")

    def finish_oauth(self) -> None:
        query = urllib.parse.parse_qs(urllib.parse.urlparse(self.path).query)
        state = query.get("state", [""])[0]
        code = query.get("code", [""])[0]
        saved = OAUTH_STATES.pop(state, None)
        if not saved or saved["expires"] < time.time() or not code:
            self.send_json(HTTPStatus.BAD_REQUEST, {"ok": False, "error": "Invalid or expired OAuth state"})
            return
        try:
            token = json_request("https://discord.com/api/v10/oauth2/token", data={"client_id": CLIENT_ID, "client_secret": CLIENT_SECRET, "grant_type": "authorization_code", "code": code, "redirect_uri": saved["redirect_uri"]}, headers={"Content-Type": "application/x-www-form-urlencoded"})
            user = json_request("https://discord.com/api/v10/users/@me", headers={"Authorization": f"Bearer {token['access_token']}"})
        except urllib.error.HTTPError as error:
            details = error.read().decode("utf-8", errors="replace")
            LOGGER.error("Discord OAuth HTTP %s: %s", error.code, details)
            self.send_json(HTTPStatus.BAD_GATEWAY, {"ok": False, "error": "Discord authorization failed"})
            return
        except Exception:
            LOGGER.exception("Discord OAuth request failed")
            self.send_json(HTTPStatus.BAD_GATEWAY, {"ok": False, "error": "Discord authorization failed"})
            return
        session_id = secrets.token_urlsafe(32)
        SESSIONS[session_id] = {"user_id": str(user["id"]), "username": user.get("global_name") or user["username"], "csrf": secrets.token_urlsafe(24), "expires": time.time() + SESSION_TTL}
        self.send_response(HTTPStatus.FOUND)
        cookie_value = f"{session_id}.{sign_session(session_id)}"
        domain_part = f"; Domain={COOKIE_DOMAIN}" if COOKIE_DOMAIN else ""
        self.send_header("Set-Cookie", f"{COOKIE_NAME}={cookie_value}; Path=/{domain_part}; HttpOnly; Secure; SameSite=Lax; Max-Age={SESSION_TTL}")
        self.send_header("Location", saved["return_to"])
        self.end_headers()

    def send_reports(self) -> None:
        reports = [{"server": server, "location_name": location, "x": x, "y": y, "confirmations": count} for server, location, x, y, count in get_trader_report_summary()]
        self.send_json(HTTPStatus.OK, {"ok": True, "reports": reports})

    def create_report(self) -> None:
        session = self.get_session()
        if not session:
            self.send_json(HTTPStatus.UNAUTHORIZED, {"ok": False, "error": "Discord authorization required"})
            return
        if self.headers.get("X-CSRF-Token") != session["csrf"]:
            self.send_json(HTTPStatus.FORBIDDEN, {"ok": False, "error": "Invalid CSRF token"})
            return
        try:
            payload = self.read_json()
            location_id = int(payload["location_id"])
            server = str(payload["server"])
        except (KeyError, TypeError, ValueError):
            self.send_json(HTTPStatus.BAD_REQUEST, {"ok": False, "error": "Invalid report payload"})
            return
        locations = get_all_locations()
        if server not in SERVERS or not 1 <= location_id <= len(locations):
            self.send_json(HTTPStatus.BAD_REQUEST, {"ok": False, "error": "Unknown server or location"})
            return
        rate_key = (session["user_id"], server)
        if time.time() - LAST_REPORTS.get(rate_key, 0) < REPORT_COOLDOWN:
            self.send_json(HTTPStatus.TOO_MANY_REQUESTS, {"ok": False, "error": "Please wait before sending another report"})
            return
        location = locations[location_id - 1]
        coords = get_location_coords(location)
        if not coords:
            self.send_json(HTTPStatus.INTERNAL_SERVER_ERROR, {"ok": False, "error": "Location coordinates are unavailable"})
            return
        register_user(int(session["user_id"]), session["username"])
        first = add_trader_report(server, location, coords[0], coords[1], int(session["user_id"]))
        LAST_REPORTS[rate_key] = time.time()
        self.send_json(HTTPStatus.OK, {"ok": True, "is_first": first, "location_name": location, "server": server})

    def get_session(self) -> dict | None:
        cookie = SimpleCookie(self.headers.get("Cookie", ""))
        value = cookie.get(COOKIE_NAME)
        if not value or "." not in value.value:
            return None
        session_id, signature = value.value.rsplit(".", 1)
        if not hmac.compare_digest(signature, sign_session(session_id)):
            return None
        session = SESSIONS.get(session_id)
        if not session or session["expires"] < time.time():
            return None
        return session

    def read_json(self) -> dict:
        length = int(self.headers.get("Content-Length", "0"))
        payload = json.loads(self.rfile.read(length).decode("utf-8"))
        if not isinstance(payload, dict):
            raise ValueError("JSON object required")
        return payload

    def send_json(self, status: HTTPStatus, payload: dict) -> None:
        body = json.dumps(payload, ensure_ascii=False).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.send_cors_headers()
        self.end_headers()
        self.wfile.write(body)

    def send_cors_headers(self) -> None:
        origin = self.headers.get("Origin", "").rstrip("/")
        if origin in CORS_ORIGINS:
            self.send_header("Access-Control-Allow-Origin", origin)
            self.send_header("Access-Control-Allow-Credentials", "true")
            self.send_header("Vary", "Origin")

    def redirect(self, location: str) -> None:
        self.send_response(HTTPStatus.FOUND)
        self.send_header("Location", location)
        self.end_headers()

    def log_message(self, _format: str, *args) -> None:
        return


def main() -> None:
    init_database()
    ThreadingHTTPServer(("0.0.0.0", 8099), ApiHandler).serve_forever()


if __name__ == "__main__":
    main()
