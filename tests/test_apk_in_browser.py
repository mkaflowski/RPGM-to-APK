#!/usr/bin/env python3
"""Test zbudowanego APK w Chromium (Playwright) - bez telefonu.

Serwuje assets/www/ PROSTO Z PLIKU APK tak, jak robi to MainActivity
(ta sama normalizacja ścieżek, dopasowanie bez wielkości liter z ut_files.txt,
404 dla braków), a window.UTAndroid emuluje synchronicznym XHR do tego serwera
z tą samą semantyką co klasa Bridge w Javie (zapis do katalogu tymczasowego,
odczyt z fallbackiem na assety).

Sprawdza: start gry do pierwszej sceny po Scene_Boot, brak błędów JS i 404,
wczytanie config.rmmzsave z PC, zapis/odczyt StorageManager w formacie PC,
DataManager.saveGame/loadGame, require() (os / greenworks), Back = Esc.

NIE sprawdza: kodu Javy (WebView, AssetManager, prawdziwy most JS<->Java),
wydajności, dźwięku ani dotyku na telefonie.

  python test_apk_in_browser.py "output/Look Outside.apk" --game "F:\\DepotDownloaderMod\\Look Outside"
"""
from __future__ import annotations

import argparse
import base64
import json
import mimetypes
import shutil
import sys
import tempfile
import threading
import time
import zipfile
import zlib
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import unquote, urlparse

for _stream in (sys.stdout, sys.stderr):
    try:
        _stream.reconfigure(encoding="utf-8", errors="replace")
    except Exception:
        pass

HOST_ORIGIN = "https://appassets.androidplatform.net/"
ANDROID_UA = ("Mozilla/5.0 (Linux; Android 14; Pixel 7 Build/UQ1A; wv) AppleWebKit/537.36 "
              "(KHTML, like Gecko) Version/4.0 Chrome/130.0.0.0 Mobile Safari/537.36")


def norm(p):
    """Kopia MainActivity.norm()."""
    if p is None:
        return None
    p = p.replace("\\", "/")
    if p.startswith(HOST_ORIGIN):
        p = p[len(HOST_ORIGIN):]
    out = []
    for seg in p.split("/"):
        if seg in ("", "."):
            continue
        if seg == "..":
            if not out:
                return None
            out.pop()
            continue
        out.append(seg)
    return "/".join(out)


class Apk:
    def __init__(self, path: Path):
        self.zip = zipfile.ZipFile(path)
        self.lock = threading.Lock()
        self.files, self.lower, self.children = {}, {}, {}
        for line in self.zip.read("assets/ut_files.txt").decode("utf-8").splitlines():
            if not line:
                continue
            p, _, size = line.rpartition("\t")
            self.files[p] = int(size)
            self.lower.setdefault(p.lower(), p)
            child = p
            while True:
                d, _, name = child.rpartition("/")
                known = d in self.children
                self.children.setdefault(d, set()).add(name)
                self.lower.setdefault(d.lower(), d)
                if known or not d:
                    break
                child = d

    def asset_file(self, p):
        if p is None:
            return None
        if p in self.files:
            return p
        r = self.lower.get(p.lower())
        return r if r in self.files else None

    def asset_dir(self, p):
        if p is None:
            return None
        if p in self.children:
            return p
        r = self.lower.get(p.lower())
        return r if r in self.children else None

    def read(self, rel):
        with self.lock:
            return self.zip.read("assets/www/" + rel)


