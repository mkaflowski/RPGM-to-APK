#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""RPG Maker MV/MZ -> Android APK, console version (question by question).

    python cli.py
    python cli.py --game "D:\\Games\\My Game"
    python cli.py --game "..." --install --push-saves
    python cli.py --pull-saves D:\\saves --game "..."
    python cli.py --check                       only show what is installed

All the work is done by rpgm_apk_lib - exactly like in the window (gui.py).
"""

from __future__ import annotations

import argparse
import os
import sys
from pathlib import Path

for _stream in (sys.stdout, sys.stderr):
    try:
        _stream.reconfigure(encoding="utf-8", errors="replace")
    except Exception:
        pass

HERE = os.path.dirname(os.path.abspath(__file__))
if HERE not in sys.path:
    sys.path.insert(0, HERE)

import app_config as cfg  # noqa: E402
import i18n  # noqa: E402
import rpgm_apk_lib as lib  # noqa: E402
from i18n import tr  # noqa: E402

EXIT_HANDLED = 10          # error already shown and Enter already pressed - the launcher must not pause again
DISCORD_URL = "https://discord.gg/s87bTQ3wfk"

GREEN, YELLOW, RED, CYAN, MAGENTA, BOLD, UNDERLINE, DIM, RESET = (
    "\033[92m", "\033[93m", "\033[91m", "\033[96m", "\033[95m", "\033[1m", "\033[4m", "\033[2m", "\033[0m")


def _enable_ansi():
    if os.name == "nt":
        try:
            import ctypes
            k = ctypes.windll.kernel32
            k.SetConsoleMode(k.GetStdHandle(-11), 7)
        except Exception:
            pass


class ConsoleReporter(lib.Reporter):
    COLORS = {"ok": GREEN, "warn": YELLOW, "error": RED, "head": CYAN + BOLD, "muted": DIM}
    PREFIX = {"ok": "OK   ", "warn": "!    ", "error": "ERR  "}

    def __init__(self):
        self._progress_line = False
        self._last_pct = -1

    def _end_progress(self):
        if self._progress_line:
            print()
            self._progress_line = False
            self._last_pct = -1

    def log(self, message: str = "", kind: str = "info") -> None:
        self._end_progress()
        color = self.COLORS.get(kind, "")
        prefix = self.PREFIX.get(kind, "")
        for i, line in enumerate(str(message).splitlines() or [""]):
            print(f"  {color}{prefix if i == 0 else ' ' * len(prefix)}{line}{RESET if color else ''}")

    def progress(self, label: str, done: int, total: int) -> None:
        pct = done * 100 // max(total, 1)
        if pct == self._last_pct and done < total:
            return
        self._last_pct = pct
        extra = f"  ({done >> 20}/{total >> 20} MB)" if total > (1 << 20) else ""
        print(f"\r  {label}... {pct:3d}%{extra}   ", end="", flush=True)
        self._progress_line = True
        if done >= total:
            self._end_progress()

    def step(self, n: int, total: int, title: str) -> None:
        self._end_progress()
        print(f"\n{CYAN}{'-' * 64}\n [{n}/{total}] {title}\n{'-' * 64}{RESET}")


def ask(prompt: str, default: str = "") -> str:
    try:
        answer = input(prompt).strip()
    except EOFError:
        return default
    return answer or default


def confirm(question: str, default_yes: bool = True) -> bool:
    hint = tr("[T/n]", "[Y/n]") if default_yes else tr("[t/N]", "[y/N]")
    answer = ask(f"  {question} {hint}: ", "y" if default_yes else "n").lower()
    return answer in ("t", "tak", "y", "yes")


def wait_for_enter():
    try:
        input("\n" + tr("Naciśnij Enter, aby zamknąć...", "Press Enter to close..."))
    except (EOFError, KeyboardInterrupt):
        pass


def banner():
    print(f"{'=' * 64}\n {BOLD}{GREEN}RPGM to APK {lib.__version__}  -  RPG Maker MV/MZ -> Android{RESET}\n"
          f" {DIM}{lib.REPO_URL}{RESET}\n"
          f" {tr('Folder gry nie jest modyfikowany.', 'The game folder is never modified.')}\n"
          f" {MAGENTA}Discord: {UNDERLINE}{DISCORD_URL}{RESET}\n{'=' * 64}")


# ------------------------------------------------------------------ steps

def choose_game(arg: str | None, rep: ConsoleReporter) -> lib.Game:
    if arg:
        game = lib.detect_game(lib.parse_path(arg))
        if not game:
            raise lib.BuildError(tr(
                f"To nie jest gra RPG Maker MV/MZ (brak index.html + js/rmmz_core.js lub js/rpg_core.js): {arg}",
                f"Not an RPG Maker MV/MZ game (no index.html + js/rmmz_core.js or js/rpg_core.js): {arg}"))
        return game
    print(tr("\nSzukam gier RPG Maker MV/MZ (Steam + ostatnio używane)...",
             "\nLooking for RPG Maker MV/MZ games (Steam + recently used)..."))
    games, seen = [], set()
    for path in cfg.recent_games():
        g = lib.detect_game(Path(path))
        if g:
            games.append(g)
    for g in lib.find_steam_games():
        games.append(g)
    unique = []
    for g in games:
        key = os.path.normcase(str(g.folder))
        if key not in seen:
            seen.add(key)
            unique.append(g)
    if unique:
        for i, g in enumerate(unique, 1):
            print(f"  {i}. {g.title}  [{g.engine}]  {DIM}{g.folder}{RESET}")
        prompt = tr("\nEnter = 1, numer z listy albo wklej ścieżkę do folderu gry: ",
                    "\nEnter = 1, a number from the list, or paste the game folder path: ")
    else:
        print("  " + tr("Nie znaleziono automatycznie.", "Nothing found automatically."))
        prompt = tr("\nWklej ścieżkę do folderu gry (albo przeciągnij tu Game.exe): ",
                    "\nPaste the game folder path (or drag Game.exe here): ")
    while True:
        raw = ask(prompt, "1" if unique else "")
        if not raw:
            continue
        if raw.isdigit() and unique and 1 <= int(raw) <= len(unique):
            return unique[int(raw) - 1]
        game = lib.detect_game(lib.parse_path(raw))
        if game:
            return game
        rep.log(tr(f"To nie jest gra RPG Maker MV/MZ: {raw}", f"Not an RPG Maker MV/MZ game: {raw}"), "error")


def show_tools(tc: lib.Toolchain, rep: ConsoleReporter):
    for c in tc.components:
        if c.ok:
            mark, color = "OK ", GREEN
        elif c.required:
            mark, color = "BRAK" if i18n.lang() == "pl" else "MISS", RED
        else:
            mark, color = " -  ", YELLOW
        print(f"  {color}{mark:4}{RESET} {c.name:22} {DIM}{c.detail}{RESET}")


def ensure_tools(args, rep: ConsoleReporter, need_adb: bool) -> lib.Toolchain:
    tc = lib.check_tools(args.sdk, args.jdk, args.rebuild_dex)
    show_tools(tc, rep)
    wanted = [c for c in tc.components if not c.ok and c.download and (c.required or (need_adb and c.key == "adb"))]
    blocking = [c for c in tc.missing_required if not c.download]
    if blocking:
        raise lib.BuildError(tr("Tego nie da się pobrać automatycznie: ", "This cannot be downloaded automatically: ")
                             + ", ".join(f"{c.name} ({c.detail})" for c in blocking))
    if wanted:
        print("\n  " + tr("Do pobrania (oficjalne źródła, do folderu programu):",
                          "To download (official sources, into the program folder):"))
        for c in wanted:
            print(f"    - {c.download}")
        print(f"  {lib.tools_dir()}")
        print("  " + tr(f"Pobierając składniki Android SDK akceptujesz licencję: {lib.SDK_LICENSE_URL}",
                        f"By downloading Android SDK components you accept its license: {lib.SDK_LICENSE_URL}"))
        if not confirm(tr("Pobrać teraz?", "Download now?")):
            raise lib.BuildError(tr("Bez tych narzędzi nie da się zbudować APK.",
                                    "The APK cannot be built without these tools."))
        lib.download_components([c.key for c in wanted], rep)
        tc = lib.check_tools(args.sdk, args.jdk, args.rebuild_dex)
        if not tc.ready:
            show_tools(tc, rep)
            raise lib.BuildError(tr("Nadal czegoś brakuje.", "Something is still missing."))
    return tc


def pick_device(tc: lib.Toolchain, serial: str | None) -> lib.Device:
    if not tc.adb:
        raise lib.BuildError(tr("Brak adb.", "adb not found."))
    devices = lib.list_devices(tc.adb)
    if serial:
        dev = next((d for d in devices if d.serial == serial), None)
        if not dev or not dev.ready:
            raise lib.BuildError(tr(f"Urządzenie {serial} nie jest gotowe.", f"Device {serial} is not ready."))
        return dev
    ready = [d for d in devices if d.ready]
    for d in devices:
        if not d.ready:
            print(f"  {YELLOW}{d}{RESET}  {lib.device_hint(d)}")
    if not ready:
        raise lib.BuildError(tr("Brak podłączonego telefonu. Włącz debugowanie USB (Opcje programisty) i podłącz kabel.",
                                "No phone connected. Enable USB debugging (Developer options) and plug in the cable."))
    if len(ready) == 1:
        print(f"  {tr('urządzenie', 'device')}: {ready[0]}")
        return ready[0]
    for i, d in enumerate(ready, 1):
        print(f"  {i}. {d}")
    choice = ask(tr("Wybierz numer urządzenia: ", "Pick the device number: "), "1")
    if not choice.isdigit() or not 1 <= int(choice) <= len(ready):
        raise lib.BuildError(tr("Nieprawidłowy numer urządzenia", "Invalid device number"))
    return ready[int(choice) - 1]


# ------------------------------------------------------------------ main

def main() -> int:
    _enable_ansi()
    ap = argparse.ArgumentParser(description="RPG Maker MV/MZ (PC) -> Android APK (WebView).")
    ap.add_argument("--game", help="game folder (default: Steam + recent / ask)")
    ap.add_argument("--out", help="output .apk (default: output/<title>.apk in the program folder)")
    ap.add_argument("--package", help=f"package name (default {lib.PACKAGE_PREFIX}.<title>)")
    ap.add_argument("--label", help="name under the icon (default: gameTitle from System.json)")
    ap.add_argument("--orientation", choices=sorted(lib.ORIENTATIONS), default=None)
    ap.add_argument("--sdk", help="Android SDK folder")
    ap.add_argument("--jdk", help="Java 11+ folder (JAVA_HOME)")
    ap.add_argument("--keystore", help="own keystore (default ~/.android/debug.keystore)")
    ap.add_argument("--ks-pass", default="android")
    ap.add_argument("--ks-alias", default="androiddebugkey")
    ap.add_argument("--key-pass")
    ap.add_argument("--install", action="store_true", help="install on the phone via adb after building")
    ap.add_argument("--push-saves", action="store_true", help="copy PC saves to the phone after installing")
    ap.add_argument("--pull-saves", metavar="DIR", help="only copy saves from the phone to DIR (no build)")
    ap.add_argument("--serial", help="adb -s <serial>")
    ap.add_argument("--check", action="store_true", help="only show which tools are installed")
    ap.add_argument("--rebuild-dex", action="store_true", help="compile MainActivity.java again (needs a JDK)")
    ap.add_argument("--lang", choices=i18n.SUPPORTED, help="language: pl / en")
    ap.add_argument("--no-pause", action="store_true", help="do not wait for Enter at the end")
    args = ap.parse_args()
    i18n.set_lang(args.lang)
    rep = ConsoleReporter()
    banner()

    if args.check:
        tc = lib.check_tools(args.sdk, args.jdk, args.rebuild_dex)
        show_tools(tc, rep)
        if tc.adb:
            for d in lib.list_devices(tc.adb):
                print(f"  {'OK ' if d.ready else ' ! '}  {tr('telefon', 'phone'):22} {d}  {lib.device_hint(d)}")
        print()
        rep.log(tr("Wszystko gotowe do budowania APK.", "Everything is ready to build an APK.") if tc.ready else
                tr("Brakujące narzędzia pobierzesz przy pierwszej konwersji.",
                   "Missing tools will be downloaded on the first conversion."), "ok" if tc.ready else "warn")
        return 0

    if args.pull_saves:
        game = lib.detect_game(lib.parse_path(args.game)) if args.game else None
        package = args.package or (game.default_package if game else None)
        if not package:
            raise lib.BuildError(tr("--pull-saves wymaga --package albo --game", "--pull-saves needs --package or --game"))
        tc = ensure_tools(args, rep, need_adb=True)
        dev = pick_device(tc, args.serial)
        lib.pull_saves(tc.adb, dev.serial, package, lib.parse_path(args.pull_saves), rep)
        return 0

    with_phone = args.install or args.push_saves
    total = 5 if with_phone else 4
    rep.step(1, total, tr("Gra", "Game"))
    game = choose_game(args.game, rep)
    cfg.remember_game(str(game.folder))
    package = args.package or game.default_package
    if not lib.valid_package(package):
        raise lib.BuildError(tr(f"Nieprawidłowa nazwa pakietu: {package}", f"Invalid package name: {package}"))
    rep.log(f"{game.title}  [RPG Maker {game.engine}]", "ok")
    rep.log(f"{tr('pliki gry', 'game files')}: {game.web}", "muted")
    rep.log(f"{tr('pakiet', 'package')}: {package}", "muted")

    rep.step(2, total, tr("Narzędzia", "Tools"))
    tc = ensure_tools(args, rep, need_adb=with_phone)

    out = Path(os.path.abspath(lib.parse_path(args.out))) if args.out else \
        lib.output_dir() / f"{game.safe_title}.apk"
    orientation = args.orientation or cfg.get("orientation") or "landscape"
    opts = lib.BuildOptions(game=game, out=out, package=package, label=args.label or game.title,
                            orientation=orientation,
                            keystore=Path(args.keystore) if args.keystore else None, ks_pass=args.ks_pass,
                            ks_alias=args.ks_alias, key_pass=args.key_pass, rebuild_dex=args.rebuild_dex)
    rep.step(3, total, tr("Budowanie APK", "Building the APK"))
    result = lib.build_apk(opts, tc, rep)
    rep.step(4, total, tr("Gotowe APK", "Finished APK"))
    rep.log(f"{result.apk}  ({lib.human_size(result.size)}, {result.files} {tr('plików gry', 'game files')})", "ok")

    if with_phone:
        rep.step(5, total, tr("Telefon (adb)", "Phone (adb)"))
        dev = pick_device(tc, args.serial)
        lib.install_apk(tc.adb, dev.serial, result.apk, package, rep)
        if args.push_saves:
            lib.push_saves(tc.adb, dev.serial, game, package, rep)

    print(f"\n{GREEN}{BOLD}{tr('Gotowe!', 'Done!')}{RESET}  {result.apk}")
    print("  " + tr("Skopiuj APK na telefon i otwórz go (zgoda na instalację z nieznanych źródeł).",
                    "Copy the APK to the phone and open it (allow installing from unknown sources)."))
    print("  " + tr(f"Zapisy na telefonie: {lib.remote_save_dir(package)}/ (ten sam format co na PC)",
                    f"Saves on the phone: {lib.remote_save_dir(package)}/ (same format as on PC)"))
    print("  " + tr("Nie udostępniaj zbudowanego APK - zawiera pliki gry.",
                    "Do not share the built APK - it contains the game files."))
    print(f"  {MAGENTA}Discord: {UNDERLINE}{DISCORD_URL}{RESET}")
    return 0


if __name__ == "__main__":
    try:
        code = main()
    except KeyboardInterrupt:
        print("\n" + tr("Przerwano.", "Cancelled."))
        code = 1
    except lib.BuildError as exc:
        ConsoleReporter().log(str(exc), "error")
        code = 1
    except Exception as exc:                     # never close the window without a message
        import traceback
        traceback.print_exc()
        ConsoleReporter().log(tr(f"Nieoczekiwany błąd: {exc}", f"Unexpected error: {exc}"), "error")
        code = 1
    if "--no-pause" not in sys.argv:
        wait_for_enter()
        if code:
            code = EXIT_HANDLED
    sys.exit(code)
