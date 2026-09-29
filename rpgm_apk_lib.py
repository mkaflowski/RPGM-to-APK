#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""RPG Maker MV / MZ (PC, NW.js) -> native Android APK. The engine.

The game is not emulated: its HTML5/JS files go into assets/ of a tiny WebView
app (android/MainActivity.java) and run on the phone's own Chromium.
The game folder on the PC is never modified.

This module has no print() and no input(): the console front end (cli.py) and
the window (gui.py) pass a Reporter and ask their own questions. Both use
exactly the same functions, so they build byte-identical APKs.

Build steps (no Gradle, no Android Studio needed):
  aapt2 link (manifest + icon) -> classes.dex (shipped prebuilt, or javac + d8)
  -> game files into assets/www -> zipalign -> apksigner (v2 signature)

Missing tools are downloaded on request from official sources into
<program folder>/.tools: Android build-tools / platform-tools / android.jar
from Google's Android SDK repository, a Java 17 JRE from Eclipse Temurin.
"""

from __future__ import annotations

import hashlib
import json
import os
import re
import shutil
import subprocess
import sys
import tempfile
import time
import unicodedata
import urllib.request
import xml.etree.ElementTree as ET
import zipfile
from dataclasses import dataclass, field
from pathlib import Path
from xml.sax.saxutils import escape as xml_escape

from i18n import tr

__version__ = "1.0.0"
REPO_URL = "https://github.com/mkaflowski/RPGM-to-APK"

HERE = Path(__file__).resolve().parent
ANDROID_SRC = HERE / "android"
JAVA_SRC = ANDROID_SRC / "MainActivity.java"
BRIDGE_JS = ANDROID_SRC / "UT_AndroidBridge.js"
PREBUILT_DEX = ANDROID_SRC / "classes.dex"             # compiled MainActivity.java (no JDK needed)
PREBUILT_SHA = ANDROID_SRC / "classes.dex.sha256"      # sha256 of the MainActivity.java it came from
BRIDGE_NAME = "UT_AndroidBridge"
ACTIVITY = "pl.unitytranslator.rpgm.MainActivity"
PACKAGE_PREFIX = "pl.unitytranslator.rpgm"
MIN_SDK = 24
EXE = ".exe" if os.name == "nt" else ""

REPO_XML = "https://dl.google.com/android/repository/repository2-3.xml"
REPO_BASE = "https://dl.google.com/android/repository/"
PLATFORM_PKG = "platforms;android-35"
BUILD_TOOLS_PKG = "build-tools;36.0.0"
PLATFORM_TOOLS_PKG = "platform-tools"
JRE_FEATURE = 17
SDK_LICENSE_URL = "https://developer.android.com/studio/terms"
USER_AGENT = "rpgm-to-apk"

# not packed into the APK (NW.js / Steam runtime, system junk)
SKIP_EXT = {".exe", ".dll", ".so", ".dylib", ".node", ".lib", ".pdb", ".pak", ".original", ".log"}
SKIP_ROOT_FILES = {"icudtl.dat", "v8_context_snapshot.bin", "snapshot_blob.bin", "natives_blob.bin",
                   "vk_swiftshader_icd.json", "steam_appid.txt", "chromedriver"}
SKIP_ROOT_DIRS = {"locales", "swiftshader", "save", "pnacl"}
SKIP_NAMES = {"thumbs.db", "desktop.ini", ".ds_store"}
# already compressed -> ZIP_STORED (faster, AssetManager can mmap them and serve Range requests)
STORE_EXT = {".ogg", ".m4a", ".mp3", ".png", ".jpg", ".jpeg", ".webp", ".gif", ".webm", ".mp4", ".ogv",
             ".png_", ".ogg_", ".m4a_", ".rpgmvp", ".rpgmvo", ".rpgmvm", ".rpgmvw",
             ".woff", ".woff2", ".zip", ".7z", ".gz"}
JAVA_KEYWORDS = set("abstract assert boolean break byte case catch char class const continue default do "
                    "double else enum extends final finally float for goto if implements import instanceof "
                    "int interface long native new package private protected public return short static "
                    "strictfp super switch synchronized this throw throws transient try void volatile while "
                    "true false null var".split())
ORIENTATIONS = {"landscape": "sensorLandscape", "portrait": "sensorPortrait", "any": "fullUser"}
MAX_APK_BYTES = 3_800_000_000        # above ~4 GB the APK would need ZIP64 - Android refuses to install it


class BuildError(RuntimeError):
    """Expected failure with a message for the user (no traceback needed)."""


class Reporter:
    """Where the engine talks to. kinds: info, ok, warn, error, head, muted."""

    def log(self, message: str = "", kind: str = "info") -> None:
        pass

    def progress(self, label: str, done: int, total: int) -> None:
        pass

    def step(self, n: int, total: int, title: str) -> None:
        self.log(f"[{n}/{total}] {title}", "head")


# ====================================================================== paths

def app_dir() -> Path:
    """Program folder: launchers set RPGM_APK_DIR, otherwise the folder of this file."""
    env = os.environ.get("RPGM_APK_DIR")
    return Path(env).resolve() if env else HERE


def tools_dir() -> Path:
    """Downloaded tools live inside the program folder - deleting it cleans everything."""
    d = app_dir() / ".tools"
    d.mkdir(parents=True, exist_ok=True)
    return d


def output_dir() -> Path:
    return app_dir() / "output"


def parse_path(raw: str) -> Path:
    """Pasted / dragged path: quotes, file:// URIs, escaped spaces from a Linux terminal."""
    s = raw.strip().strip('"').strip("'").strip()
    if s.startswith("file://"):
        from urllib.parse import unquote, urlparse
        s = unquote(urlparse(s).path)
        if os.name == "nt" and re.match(r"^/[A-Za-z]:", s):
            s = s[1:]
    if os.name != "nt":
        s = re.sub(r"\\(.)", r"\1", s)
    return Path(os.path.expanduser(s))


def human_size(size: float) -> str:
    for unit, div in (("GB", 1e9), ("MB", 1e6), ("KB", 1e3)):
        if size >= div:
            return f"{size / div:.1f} {unit}"
    return f"{int(size)} B"


# ====================================================================== games

class Game:
    def __init__(self, folder: Path, web: Path, engine: str):
        self.folder = folder            # what the user picked / the folder in steamapps/common
        self.web = web                  # folder with index.html (MZ: folder, MV: usually folder/www)
        self.engine = engine            # "MZ" / "MV"
        self.system = {}
        try:
            self.system = json.loads((web / "data" / "System.json").read_text(encoding="utf-8-sig"))
        except Exception:
            pass
        self.title = str(self.system.get("gameTitle") or "").strip() or folder.name

    @property
    def icon(self) -> Path | None:
        cands = [self.web / "icon" / "icon.png"]
        for pkg in (self.folder / "package.json", self.web / "package.json"):
            try:
                rel = (json.loads(pkg.read_text(encoding="utf-8-sig")).get("window") or {}).get("icon")
                if rel:
                    cands.insert(0, pkg.parent / rel)
            except Exception:
                pass
        return next((c for c in cands if c.is_file()), None)

    @property
    def save_dir(self) -> Path:
        return self.web / "save"

    @property
    def default_package(self) -> str:
        return f"{PACKAGE_PREFIX}.{slugify(self.title)}"

    @property
    def safe_title(self) -> str:
        return re.sub(r'[<>:"/\\|?*]+', "_", self.title).strip() or "game"

    def __repr__(self):
        return f"Game({self.title!r}, {self.engine}, {str(self.folder)!r})"


