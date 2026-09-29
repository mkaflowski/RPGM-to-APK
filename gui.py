#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""RPG Maker MV/MZ -> Android APK, window version (tkinter).

Only a view: all the work is done by rpgm_apk_lib, exactly the same code the
console version (cli.py) calls. On top of it the window adds:

* a "Tools" panel - what is installed, what is missing, one click to download,
* game picker (Steam + recently used + Browse), phone picker (adb),
* a worker thread - building takes a minute, the window must keep repainting.

Thread rule: the engine runs in the worker thread and NEVER touches widgets.
It talks to the window only through a queue.Queue drained by after().
"""

from __future__ import annotations

import os
import queue
import subprocess
import sys
import threading
import time
import traceback
from pathlib import Path
from typing import Callable, Optional

for _stream in (sys.stdout, sys.stderr):
    try:
        _stream.reconfigure(encoding="utf-8", errors="replace")
    except Exception:
        pass

HERE = os.path.dirname(os.path.abspath(__file__))
if HERE not in sys.path:
    sys.path.insert(0, HERE)

import i18n  # noqa: E402
from i18n import tr  # noqa: E402

EXIT_HANDLED = 10
LOG_NAME = "rpgm-to-apk.log"
DISCORD_URL = "https://discord.gg/s87bTQ3wfk"


def _tk_missing_hint() -> str:
    return tr(
        "[BŁĄD] Ten Python nie ma modułu tkinter, więc okno się nie otworzy.\n\n"
        "  Steam Deck / Arch : sudo pacman -S tk\n"
        "  Debian / Ubuntu   : sudo apt install python3-tk\n"
        "  Fedora            : sudo dnf install python3-tkinter\n"
        "  Windows           : zainstaluj Pythona z python.org z opcją \"tcl/tk and IDLE\"\n\n"
        "Zawsze możesz użyć wersji konsolowej: convert.bat / sh convert.sh",
        "[ERROR] This Python has no tkinter module, so the window cannot open.\n\n"
        "  Steam Deck / Arch : sudo pacman -S tk\n"
        "  Debian / Ubuntu   : sudo apt install python3-tk\n"
        "  Fedora            : sudo dnf install python3-tkinter\n"
        "  Windows           : install Python from python.org with \"tcl/tk and IDLE\"\n\n"
        "You can always use the console version: convert.bat / sh convert.sh")


def _die(message: str) -> None:
    print("\n" + message + "\n")
    try:
        input(tr("Naciśnij Enter, aby zamknąć... ", "Press Enter to close... "))
    except Exception:
        pass
    raise SystemExit(EXIT_HANDLED)


try:
    import tkinter as tk
    import tkinter.font as tkfont
    from tkinter import filedialog, messagebox, ttk
except ImportError:
    _die(_tk_missing_hint())

import app_config as cfg  # noqa: E402
import rpgm_apk_lib as lib  # noqa: E402

COL_ERROR = "#b00020"
COL_WARN = "#a06000"
COL_OK = "#1c7a30"
COL_MUTED = "#6a6a6a"
COL_HEAD = "#0d47a1"


def app_dir() -> str:
    return str(lib.app_dir())


def open_in_file_manager(path: str) -> None:
    try:
        if os.name == "nt":
            if os.path.isfile(path):
                subprocess.Popen(["explorer", "/select,", path])
            else:
                os.startfile(path)  # noqa: S606
        elif sys.platform == "darwin":
            subprocess.Popen(["open", path if os.path.isdir(path) else os.path.dirname(path)])
        else:
            subprocess.Popen(["xdg-open", path if os.path.isdir(path) else os.path.dirname(path)])
    except Exception:
        pass


def short_home(text: str) -> str:
    """C:\\Users\\name\\AppData\\... -> ~\\AppData\\... (the list column is narrow)."""
    home = str(Path.home())
    return text.replace(home, "~") if home and len(home) > 3 else text


def renderer_labels() -> dict:
    return {"gecko": tr("GeckoView - zalecany (64-bit, +~100 MB, telefony ARM64)",
                        "GeckoView - recommended (64-bit, +~100 MB, ARM64 phones)"),
            "webview": tr("systemowy WebView (małe APK)", "system WebView (small APK)")}


def orientation_labels() -> dict:
    return {"landscape": tr("poziomo (obrót 180°)", "landscape (rotates 180°)"),
            "portrait": tr("pionowo", "portrait"),
            "any": tr("dowolnie (obraca się)", "any (follows rotation)")}


class GuiReporter(lib.Reporter):
    """Engine Reporter mapped to the message queue + a log file. Called ONLY by the worker."""

    def __init__(self, sink: "queue.Queue", log_path: str):
        self.sink = sink
        self.log_path = log_path
        self._last = (None, -1)

    def log(self, message: str = "", kind: str = "info") -> None:
        self.sink.put(("log", str(message), kind))
        self.write(message)

    def progress(self, label: str, done: int, total: int) -> None:
        pct = done * 100 // max(total, 1)
        if (label, pct) == self._last and done < total:
            return
        self._last = (label, pct)
        self.sink.put(("progress", label, pct))

    def write(self, message: str) -> None:
        try:
            with open(self.log_path, "a", encoding="utf-8") as fh:
                fh.write("%s %s\n" % (time.strftime("%Y-%m-%d %H:%M:%S"), message))
        except OSError:
            pass


class App:
    def __init__(self, root: "tk.Tk"):
        self.root = root
        self.events: "queue.Queue" = queue.Queue()
        self.reporter = GuiReporter(self.events, os.path.join(app_dir(), LOG_NAME))
        self.busy = False
        self.games: list[lib.Game] = []
        self.game: Optional[lib.Game] = None
        self.game_stats: dict = {}
        self.tc: Optional[lib.Toolchain] = None
        self.devices: list[lib.Device] = []
        self.last_apk: Optional[Path] = None
        self._auto_package = ""
        self._auto_out = ""
        self._buttons: list = []

        root.title(f"RPGM to APK {lib.__version__} - RPG Maker MV/MZ -> Android")
        root.minsize(980, 680)
        geometry = cfg.get("window_geometry")
        try:
            root.geometry(geometry if isinstance(geometry, str) and geometry else "1120x800")
        except tk.TclError:
            root.geometry("1120x800")
        root.protocol("WM_DELETE_WINDOW", self.on_close)

        self._init_style()
        self._build()
        self.reporter.write("=== GUI start, Python %s" % sys.version.split()[0])
        self._welcome()
        self.root.after(80, self._pump)
        self.root.after(150, self.refresh_all)

    # ------------------------------------------------------------ building the window

    def _init_style(self):
        style = ttk.Style()
        for theme in ("vista", "clam", "default"):
            if theme in style.theme_names():
                try:
                    style.theme_use(theme)
                    break
                except tk.TclError:
                    continue
        self.font_bold = tkfont.nametofont("TkDefaultFont").copy()
        self.font_bold.configure(weight="bold")
        self.font_big = tkfont.nametofont("TkDefaultFont").copy()
        self.font_big.configure(weight="bold", size=self.font_big.cget("size") + 2)
        style.configure("Big.TButton", font=self.font_big, padding=(16, 6))
        style.configure("Ok.TLabel", foreground=COL_OK)
        style.configure("Warn.TLabel", foreground=COL_WARN)
        style.configure("Err.TLabel", foreground=COL_ERROR)
        style.configure("Muted.TLabel", foreground=COL_MUTED)

    def _build(self):
        self._buttons = []
        self.outer = ttk.Frame(self.root, padding=10)
        self.outer.pack(fill="both", expand=True)

        top = ttk.Frame(self.outer)
        top.pack(fill="x")
        top.columnconfigure(0, weight=3, uniform="cols")
        top.columnconfigure(1, weight=2, uniform="cols")
        left = ttk.Frame(top)
        left.grid(row=0, column=0, sticky="nsew", padx=(0, 8))
        right = ttk.Frame(top)
        right.grid(row=0, column=1, sticky="nsew")

        self._build_game(left)
        self._build_apk(left)
        self._build_phone(left)
        self._build_tools(right)
        self._build_actions(self.outer)
        self._build_progress(self.outer)
        self._build_log(self.outer)

    def _btn(self, parent, text, command, **kw):
        b = ttk.Button(parent, text=text, command=command, **kw)
        self._buttons.append(b)
        return b

    def _build_game(self, parent):
        box = ttk.LabelFrame(parent, text=tr(" Gra (RPG Maker MV / MZ na PC) ", " Game (RPG Maker MV / MZ for PC) "),
                             padding=8)
        box.pack(fill="x")
        box.columnconfigure(0, weight=1)
        self.game_var = tk.StringVar()
        self.game_combo = ttk.Combobox(box, textvariable=self.game_var, values=[], state="readonly")
        self.game_combo.grid(row=0, column=0, sticky="ew", padx=(0, 6))
        self.game_combo.bind("<<ComboboxSelected>>", lambda _e: self.on_game_selected())
        self._btn(box, tr("Przeglądaj...", "Browse..."), self.browse_game).grid(row=0, column=1)
        self._btn(box, tr("Szukaj gier", "Find games"), self.detect_games).grid(row=0, column=2, padx=(6, 0))
        self.game_info = tk.StringVar(value=tr("Wybierz grę z listy albo wskaż jej folder.",
                                               "Pick a game from the list or browse to its folder."))
        ttk.Label(box, textvariable=self.game_info, style="Muted.TLabel", wraplength=620,
                  justify="left").grid(row=1, column=0, columnspan=3, sticky="w", pady=(6, 0))

    def _build_apk(self, parent):
        box = ttk.LabelFrame(parent, text=tr(" APK ", " APK "), padding=8)
        box.pack(fill="x", pady=(8, 0))
        box.columnconfigure(1, weight=1)
        ttk.Label(box, text=tr("Nazwa pod ikoną:", "Name under the icon:")).grid(row=0, column=0, sticky="w")
        self.label_var = tk.StringVar()
        ttk.Entry(box, textvariable=self.label_var).grid(row=0, column=1, columnspan=2, sticky="ew", pady=2)
        ttk.Label(box, text=tr("Pakiet:", "Package:")).grid(row=1, column=0, sticky="w")
        self.package_var = tk.StringVar()
        ttk.Entry(box, textvariable=self.package_var).grid(row=1, column=1, columnspan=2, sticky="ew", pady=2)
        ttk.Label(box, text=tr("Orientacja:", "Orientation:")).grid(row=2, column=0, sticky="w")
        self.orient_labels = orientation_labels()
        self.orient_var = tk.StringVar(value=self.orient_labels.get(cfg.get("orientation") or "landscape"))
        ttk.Combobox(box, textvariable=self.orient_var, values=list(self.orient_labels.values()),
                     state="readonly", width=28).grid(row=2, column=1, sticky="w", pady=2)
        ttk.Label(box, text=tr("Silnik gry:", "Game engine:")).grid(row=3, column=0, sticky="w")
        self.renderer_labels = renderer_labels()
        saved = cfg.get("renderer")
        self.renderer_var = tk.StringVar(value=self.renderer_labels.get(saved if saved in lib.RENDERERS
                                                                        else lib.DEFAULT_RENDERER))
        renderer_combo = ttk.Combobox(box, textvariable=self.renderer_var, values=list(self.renderer_labels.values()),
                                      state="readonly", width=52)
        renderer_combo.grid(row=3, column=1, columnspan=2, sticky="w", pady=2)
        renderer_combo.bind("<<ComboboxSelected>>", lambda _e: self.refresh_tools())
        ttk.Label(box, text=tr("Plik APK:", "APK file:")).grid(row=4, column=0, sticky="w")
        self.out_var = tk.StringVar()
        ttk.Entry(box, textvariable=self.out_var).grid(row=4, column=1, sticky="ew", pady=2)
        self._btn(box, "...", self.browse_out, width=3).grid(row=4, column=2, padx=(6, 0))

    def _build_phone(self, parent):
        box = ttk.LabelFrame(parent, text=tr(" Telefon przez USB (opcjonalnie) ", " Phone over USB (optional) "),
                             padding=8)
        box.pack(fill="x", pady=(8, 0))
        box.columnconfigure(1, weight=1)
        ttk.Label(box, text=tr("Urządzenie:", "Device:")).grid(row=0, column=0, sticky="w")
        self.device_var = tk.StringVar()
        self.device_combo = ttk.Combobox(box, textvariable=self.device_var, values=[], state="readonly")
        self.device_combo.grid(row=0, column=1, sticky="ew", padx=(6, 6))
        self._btn(box, tr("Odśwież", "Refresh"), self.refresh_devices).grid(row=0, column=2)
        self.install_var = tk.BooleanVar(value=bool(cfg.get("install_after", False)))
        self.push_var = tk.BooleanVar(value=False)
        ttk.Checkbutton(box, text=tr("Zainstaluj na telefonie po zbudowaniu", "Install on the phone after building"),
                        variable=self.install_var).grid(row=1, column=0, columnspan=3, sticky="w", pady=(6, 0))
        self.push_check = ttk.Checkbutton(box, text=tr("Skopiuj zapisy z PC na telefon", "Copy PC saves to the phone"),
                                          variable=self.push_var)
        self.push_check.grid(row=2, column=0, columnspan=3, sticky="w")
        self._btn(box, tr("Pobierz zapisy z telefonu na PC...", "Copy saves from the phone to PC..."),
                  self.pull_saves).grid(row=3, column=0, columnspan=3, sticky="w", pady=(6, 0))
        self.phone_hint = tk.StringVar(value="")
        ttk.Label(box, textvariable=self.phone_hint, style="Muted.TLabel", wraplength=620,
                  justify="left").grid(row=4, column=0, columnspan=3, sticky="w", pady=(4, 0))

    def _build_tools(self, parent):
        box = ttk.LabelFrame(parent, text=tr(" Narzędzia ", " Tools "), padding=8)
        box.pack(fill="both", expand=True)
        self.tools_tree = ttk.Treeview(box, columns=("state", "detail"), show="tree headings", height=9,
                                       selectmode="browse")
        self.tools_tree.heading("#0", text=tr("Składnik", "Component"))
        self.tools_tree.heading("state", text=tr("Stan", "State"))
        self.tools_tree.heading("detail", text=tr("Szczegóły", "Details"))
        self.tools_tree.column("#0", width=150, stretch=False)
        self.tools_tree.column("state", width=90, stretch=False, anchor="center")
        self.tools_tree.column("detail", width=260, stretch=True)
        self.tools_tree.tag_configure("ok", foreground=COL_OK)
        self.tools_tree.tag_configure("missing", foreground=COL_ERROR)
        self.tools_tree.tag_configure("optional", foreground=COL_WARN)
        self.tools_tree.pack(fill="both", expand=True)
        self.tools_tree.bind("<<TreeviewSelect>>", self._tools_selected)
        self.tool_detail = tk.StringVar(value=tr("Kliknij składnik, żeby zobaczyć szczegóły.",
                                                 "Click a component to see the details."))
        ttk.Label(box, textvariable=self.tool_detail, style="Muted.TLabel", wraplength=420,
                  justify="left").pack(fill="x", pady=(4, 0))
        self.tools_summary = tk.StringVar(value=tr("Sprawdzam...", "Checking..."))
        self.tools_summary_label = ttk.Label(box, textvariable=self.tools_summary, wraplength=420, justify="left")
        self.tools_summary_label.pack(fill="x", pady=(6, 0))
        row = ttk.Frame(box)
        row.pack(fill="x", pady=(6, 0))
        self._btn(row, tr("Sprawdź ponownie", "Check again"), self.refresh_tools).pack(side="left")
        self.btn_download = self._btn(row, tr("Pobierz brakujące", "Download missing"), self.download_missing)
        self.btn_download.pack(side="left", padx=(6, 0))
        self._btn(row, tr("Folder narzędzi", "Tools folder"),
                  lambda: open_in_file_manager(str(lib.tools_dir()))).pack(side="left", padx=(6, 0))

    def _build_actions(self, parent):
        row = ttk.Frame(parent)
        row.pack(fill="x", pady=(10, 0))
        self.btn_build = self._btn(row, tr("Zbuduj APK", "Build APK"), self.build, style="Big.TButton")
        self.btn_build.pack(side="left")
        self.btn_open = ttk.Button(row, text=tr("Pokaż APK w folderze", "Show APK in folder"),
                                   command=self.open_output)
        self.btn_open.pack(side="left", padx=(8, 0))
        lang_box = ttk.Frame(row)
        lang_box.pack(side="right")
        ttk.Label(lang_box, text=tr("Język:", "Language:")).pack(side="left", padx=(0, 4))
        self.lang_var = tk.StringVar(value="Polski" if i18n.lang() == "pl" else "English")
        lang_combo = ttk.Combobox(lang_box, textvariable=self.lang_var, values=["Polski", "English"],
                                  state="readonly", width=9)
        lang_combo.pack(side="left")
        lang_combo.bind("<<ComboboxSelected>>", lambda _e: self.change_language())

    def _build_progress(self, parent):
        row = ttk.Frame(parent)
        row.pack(fill="x", pady=(10, 0))
        self.progress = ttk.Progressbar(row, orient="horizontal", mode="determinate", maximum=100)
        self.progress.pack(side="left", fill="x", expand=True)
        self.progress_var = tk.StringVar(value=tr("Gotowy.", "Ready."))
        ttk.Label(row, textvariable=self.progress_var, width=40, anchor="w").pack(side="left", padx=(8, 0))

    def _build_log(self, parent):
        box = ttk.LabelFrame(parent, text=tr(" Co się dzieje ", " What is happening "), padding=6)
        box.pack(fill="both", expand=True, pady=(10, 0))
        self.log_text = tk.Text(box, height=10, wrap="word", state="disabled", borderwidth=0, highlightthickness=0,
                                font=tkfont.nametofont("TkDefaultFont"))
        bar = ttk.Scrollbar(box, orient="vertical", command=self.log_text.yview)
        self.log_text.configure(yscrollcommand=bar.set)
        bar.pack(side="right", fill="y")
        self.log_text.pack(side="left", fill="both", expand=True)
        for tag, color in (("error", COL_ERROR), ("warn", COL_WARN), ("ok", COL_OK), ("muted", COL_MUTED)):
            self.log_text.tag_configure(tag, foreground=color)
        self.log_text.tag_configure("head", foreground=COL_HEAD, font=self.font_bold)

    def _welcome(self):
        self.log(tr("Robi z gry RPG Maker MV/MZ (wersja na PC) aplikację na Androida. Folder gry nie jest zmieniany.",
                    "Turns an RPG Maker MV/MZ game (PC version) into an Android app. The game folder is not changed."),
                 "head")
        self.log(f"RPGM to APK {lib.__version__}  ·  {lib.REPO_URL}  ·  Discord: {DISCORD_URL}", "muted")
        self.log(tr(f"Log: {os.path.join(app_dir(), LOG_NAME)}", f"Log: {os.path.join(app_dir(), LOG_NAME)}"), "muted")

    # ------------------------------------------------------------ log / queue

    def log(self, message: str, kind: str = "info"):
        self.log_text.configure(state="normal")
        self.log_text.insert("end", message + "\n", (kind,) if kind != "info" else ())
        self.log_text.see("end")
        self.log_text.configure(state="disabled")

    def _pump(self):
        try:
            while True:
                event = self.events.get_nowait()
                kind = event[0]
                if kind == "log":
                    self.log(event[1], event[2])
                elif kind == "progress":
                    self.progress.configure(value=event[2])
                    self.progress_var.set(f"{event[1]}  {event[2]}%")
                elif kind == "done":
                    _, callback, result, error, quiet = event
                    if not quiet:
                        self._set_busy(False)
                        self.progress_var.set(tr("Gotowy.", "Ready."))
                    if error is not None:
                        self._show_error(error)
                    if callback:
                        try:
                            callback(result, error)
                        except Exception as exc:          # a bug in a callback must not kill the pump
                            self._show_error(exc)
        except queue.Empty:
            pass
        self.root.after(80, self._pump)

    def _show_error(self, error: BaseException):
        if isinstance(error, lib.BuildError):
            self.log(str(error), "error")
            messagebox.showerror(tr("Błąd", "Error"), str(error), parent=self.root)
        else:
            detail = "".join(traceback.format_exception(type(error), error, error.__traceback__))
            self.reporter.write("ERROR " + detail)
            self.log(tr(f"Nieoczekiwany błąd: {error}", f"Unexpected error: {error}"), "error")
            messagebox.showerror(tr("Błąd", "Error"),
                                 tr(f"Nieoczekiwany błąd:\n{error}\n\nSzczegóły w pliku {LOG_NAME}.",
                                    f"Unexpected error:\n{error}\n\nDetails in {LOG_NAME}."), parent=self.root)

    def _set_busy(self, busy: bool):
        self.busy = busy
        state = "disabled" if busy else "normal"
        for b in self._buttons:
            try:
                b.configure(state=state)
            except tk.TclError:
                pass
        self.game_combo.configure(state="disabled" if busy else "readonly")
        if not busy:
            self.progress.configure(value=0)
            downloadable = bool(self.tc and any(not c.ok and c.download for c in self.tc.components))
            self.btn_download.configure(state="normal" if downloadable else "disabled")

    def run_task(self, fn: Callable, done: Optional[Callable] = None, quiet: bool = False):
        """fn(reporter) in the worker thread, done(result, error) back in the window thread."""
        if self.busy:
            return
        if not quiet:
            self._set_busy(True)

        def worker():
            result, error = None, None
            try:
                result = fn(self.reporter)
            except BaseException as exc:          # noqa: BLE001 - everything goes back to the window
                error = exc
            self.events.put(("done", done, result, error, quiet))

        threading.Thread(target=worker, daemon=True).start()

    # ------------------------------------------------------------ games

    def _game_label(self, g: lib.Game) -> str:
        return f"{g.title}   [{g.engine}]   {g.folder}"

    def refresh_all(self):
        renderer = self._renderer()

        def work(rep):
            games, seen = [], set()
            for path in cfg.recent_games():
                g = lib.detect_game(Path(path))
                if g:
                    games.append(g)
            games += lib.find_steam_games()
            unique = []
            for g in games:
                key = os.path.normcase(str(g.folder))
                if key not in seen:
                    seen.add(key)
                    unique.append(g)
            tc = lib.check_tools(renderer=renderer)
            devices = lib.list_devices(tc.adb) if tc.adb else []
            return unique, tc, devices

        def done(result, error):
            if error:
                return
            games, tc, devices = result
            self._set_games(games)
            self._set_tools(tc)
            self._set_devices(devices)
            if games and not self.game:
                self.game_combo.current(0)
                self.on_game_selected()
            elif not games:
                self.log(tr("Nie znalazłem gier MV/MZ w Steamie - wskaż folder gry przyciskiem „Przeglądaj...”.",
                            "No MV/MZ games found in Steam - pick the game folder with \"Browse...\"."), "muted")

        self.run_task(work, done)

    def _set_games(self, games: list):
        self.games = games
        self.game_combo.configure(values=[self._game_label(g) for g in games])

    def detect_games(self):
        def work(rep):
            rep.log(tr("Szukam gier w bibliotekach Steama...", "Looking for games in Steam libraries..."))
            return lib.find_steam_games()

        def done(games, error):
            if error:
                return
            known = {os.path.normcase(str(g.folder)) for g in self.games}
            new = [g for g in games if os.path.normcase(str(g.folder)) not in known]
            self._set_games(self.games + new)
            self.log(tr(f"Znaleziono gier RPG Maker MV/MZ w Steamie: {len(games)}",
                        f"RPG Maker MV/MZ games found in Steam: {len(games)}"), "ok" if games else "muted")

        self.run_task(work, done)

    def browse_game(self):
        path = filedialog.askdirectory(parent=self.root, title=tr("Folder gry RPG Maker MV/MZ",
                                                                    "RPG Maker MV/MZ game folder"))
        if not path:
            return
        game = lib.detect_game(Path(path))
        if not game:
            messagebox.showwarning(tr("To nie jest gra MV/MZ", "Not an MV/MZ game"),
                                   tr("W tym folderze nie ma gry RPG Maker MV ani MZ (brak index.html, js/ i data/).\n\n"
                                      "RPG Maker XP / VX / VX Ace / 2000 / 2003 nie są obsługiwane.",
                                      "This folder has no RPG Maker MV or MZ game (no index.html, js/ and data/).\n\n"
                                      "RPG Maker XP / VX / VX Ace / 2000 / 2003 are not supported."),
                                   parent=self.root)
            return
        key = os.path.normcase(str(game.folder))
        existing = next((i for i, g in enumerate(self.games) if os.path.normcase(str(g.folder)) == key), None)
        if existing is None:
            self._set_games([game] + self.games)
            existing = 0
        self.game_combo.current(existing)
        self.on_game_selected()

    def on_game_selected(self):
        idx = self.game_combo.current()
        if idx < 0 or idx >= len(self.games):
            return
        game = self.games[idx]
        self.game = game
        cfg.remember_game(str(game.folder))
        if not self.label_var.get() or self.label_var.get() == getattr(self, "_auto_label", ""):
            self.label_var.set(game.title)
        self._auto_label = game.title
        if not self.package_var.get() or self.package_var.get() == self._auto_package:
            self.package_var.set(game.default_package)
        self._auto_package = game.default_package
        out = str(lib.output_dir() / f"{game.safe_title}.apk")
        if not self.out_var.get() or self.out_var.get() == self._auto_out:
            self.out_var.set(out)
        self._auto_out = out
        self.game_info.set(f"RPG Maker {game.engine}  ·  {game.web}  ·  " + tr("liczę rozmiar...", "counting size..."))
        saves = lib.pc_saves(game)
        self.push_check.configure(text=tr(f"Skopiuj zapisy z PC na telefon ({len(saves)} w {game.save_dir})",
                                          f"Copy PC saves to the phone ({len(saves)} in {game.save_dir})"))
        self._update_phone_row()

        def work(rep):
            files, skipped, size = lib.collect_files(game)
            return len(files), skipped, size

        def done(result, error):
            if error or self.game is not game:
                return
            files, skipped, size = result
            warn = ""
            if size > lib.MAX_APK_BYTES:
                warn = tr("  ·  ZA DUŻA na APK (limit ~3,8 GB)", "  ·  TOO BIG for an APK (limit ~3.8 GB)")
            self.game_info.set(f"RPG Maker {game.engine}  ·  {files} " + tr("plików", "files") +
                               f"  ·  {lib.human_size(size)}{warn}  ·  {game.web}")

        self.run_task(work, done, quiet=True)

    def browse_out(self):
        initial = self.out_var.get() or str(lib.output_dir() / "game.apk")
        path = filedialog.asksaveasfilename(parent=self.root, defaultextension=".apk",
                                            filetypes=[("Android APK", "*.apk")],
                                            initialdir=os.path.dirname(initial),
                                            initialfile=os.path.basename(initial))
        if path:
            self.out_var.set(path)

    # ------------------------------------------------------------ tools

    def _set_tools(self, tc: lib.Toolchain):
        self.tc = tc
        tree = self.tools_tree
        tree.delete(*tree.get_children())
        self._tool_details = {}
        for c in tc.components:
            if c.ok:
                state, tag = tr("✓ jest", "✓ found"), "ok"
            elif c.required:
                state, tag = tr("✗ brak", "✗ missing"), "missing"
            else:
                state, tag = tr("– brak", "– missing"), "optional"
            if c.ok and c.key == "keystore" and not lib.debug_keystore().is_file():
                state = tr("✓ utworzy", "✓ will create")
            iid = tree.insert("", "end", text=c.name, values=(state, short_home(c.detail)), tags=(tag,))
            self._tool_details[iid] = c.detail + (f"\n{tr('do pobrania', 'download')}: {c.download}"
                                                  if c.download and not c.ok else "")
        missing = tc.missing_required
        downloadable = [c for c in tc.components if not c.ok and c.download]
        if not missing:
            self.tools_summary.set(tr("Wszystko gotowe do budowania APK.", "Everything is ready to build APKs."))
            self.tools_summary_label.configure(style="Ok.TLabel")
        elif all(c.download for c in missing):
            self.tools_summary.set(tr("Brakuje: ", "Missing: ") + ", ".join(c.name for c in missing) + ".  " +
                                   tr("Pobierz je przyciskiem poniżej albo zrobi się to przy budowaniu.",
                                      "Download them with the button below, or it happens when you build."))
            self.tools_summary_label.configure(style="Warn.TLabel")
        else:
            self.tools_summary.set(tr("Brakuje: ", "Missing: ") + "; ".join(f"{c.name} - {c.detail}" for c in missing))
            self.tools_summary_label.configure(style="Err.TLabel")
        self.btn_download.configure(state="normal" if downloadable and not self.busy else "disabled")
        self._update_phone_row()

    def _tools_selected(self, _event=None):
        sel = self.tools_tree.selection()
        if sel:
            name = self.tools_tree.item(sel[0], "text")
            self.tool_detail.set(f"{name}: {getattr(self, '_tool_details', {}).get(sel[0], '')}")

    def refresh_tools(self):
        renderer = self._renderer()

        def work(rep):
            tc = lib.check_tools(renderer=renderer)
            return tc, (lib.list_devices(tc.adb) if tc.adb else [])

        def done(result, error):
            if error:
                return
            self._set_tools(result[0])
            self._set_devices(result[1])
            self.log(tr("Narzędzia sprawdzone.", "Tools checked."), "muted")

        self.run_task(work, done)

    def _confirm_download(self, comps: list) -> bool:
        lines = "\n".join(f"  •  {c.download}" for c in comps)
        return messagebox.askyesno(
            tr("Pobieranie narzędzi", "Download tools"),
            tr(f"Do pobrania z oficjalnych źródeł:\n\n{lines}\n\nMiejsce: {lib.tools_dir()}\n"
               "Nic nie trafia do systemu - usunięcie folderu programu sprząta wszystko.\n\n"
               f"Pobierając składniki Android SDK akceptujesz licencję:\n{lib.SDK_LICENSE_URL}\n\nPobrać teraz?",
               f"To download from official sources:\n\n{lines}\n\nInto: {lib.tools_dir()}\n"
               "Nothing is installed system-wide - deleting the program folder cleans everything.\n\n"
               f"By downloading Android SDK components you accept its license:\n{lib.SDK_LICENSE_URL}\n\n"
               "Download now?"), parent=self.root)

    def download_missing(self):
        if not self.tc:
            return
        comps = [c for c in self.tc.components if not c.ok and c.download]
        if not comps or not self._confirm_download(comps):
            return
        renderer = self._renderer()

        def work(rep):
            lib.download_components([c.key for c in comps], rep)
            tc = lib.check_tools(renderer=renderer)
            return tc, (lib.list_devices(tc.adb) if tc.adb else [])

        def done(result, error):
            if error:
                return
            self._set_tools(result[0])
            self._set_devices(result[1])

        self.run_task(work, done)

    # ------------------------------------------------------------ phone

    def _set_devices(self, devices: list):
        self.devices = devices
        labels = [str(d) for d in devices]
        self.device_combo.configure(values=labels)
        ready = [i for i, d in enumerate(devices) if d.ready]
        if ready:
            self.device_combo.current(ready[0])
        elif labels:
            self.device_combo.current(0)
        else:
            self.device_var.set("")
        self._update_phone_row()

    def _update_phone_row(self):
        if not self.tc or not self.tc.adb:
            self.phone_hint.set(tr("adb nie jest zainstalowany - pobierz go w panelu „Narzędzia” (~7 MB). "
                                   "Bez niego APK skopiujesz na telefon ręcznie.",
                                   "adb is not installed - download it in the \"Tools\" panel (~7 MB). "
                                   "Without it, copy the APK to the phone by hand."))
        elif not self.devices:
            self.phone_hint.set(tr("Brak telefonu. Włącz w nim Opcje programisty -> Debugowanie USB i podłącz kabel.",
                                   "No phone. Enable Developer options -> USB debugging on it and plug in the cable."))
        else:
            dev = self.selected_device(any_state=True)
            self.phone_hint.set(lib.device_hint(dev) if dev and not dev.ready else
                                tr(f"Zapisy na telefonie: {lib.remote_save_dir(self.package_var.get() or '<pakiet>')}",
                                   f"Saves on the phone: {lib.remote_save_dir(self.package_var.get() or '<package>')}"))

    def selected_device(self, any_state: bool = False) -> Optional[lib.Device]:
        idx = self.device_combo.current()
        if 0 <= idx < len(self.devices):
            d = self.devices[idx]
            return d if (d.ready or any_state) else None
        return None

    def refresh_devices(self):
        if not self.tc or not self.tc.adb:
            self._update_phone_row()
            return
        adb = self.tc.adb
        self.run_task(lambda rep: lib.list_devices(adb), lambda devs, err: None if err else self._set_devices(devs))

    def pull_saves(self):
        package = self.package_var.get().strip()
        dev = self.selected_device()
        if not package or not lib.valid_package(package):
            messagebox.showwarning("APK", tr("Najpierw wybierz grę (potrzebna nazwa pakietu).",
                                             "Pick a game first (the package name is needed)."), parent=self.root)
            return
        if not self.tc or not self.tc.adb or not dev:
            messagebox.showwarning(tr("Telefon", "Phone"), self.phone_hint.get() or tr("Brak telefonu.", "No phone."),
                                   parent=self.root)
            return
        dest = filedialog.askdirectory(parent=self.root, title=tr("Gdzie zapisać zapisy z telefonu?",
                                                                   "Where to put the saves from the phone?"))
        if not dest:
            return
        adb = self.tc.adb
        self.run_task(lambda rep: lib.pull_saves(adb, dev.serial, package, Path(dest), rep),
                      lambda n, err: None if err else open_in_file_manager(dest))

    # ------------------------------------------------------------ build

    def _renderer(self) -> str:
        label = self.renderer_var.get()
        return next((k for k, v in self.renderer_labels.items() if v == label), lib.DEFAULT_RENDERER)

    def _orientation(self) -> str:
        label = self.orient_var.get()
        return next((k for k, v in self.orient_labels.items() if v == label), "landscape")

    def build(self):
        game = self.game
        if not game:
            messagebox.showwarning("APK", tr("Najpierw wybierz grę.", "Pick a game first."), parent=self.root)
            return
        package = self.package_var.get().strip()
        if not lib.valid_package(package):
            messagebox.showwarning("APK", tr(f"Nieprawidłowa nazwa pakietu:\n{package}\n\nPrzykład: pl.moje.gra",
                                             f"Invalid package name:\n{package}\n\nExample: com.my.game"),
                                   parent=self.root)
            return
        out_raw = self.out_var.get().strip() or str(lib.output_dir() / f"{game.safe_title}.apk")
        out = Path(out_raw)
        if out.suffix.lower() != ".apk":
            out = out.with_suffix(".apk")
        install = self.install_var.get()
        push = self.push_var.get()
        with_phone = install or push
        dev = self.selected_device() if with_phone else None
        renderer = self._renderer()
        tc = lib.check_tools(renderer=renderer)
        need = [c for c in tc.components if not c.ok and c.download and (c.required or (with_phone and c.key == "adb"))]
        blocking = [c for c in tc.missing_required if not c.download]
        if blocking:
            messagebox.showerror(tr("Brak narzędzi", "Missing tools"),
                                 "\n".join(f"{c.name}: {c.detail}" for c in blocking), parent=self.root)
            return
        if need and not self._confirm_download(need):
            return
        if with_phone and not dev and not (tc.adb is None and any(c.key == "adb" for c in need)):
            if not messagebox.askyesno(tr("Brak telefonu", "No phone"),
                                       tr("Nie wybrano gotowego telefonu. Zbudować APK bez instalacji?",
                                          "No ready phone selected. Build the APK without installing?"),
                                       parent=self.root):
                return
            install = push = False
        opts = lib.BuildOptions(game=game, out=out, package=package,
                                label=self.label_var.get().strip() or game.title, orientation=self._orientation(),
                                renderer=renderer)
        cfg.set_value("orientation", opts.orientation)
        cfg.set_value("renderer", opts.renderer)
        cfg.set_value("install_after", bool(self.install_var.get()))
        serial = dev.serial if dev else None

        def work(rep):
            nonlocal serial
            toolchain = tc
            if need:
                lib.download_components([c.key for c in need], rep)
                toolchain = lib.check_tools(renderer=renderer)
            rep.log(tr(f"Buduję: {game.title} -> {out}", f"Building: {game.title} -> {out}"), "head")
            result = lib.build_apk(opts, toolchain, rep)
            rep.log(f"{result.apk}  ({lib.human_size(result.size)})", "ok")
            if (install or push) and toolchain.adb:
                if not serial:
                    ready = [d for d in lib.list_devices(toolchain.adb) if d.ready]
                    serial = ready[0].serial if len(ready) == 1 else None
                if serial:
                    lib.install_apk(toolchain.adb, serial, result.apk, package, rep)
                    if push:
                        lib.push_saves(toolchain.adb, serial, game, package, rep)
                else:
                    rep.log(tr("Nie wybrano telefonu - pomijam instalację.", "No phone selected - skipping install."),
                            "warn")
            return result, toolchain

        def done(res, error):
            if error:
                return
            result, toolchain = res
            self.last_apk = result.apk
            self._set_tools(toolchain)
            self.log(tr("Gotowe! Skopiuj APK na telefon i otwórz go. Nie udostępniaj APK - zawiera pliki gry.",
                        "Done! Copy the APK to the phone and open it. Do not share the APK - it contains the game."),
                     "ok")
            if messagebox.askyesno(tr("Gotowe", "Done"),
                                   tr(f"APK gotowe:\n{result.apk}\n({lib.human_size(result.size)})\n\n"
                                      "Pokazać plik w folderze?",
                                      f"APK ready:\n{result.apk}\n({lib.human_size(result.size)})\n\n"
                                      "Show the file in its folder?"), parent=self.root):
                open_in_file_manager(str(result.apk))

        self.run_task(work, done)

    def open_output(self):
        if self.last_apk and self.last_apk.is_file():
            open_in_file_manager(str(self.last_apk))
            return
        target = Path(self.out_var.get()) if self.out_var.get() else lib.output_dir()
        folder = target if target.is_dir() else target.parent
        folder.mkdir(parents=True, exist_ok=True)
        open_in_file_manager(str(folder))

    # ------------------------------------------------------------ language / closing

    def change_language(self):
        code = "pl" if self.lang_var.get() == "Polski" else "en"
        if code == i18n.lang():
            return
        cfg.set_value("lang", code)
        i18n.set_lang(code)
        state = {"label": self.label_var.get(), "package": self.package_var.get(), "out": self.out_var.get(),
                 "orient": self._orientation(), "renderer": self._renderer(),
                 "install": self.install_var.get(), "push": self.push_var.get(),
                 "game": self.game_combo.current(), "device": self.device_combo.current()}
        self.outer.destroy()
        self._init_style()
        self._build()
        self.label_var.set(state["label"])
        self.package_var.set(state["package"])
        self.out_var.set(state["out"])
        self.orient_var.set(self.orient_labels[state["orient"]])
        self.renderer_var.set(self.renderer_labels[state["renderer"]])
        self.install_var.set(state["install"])
        self.push_var.set(state["push"])
        self._set_games(self.games)
        if state["game"] >= 0:
            self.game_combo.current(state["game"])
            self.on_game_selected()
        if self.tc:
            self._set_tools(self.tc)
        self._set_devices(self.devices)
        if 0 <= state["device"] < len(self.devices):
            self.device_combo.current(state["device"])
        self._welcome()

    def on_close(self):
        if self.busy and not messagebox.askyesno(
                tr("Trwa praca", "Work in progress"),
                tr("Budowanie albo pobieranie jeszcze trwa. Zamknąć mimo to?",
                   "Building or downloading is still running. Close anyway?"), parent=self.root):
            return
        try:
            cfg.set_value("window_geometry", self.root.geometry())
        except Exception:
            pass
        self.reporter.write("=== GUI end")
        self.root.destroy()


def _enable_dpi_awareness() -> None:
    if os.name != "nt":
        return
    try:
        import ctypes
        ctypes.windll.shcore.SetProcessDpiAwareness(1)
    except Exception:
        try:
            import ctypes
            ctypes.windll.user32.SetProcessDPIAware()
        except Exception:
            pass


def _crash(exc: BaseException) -> int:
    detail = traceback.format_exc()
    try:
        with open(os.path.join(app_dir(), LOG_NAME), "a", encoding="utf-8") as fh:
            fh.write("%s CRASH %r\n%s\n" % (time.strftime("%Y-%m-%d %H:%M:%S"), exc, detail))
    except OSError:
        pass
    try:
        messagebox.showerror("RPGM to APK",
                             tr(f"Program się wysypał:\n\n{exc}\n\nSzczegóły: {os.path.join(app_dir(), LOG_NAME)}\n"
                                "Możesz użyć wersji konsolowej (convert.bat / sh convert.sh).",
                                f"The program crashed:\n\n{exc}\n\nDetails: {os.path.join(app_dir(), LOG_NAME)}\n"
                                "You can use the console version (convert.bat / sh convert.sh)."))
    except Exception:
        print(detail)
    return 1


def _detach() -> bool:
    """--detach (gui.bat): restart under pythonw.exe so no console window stays behind.
    True = the window runs in the new process, this one can exit."""
    if os.name != "nt":
        return False
    exe = Path(sys.executable)
    pyw = exe.with_name("pythonw.exe")
    if exe.name.lower() != "python.exe" or not pyw.is_file():
        return False
    args = [a for a in sys.argv[1:] if a != "--detach"]
    try:
        subprocess.Popen([str(pyw), os.path.abspath(__file__), *args], close_fds=True,
                         creationflags=0x00000008 | 0x00000200)   # DETACHED_PROCESS | CREATE_NEW_PROCESS_GROUP
        return True
    except OSError:
        return False


def main() -> int:
    if "--detach" in sys.argv:
        if _detach():
            return 0
        sys.argv.remove("--detach")
    if "--lang" in sys.argv:
        i = sys.argv.index("--lang")
        if i + 1 < len(sys.argv):
            i18n.set_lang(sys.argv[i + 1])
    _enable_dpi_awareness()
    try:
        root = tk.Tk()
    except tk.TclError as exc:
        _die(tr(f"[BŁĄD] Nie mogę otworzyć okna ({exc}).\n\nUżyj wersji konsolowej: convert.bat / sh convert.sh",
                f"[ERROR] Cannot open a window ({exc}).\n\nUse the console version: convert.bat / sh convert.sh"))
        return EXIT_HANDLED
    App(root)
    root.mainloop()
    return 0


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except SystemExit:
        raise
    except KeyboardInterrupt:
        raise SystemExit(EXIT_HANDLED)
    except Exception as _exc:
        raise SystemExit(_crash(_exc))
