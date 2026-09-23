"""Settings live in config.json next to the app; missing keys fall back to defaults."""
from __future__ import annotations

import copy
import json
import os
import sys
from pathlib import Path

# Your files (settings, logs, debug shots, terrain cache) live next to the app: beside
# WARDOGS-Arty.exe in a packaged build, or in the project folder when run from source.
FROZEN = bool(getattr(sys, "frozen", False))
APP_DIR = Path(sys.executable).resolve().parent if FROZEN else Path(__file__).resolve().parent.parent
CONFIG_FILE = Path(os.environ.get("WARDOGS_ARTY_CONFIG") or APP_DIR / "config.json")
DEBUG_DIR = APP_DIR / "debug"
SHOT_LOG = Path(os.environ.get("WARDOGS_ARTY_SHOTLOG") or APP_DIR / "logs" / "shots.csv")
SIGHT_LOG = SHOT_LOG.with_name("sight_table.csv")  # (mil, range) rows the SPH-2 sight printed
ELEV_LOG = SHOT_LOG.with_name("elevations.csv")  # every height the game printed, where, and from which readout
# Sight rows already seen in play, shipped with the app so a new install starts on the game's numbers.
GAME_TABLE_SEED = Path(__file__).resolve().parent.parent / "data" / "game_table_seed.csv"

DEFAULTS: dict = {
    # Any of F1-F24, A-Z, 0-9, Numpad0-9, Insert, Home, ... with Ctrl+/Alt+/Shift+ prefixes.
    # A registered key never reaches the game, so pick ones WARDOGS doesn't use.
    "hotkeys": {"gun": "F7", "target": "F8", "impact": "F9", "snapshot": "F6", "dial": "F11"},
    "weapon": "mortar",
    "always_on_top": False,
    "watch_clipboard": True,  # coordinates copied anywhere (game, Discord) become the target
    "keep_correction": False,  # carry a fire correction over to the next target
    "auto_trim": True,  # apply the gun spot's learned steady error (see arty/trim.py)
    "terrain_map": "",  # "", "bakurani", "ozeti" or "zestafona": ground heights from the map's terrain
    "terrain_auto": False,  # True when the app picked terrain_map itself from the heights (it may switch)
    "voice": {"enabled": True, "volume": 70, "rate": 1},  # spoken callouts; rate runs -10 (slow) to 10
    "window": None,  # "WxH+X+Y", remembered on exit
    "history_size": 12,
    "save_failed_reads": True,  # keep the last few failed screen reads in debug/ for tuning
    "readout_memory": {},  # where the map's coordinate readout was last found
}


def _merge(base: dict, override: dict) -> dict:
    out = copy.deepcopy(base)
    for k, v in override.items():
        out[k] = _merge(out[k], v) if isinstance(v, dict) and isinstance(out.get(k), dict) else v
    return out


def load(path: Path = CONFIG_FILE) -> dict:
    try:
        return _merge(DEFAULTS, json.loads(path.read_text(encoding="utf-8")))
    except FileNotFoundError:
        return copy.deepcopy(DEFAULTS)
    except (OSError, ValueError):
        # Unreadable file: keep a copy for the user and start from defaults.
        try:
            path.replace(path.with_suffix(".broken.json"))
        except OSError:
            pass
        return copy.deepcopy(DEFAULTS)


def save(cfg: dict, path: Path = CONFIG_FILE) -> None:
    tmp = path.with_suffix(".tmp")
    tmp.write_text(json.dumps(cfg, indent=2) + "\n", encoding="utf-8")
    os.replace(tmp, path)