def detect_game(path: Path) -> Game | None:
    """RPG Maker MV/MZ folder (or its Game.exe / index.html) -> Game, else None."""
    try:
        if path.is_file():
            path = path.parent
    except OSError:
        return None
    cands = [path, path / "www"]
    pkg = path / "package.json"
    try:
        if pkg.is_file():
            main = json.loads(pkg.read_text(encoding="utf-8-sig")).get("main", "")
            if main and main.lower().endswith((".html", ".htm")):
                cands.insert(0, (path / main).parent)
    except Exception:
        pass
    for web in cands:
        try:
            if not (web / "index.html").is_file() or not (web / "data" / "System.json").is_file():
                continue
            if (web / "js" / "rmmz_core.js").is_file():
                return Game(path, web, "MZ")
            if (web / "js" / "rpg_core.js").is_file():
                return Game(path, web, "MV")
        except OSError:
            continue
    return None


def steam_libraries() -> list[Path]:
    roots = []
    if os.name == "nt":
        try:
            import winreg
            for hive, key, value in ((winreg.HKEY_CURRENT_USER, r"Software\Valve\Steam", "SteamPath"),
                                     (winreg.HKEY_LOCAL_MACHINE, r"SOFTWARE\WOW6432Node\Valve\Steam", "InstallPath"),
                                     (winreg.HKEY_LOCAL_MACHINE, r"SOFTWARE\Valve\Steam", "InstallPath")):
                try:
                    with winreg.OpenKey(hive, key) as k:
                        roots.append(Path(winreg.QueryValueEx(k, value)[0]))
                except OSError:
                    pass
        except Exception:
            pass
    else:
        home = Path.home()
        roots += [home / ".local/share/Steam", home / ".steam/steam", home / ".steam/root",
                  home / ".steam/debian-installation",
                  home / ".var/app/com.valvesoftware.Steam/.local/share/Steam",
                  home / "snap/steam/common/.local/share/Steam", home / "Library/Application Support/Steam"]
        for media in (Path("/run/media"), Path("/media"), Path("/mnt")):
            try:
                roots += [p for p in media.glob("*/*") if (p / "steamapps").is_dir()]
                roots += [p for p in media.glob("*") if (p / "steamapps").is_dir()]
            except Exception:
                pass
    libs, seen = [], set()

    def add(p: Path):
        try:
            real = Path(os.path.realpath(p))
            key = os.path.normcase(str(real))
            if key not in seen and (real / "steamapps").is_dir():
                seen.add(key)
                libs.append(real)
        except Exception:
            pass

    for root in roots:
        try:
            add(root)
            vdf = root / "steamapps" / "libraryfolders.vdf"
            if vdf.is_file():
                for m in re.finditer(r'"path"\s*"([^"]+)"', vdf.read_text(encoding="utf-8", errors="replace")):
                    add(Path(m.group(1).replace("\\\\", "\\")))
        except Exception:
            pass
    return libs


def find_steam_games() -> list[Game]:
    games = []
    for lib in steam_libraries():
        try:
            dirs = sorted(p for p in (lib / "steamapps" / "common").iterdir() if p.is_dir())
        except Exception:
            continue
        for d in dirs:
            try:
                g = detect_game(d)
                if g:
                    games.append(g)
            except Exception:
                pass
    return games


def slugify(text: str) -> str:
    text = text.translate(str.maketrans({"ł": "l", "Ł": "L", "đ": "d", "Đ": "D", "ø": "o", "Ø": "O", "ß": "ss"}))
    s = unicodedata.normalize("NFKD", text).encode("ascii", "ignore").decode().lower()
    s = re.sub(r"[^a-z0-9]+", "_", s).strip("_") or "game"
    if s[0].isdigit() or s in JAVA_KEYWORDS:
        s = "g_" + s
    return s[:40].rstrip("_")


def valid_package(pkg: str) -> bool:
    segs = pkg.split(".")
    return len(segs) >= 2 and all(re.fullmatch(r"[A-Za-z][A-Za-z0-9_]*", s) and s not in JAVA_KEYWORDS
                                  for s in segs)


# ====================================================================== downloads

def _download(url: str, dest: Path, rep: Reporter, label: str, *, sha1: str | None = None,
              sha256: str | None = None) -> Path:
    dest.parent.mkdir(parents=True, exist_ok=True)
    part = dest.with_name(dest.name + ".part")
    h1, h256 = hashlib.sha1(), hashlib.sha256()
    req = urllib.request.Request(url, headers={"User-Agent": USER_AGENT})
    try:
        with urllib.request.urlopen(req, timeout=120) as r, open(part, "wb") as f:
            total = int(r.headers.get("Content-Length") or 0)
            done = 0
            while True:
                chunk = r.read(1 << 20)
                if not chunk:
                    break
                f.write(chunk)
                h1.update(chunk)
                h256.update(chunk)
                done += len(chunk)
                rep.progress(label, done, total or done)
    except OSError as e:
        part.unlink(missing_ok=True)
        raise BuildError(tr(f"Nie udało się pobrać {label}: {e}\nSprawdź połączenie z internetem.",
                            f"Could not download {label}: {e}\nCheck your internet connection.")) from e
    if (sha1 and h1.hexdigest() != sha1.lower()) or (sha256 and h256.hexdigest() != sha256.lower()):
        part.unlink(missing_ok=True)
        raise BuildError(tr(f"Suma kontrolna pobranego pliku się nie zgadza: {url}",
                            f"Checksum mismatch for the downloaded file: {url}"))
    os.replace(part, dest)
    return dest


