#!/usr/bin/env python3
"""Unit tests of rpgm_apk_lib - standard library only, no network, no Android tools.

    python -m unittest discover -s tests -v
"""

from __future__ import annotations

import json
import os
import sys
import tempfile
import unittest
import xml.dom.minidom
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
os.environ.setdefault("RPGM_APK_LANG", "en")

import i18n  # noqa: E402
import rpgm_apk_lib as lib  # noqa: E402


def make_game(base: Path, engine: str = "MZ", www: bool = False, title: str = "Test Game") -> Path:
    web = base / "www" if www else base
    (web / "js" / "plugins").mkdir(parents=True)
    (web / "data").mkdir()
    (web / "index.html").write_text('<html><head><meta name="viewport" content="user-scalable=no"></head>'
                                    '<body></body></html>', encoding="utf-8")
    (web / "js" / ("rmmz_core.js" if engine == "MZ" else "rpg_core.js")).write_text("//", encoding="utf-8")
    (web / "js" / "plugins.js").write_text('var $plugins =\n[\n{"name":"A","status":true}\n];', encoding="utf-8")
    (web / "data" / "System.json").write_text(json.dumps({"gameTitle": title}), encoding="utf-8")
    if www:
        (base / "package.json").write_text(json.dumps({"main": "www/index.html"}), encoding="utf-8")
    return base


class Names(unittest.TestCase):
    def test_slugify(self):
        self.assertEqual(lib.slugify("Zażółć: 2 Gęśl!"), "zazolc_2_gesl")
        self.assertEqual(lib.slugify("123 Game"), "g_123_game")
        self.assertEqual(lib.slugify("new"), "g_new")
        self.assertEqual(lib.slugify("日本語"), "game")

    def test_valid_package(self):
        self.assertTrue(lib.valid_package("pl.unitytranslator.rpgm.look_outside"))
        for bad in ("single", "com.1abc", "com.new.x", "com..x", "com.a-b"):
            self.assertFalse(lib.valid_package(bad), bad)


class Patches(unittest.TestCase):
    def test_plugins_first_and_idempotent(self):
        raw = b'\xef\xbb\xbfvar $plugins =\n[\n{"name":"A","status":true}\n];'
        out = lib.patch_plugins_js(raw)
        self.assertTrue(out.startswith(b"\xef\xbb\xbf"))
        text = out.decode("utf-8-sig")
        self.assertLess(text.index(lib.BRIDGE_NAME), text.index('"A"'))
        self.assertEqual(lib.patch_plugins_js(out), out)

    def test_plugins_empty_array_is_valid_js(self):
        text = lib.patch_plugins_js(b"var $plugins = [];").decode()
        self.assertIn(lib.BRIDGE_NAME, text)
        self.assertTrue(text.rstrip().endswith("},];"))       # trailing comma = still one element

    def test_plugins_missing(self):
        with self.assertRaises(lib.BuildError):
            lib.patch_plugins_js(b"var somethingElse = [];")

    def test_index_viewport_replaced(self):
        raw = b'<html><head><meta name="viewport" content="user-scalable=no"></head><body></body></html>'
        text = lib.patch_index_html(raw).decode()
        self.assertEqual(text.count('name="viewport"'), 1)
        self.assertIn("width=device-width", text)
        self.assertIn('id="ut-android-layout"', text)
        self.assertEqual(lib.patch_index_html(text.encode()), text.encode())

    def test_index_without_viewport_or_head(self):
        text = lib.patch_index_html(b"<html><head><title>x</title></head></html>").decode()
        self.assertIn("width=device-width", text)
        self.assertLess(text.index("viewport"), text.index("<title>"))
        text = lib.patch_index_html(b"<canvas></canvas>").decode()
        self.assertTrue(text.startswith("<meta"))

    def test_manifest_escapes_label(self):
        xml_text = lib.manifest_xml("com.a.b", 'Tom & "Jerry" <3', True, "landscape")
        doc = xml.dom.minidom.parseString(xml_text.encode())
        app = doc.getElementsByTagName("application")[0]
        self.assertEqual(app.getAttribute("android:label"), 'Tom & "Jerry" <3')
        self.assertEqual(doc.documentElement.getAttribute("package"), "com.a.b")


