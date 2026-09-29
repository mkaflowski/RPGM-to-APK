#!/usr/bin/env python3
"""Maintainer tool: builds the GeckoView runtime pack that rpgm-to-apk downloads.

    python make_gecko_runtime.py --gradle <gradle 8.9+> --sdk <Android SDK> --jdk <JDK 17>

Users never run this. It builds android/gecko-runtime (an app with no code of its own,
only GeckoView and its dependencies) with Gradle once, and packs what the converter needs
to build game APKs WITHOUT Gradle:

    runtime.json            versions
    template.apk            GeckoView + androidx dex, native libraries (arm64-v8a), assets
    res-flats.zip           compiled resources (aapt2 .flat) of GeckoView and androidx
    stable-ids.txt          their resource ids - game APKs are linked with aapt2 --stable-ids,
                            so the R classes inside the template dex stay correct
    AndroidManifest.xml     merged manifest (GeckoView child-process services, providers...)
    geckoview-classes.jar   compile classpath for android/GeckoHost.java

Upload the zip as the asset of the GitHub release "gecko-runtime-<version>" and put its
SHA-256 and size into GECKO_PACK_SHA256 / GECKO_PACK_MB in rpgm_apk_lib.py.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import subprocess
import sys
import zipfile
from pathlib import Path

HERE = Path(__file__).resolve().parent
PROJECT = HERE / "android" / "gecko-runtime"
sys.path.insert(0, str(HERE))
import rpgm_apk_lib as lib  # noqa: E402


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--gradle", default=os.environ.get("GRADLE", "gradle"))
    ap.add_argument("--sdk", default=os.environ.get("ANDROID_HOME"))
    ap.add_argument("--jdk", default=os.environ.get("JAVA_HOME"))
    ap.add_argument("--gecko-build", default="142.0.20250827004350", help="GeckoView Maven version")
    ap.add_argument("--out", default=str(HERE / "dist"))
    args = ap.parse_args()
    if not args.sdk:
        sys.exit("--sdk (or ANDROID_HOME) is required")
    env = dict(os.environ)
    if args.jdk:
        env["JAVA_HOME"] = args.jdk
    (PROJECT / "local.properties").write_text("sdk.dir=" + Path(args.sdk).as_posix() + "\n", encoding="utf-8")
    subprocess.run([args.gradle, "-p", str(PROJECT), "--no-daemon", "--console=plain",
                    f"-PgeckoVersion={args.gecko_build}", "clean", "assembleRelease", "geckoClassesJar"],
                   env=env, check=True, shell=os.name == "nt")

    build = PROJECT / "build"
    apk = build / "outputs" / "apk" / "release" / "gecko-runtime-release.apk"
    ids = build / "intermediates" / "stable_resource_ids_file" / "release" / "processReleaseResources" / "stableIds.txt"
    manifest = build / "intermediates" / "merged_manifest" / "release" / "processReleaseMainManifest" / "AndroidManifest.xml"
    flats = sorted((build / "intermediates" / "merged_res" / "release" / "mergeReleaseResources").glob("*.flat"))
    jar = build / "gecko-classes" / "classes.jar"
    for p in (apk, ids, manifest, jar):
        if not p.is_file():
            sys.exit(f"missing build output: {p}")
    if not flats:
        sys.exit("no compiled resources (.flat)")
    text = manifest.read_text(encoding="utf-8")
    if lib.GECKO_TEMPLATE_PACKAGE not in text:
        sys.exit("unexpected template package in the merged manifest")
    with zipfile.ZipFile(apk) as z:
        version = next((n for n in z.namelist() if n.startswith("lib/")), None)
        if not version:
            sys.exit("template APK has no native libraries")

    out = Path(args.out)
    out.mkdir(parents=True, exist_ok=True)
    pack = out / lib.GECKO_PACK
    tmp = pack.with_suffix(".zip.part")
    info = {"gecko_version": lib.GECKO_VERSION, "gecko_build": args.gecko_build, "abi": lib.GECKO_ABI,
            "template_package": lib.GECKO_TEMPLATE_PACKAGE, "min_sdk": lib.MIN_SDK}
    with zipfile.ZipFile(tmp, "w", zipfile.ZIP_DEFLATED, compresslevel=9) as z:
        z.writestr("runtime.json", json.dumps(info, indent=2) + "\n")
        z.write(apk, "template.apk")
        z.write(ids, "stable-ids.txt")
        z.writestr("AndroidManifest.xml", text)
        z.write(jar, "geckoview-classes.jar")
        flat_zip = out / "res-flats.zip"
        with zipfile.ZipFile(flat_zip, "w", zipfile.ZIP_STORED) as fz:
            for f in flats:
                fz.write(f, f.name)
        z.write(flat_zip, "res-flats.zip")
        flat_zip.unlink()
    os.replace(tmp, pack)
    digest = hashlib.sha256(pack.read_bytes()).hexdigest()
    size_mb = round(pack.stat().st_size / 1e6)
    print(json.dumps({"pack": str(pack), "sha256": digest, "mb": size_mb, **info}, indent=2))
    print(f'\nrpgm_apk_lib.py:\nGECKO_PACK_SHA256 = "{digest}"\nGECKO_PACK_MB = {size_mb}')


if __name__ == "__main__":
    main()