class Bridge:
    """Kopia semantyki MainActivity.Bridge (katalog zapisu = root)."""

    def __init__(self, apk: Apk, root: Path):
        self.apk, self.root = apk, root

    def f(self, p):
        return self.root if p == "" else self.root / p

    def read_bytes(self, raw):
        p = norm(raw)
        if p is None:
            return None
        f = self.f(p)
        if f.is_file():
            return f.read_bytes()
        a = self.apk.asset_file(p)
        return self.apk.read(a) if a else None

    def write_bytes(self, raw, data):
        p = norm(raw)
        if not p:
            return False
        f = self.f(p)
        f.parent.mkdir(parents=True, exist_ok=True)
        tmp = f.with_name(f.name + ".uttmp")
        tmp.write_bytes(data)
        tmp.replace(f)
        return True

    def exists(self, raw):
        p = norm(raw)
        return p is not None and (p == "" or self.f(p).exists() or self.apk.asset_file(p) is not None
                                  or self.apk.asset_dir(p) is not None)

    def isDir(self, raw):
        p = norm(raw)
        return p is not None and (p == "" or self.f(p).is_dir() or self.apk.asset_dir(p) is not None)

    def readText(self, raw):
        b = self.read_bytes(raw)
        return None if b is None else b.decode("utf-8", errors="replace")

    def readBase64(self, raw):
        b = self.read_bytes(raw)
        return None if b is None else base64.b64encode(b).decode()

    def writeText(self, raw, data):
        return self.write_bytes(raw, (data or "").encode("utf-8"))

    def writeBase64(self, raw, b64):
        return self.write_bytes(raw, base64.b64decode(b64 or ""))

    def mkdir(self, raw, recursive):
        p = norm(raw)
        if p is None:
            return False
        f = self.f(p)
        if f.is_dir():
            return True
        try:
            f.mkdir(parents=bool(recursive))
            return True
        except OSError:
            return False

    def unlink(self, raw):
        p = norm(raw)
        if not p or not self.f(p).is_file():
            return False
        self.f(p).unlink()
        return True

    def rmdir(self, raw):
        p = norm(raw)
        if not p or not self.f(p).is_dir():
            return False
        try:
            self.f(p).rmdir()
            return True
        except OSError:
            return False

    def rename(self, a, b):
        a, b = norm(a), norm(b)
        if not a or not b:
            return False
        fa, fb = self.f(a), self.f(b)
        if not fa.exists():
            data = self.read_bytes(a)
            return data is not None and self.write_bytes(b, data)
        fb.parent.mkdir(parents=True, exist_ok=True)
        fa.replace(fb)
        return True

    def readdir(self, raw):
        p = norm(raw)
        if p is None:
            return None
        names, found = set(), False
        if self.f(p).is_dir():
            found = True
            names |= {n.name for n in self.f(p).iterdir() if not n.name.endswith(".uttmp")}
        d = self.apk.asset_dir(p)
        if d is not None:
            found = True
            names |= self.apk.children[d]
        return json.dumps(sorted(names)) if found else None

    def stat(self, raw):
        p = norm(raw)
        if p is None:
            return None
        f = self.f(p)
        if f.exists():
            return json.dumps({"dir": f.is_dir(), "size": f.stat().st_size if f.is_file() else 0,
                               "mtime": int(f.stat().st_mtime * 1000)})
        a = self.apk.asset_file(p)
        if a:
            return json.dumps({"dir": False, "size": self.apk.files[a], "mtime": 0})
        if p == "" or self.apk.asset_dir(p) is not None:
            return json.dumps({"dir": True, "size": 0, "mtime": 0})
        return None

    def dataDir(self):
        return str(self.root)

    def missing(self):
        return json.dumps(sorted(set(self.log404))[:50])

    def openUrl(self, url):
        self.opened = url

    def exit(self):
        self.exited = True


BRIDGE_JS = """
(() => {
  const call = (m, args) => {
    const x = new XMLHttpRequest();
    x.open('POST', '/__ut/' + m, false);
    x.send(JSON.stringify(args));
    return JSON.parse(x.responseText).r;
  };
  const names = %s;
  const B = {};
  for (const n of names) B[n] = (...a) => call(n, a);
  window.UTAndroid = B;
})();
"""
BRIDGE_METHODS = ["exists", "isDir", "readText", "readBase64", "writeText", "writeBase64", "mkdir", "unlink",
                  "rmdir", "rename", "readdir", "stat", "dataDir", "missing", "openUrl", "exit"]