class Games(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.base = Path(self.tmp.name)

    def tearDown(self):
        self.tmp.cleanup()

    def test_detect_mz(self):
        root = make_game(self.base / "mz", "MZ", title="Look Inside")
        g = lib.detect_game(root)
        self.assertEqual((g.engine, g.title, g.web), ("MZ", "Look Inside", root))
        (root / "Game.exe").write_bytes(b"MZ")
        self.assertEqual(lib.detect_game(root / "Game.exe").web, root)      # dragged Game.exe

    def test_detect_mv_www(self):
        root = make_game(self.base / "mv", "MV", www=True)
        g = lib.detect_game(root)
        self.assertEqual((g.engine, g.web), ("MV", root / "www"))
        self.assertEqual(g.save_dir, root / "www" / "save")

    def test_not_a_game(self):
        (self.base / "x").mkdir()
        self.assertIsNone(lib.detect_game(self.base / "x"))
        self.assertIsNone(lib.detect_game(self.base / "missing"))

    def test_collect_skips_pc_runtime(self):
        root = make_game(self.base / "g")
        for rel in ("nw.dll", "Game.exe", "icudtl.dat", "v8_context_snapshot.bin", "debug.log",
                    "locales/en.pak", "swiftshader/libEGL.dll", "save/file1.rmmzsave",
                    "lib/greenworks-win64.node", "lib/steam_api.dll", "Thumbs.db",
                    "credits.html", "data/Map001.json", "data/extra.bin", "img/pictures/a.png_"):
            p = root / rel
            p.parent.mkdir(parents=True, exist_ok=True)
            p.write_bytes(b"x")
        files, skipped, _ = lib.collect_files(lib.detect_game(root))
        rels = {r for _, r in files}
        for kept in ("credits.html", "data/Map001.json", "data/extra.bin", "img/pictures/a.png_",
                     "index.html", "js/plugins.js"):
            self.assertIn(kept, rels)
        for gone in ("nw.dll", "Game.exe", "icudtl.dat", "v8_context_snapshot.bin", "debug.log",
                     "locales/en.pak", "save/file1.rmmzsave", "lib/greenworks-win64.node", "Thumbs.db"):
            self.assertNotIn(gone, rels)
        self.assertGreater(skipped, 0)

    def test_pc_saves(self):
        root = make_game(self.base / "s")
        (root / "save").mkdir()
        for n in ("file1.rmmzsave", "config.rmmzsave", "notes.txt"):
            (root / "save" / n).write_text("x")
        self.assertEqual([p.name for p in lib.pc_saves(lib.detect_game(root))],
                         ["config.rmmzsave", "file1.rmmzsave"])


class Tools(unittest.TestCase):
    def test_check_tools_components(self):
        tc = lib.check_tools()
        keys = [c.key for c in tc.components]
        for k in ("python", "java", "build_tools", "android_jar", "dex", "keystore", "adb"):
            self.assertIn(k, keys)
        for c in tc.components:
            if not c.ok and c.required:
                self.assertIn(c, tc.missing_required)

    def test_prebuilt_dex_matches_source(self):
        self.assertTrue(lib.prebuilt_dex_ok(), "android/classes.dex is out of date - build once with --rebuild-dex")

    def test_parse_path(self):
        self.assertEqual(lib.parse_path('"C:\\a b\\c"' if os.name == "nt" else "'/a b/c'"),
                         Path("C:\\a b\\c" if os.name == "nt" else "/a b/c"))

    def test_devices_parse(self):
        dev = lib.Device("R5CT", "unauthorized", "")
        self.assertFalse(dev.ready)
        self.assertTrue(lib.device_hint(dev))


class Lang(unittest.TestCase):
    def test_switch(self):
        i18n.set_lang("pl")
        self.assertEqual(i18n.tr("tak", "yes"), "tak")
        i18n.set_lang("en")
        self.assertEqual(i18n.tr("tak", "yes"), "yes")


if __name__ == "__main__":
    unittest.main()
