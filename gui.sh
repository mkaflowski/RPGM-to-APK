#!/bin/sh
# RPG Maker MV/MZ -> Android APK - the WINDOW version (Linux / Steam Deck).
# POSIX sh, no bashisms. Run it with:  sh gui.sh
# A ZIP does not keep the +x bit, so ./gui.sh alone may not work.
# Console version: sh convert.sh

DIR=$(CDPATH= cd -- "$(dirname -- "$0")" && pwd)
cd "$DIR" || exit 1
RPGM_APK_DIR="$DIR"
PYTHONDONTWRITEBYTECODE=1
export RPGM_APK_DIR PYTHONDONTWRITEBYTECODE

# The terminal must never close without a message. No "set -e" and no "exec":
# after them no pause would ever run.
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

[ -f "$DIR/gui.py" ] || fail "Program files not found. Unpack the whole archive."

PY=""
for c in python3 python3.13 python3.12 python3.11 python3.10 python; do
    if command -v "$c" >/dev/null 2>&1 && \
       "$c" -c 'import sys; sys.exit(0 if sys.version_info >= (3, 10) else 1)' >/dev/null 2>&1; then
        PY="$c"
        break
    fi
done
[ -n "$PY" ] || fail "Python 3.10+ not found. Install the python3 package."

if ! "$PY" -c "import tkinter" >/dev/null 2>&1; then
    fail "Python has no tkinter, so the window cannot open.
  Steam Deck / Arch : sudo pacman -S tk
  Debian / Ubuntu   : sudo apt install python3-tk
  Fedora            : sudo dnf install python3-tkinter
You can also use the console version:  sh convert.sh"
fi

"$PY" "$DIR/gui.py" "$@"
STATUS=$?
# 10 = the program already showed the error.
if [ "$STATUS" -ne 0 ] && [ "$STATUS" -ne 10 ]; then
    fail "The program exited with code $STATUS. Details are in rpgm-to-apk.log."
fi
exit 0