def _repo_archive(package: str, host_os: str | None = None) -> tuple[str, str]:
    """(url, sha1) of a package in the official Android SDK repository."""
    try:
        with urllib.request.urlopen(urllib.request.Request(REPO_XML, headers={"User-Agent": USER_AGENT}),
                                    timeout=60) as r:
            root = ET.fromstring(r.read())
    except OSError as e:
        raise BuildError(tr(f"Brak połączenia z repozytorium Android SDK: {e}",
                            f"Cannot reach the Android SDK repository: {e}")) from e
    for pkg in root.iter():
        if pkg.tag.split("}")[-1] != "remotePackage" or pkg.get("path") != package:
            continue
        for arch in pkg.iter():
            if arch.tag.split("}")[-1] != "archive":
                continue
            host = next((e.text for e in arch.iter() if e.tag.split("}")[-1] == "host-os"), None)
            if host_os and host and host != host_os:
                continue
            url = next((e.text for e in arch.iter() if e.tag.split("}")[-1] == "url"), None)
            sha1 = next((e.text for e in arch.iter() if e.tag.split("}")[-1] == "checksum"), None)
            if url:
                return url.strip(), (sha1 or "").strip()
    raise BuildError(tr(f"Nie znalazłem {package} ({host_os or 'dowolny system'}) w repozytorium Android SDK",
                        f"{package} ({host_os or 'any OS'}) not found in the Android SDK repository"))


def _host_os() -> str:
    return {"win32": "windows", "darwin": "macosx"}.get(sys.platform, "linux")


def _extract_zip(archive: Path, dest: Path):
    base = str(dest.resolve())
    with zipfile.ZipFile(archive) as z:
        for member in z.infolist():
            target = dest / member.filename
            if not str(target.resolve()).startswith(base):
                raise BuildError(f"Unsafe path in archive: {member.filename}")
            z.extract(member, dest)
            mode = (member.external_attr >> 16) & 0o777
            if os.name != "nt" and mode and not member.is_dir():
                os.chmod(target, mode)


def download_build_tools(rep: Reporter) -> tuple[Path, Path]:
    sdk = tools_dir() / "sdk"
    dest = sdk / "build-tools" / BUILD_TOOLS_PKG.split(";")[1]
    url, sha1 = _repo_archive(BUILD_TOOLS_PKG, _host_os())
    archive = _download(REPO_BASE + url, tools_dir() / url, rep, "Android build-tools", sha1=sha1)
    tmp = tools_dir() / "build-tools.extract"
    shutil.rmtree(tmp, ignore_errors=True)
    _extract_zip(archive, tmp)
    inner = next((p.parent for p in tmp.rglob(f"aapt2{EXE}")), None)
    if not inner or not _build_tools_ok(inner, True):
        raise BuildError(f"{url}: aapt2 / zipalign / apksigner missing")
    shutil.rmtree(dest, ignore_errors=True)
    dest.parent.mkdir(parents=True, exist_ok=True)
    shutil.move(str(inner), str(dest))
    shutil.rmtree(tmp, ignore_errors=True)
    archive.unlink(missing_ok=True)
    rep.log(f"build-tools -> {dest}", "ok")
    return sdk, dest


def download_platform_tools(rep: Reporter) -> Path:
    sdk = tools_dir() / "sdk"
    url, sha1 = _repo_archive(PLATFORM_TOOLS_PKG, _host_os())
    archive = _download(REPO_BASE + url, tools_dir() / url, rep, "adb (platform-tools)", sha1=sha1)
    shutil.rmtree(sdk / "platform-tools", ignore_errors=True)
    sdk.mkdir(parents=True, exist_ok=True)
    _extract_zip(archive, sdk)                      # the archive already contains platform-tools/
    archive.unlink(missing_ok=True)
    adb = sdk / "platform-tools" / f"adb{EXE}"
    if not adb.is_file():
        raise BuildError(f"{url}: adb missing")
    rep.log(f"adb -> {adb}", "ok")
    return adb


def download_jre(rep: Reporter) -> Path:
    import platform
    os_name = {"win32": "windows", "darwin": "mac"}.get(sys.platform, "linux")
    arch = "aarch64" if platform.machine().lower() in ("arm64", "aarch64") else "x64"
    api = (f"https://api.adoptium.net/v3/assets/latest/{JRE_FEATURE}/hotspot?architecture={arch}"
           f"&image_type=jre&os={os_name}&vendor=eclipse")
    try:
        with urllib.request.urlopen(urllib.request.Request(api, headers={"User-Agent": USER_AGENT}),
                                    timeout=60) as r:
            assets = json.loads(r.read())
    except OSError as e:
        raise BuildError(tr(f"Brak połączenia z Adoptium (Java): {e}", f"Cannot reach Adoptium (Java): {e}")) from e
    if not assets:
        raise BuildError(f"Adoptium: no JRE {JRE_FEATURE} for {os_name}/{arch}")
    pkg = assets[0]["binary"]["package"]
    archive = _download(pkg["link"], tools_dir() / pkg["name"], rep, f"Java {JRE_FEATURE} (JRE)",
                        sha256=pkg.get("checksum"))
    root = tools_dir() / "jre"
    shutil.rmtree(root, ignore_errors=True)
    root.mkdir(parents=True)
    if archive.name.endswith(".zip"):
        _extract_zip(archive, root)
    else:
        import tarfile
        with tarfile.open(archive) as tar:
            try:
                tar.extractall(root, filter="tar")
            except TypeError:
                tar.extractall(root)
    archive.unlink(missing_ok=True)
    homes = [p for d in root.iterdir() if d.is_dir() for p in (d, d / "Contents" / "Home")]
    home = next((p for p in homes if _java_major(p, False) >= 11), None)
    if not home:
        raise BuildError(f"The unpacked JRE does not work: {root}")
    rep.log(f"Java -> {home}", "ok")
    return home


def download_android_jar(rep: Reporter) -> Path:
    dest = tools_dir() / "android-35" / "android.jar"
    url, sha1 = _repo_archive(PLATFORM_PKG)
    archive = _download(REPO_BASE + url, dest.parent / url, rep, "android.jar (Android 15)", sha1=sha1)
    with zipfile.ZipFile(archive) as z:
        member = next((n for n in z.namelist() if n.endswith("/android.jar")), None)
        if not member:
            raise BuildError(f"{url}: android.jar missing")
        data = z.read(member)
    part = dest.with_suffix(".jar.part")
    part.write_bytes(data)
    os.replace(part, dest)
    archive.unlink(missing_ok=True)
    rep.log(f"android.jar -> {dest}", "ok")
    return dest


# ====================================================================== finding tools

def _version_key(name: str):
    return [int(x) if x.isdigit() else -1 for x in re.split(r"[.\-_]", name)]


def _build_tools_ok(bt: Path, need_d8: bool) -> bool:
    need = [bt / f"aapt2{EXE}", bt / f"zipalign{EXE}", bt / "lib" / "apksigner.jar"]
    if need_d8:
        need.append(bt / "lib" / "d8.jar")
    return all(p.is_file() for p in need)


def _sdk_candidates(arg: str | None) -> list[Path]:
    cands = [Path(arg)] if arg else []
    for var in ("ANDROID_HOME", "ANDROID_SDK_ROOT"):
        if os.environ.get(var):
            cands.append(Path(os.environ[var]))
    if os.name == "nt" and os.environ.get("LOCALAPPDATA"):
        cands.append(Path(os.environ["LOCALAPPDATA"]) / "Android" / "Sdk")
    cands += [Path.home() / "Android" / "Sdk", Path.home() / "Library" / "Android" / "sdk",
              Path("/opt/android-sdk"), Path("/usr/lib/android-sdk"), tools_dir() / "sdk"]
    return cands


