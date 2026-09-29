#!/usr/bin/env python3
"""Builds the package to share: dist/<name>/ with exactly what the user needs.

    python make_dist.py --clean
    python make_dist.py --clean --zip

The package is FLAT - what the user sees after unpacking:

    <package>/
      gui.bat / gui.sh              window version
      convert.bat / convert.sh      console version
      find_python.bat, bootstrap_python.ps1   Python finder / portable Python (Windows)
      HOW-TO-USE.txt, JAK-UZYWAC.txt, README.md
      gui.py, cli.py, rpgm_apk_lib.py, i18n.py, app_config.py
      android/  MainActivity.java, UT_AndroidBridge.js, classes.dex (+ .sha256)

Not included: tests/, .tools/ (downloaded on the user's machine), output/,
.python/, logs, config. Standard library only.
"""

from __future__ import annotations

import argparse
import hashlib
import os
import shutil
import sys
import zipfile

HERE = os.path.dirname(os.path.abspath(__file__))

FILES = (
    "gui.bat", "gui.sh", "convert.bat", "convert.sh", "find_python.bat", "bootstrap_python.ps1",
    "HOW-TO-USE.txt", "JAK-UZYWAC.txt", "README.md",
    "gui.py", "cli.py", "rpgm_apk_lib.py", "i18n.py", "app_config.py",
    "android/MainActivity.java", "android/UT_AndroidBridge.js",
    "android/classes.dex", "android/classes.dex.sha256",
)
EXECUTABLE = {"gui.sh", "convert.sh"}


def check_dex() -> None:
    src = os.path.join(HERE, "android", "MainActivity.java")
    sha = open(os.path.join(HERE, "android", "classes.dex.sha256"), encoding="utf-8").read().strip()
    with open(src, "rb") as fh:
        if hashlib.sha256(fh.read().replace(b"\r\n", b"\n")).hexdigest() != sha:
            sys.exit("android/classes.dex is older than MainActivity.java - build any game once with "
                     "`python cli.py --rebuild-dex` (needs a JDK), then run make_dist.py again.")


def build(out_dir: str, name: str, clean: bool, make_zip: bool) -> int:
    check_dex()
    root = os.path.join(out_dir, name)
    if clean and os.path.isdir(root):
        shutil.rmtree(root)
    os.makedirs(root, exist_ok=True)
    total = 0
    for rel in FILES:
        src = os.path.join(HERE, rel.replace("/", os.sep))
        if not os.path.isfile(src):
            sys.exit(f"missing {rel}")
        dst = os.path.join(root, rel.replace("/", os.sep))
        os.makedirs(os.path.dirname(dst), exist_ok=True)
        shutil.copy2(src, dst)
        total += os.path.getsize(src)
    print(f"package: {root}")
    print(f"  files: {len(FILES)}, {total / 1024:.0f} KB")
    if make_zip:
        zip_path = root + ".zip"
        if os.path.isfile(zip_path):
            os.remove(zip_path)
        with zipfile.ZipFile(zip_path, "w", zipfile.ZIP_DEFLATED, compresslevel=9) as zf:
            for rel in FILES:
                info = zipfile.ZipInfo.from_file(os.path.join(root, rel.replace("/", os.sep)),
                                                 f"{name}/{rel}")        # forward slashes for Linux
                if rel in EXECUTABLE:
                    info.external_attr = (0o100755 << 16)
                with open(os.path.join(root, rel.replace("/", os.sep)), "rb") as fh:
                    zf.writestr(info, fh.read(), compress_type=zipfile.ZIP_DEFLATED)
        print(f"  ZIP: {zip_path} ({os.path.getsize(zip_path) / 1024:.0f} KB)")
    return 0


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--out", default=os.path.join(HERE, "dist"))
    ap.add_argument("--name", default="RPGM-to-APK")
    ap.add_argument("--clean", action="store_true", help="remove the previous package folder first")
    ap.add_argument("--zip", action="store_true", help="also create a ZIP")
    args = ap.parse_args()
    return build(os.path.abspath(args.out), args.name, args.clean, args.zip)


if __name__ == "__main__":
    sys.exit(main())