# IndexedDB, które nigdy nie odpowiada - tak wygląda zawieszone IndexedDB w WebView
HANG_INDEXEDDB_JS = """
(() => {
  const hang = () => { const r = {}; return new Proxy(r, {set(t, k, v) { t[k] = v; return true; }}); };
  try { indexedDB.open = hang; indexedDB.deleteDatabase = hang; } catch (e) {}
})();
"""


def make_handler(apk: Apk, bridge: Bridge, log404: list):
    class H(BaseHTTPRequestHandler):
        def log_message(self, *a):
            pass

        def do_POST(self):
            m = urlparse(self.path).path[len("/__ut/"):]
            args = json.loads(self.rfile.read(int(self.headers.get("Content-Length", 0))) or b"[]")
            r = getattr(bridge, m)(*args) if m in BRIDGE_METHODS else None
            body = json.dumps({"r": r}).encode()
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

        def do_GET(self):
            p = norm(unquote(urlparse(self.path).path)) or "index.html"
            real = apk.asset_file(p)
            if real is None:
                log404.append(p)
                self.send_response(404)
                self.send_header("Content-Length", "0")
                self.end_headers()
                return
            data = apk.read(real)
            mime = {".wasm": "application/wasm", ".js": "application/javascript", ".json": "application/json",
                    ".ogg": "audio/ogg", ".png_": "application/octet-stream"}.get(Path(real).suffix.lower()) \
                or mimetypes.guess_type(real)[0] or "application/octet-stream"
            self.send_response(200)
            self.send_header("Content-Type", mime)
            self.send_header("Content-Length", str(len(data)))
            self.end_headers()
            try:
                self.wfile.write(data)
            except (BrokenPipeError, ConnectionResetError):
                pass
    return H