def find_build_tools(arg: str | None, need_d8: bool) -> tuple[Path, Path] | None:
    for sdk in _sdk_candidates(arg):
        bt_root = sdk / "build-tools"
        if not bt_root.is_dir():
            continue
        for bt in sorted((p for p in bt_root.iterdir() if p.is_dir()), key=lambda p: _version_key(p.name),
                         reverse=True):
            if _build_tools_ok(bt, need_d8):
                return sdk, bt
    return None


def find_android_jar(arg: str | None) -> tuple[Path, int] | None:
    found = []
    for sdk in _sdk_candidates(arg):
        plats = sdk / "platforms"
        if not plats.is_dir():
            continue
        for d in plats.iterdir():
            m = re.fullmatch(r"android-(\d+)(?:[.\-].*)?", d.name)
            if m and (d / "android.jar").is_file() and int(m.group(1)) >= 30:
                found.append((int(m.group(1)), d / "android.jar"))
    if found:
        api, jar = max(found)
        return jar, api
    cached = tools_dir() / "android-35" / "android.jar"
    return (cached, 35) if cached.is_file() else None


def find_adb(arg: str | None = None) -> Path | None:
    for sdk in _sdk_candidates(arg):
        p = sdk / "platform-tools" / f"adb{EXE}"
        if p.is_file():
            return p
    found = shutil.which("adb")
    return Path(found) if found else None


def _java_major(home: Path, need_javac: bool) -> int:
    b = home / "bin"
    if not (b / f"java{EXE}").is_file() or not (b / f"keytool{EXE}").is_file():
        return 0
    if need_javac and not (b / f"javac{EXE}").is_file():
        return 0
    try:
        r = subprocess.run([str(b / f"java{EXE}"), "-version"], capture_output=True, text=True, timeout=60,
                           **_no_window())
        m = re.search(r'version\s+"(\d+)(?:\.(\d+))?', (r.stdout or "") + (r.stderr or ""))
        if not m:
            return 0
        major = int(m.group(1))
        return int(m.group(2) or 0) if major == 1 else major
    except Exception:
        return 0


def find_java(arg: str | None, need_javac: bool) -> tuple[Path, int] | None:
    """(JAVA_HOME, major version) of the best Java 11+ with java + keytool (+ javac if needed)."""
    if arg:
        major = _java_major(Path(arg), need_javac)
        if major >= 11:
            return Path(arg), major
    cands = []
    jre_root = tools_dir() / "jre"
    if jre_root.is_dir():
        for p in jre_root.iterdir():
            if p.is_dir():
                cands += [p, p / "Contents" / "Home"]
    if os.environ.get("JAVA_HOME"):
        cands.append(Path(os.environ["JAVA_HOME"]))
    if os.name == "nt":
        pf = [Path(os.environ.get("ProgramFiles", r"C:\Program Files")),
              Path(os.environ.get("ProgramFiles(x86)", r"C:\Program Files (x86)"))]
        cands += [p / "Android" / "Android Studio" / "jbr" for p in pf]
        for base in pf:
            for vendor in ("Java", "Eclipse Adoptium", "Zulu", "Microsoft", "BellSoft", "Amazon Corretto"):
                try:
                    cands += sorted((base / vendor).iterdir(), reverse=True)
                except OSError:
                    pass
    else:
        cands += [Path("/opt/android-studio/jbr"), Path.home() / "android-studio" / "jbr",
                  Path("/Applications/Android Studio.app/Contents/jbr/Contents/Home")]
        try:
            cands += sorted(Path("/usr/lib/jvm").iterdir(), reverse=True)
        except OSError:
            pass
    for tool in ("javac", "java"):
        found = shutil.which(tool)
        if found:
            cands.append(Path(os.path.realpath(found)).parent.parent)
    best, best_major, seen = None, 0, set()
    for c in cands:
        key = os.path.normcase(str(c))
        if key in seen:
            continue
        seen.add(key)
        major = _java_major(c, need_javac)
        if major >= 11 and major > best_major:
            best, best_major = c, major
    return (best, best_major) if best else None


def _sha256(path: Path) -> str:
    """Line-ending independent: a git checkout with CRLF must still match the shipped classes.dex."""
    return hashlib.sha256(path.read_bytes().replace(b"\r\n", b"\n")).hexdigest()


def prebuilt_dex_ok() -> bool:
    try:
        return PREBUILT_DEX.is_file() and PREBUILT_SHA.read_text(encoding="utf-8").strip() == _sha256(JAVA_SRC)
    except OSError:
        return False


def debug_keystore() -> Path:
    return Path.home() / ".android" / "debug.keystore"


def _no_window() -> dict:
    """No flashing console windows for child processes when running under pythonw."""
    if os.name == "nt":
        return {"creationflags": 0x08000000}          # CREATE_NO_WINDOW
    return {}


# ====================================================================== tool status

@dataclass
class Component:
    key: str
    name: str
    required: bool
    ok: bool
    detail: str
    download: str | None = None       # what would be downloaded (with size), None = cannot download


@dataclass
class Toolchain:
    sdk: Path | None = None
    build_tools: Path | None = None
    java: Path | None = None
    javac: bool = False
    android_jar: Path | None = None
    api: int = 35
    adb: Path | None = None
    need_javac: bool = False
    components: list[Component] = field(default_factory=list)

    def component(self, key: str) -> Component | None:
        return next((c for c in self.components if c.key == key), None)

    @property
    def missing_required(self) -> list[Component]:
        return [c for c in self.components if c.required and not c.ok]

    @property
    def ready(self) -> bool:
        return not self.missing_required

    # -- running tools
    def exe(self, name: str) -> str:
        return str(self.build_tools / (name + EXE))

    def jbin(self, name: str) -> str:
        return str(self.java / "bin" / (name + EXE))

    def java_env(self):
        env = dict(os.environ)
        env["JAVA_HOME"] = str(self.java)
        env["PATH"] = str(self.java / "bin") + os.pathsep + env.get("PATH", "")
        return env

    def run(self, cmd, what):
        r = subprocess.run([str(c) for c in cmd], capture_output=True, text=True, encoding="utf-8",
                           errors="replace", env=self.java_env(), **_no_window())
        out = (r.stdout or "") + (r.stderr or "")
        if r.returncode:
            raise BuildError(tr(f"{what} nie powiódł się (kod {r.returncode}):\n{out.strip()[-4000:]}",
                                f"{what} failed (code {r.returncode}):\n{out.strip()[-4000:]}"))
        return out

    def java_tool(self, jar: str, main: str | None, args, what):
        cmd = [self.jbin("java"), "-Xmx1536m"]
        cmd += ["-cp", str(self.build_tools / "lib" / jar), main] if main else \
            ["-jar", str(self.build_tools / "lib" / jar)]
        return self.run(cmd + list(args), what)


