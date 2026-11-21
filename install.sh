#!/usr/bin/env bash
# Window Sizer installer: KWin script (window context menu) + tray application.
#
# Usage: ./install.sh [--no-autostart]
set -u

KWIN_ID=window-sizer

HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
KWIN_SRC="$HERE/kwin/$KWIN_ID"
APP_SRC="$HERE/app"

KWIN_DEST="$HOME/.local/share/kwin/scripts/$KWIN_ID"
APP_DIR="$HOME/.local/share/window-sizer"
BIN_DIR="$HOME/.local/bin"
LAUNCHER="$BIN_DIR/window-sizer"
DESKTOP_DIR="$HOME/.local/share/applications"
AUTOSTART_DIR="$HOME/.config/autostart"

AUTOSTART=1
for arg in "$@"; do
    case "$arg" in
        --no-autostart) AUTOSTART=0 ;;
        -h|--help) sed -n '2,4p' "$0"; exit 0 ;;
        *) echo "Unknown option: $arg" >&2; exit 1 ;;
    esac
done

die() { echo "ERROR: $*" >&2; exit 1; }

remove_kwin_script() {   # $1 = script id
    busctl --user call org.kde.KWin /Scripting org.kde.kwin.Scripting unloadScript s "$1" >/dev/null 2>&1
    kwriteconfig6 --file kwinrc --group Plugins --key "${1}Enabled" --delete
    kpackagetool6 --type=KWin/Script -r "$1" >/dev/null 2>&1
    rm -rf "$HOME/.local/share/kwin/scripts/$1"
}

# --- Prerequisites ----------------------------------------------------------
for cmd in python3 kpackagetool6 kwriteconfig6 kreadconfig6 busctl; do
    command -v "$cmd" >/dev/null 2>&1 || die "'$cmd' not found."
done
python3 -c "import PySide6.QtWidgets" 2>/dev/null || die \
"PySide6 is not installed for python3. Install it with one of:
    sudo dnf install python3-pyside6
    python3 -m pip install --user PySide6"
[ -f "$KWIN_SRC/metadata.json" ] || die "$KWIN_SRC/metadata.json not found."
[ -f "$APP_SRC/window_sizer.py" ] || die "$APP_SRC/window_sizer.py not found."

# --- Stop anything running and remove previous installs ---------------------
pkill -f "$APP_DIR/window_sizer.py" 2>/dev/null
remove_kwin_script "$KWIN_ID"

# --- KWin script ------------------------------------------------------------
echo "Installing KWin script..."
kpackagetool6 --type=KWin/Script -i "$KWIN_SRC" || die "kpackagetool6 install failed."
[ -f "$KWIN_DEST/metadata.json" ] || die "install reported success but $KWIN_DEST/metadata.json is missing."
diff -r "$KWIN_SRC" "$KWIN_DEST" >/dev/null || echo "WARNING: installed KWin script differs from source."

# --- Tray application -------------------------------------------------------
echo "Installing tray application..."
mkdir -p "$APP_DIR" "$BIN_DIR" "$DESKTOP_DIR"
install -m 644 "$APP_SRC/window_sizer.py" "$APP_DIR/window_sizer.py"
# The tray app reuses the placement logic from the KWin script.
install -m 644 "$KWIN_SRC/contents/code/main.js" "$APP_DIR/kwin_shared.js"

cat > "$LAUNCHER" <<LAUNCH
#!/usr/bin/env bash
exec python3 "$APP_DIR/window_sizer.py" "\$@"
LAUNCH
chmod +x "$LAUNCHER"

cat > "$DESKTOP_DIR/window-sizer.desktop" <<DESKTOP
[Desktop Entry]
Type=Application
Name=Window Sizer
Comment=Window size and position profiles
Exec=$LAUNCHER --editor
Icon=transform-scale
Terminal=false
Categories=Utility;Settings;
StartupWMClass=window-sizer
DESKTOP

if [ "$AUTOSTART" -eq 1 ]; then
    mkdir -p "$AUTOSTART_DIR"
    cat > "$AUTOSTART_DIR/window-sizer-tray.desktop" <<AUTO
[Desktop Entry]
Type=Application
Name=Window Sizer (tray)
Exec=$LAUNCHER
Icon=transform-scale
Terminal=false
X-KDE-autostart-after=panel
AUTO
else
    rm -f "$AUTOSTART_DIR/window-sizer-tray.desktop"
fi

# --- Enable the KWin script and start the tray application ------------------
kwriteconfig6 --file kwinrc --group Plugins --key "${KWIN_ID}Enabled" true
busctl --user call org.kde.KWin /KWin org.kde.KWin reconfigure

setsid -f "$LAUNCHER" >/dev/null 2>&1

sleep 3
echo "--- KWin log (last 20 seconds):"
journalctl --user --since "-20 sec" 2>/dev/null | grep -i "window-sizer" \
    || echo "No load message yet. See README.md, Troubleshooting."

echo
echo "Done. No profiles exist yet: left-click the tray icon to open the editor and add some."
echo "Window menu: right-click a window's title bar -> Window Size (shown once profiles exist)."
case ":$PATH:" in
    *":$BIN_DIR:"*) ;;
    *) echo "Note: $BIN_DIR is not in PATH; start the app with $LAUNCHER." ;;
esac
