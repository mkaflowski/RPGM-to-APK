#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""Two UI languages: Polish and English. `tr("po polsku", "in English")`.

The language comes from (first match wins):
  1. set_lang() - the --lang option or the GUI language switch,
  2. RPGM_APK_LANG environment variable ("pl" / "en"),
  3. the saved setting (app_config "lang"),
  4. the system UI language - Polish system = Polish, anything else = English.

Strings sit next to the code that uses them (no catalogue files): the tool is
small, and a pair of literals is easier to keep in sync than message IDs.
"""

from __future__ import annotations

import locale
import os

SUPPORTED = ("pl", "en")
_lang: str | None = None


def _system_lang() -> str:
    if os.name == "nt":
        try:
            import ctypes
            # LANGID: the low 10 bits are the primary language, 0x15 = Polish
            if ctypes.windll.kernel32.GetUserDefaultUILanguage() & 0x3FF == 0x15:
                return "pl"
            return "en"
        except Exception:
            pass
    for var in ("LC_ALL", "LC_MESSAGES", "LANG", "LANGUAGE"):
        value = os.environ.get(var, "")
        if value:
            return "pl" if value.lower().startswith("pl") else "en"
    try:
        name = (locale.getlocale()[0] or "").lower()
    except Exception:
        name = ""
    return "pl" if name.startswith(("pl", "polish")) else "en"


def lang() -> str:
    global _lang
    if _lang is None:
        env = os.environ.get("RPGM_APK_LANG", "").lower()
        if env in SUPPORTED:
            _lang = env
        else:
            try:
                import app_config
                saved = app_config.get("lang")
            except Exception:
                saved = None
            _lang = saved if saved in SUPPORTED else _system_lang()
    return _lang


def set_lang(code: str | None) -> None:
    global _lang
    if code in SUPPORTED:
        _lang = code


def tr(pl: str, en: str) -> str:
    return pl if lang() == "pl" else en