def check_tools(sdk_arg: str | None = None, java_arg: str | None = None, rebuild_dex: bool = False) -> Toolchain:
    """Look for everything, download nothing. Fast enough for a status panel (~1 s)."""
    tc = Toolchain()
    dex_ok = prebuilt_dex_ok() and not rebuild_dex
    tc.need_javac = not dex_ok
    comps = tc.components

    comps.append(Component("python", "Python", True, sys.version_info >= (3, 10),
                           f"{sys.version.split()[0]}  ({sys.executable})"))
    try:
        import PIL  # noqa: F401
        pil = True
    except Exception:
        pil = False
    comps.append(Component("pillow", "Pillow", False, pil,
                           tr("skalowanie ikony gry", "scales the game icon") if pil else
                           tr("brak - ikona gry trafi do APK bez skalowania (to nie problem)",
                              "missing - the game icon goes into the APK unscaled (not a problem)")))

    java = find_java(java_arg, tc.need_javac)
    if java:
        tc.java = java[0]
        tc.javac = (tc.java / "bin" / f"javac{EXE}").is_file()
        comps.append(Component("java", "Java", True, True, f"{java[1]}  ({tc.java})"))
    elif tc.need_javac:
        comps.append(Component("java", "Java (JDK)", True, False,
                               tr("potrzebny JDK 11+ z javac (MainActivity.java zmieniony)",
                                  "JDK 11+ with javac needed (MainActivity.java was changed)")))
    else:
        comps.append(Component("java", "Java", True, False, tr("brak Javy 11+", "no Java 11+ found"),
                               f"Java {JRE_FEATURE} JRE (Eclipse Temurin), ~45 MB"))

    bt = find_build_tools(sdk_arg, tc.need_javac)
    if bt:
        tc.sdk, tc.build_tools = bt
        comps.append(Component("build_tools", "Android build-tools", True, True, str(tc.build_tools)))
    else:
        comps.append(Component("build_tools", "Android build-tools", True, False,
                               tr("brak (aapt2, zipalign, apksigner)", "missing (aapt2, zipalign, apksigner)"),
                               "Android build-tools 36 (Google), ~60 MB"))

    jar = find_android_jar(sdk_arg)
    if jar:
        tc.android_jar, tc.api = jar
        comps.append(Component("android_jar", "android.jar", True, True, f"API {tc.api}  ({tc.android_jar})"))
    else:
        comps.append(Component("android_jar", "android.jar", True, False, tr("brak", "missing"),
                               "Android SDK Platform 35 (Google), ~60 MB"))

    if dex_ok:
        comps.append(Component("dex", "classes.dex", True, True, tr("dołączony do programu", "shipped prebuilt")))
    else:
        can = tc.javac and bool(bt) and _build_tools_ok(tc.build_tools, True)
        comps.append(Component("dex", "classes.dex", True, can,
                               tr("zostanie skompilowany (javac + d8)", "will be compiled (javac + d8)") if can else
                               tr("wymaga JDK z javac i d8", "needs a JDK with javac and d8")))

    ks = debug_keystore()
    comps.append(Component("keystore", tr("Klucz podpisu", "Signing key"), False, True,
                           str(ks) if ks.is_file() else
                           tr(f"zostanie utworzony: {ks}", f"will be created: {ks}")))

    tc.adb = find_adb(sdk_arg)
    comps.append(Component("adb", "adb", False, bool(tc.adb),
                           str(tc.adb) if tc.adb else
                           tr("brak - potrzebny tylko do instalacji przez USB i zapisów",
                              "missing - only needed for USB install and saves"),
                           None if tc.adb else "Android platform-tools (Google), ~7 MB"))
    return tc


def download_components(keys: list[str], rep: Reporter) -> None:
    """Download the given component keys (see Component.download)."""
    order = [("build_tools", download_build_tools), ("android_jar", download_android_jar),
             ("java", download_jre), ("adb", download_platform_tools)]
    for key, fn in order:
        if key in keys:
            rep.log(tr(f"Pobieram: {key}", f"Downloading: {key}"), "head")
            fn(rep)


# ====================================================================== build

@dataclass
class BuildOptions:
    game: Game
    out: Path
    package: str
    label: str
    orientation: str = "landscape"
    keystore: Path | None = None
    ks_pass: str = "android"
    ks_alias: str = "androiddebugkey"
    key_pass: str | None = None
    rebuild_dex: bool = False


@dataclass
class BuildResult:
    apk: Path
    size: int
    files: int
    game_bytes: int
    skipped: int


def compile_dex(tc: Toolchain, work: Path, rep: Reporter, rebuild: bool = False) -> Path:
    if not rebuild and prebuilt_dex_ok():
        rep.log(tr("classes.dex (dołączony)", "classes.dex (prebuilt)"), "ok")
        return PREBUILT_DEX
    if not tc.javac:
        raise BuildError(tr("Potrzebna kompilacja MainActivity.java, a znaleziona Java nie ma javac (to JRE).",
                            "MainActivity.java must be compiled, but the Java found has no javac (it is a JRE)."))
    if not _build_tools_ok(tc.build_tools, True):
        raise BuildError(f"{tc.build_tools}: lib/d8.jar missing")
    classes = work / "classes"
    classes.mkdir(parents=True, exist_ok=True)
    tc.run([tc.jbin("javac"), "-source", "8", "-target", "8", "-Xlint:-options", "-nowarn",
            "-encoding", "UTF-8", "-bootclasspath", tc.android_jar, "-d", classes, JAVA_SRC], "javac")
    class_files = sorted(str(p) for p in classes.rglob("*.class"))
    if not class_files:
        raise BuildError("javac: no .class files")
    dex_out = work / "dex"
    dex_out.mkdir(exist_ok=True)
    tc.java_tool("d8.jar", "com.android.tools.r8.D8",
                 ["--release", "--min-api", str(MIN_SDK), "--lib", str(tc.android_jar),
                  "--output", str(dex_out), *class_files], "d8")
    dex = dex_out / "classes.dex"
    if not dex.is_file():
        raise BuildError("d8: no classes.dex")
    rep.log(tr(f"classes.dex skompilowany ({dex.stat().st_size} B)", f"classes.dex compiled ({dex.stat().st_size} B)"),
            "ok")
    try:                                  # refresh the shipped copy -> the package works without a JDK
        shutil.copy2(dex, PREBUILT_DEX)
        PREBUILT_SHA.write_text(_sha256(JAVA_SRC) + "\n", encoding="utf-8")
        return PREBUILT_DEX
    except OSError:
        return dex


