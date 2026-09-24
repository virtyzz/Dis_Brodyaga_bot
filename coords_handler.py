import json
import os
from typing import Dict, List, Tuple, Optional

COORDS_FILE = os.path.join(os.path.dirname(__file__), "Coords.json")
BUILDINGS_DIR = os.path.join(os.path.dirname(__file__), "Buildings")

# Кэш для хранения данных координат
_locations_cache = None


def load_coords() -> List[dict]:
    """Загрузка координат из JSON файла"""
    global _locations_cache
    if _locations_cache is not None:
        return _locations_cache

    with open(COORDS_FILE, "r", encoding="utf-8") as f:
        data = json.load(f)

    # Структура: [{"param1": "ip:port", "param2": [...locations]}]
    _locations_cache = data[0]["param2"]
    return _locations_cache


def get_all_locations() -> List[str]:
    """Получение списка всех уникальных названий локаций"""
    locations = load_coords()
    return [loc["name"] for loc in locations]


def get_location_coords(location_name: str) -> Optional[Tuple[int, int]]:
    """Получение координат локации по названию (X, Y)"""
    locations = load_coords()
    for loc in locations:
        if loc["name"] == location_name:
            # X = position[0], Y = position[2]
            x = int(loc["position"][0])
            y = int(loc["position"][2])
            return (x, y)
    return None


def get_locations_by_base(base_name: str) -> List[str]:
    """
    Получение всех вариантов локации по базовому названию.
    Например: "Березино" -> ["Березино-1", "Березино-2"]
    """
    all_locations = get_all_locations()
    return [loc for loc in all_locations if loc.startswith(base_name)]


def get_unique_base_locations() -> List[str]:
    """
    Получение списка уникальных базовых названий локаций.
    Например: ["Березино-1", "Березино-2"] -> ["Березино"]
    """
    all_locations = get_all_locations()
    base_names = set()

    for loc in all_locations:
        # Убираем "-N" из конца если есть
        if "-" in loc and loc.rsplit("-", 1)[1].isdigit():
            base_name = loc.rsplit("-", 1)[0]
        else:
            base_name = loc
        base_names.add(base_name)

    return sorted(list(base_names))


def get_screenshot_path(location_name: str) -> Optional[str]:
    """Получение пути к скриншоту локации"""
    screenshot_path = os.path.join(BUILDINGS_DIR, f"{location_name}.png")
    if os.path.exists(screenshot_path):
        return screenshot_path
    return None


def get_location_groups() -> Dict[str, List[str]]:
    """
    Группировка локаций по базовым названиям.
    Возвращает: {"Березино": ["Березино-1", "Березино-2"], ...}
    """
    all_locations = get_all_locations()
    groups = {}

    for loc in all_locations:
        if "-" in loc and loc.rsplit("-", 1)[1].isdigit():
            base_name = loc.rsplit("-", 1)[0]
        else:
            base_name = loc

        if base_name not in groups:
            groups[base_name] = []
        groups[base_name].append(loc)

    return groups


SERVERS = ["cherno-1", "cherno-2", "cherno-3", "cherno-4"]

# Stable identifiers used by the database and API stay separate from the
# names shown to players in Discord.
SERVER_DISPLAY_NAMES = {
    "cherno-1": "Chernarus 1",
    "cherno-2": "Chernarus 2",
    "cherno-3": "Chernarus 3",
    "cherno-4": "Chernarus 4",
}

BRODYAGA_LOCATION_SLUGS = [
    "berezino-1", "berezino-2", "biathlon-arena-1", "biathlon-arena-2",
    "vybor-1", "vybor-2", "gorka-1", "gorka-2", "zelenogorsk",
    "krasnostav-1", "krasnostav-2", "novaya-petrovka", "novodmitrovsk",
    "polyana", "pustoshka-1", "pustoshka-2", "svetlojarsk", "severograd",
    "stary-sobor-1", "stary-sobor-2", "stary-sobor-3", "topolniki",
    "chernaya-polyana-1", "chernaya-polyana-2", "chernaya-polyana-3",
    "chernogorsk", "elektrozavodsk",
]


def get_server_display_name(server: str) -> str:
    """Return the player-facing name while preserving unknown server IDs."""
    return SERVER_DISPLAY_NAMES.get(server, server)


def get_brodyaga_map_url(location_name: str) -> Optional[str]:
    """Build a DayZ-Map URL that opens the exact trader marker popup."""
    locations = get_all_locations()
    try:
        location_index = locations.index(location_name)
    except ValueError:
        return None
    if location_index >= len(BRODYAGA_LOCATION_SLUGS):
        return None
    return f"https://dayz-map.ru/brodyaga/{BRODYAGA_LOCATION_SLUGS[location_index]}"