def decode_pc_save(raw: bytes):
    """Plik .rmmzsave z NW.js: utf-8 zapis 'binary string' z pako.deflate."""
    return json.loads(zlib.decompress(raw.decode("utf-8").encode("latin-1")).decode("utf-8"))


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("apk")
    ap.add_argument("--game", help="folder gry na PC (do testu zapisów z PC w save/)")
    ap.add_argument("--timeout", type=int, default=120)
    ap.add_argument("--headed", action="store_true")
    ap.add_argument("--screenshot", default=None)
    ap.add_argument("--hang-indexeddb", action="store_true",
                    help="symuluj zawieszone IndexedDB (czarny ekran w MZ bez obejścia)")
    args = ap.parse_args()

    apk_path = Path(args.apk)
    apk = Apk(apk_path)
    root = Path(tempfile.mkdtemp(prefix="rpgm_apk_test_"))
    bridge = Bridge(apk, root)
    results, log404, console_err, page_err = [], [], [], []
    bridge.log404 = log404

    def check(name, cond, detail=""):
        results.append((name, bool(cond), detail))
        print(f"  {'OK ' if cond else 'FAIL'} {name}" + (f"  - {detail}" if detail else ""))

    pc_config = None
    if args.game:
        src = Path(args.game) / "save" / "config.rmmzsave"
        if src.is_file():
            (root / "save").mkdir(parents=True, exist_ok=True)
            shutil.copy2(src, root / "save" / "config.rmmzsave")
            pc_config = decode_pc_save(src.read_bytes())

    server = ThreadingHTTPServer(("127.0.0.1", 0), make_handler(apk, bridge, log404))
    threading.Thread(target=server.serve_forever, daemon=True).start()
    url = f"http://127.0.0.1:{server.server_address[1]}/index.html"

    from playwright.sync_api import sync_playwright
    with sync_playwright() as pw:
        browser = pw.chromium.launch(headless=not args.headed, args=[
            "--autoplay-policy=no-user-gesture-required", "--use-angle=swiftshader",
            "--enable-unsafe-swiftshader", "--ignore-gpu-blocklist"])
        ctx = browser.new_context(user_agent=ANDROID_UA, viewport={"width": 915, "height": 412},
                                  is_mobile=True, has_touch=True, device_scale_factor=2)
        page = ctx.new_page()
        page.on("console", lambda m: console_err.append(m.text) if m.type == "error" else None)
        page.on("pageerror", lambda e: page_err.append(str(e)))
        page.add_init_script(BRIDGE_JS % json.dumps(BRIDGE_METHODS))
        if args.hang_indexeddb:
            page.add_init_script(HANG_INDEXEDDB_JS)
        page.goto(url)

        t0, scene = time.time(), None
        while time.time() - t0 < args.timeout:
            scene = page.evaluate("""() => (typeof SceneManager !== 'undefined' && SceneManager._scene
                && SceneManager._scene.constructor.name !== 'Scene_Boot' && SceneManager._scene.isStarted
                && SceneManager._scene.isStarted()) ? SceneManager._scene.constructor.name : null""")
            err = page.evaluate("() => { const e = document.getElementById('errorPrinter');"
                                " return e ? e.innerText : null }")
            if scene or err:
                break
            time.sleep(0.5)
        check("gra wystartowała (scena po Scene_Boot)", scene, f"{scene} po {time.time() - t0:.0f} s")
        err = page.evaluate("() => { const e = document.getElementById('errorPrinter'); return e ? e.innerText : null }")
        check("brak ekranu błędu RPG Makera", not err, err or "")
        check("wtyczka Androida aktywna", page.evaluate("() => window.UT_ANDROID === true"))
        check("Utils.isNwjs() == false", page.evaluate("() => Utils.isNwjs() === false"))
        check("StorageManager.isLocalMode() == true", page.evaluate("() => StorageManager.isLocalMode()"))
        time.sleep(8)   # gra chwilę działa sama, zanim test zacznie ruszać jej stan
        shot = Path(args.screenshot) if args.screenshot else apk_path.with_name(apk_path.stem + "_test.png")
        page.screenshot(path=str(shot))
        print(f"  zrzut ekranu: {shot}")
        lay = page.evaluate("""() => {
            const se = document.scrollingElement || document.documentElement;
            window.scrollTo(200, 200);
            const c = document.getElementById('gameCanvas') || document.querySelector('canvas');
            const r = c ? c.getBoundingClientRect() : {width: 0, height: 0};
            return {iw: innerWidth, ih: innerHeight, sw: se.scrollWidth, sh: se.scrollHeight,
                    sx: scrollX, sy: scrollY, cw: Math.round(r.width), ch: Math.round(r.height),
                    cx: Math.round(r.left), cy: Math.round(r.top)};
        }""")
        check("strony nie da się przewinąć (nic nie wystaje poza ekran)",
              lay["sw"] <= lay["iw"] and lay["sh"] <= lay["ih"] and lay["sx"] == 0 and lay["sy"] == 0,
              json.dumps(lay))
        check("gra wypełnia ekran w jednym wymiarze (skalowanie z zachowaniem proporcji)",
              abs(lay["ch"] - lay["ih"]) <= 2 or abs(lay["cw"] - lay["iw"]) <= 2, json.dumps(lay))
        natural_err = list(console_err) + list(page_err)
        check("brak błędów JS podczas samodzielnego działania gry",
              not [e for e in natural_err if "greenworks" not in e.lower()],
              " | ".join(natural_err)[:600])

        if pc_config is not None:
            cfg = page.evaluate("() => ConfigManager.makeData()")
            diff = {k: (v, cfg.get(k)) for k, v in pc_config.items() if k in cfg and cfg.get(k) != v}
            check("config.rmmzsave z PC wczytany", not diff, json.dumps(diff, ensure_ascii=False)[:300])

        r = page.evaluate("""async () => {
            const obj = {text: 'Zażółć gęślą jaźń \\u0000 \\u00ff', n: [1, 2, 3], nested: {a: true}};
            await StorageManager.saveObject('ut_test', obj);
            const back = await StorageManager.loadObject('ut_test');
            return {same: JSON.stringify(back) === JSON.stringify(obj), exists: StorageManager.exists('ut_test')};
        }""")
        f = root / "save" / "ut_test.rmmzsave"
        pc_ok = False
        if f.is_file():
            try:
                pc_ok = decode_pc_save(f.read_bytes())["text"].startswith("Zażółć")
            except Exception as e:
                pc_ok = False
                print("    ", e)
        check("StorageManager save/load (round-trip)", r["same"] and r["exists"], json.dumps(r))
        check("plik zapisu w formacie NW.js (czytelny jak z PC)", pc_ok, str(f))

        r = page.evaluate("""async () => {
            try {
                DataManager.setupNewGame();
                const ok = await DataManager.saveGame(1);
                const loaded = await DataManager.loadGame(1);
                return {ok: true, loaded: loaded, info: !!DataManager.savefileInfo(1)};
            } catch (e) { return {ok: false, err: String(e && e.stack || e)}; }
        }""")
        check("DataManager.saveGame(1) / loadGame(1)", r.get("ok") and r.get("loaded") is not False,
              json.dumps(r)[:300])
        check("file1.rmmzsave + global.rmmzsave zapisane",
              (root / "save" / "file1.rmmzsave").is_file() and (root / "save" / "global.rmmzsave").is_file())

        r = page.evaluate("""() => {
            const out = {};
            try { out.ash = typeof youAreAsh === 'function' ? youAreAsh() : 'n/a'; } catch (e) { out.ash = 'ERR ' + e; }
            try { require('./greenworks'); out.gw = 'loaded?!'; } catch (e) { out.gw = e.code; }
            out.user = require('os').userInfo().username;
            const fs = require('fs');
            fs.writeFileSync('save/ut_bin.dat', new Uint8Array([0, 1, 254, 255]));
            out.bin = Array.from(fs.readFileSync('save/ut_bin.dat'));
            out.asset = fs.existsSync('data/System.json') && fs.readFileSync('DATA/system.json', 'utf8').length > 0;
            out.dir = fs.readdirSync('save').includes('ut_bin.dat');
            fs.unlinkSync('save/ut_bin.dat');
            out.gone = !fs.existsSync('save/ut_bin.dat');
            out.join = require('path').join('save/', 'file1.rmmzsave');
            return out;
        }""")
        check("youAreAsh() bez wyjątku", not str(r["ash"]).startswith("ERR"), str(r["ash"]))
        check("require('./greenworks') -> MODULE_NOT_FOUND", r["gw"] == "MODULE_NOT_FOUND", r["gw"])
        check("fs: binarne zapis/odczyt, assety (bez wielkości liter), readdir, unlink",
              r["bin"] == [0, 1, 254, 255] and r["asset"] and r["dir"] and r["gone"], json.dumps(r))
        check("path.join", r["join"] == "save/file1.rmmzsave", r["join"])

        r = page.evaluate("""async () => {
            window.__utBack();
            const down = !!Input._currentState.escape;
            await new Promise(res => setTimeout(res, 250));
            return {down, up: !Input._currentState.escape};
        }""")
        check("Wstecz = Esc (wciśnięcie i puszczenie)", r["down"] and r["up"], json.dumps(r))
        browser.close()
    server.shutdown()

    real_404 = sorted(set(log404))
    check("brak 404 przy starcie", not real_404, ", ".join(real_404[:10]))
    check("brak nieprzechwyconych wyjątków JS", not page_err, " | ".join(page_err[:5]))
    later = console_err[len([e for e in natural_err if e in console_err]):]
    if later:
        print(f"  (console.error po manipulacjach testu - setupNewGame w trakcie sceny, oczekiwane: {len(later)}) "
              + " | ".join(later[:3])[:400])
    leftovers = [p.name for p in root.rglob("*.uttmp")]
    check("brak plików .uttmp", not leftovers, ", ".join(leftovers))
    shutil.rmtree(root, ignore_errors=True)
    failed = [n for n, c, _ in results if not c]
    print(f"\n{len(results) - len(failed)}/{len(results)} OK" + (f", nie przeszło: {failed}" if failed else ""))
    return 1 if failed else 0


if __name__ == "__main__":
    sys.exit(main())
