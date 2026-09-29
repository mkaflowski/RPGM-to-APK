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


MERGED_GECKO_MANIFEST = f'''<?xml version="1.0" encoding="utf-8"?>
<manifest xmlns:android="http://schemas.android.com/apk/res/android" package="{lib.GECKO_TEMPLATE_PACKAGE}">
    <uses-sdk android:minSdkVersion="24" android:targetSdkVersion="35" />
    <uses-permission android:name="android.permission.INTERNET" />
    <uses-feature android:name="android.hardware.touchscreen" android:required="false" />
    <uses-feature android:glEsVersion="0x00020000" android:required="true" />
    <permission android:name="{lib.GECKO_TEMPLATE_PACKAGE}.DYNAMIC_RECEIVER_NOT_EXPORTED_PERMISSION"
        android:protectionLevel="signature" />
    <application android:appComponentFactory="androidx.core.app.CoreComponentFactory" android:extractNativeLibs="false">
        <service android:name="org.mozilla.gecko.process.GeckoChildProcessServices$tab0" android:process=":tab0" />
        <provider android:name="androidx.startup.InitializationProvider"
            android:authorities="{lib.GECKO_TEMPLATE_PACKAGE}.androidx-startup" android:exported="false" />
    </application>
</manifest>'''


class Gecko(unittest.TestCase):
    def test_default_renderer_is_gecko(self):
        self.assertEqual(lib.DEFAULT_RENDERER, "gecko")
        self.assertEqual(lib.BuildOptions(game=None, out=Path("x.apk"), package="a.b", label="x").renderer, "gecko")

    def test_manifest_merges_geckoview(self):
        text = lib.manifest_xml("com.my.game", "Game", False, "landscape", "gecko", MERGED_GECKO_MANIFEST)
        doc = xml.dom.minidom.parseString(text.encode())
        self.assertNotIn(lib.GECKO_TEMPLATE_PACKAGE, text)
        app = doc.getElementsByTagName("application")[0]
        self.assertEqual(app.getAttribute("android:appComponentFactory"), "androidx.core.app.CoreComponentFactory")
        self.assertEqual(app.getAttribute("android:extractNativeLibs"), "true")
        names = [s.getAttribute("android:name") for s in doc.getElementsByTagName("service")]
        self.assertIn("org.mozilla.gecko.process.GeckoChildProcessServices$tab0", names)
        provider = doc.getElementsByTagName("provider")[0]
        self.assertEqual(provider.getAttribute("android:authorities"), "com.my.game.androidx-startup")
        meta = {m.getAttribute("android:name"): m.getAttribute("android:value")
                for m in doc.getElementsByTagName("meta-data")}
        self.assertEqual(meta.get("ut.renderer"), "gecko")
        features = [f.getAttribute("android:name") or f.getAttribute("android:glEsVersion")
                    for f in doc.getElementsByTagName("uses-feature")]
        self.assertEqual(len(features), len(set(features)), features)     # no duplicates from the merge
        self.assertEqual(len(doc.getElementsByTagName("uses-sdk")), 0)

    def test_webview_manifest_has_no_gecko(self):
        text = lib.manifest_xml("com.my.game", "Game", False, "landscape")
        self.assertNotIn("gecko", text.lower())

    def test_stable_ids_use_game_package(self):
        ids = f"{lib.GECKO_TEMPLATE_PACKAGE}:string/a = 0x7f010000\n{lib.GECKO_TEMPLATE_PACKAGE}:drawable/b = 0x7f020000\n"
        out = lib.stable_ids_for(ids, "com.my.game")
        self.assertEqual(out, "com.my.game:string/a = 0x7f010000\ncom.my.game:drawable/b = 0x7f020000\n")

    def test_index_gets_gecko_host_first(self):
        raw = b'<html><head><script src="js/main.js"></script></head><body></body></html>'
        text = lib.patch_index_html(raw, gecko=True).decode()
        self.assertLess(text.index("UT_GeckoHost.js"), text.index("js/main.js"))
        self.assertNotIn("UT_GeckoHost", lib.patch_index_html(raw).decode())

    def test_template_entries(self):
        with tempfile.TemporaryDirectory() as tmp:
            apk = Path(tmp) / "t.apk"
            import zipfile
            with zipfile.ZipFile(apk, "w") as z:
                for name in ("AndroidManifest.xml", "resources.arsc", "res/a.xml", "classes.dex", "classes2.dex",
                             "classes10.dex", "META-INF/CERT.RSA", "META-INF/MANIFEST.MF",
                             "META-INF/services/x.Factory", "lib/arm64-v8a/libxul.so", "assets/omni.ja",
                             "assets/dexopt/baseline.prof", "kotlin/kotlin.kotlin_builtins"):
                    z.writestr(name, b"x")
            with zipfile.ZipFile(apk) as z:
                dexes, other = lib.gecko_template_entries(z)
            self.assertEqual([d.filename for d in dexes], ["classes.dex", "classes2.dex", "classes10.dex"])
            self.assertEqual(sorted(o.filename for o in other),
                             ["META-INF/services/x.Factory", "assets/omni.ja", "kotlin/kotlin.kotlin_builtins",
                              "lib/arm64-v8a/libxul.so"])


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
        for k in ("python", "java", "build_tools", "android_jar", "dex", "gecko", "keystore", "adb"):
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