def manifest_xml(package: str, label: str, has_icon: bool, orientation: str) -> str:
    icon = 'android:icon="@mipmap/ic_launcher" android:roundIcon="@mipmap/ic_launcher" ' if has_icon else ""
    return f'''<?xml version="1.0" encoding="utf-8"?>
<manifest xmlns:android="http://schemas.android.com/apk/res/android"
    package="{package}" android:installLocation="auto">
    <uses-feature android:glEsVersion="0x00020000" android:required="true"/>
    <uses-feature android:name="android.hardware.touchscreen" android:required="false"/>
    <uses-feature android:name="android.hardware.gamepad" android:required="false"/>
    <application android:label="{xml_escape(label, {'"': "&quot;"})}" {icon}
        android:allowBackup="false" android:hardwareAccelerated="true" android:largeHeap="true"
        android:isGame="true" android:appCategory="game" android:resizeableActivity="true"
        android:theme="@android:style/Theme.Material.NoActionBar.Fullscreen">
        <activity android:name="{ACTIVITY}" android:exported="true" android:launchMode="singleTask"
            android:screenOrientation="{ORIENTATIONS[orientation]}"
            android:configChanges="orientation|screenSize|smallestScreenSize|screenLayout|keyboard|keyboardHidden|navigation|uiMode|density|fontScale|layoutDirection|locale">
            <intent-filter>
                <action android:name="android.intent.action.MAIN"/>
                <category android:name="android.intent.category.LAUNCHER"/>
            </intent-filter>
        </activity>
    </application>
</manifest>
'''


def prepare_icon(src: Path | None, res: Path, rep: Reporter) -> bool:
    if not src:
        rep.log(tr("Gra nie ma ikony (icon/icon.png) - APK dostanie domyślną.",
                   "The game has no icon (icon/icon.png) - the APK gets the default one."), "warn")
        return False
    dst = res / "mipmap-xxxhdpi" / "ic_launcher.png"
    dst.parent.mkdir(parents=True, exist_ok=True)
    try:
        from PIL import Image
        with Image.open(src) as im:
            im.convert("RGBA").resize((192, 192), Image.LANCZOS).save(dst, "PNG")
        return True
    except ImportError:
        if src.read_bytes()[:8] == b"\x89PNG\r\n\x1a\n":
            shutil.copy2(src, dst)
            return True
    except Exception as e:
        rep.log(tr(f"Nie udało się przygotować ikony ({e}) - pomijam.", f"Could not prepare the icon ({e}) - skipped."),
                "warn")
        return False
    rep.log(tr("Ikona nie jest PNG, a brak Pillow do konwersji - pomijam.",
               "The icon is not a PNG and Pillow is missing - skipped."), "warn")
    return False


def link_base_apk(tc: Toolchain, work: Path, opts: BuildOptions, version_code: int, version_name: str,
                  rep: Reporter) -> Path:
    res = work / "res"
    has_icon = prepare_icon(opts.game.icon, res, rep)
    manifest = work / "AndroidManifest.xml"
    manifest.write_text(manifest_xml(opts.package, opts.label, has_icon, opts.orientation), encoding="utf-8")
    cmd = [tc.exe("aapt2"), "link", "-o", work / "base.apk", "-I", tc.android_jar, "--manifest", manifest,
           "--min-sdk-version", str(MIN_SDK), "--target-sdk-version", str(tc.api),
           "--version-code", str(version_code), "--version-name", version_name]
    if has_icon:
        flat = work / "res.zip"
        tc.run([tc.exe("aapt2"), "compile", "--dir", res, "-o", flat], "aapt2 compile")
        cmd.append(flat)
    tc.run(cmd, "aapt2 link")
    return work / "base.apk"


def patch_plugins_js(raw: bytes) -> bytes:
    """Put UT_AndroidBridge FIRST in $plugins, so every other plugin already sees it."""
    bom = raw.startswith(b"\xef\xbb\xbf")
    text = raw.decode("utf-8-sig")
    if re.search(r'"name"\s*:\s*"' + BRIDGE_NAME + '"', text):
        return raw
    entry = '\n{"name":"%s","status":true,"description":"Android bridge (rpgm-to-apk)","parameters":{}},' % BRIDGE_NAME
    new, n = re.subn(r"(\$plugins\s*=\s*\[)", lambda m: m.group(1) + entry, text, count=1)
    if n != 1:
        raise BuildError(tr("Nie znalazłem '$plugins = [' w js/plugins.js - nie da się dodać wtyczki Androida.",
                            "'$plugins = [' not found in js/plugins.js - cannot add the Android plugin."))
    data = new.encode("utf-8")
    return (b"\xef\xbb\xbf" + data) if bom else data


VIEWPORT = ('<meta name="viewport" content="width=device-width, initial-scale=1, minimum-scale=1, '
            'maximum-scale=1, user-scalable=no, viewport-fit=cover">')
# The page must not stick out of the screen (it could be dragged with a finger). Game scaling
# is left to RPG Maker - it fits the canvas keeping the aspect ratio (black bars at the sides).
LAYOUT_CSS = ('<style id="ut-android-layout">'
              'html,body{margin:0!important;padding:0!important;width:100%;height:100%;'
              'overflow:hidden!important;background:#000;overscroll-behavior:none;touch-action:none}'
              'body{position:fixed;left:0;top:0;right:0;bottom:0}'
              # e.g. Look Outside: <canvas id="refresher" style="width:100%;height:100%"> in the normal
              # page flow + the default body margin = page taller than the screen
              '#refresher{position:fixed!important;left:0;top:0;width:100%!important;height:100%!important;'
              'pointer-events:none}'
              '</style>')


def patch_index_html(raw: bytes) -> bytes:
    bom = raw.startswith(b"\xef\xbb\xbf")
    text = raw.decode("utf-8-sig", errors="replace")
    if 'id="ut-android-layout"' in text:
        return raw
    text, n = re.subn(r"<meta\s+name\s*=\s*[\"']viewport[\"'][^>]*>", lambda m: VIEWPORT, text, count=1, flags=re.I)
    inject = ("" if n else VIEWPORT) + LAYOUT_CSS
    text, n = re.subn(r"(<head[^>]*>)", lambda m: m.group(1) + "\n" + inject, text, count=1, flags=re.I)
    if not n:
        text = inject + text
    data = text.encode("utf-8")
    return (b"\xef\xbb\xbf" + data) if bom else data


def _skip(rel: str, is_dir: bool, root_level: bool, path: Path) -> bool:
    low = rel.rsplit("/", 1)[-1].lower()
    if low in SKIP_NAMES:
        return True
    if is_dir:
        if root_level and low in SKIP_ROOT_DIRS:
            return True
        if root_level and low == "lib":                  # greenworks: native Steam libraries
            try:
                return any(p.suffix.lower() in {".node", ".dll", ".so", ".dylib"} for p in path.iterdir())
            except OSError:
                return False
        return False
    if os.path.splitext(low)[1] in SKIP_EXT:
        return True
    return root_level and (low in SKIP_ROOT_FILES or low.endswith(".bin"))


