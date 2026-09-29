#!/bin/sh
# RPG Maker MV/MZ -> Android APK - the CONSOLE version (Linux / Steam Deck).
# POSIX sh, no bashisms. Run it with:  sh convert.sh   (options: sh convert.sh --help)
# A ZIP does not keep the +x bit, so ./convert.sh alone may not work.
# Window version: sh gui.sh

DIR=$(CDPATH= cd -- "$(dirname -- "$0")" && pwd)
cd "$DIR" || exit 1
RPGM_APK_DIR="$DIR"
PYTHONDONTWRITEBYTECODE=1
export RPGM_APK_DIR PYTHONDONTWRITEBYTECODE

fail() {
    echo ""
    echo "[ERROR] $1"
    echo "Program folder: $DIR"
    echo ""
    if [ -t 0 ]; then
        printf "Press Enter to close... "
        read _dummy
    fi
    exit 1
}

[ -f "$DIR/cli.py" ] || fail "Program files not found. Unpack the whole archive."

PY=""
for c in python3 python3.13 python3.12 python3.11 python3.10 python; do
    if command -v "$c" >/dev/null 2>&1 && \
       "$c" -c 'import sys; sys.exit(0 if sys.version_info >= (3, 10) else 1)' >/dev/null 2>&1; then
        PY="$c"
        break
    fi
done
[ -n "$PY" ] || fail "Python 3.10+ not found. Install the python3 package."

"$PY" "$DIR/cli.py" "$@"
STATUS=$?
# 0 = success, 10 = the program already showed the error and waited for Enter.
if [ "$STATUS" -ne 0 ] && [ "$STATUS" -ne 10 ]; then
    fail "The program exited with code $STATUS."
fi
exit 0
