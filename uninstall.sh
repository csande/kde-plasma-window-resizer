#!/usr/bin/env bash
# Window Sizer uninstaller. Removes the KWin script, tray application,
# launcher, desktop entries and the kwinrc profile mirror.
#
# Usage: ./uninstall.sh [--purge]
#   --purge   also delete the saved profiles (~/.config/window-sizer)
set -u

IDS="window-sizer window-sizer-proto"   # current id and the earlier prototype
PURGE=0
for arg in "$@"; do
    case "$arg" in
        --purge) PURGE=1 ;;
        -h|--help) sed -n '2,6p' "$0"; exit 0 ;;
        *) echo "Unknown option: $arg" >&2; exit 1 ;;
    esac
done

# Tray application
pkill -f "window-sizer/window_sizer.py" 2>/dev/null

# KWin script(s)
for id in $IDS; do
    busctl --user call org.kde.KWin /Scripting org.kde.kwin.Scripting unloadScript s "$id" >/dev/null 2>&1
    kwriteconfig6 --file kwinrc --group Plugins --key "${id}Enabled" --delete
    kwriteconfig6 --file kwinrc --group "Script-$id" --key profiles --delete
    kpackagetool6 --type=KWin/Script -r "$id" >/dev/null 2>&1
    rm -rf "$HOME/.local/share/kwin/scripts/$id"
done
busctl --user call org.kde.KWin /KWin org.kde.KWin reconfigure >/dev/null 2>&1

# Application files
rm -rf "$HOME/.local/share/window-sizer"
rm -f  "$HOME/.local/bin/window-sizer"
rm -f  "$HOME/.local/share/applications/window-sizer.desktop"
rm -f  "$HOME/.config/autostart/window-sizer-tray.desktop"

if [ "$PURGE" -eq 1 ]; then
    rm -rf "$HOME/.config/window-sizer"
    echo "Saved profiles deleted."
else
    echo "Saved profiles kept in ~/.config/window-sizer (use --purge to delete)."
fi

echo "Remaining installed files (none listed = clean):"
find "$HOME/.local/share/kwin/scripts" "$HOME/.local/share/window-sizer" "$HOME/.local/bin" \
     "$HOME/.local/share/applications" "$HOME/.config/autostart" \
     -maxdepth 1 -name "window-sizer*" 2>/dev/null
echo "Remaining kwinrc entries (none listed = clean):"
grep -i "window-sizer" "$HOME/.config/kwinrc" 2>/dev/null
echo "Done."