def collect_files(game: Game) -> tuple[list[tuple[Path, str]], int, int]:
    """[(file, path relative to the web root)], number of skipped entries, total bytes."""
    files, skipped, size = [], 0, 0
    web = game.web
    for dp, dns, fns in os.walk(web):
        d = Path(dp)
        rel_dir = d.relative_to(web).as_posix()
        rel_dir = "" if rel_dir == "." else rel_dir
        root_level = rel_dir == ""
        keep = []
        for dn in sorted(dns):
            if _skip(f"{rel_dir}/{dn}" if rel_dir else dn, True, root_level, d / dn):
                skipped += 1
            else:
                keep.append(dn)
        dns[:] = keep
        for fn in sorted(fns):
            rel = f"{rel_dir}/{fn}" if rel_dir else fn
            if _skip(rel, False, root_level, d / fn):
                skipped += 1
                continue
            p = d / fn
            files.append((p, rel))
            size += p.stat().st_size
    return files, skipped, size


def assemble_apk(game: Game, base: Path, dex: Path, unsigned: Path, rep: Reporter) -> dict:
    files, skipped, total = collect_files(game)
    rels = {r for _, r in files}
    for need in ("index.html", "js/plugins.js", "data/System.json"):
        if need not in rels:
            raise BuildError(tr(f"W grze brakuje {need}", f"The game is missing {need}"))
    if total > MAX_APK_BYTES:
        raise BuildError(tr(f"Gra ma {total / 1e9:.1f} GB - APK powyżej ~4 GB wymagałoby ZIP64, którego Android "
                            "nie obsługuje przy instalacji.",
                            f"The game is {total / 1e9:.1f} GB - an APK above ~4 GB would need ZIP64, which "
                            "Android cannot install."))
    rep.log(tr(f"{len(files)} plików, {human_size(total)} (pominięto {skipped} pozycji PC/NW.js)",
               f"{len(files)} files, {human_size(total)} (skipped {skipped} PC/NW.js entries)"))
    extra = {
        "index.html": patch_index_html((game.web / "index.html").read_bytes()),
        "js/plugins.js": patch_plugins_js((game.web / "js/plugins.js").read_bytes()),
        f"js/plugins/{BRIDGE_NAME}.js": BRIDGE_JS.read_bytes(),
    }
    index_lines = []
    shutil.copy2(base, unsigned)
    label = tr("Pakowanie plików gry", "Packing game files")
    with zipfile.ZipFile(unsigned, "a", compression=zipfile.ZIP_DEFLATED, compresslevel=6) as z:
        z.write(dex, "classes.dex", compress_type=zipfile.ZIP_DEFLATED)
        done = 0
        for i, (src, rel) in enumerate(files, 1):
            if rel in extra:
                continue
            ctype = zipfile.ZIP_STORED if os.path.splitext(rel)[1].lower() in STORE_EXT else zipfile.ZIP_DEFLATED
            z.write(src, "assets/www/" + rel, compress_type=ctype)
            size = src.stat().st_size
            index_lines.append(f"{rel}\t{size}")
            done += size
            if i % 100 == 0 or i == len(files):
                rep.progress(label, done, total)
        for rel, data in extra.items():
            z.writestr("assets/www/" + rel, data, compress_type=zipfile.ZIP_DEFLATED)
            index_lines.append(f"{rel}\t{len(data)}")
        z.writestr("assets/ut_files.txt", "\n".join(sorted(index_lines)) + "\n", compress_type=zipfile.ZIP_DEFLATED)
    seen: dict[str, list[str]] = {}
    for line in index_lines:
        rel = line.rsplit("\t", 1)[0]
        seen.setdefault(rel.lower(), []).append(rel)
    clashes = [v for v in seen.values() if len(v) > 1]
    if clashes:
        rep.log(tr(f"{len(clashes)} plików różni się tylko wielkością liter, np. {clashes[0]}",
                   f"{len(clashes)} files differ only in letter case, e.g. {clashes[0]}"), "warn")
    return {"files": len(files), "bytes": total, "skipped": skipped}


def ensure_keystore(tc: Toolchain, opts: BuildOptions, rep: Reporter) -> tuple[Path, str, str, str]:
    if opts.keystore:
        if not opts.keystore.is_file():
            raise BuildError(tr(f"Nie ma keystore: {opts.keystore}", f"Keystore not found: {opts.keystore}"))
        return opts.keystore, opts.ks_pass, opts.ks_alias, opts.key_pass or opts.ks_pass
    ks = debug_keystore()
    if not ks.is_file():
        ks.parent.mkdir(parents=True, exist_ok=True)
        tc.run([tc.jbin("keytool"), "-genkeypair", "-keystore", ks, "-storepass", "android",
                "-keypass", "android", "-alias", "androiddebugkey", "-keyalg", "RSA", "-keysize", "2048",
                "-validity", "10000", "-dname", "CN=Android Debug,O=Android,C=US"], "keytool")
        rep.log(tr(f"utworzono klucz podpisu: {ks}", f"signing key created: {ks}"), "ok")
    return ks, "android", "androiddebugkey", "android"


def sign_apk(tc: Toolchain, opts: BuildOptions, unsigned: Path, final: Path, rep: Reporter):
    aligned = final.with_name(final.name + ".aligned.tmp")
    signed = final.with_name(final.name + ".signed.tmp")
    try:
        tc.run([tc.exe("zipalign"), "-f", "-p", "4", unsigned, aligned], "zipalign")
        ks, ks_pass, alias, key_pass = ensure_keystore(tc, opts, rep)
        tc.java_tool("apksigner.jar", None,
                     ["sign", "--ks", str(ks), "--ks-pass", f"pass:{ks_pass}", "--key-pass", f"pass:{key_pass}",
                      "--ks-key-alias", alias, "--min-sdk-version", str(MIN_SDK), "--out", str(signed),
                      str(aligned)], "apksigner sign")
        out = tc.java_tool("apksigner.jar", None, ["verify", "-v", "--min-sdk-version", str(MIN_SDK), str(signed)],
                           "apksigner verify")
        if not re.search(r"Verified using v2 scheme.*: true", out):
            raise BuildError("apksigner verify: no v2 signature\n" + out)
        tc.run([tc.exe("zipalign"), "-c", "-p", "4", signed], "zipalign -c")
        os.replace(signed, final)
    finally:
        for leftover in (aligned, signed, Path(str(signed) + ".idsig"), Path(str(final) + ".idsig")):
            leftover.unlink(missing_ok=True)
    rep.log(tr("podpis v2 i wyrównanie zweryfikowane", "v2 signature and alignment verified"), "ok")


