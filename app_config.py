#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""Settings remembered between runs (last game folder, options, window size).

    Windows   %APPDATA%\\rpgm-to-apk\\config.json
    Linux     $XDG_CONFIG_HOME/rpgm-to-apk/config.json  (or ~/.config/...)

Not next to the program: the package may be unpacked to a read-only place and
people move it around. When the user folder cannot be written, the package
folder is the fallback. Settings are a convenience - every error is swallowed.
Unknown keys survive a load/save, so an older version does not wipe newer ones.
"""

from __future__ import annotations

import json
import os
import sys
from typing import Any, Dict, List, Optional

APP_NAME = "rpgm-to-apk"
CONFIG_NAME = "config.json"
MAX_RECENT = 8

_cache: Optional[Dict[str, Any]] = None


def app_dir() -> str:
    """Package root - the launchers set RPGM_APK_DIR."""
    env = os.environ.get("RPGM_APK_DIR")
    if env:
        return env
    if getattr(sys, "frozen", False):
        return os.path.dirname(sys.executable)
    return os.path.dirname(os.path.abspath(__file__))


def config_dir() -> str:
    if os.name == "nt":
        base = os.environ.get("APPDATA") or os.path.join(os.path.expanduser("~"), "AppData", "Roaming")
    else:
        base = os.environ.get("XDG_CONFIG_HOME") or os.path.join(os.path.expanduser("~"), ".config")
    return os.path.join(base, APP_NAME)


def config_path() -> str:
    return os.path.join(config_dir(), CONFIG_NAME)


def _fallback_path() -> str:
    return os.path.join(app_dir(), CONFIG_NAME)


def _read(path: str) -> Optional[Dict[str, Any]]:
    try:
        with open(path, "r", encoding="utf-8") as fh:
            data = json.load(fh)
    except (OSError, ValueError):
        return None
    return data if isinstance(data, dict) else None


def load(refresh: bool = False) -> Dict[str, Any]:
    global _cache
    if _cache is not None and not refresh:
        return _cache
    _cache = _read(config_path()) or _read(_fallback_path()) or {}
    return _cache


def save(data: Optional[Dict[str, Any]] = None) -> bool:
    global _cache
    if data is not None:
        _cache = data
    text = json.dumps(_cache or {}, indent=2, ensure_ascii=False, sort_keys=True) + "\n"
    for target in (config_path(), _fallback_path()):
        tmp = target + ".tmp"
        try:
            os.makedirs(os.path.dirname(target), exist_ok=True)
            with open(tmp, "w", encoding="utf-8") as fh:
                fh.write(text)
            os.replace(tmp, target)
            return True
        except OSError:
            try:
                os.remove(tmp)
            except OSError:
                pass
    return False


def get(key: str, default: Any = None) -> Any:
    return load().get(key, default)


def set_value(key: str, value: Any) -> bool:
    data = load()
    if data.get(key) == value:
        return True
    data[key] = value
    return save(data)


def recent_games() -> List[str]:
    value = load().get("recent_games")
    out: List[str] = []
    if isinstance(value, list):
        for item in value:
            if isinstance(item, str) and item and item not in out:
                out.append(item)
    return out[:MAX_RECENT]


def remember_game(path: str) -> bool:
    if not path:
        return False
    path = os.path.normpath(path)
    key = os.path.normcase(path)
    recent = [p for p in recent_games() if os.path.normcase(os.path.normpath(p)) != key]
    data = load()
    data["recent_games"] = ([path] + recent)[:MAX_RECENT]
    return save(data)


def forget_game(path: str) -> bool:
    key = os.path.normcase(os.path.normpath(path))
    data = load()
    data["recent_games"] = [p for p in recent_games() if os.path.normcase(os.path.normpath(p)) != key]
    return save(data)