def build_apk(opts: BuildOptions, tc: Toolchain, rep: Reporter) -> BuildResult:
    if not tc.ready:
        names = ", ".join(c.name for c in tc.missing_required)
        raise BuildError(tr(f"Brakuje narzędzi: {names}", f"Missing tools: {names}"))
    if not valid_package(opts.package):
        raise BuildError(tr(f"Nieprawidłowa nazwa pakietu: {opts.package}", f"Invalid package name: {opts.package}"))
    if opts.orientation not in ORIENTATIONS:
        raise BuildError(f"orientation: {opts.orientation}")
    out = opts.out
    out.parent.mkdir(parents=True, exist_ok=True)
    unsigned = out.with_name(out.name + ".unsigned.tmp")
    version_code = int(time.time() // 60)          # grows with every build -> installs as an update
    version_name = time.strftime("%Y.%m.%d.%H%M")
    try:
        with tempfile.TemporaryDirectory(prefix="rpgm_apk_") as tmp:
            work = Path(tmp)
            dex = compile_dex(tc, work, rep, opts.rebuild_dex)
            base = link_base_apk(tc, work, opts, version_code, version_name, rep)
            rep.log(tr("manifest + ikona (aapt2)", "manifest + icon (aapt2)"), "ok")
            stats = assemble_apk(opts.game, base, dex, unsigned, rep)
        sign_apk(tc, opts, unsigned, out, rep)
    finally:
        unsigned.unlink(missing_ok=True)
    return BuildResult(out, out.stat().st_size, stats["files"], stats["bytes"], stats["skipped"])


# ====================================================================== adb

@dataclass
class Device:
    serial: str
    state: str
    model: str = ""

    @property
    def ready(self) -> bool:
        return self.state == "device"

    def __str__(self):
        return f"{self.model or self.serial} ({self.serial})" + ("" if self.ready else f" - {self.state}")


def remote_save_dir(package: str) -> str:
    return f"/sdcard/Android/data/{package}/files/save"


def adb_run(adb: Path, serial: str | None, args, check=True, timeout=None):
    cmd = [str(adb)] + (["-s", serial] if serial else []) + [str(a) for a in args]
    try:
        r = subprocess.run(cmd, capture_output=True, text=True, encoding="utf-8", errors="replace",
                           timeout=timeout, **_no_window())
    except subprocess.TimeoutExpired as e:
        raise BuildError(f"adb {' '.join(map(str, args))}: timeout") from e
    if check and r.returncode:
        raise BuildError(f"adb {' '.join(map(str, args))} ({r.returncode}):\n{r.stdout}{r.stderr}")
    return r


def list_devices(adb: Path) -> list[Device]:
    r = adb_run(adb, None, ["devices", "-l"], check=False, timeout=30)
    devices = []
    for line in (r.stdout or "").splitlines()[1:]:
        parts = line.split()
        if len(parts) < 2:
            continue
        model = next((p.split(":", 1)[1].replace("_", " ") for p in parts[2:] if p.startswith("model:")), "")
        devices.append(Device(parts[0], parts[1], model))
    return devices


def device_hint(dev: Device) -> str:
    if dev.state == "unauthorized":
        return tr("Odblokuj telefon i zatwierdź „Zezwolić na debugowanie USB?”.",
                  "Unlock the phone and accept \"Allow USB debugging?\".")
    if dev.state == "offline":
        return tr("Urządzenie offline - odłącz i podłącz kabel.", "Device offline - unplug and replug the cable.")
    return ""


def install_apk(adb: Path, serial: str, apk: Path, package: str, rep: Reporter):
    rep.log(tr("Instaluję na telefonie (duże APK może potrwać kilka minut)...",
               "Installing on the phone (a large APK may take a few minutes)..."))
    r = adb_run(adb, serial, ["install", "-r", str(apk)], check=False)
    out = (r.stdout or "") + (r.stderr or "")
    if r.returncode or "Success" not in out:
        if "INSTALL_FAILED_UPDATE_INCOMPATIBLE" in out:
            raise BuildError(tr(
                f"Na telefonie jest {package} podpisany innym kluczem (zbudowany na innym komputerze). "
                f"Odinstaluj go ręcznie - UWAGA: to skasuje zapisy z {remote_save_dir(package)}, "
                "najpierw je pobierz.",
                f"The phone has {package} signed with a different key (built on another computer). "
                f"Uninstall it manually - WARNING: this deletes the saves in {remote_save_dir(package)}, "
                "copy them first."))
        if "INSUFFICIENT_STORAGE" in out:
            raise BuildError(tr("Za mało miejsca na telefonie.", "Not enough storage on the phone."))
        raise BuildError("adb install:\n" + out)
    rep.log(tr("zainstalowano", "installed"), "ok")


def _ensure_app_dirs(adb: Path, serial: str, package: str):
    """The first app start creates Android/data/<pkg>/files with the right owner."""
    adb_run(adb, serial, ["shell", "am", "start", "-n", f"{package}/{ACTIVITY}"], check=False)
    time.sleep(4)
    adb_run(adb, serial, ["shell", "am", "force-stop", package], check=False)
    adb_run(adb, serial, ["shell", "mkdir", "-p", remote_save_dir(package)], check=False)


def pc_saves(game: Game) -> list[Path]:
    try:
        return [p for p in sorted(game.save_dir.iterdir())
                if p.is_file() and p.suffix.lower() in (".rmmzsave", ".rpgsave")]
    except OSError:
        return []


def push_saves(adb: Path, serial: str, game: Game, package: str, rep: Reporter) -> int:
    saves = pc_saves(game)
    if not saves:
        rep.log(tr(f"Brak zapisów w {game.save_dir}", f"No saves in {game.save_dir}"), "warn")
        return 0
    _ensure_app_dirs(adb, serial, package)
    remote = remote_save_dir(package)
    for p in saves:
        adb_run(adb, serial, ["push", str(p), f"{remote}/{p.name}"])
        size = adb_run(adb, serial, ["shell", "stat", "-c", "%s", f"{remote}/{p.name}"], check=False).stdout.strip()
        if size != str(p.stat().st_size):
            raise BuildError(tr(f"Niekompletne przesłanie {p.name}", f"Incomplete transfer of {p.name}"))
    rep.log(tr(f"przesłano {len(saves)} zapisów -> {remote}", f"copied {len(saves)} saves -> {remote}"), "ok")
    return len(saves)


def pull_saves(adb: Path, serial: str, package: str, dest: Path, rep: Reporter) -> int:
    remote = remote_save_dir(package)
    listing = adb_run(adb, serial, ["shell", "ls", remote], check=False)
    names = [n for n in (listing.stdout or "").split() if n.endswith((".rmmzsave", ".rpgsave"))]
    if not names:
        raise BuildError(tr(f"Brak zapisów w {remote}", f"No saves in {remote}"))
    dest.mkdir(parents=True, exist_ok=True)
    for n in names:
        adb_run(adb, serial, ["pull", f"{remote}/{n}", str(dest / n)])
    rep.log(tr(f"pobrano {len(names)} zapisów -> {dest}", f"copied {len(names)} saves -> {dest}"), "ok")
    return len(names)
