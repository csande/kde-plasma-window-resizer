# KDE Plasma Window Sizer

Window size and position profiles for KDE Plasma 6, in the spirit of the old
Windows "Sizer" utility. Pick a profile such as "1000x800 centered" or
"600x400 lower right" from a window's context menu or from a tray icon, and the
window is resized and moved to match.

Components:

| Component | What it does |
|---|---|
| KWin script (`kwin/window-sizer`) | Adds a **Window Size** submenu to the window context menu |
| Tray application (`app/window_sizer.py`) | Tray icon, profile editor, "apply to active window" menu |

The profile editor, profile storage, and geometry calculations were checked
with automated tests using mocks. Behavior inside a live KWin session depends
on the Plasma 6 scripting API; see [Troubleshooting](#troubleshooting).

## Requirements

* KDE Plasma 6 (Wayland or X11)
* Python 3 with PySide6 (Qt 6)
* `kpackagetool6`, `kwriteconfig6`, `kreadconfig6` (part of Plasma / KDE Frameworks 6)
* `busctl` (part of systemd)

On Fedora:

```bash
sudo dnf install python3-pyside6
```

If that package is unavailable, `python3 -m pip install --user PySide6` also works.

## Install

```bash
./install.sh              # add --no-autostart to skip starting the tray app at login
```

The installer:

1. Installs and enables the KWin script.
2. Installs the tray application under `~/.local/share/window-sizer/`, with a
   launcher at `~/.local/bin/window-sizer`, an application-menu entry, and
   (unless `--no-autostart`) an autostart entry,
3. Starts the tray application and prints recent KWin log lines.

A new installation has **no profiles**. Until at least one exists, the window
context menu shows no **Window Size** entry and the tray menu shows
"(No profiles)". Open the editor (left-click the tray icon) and add some.

Reinstalling (for example after editing the source) is safe: running
`./install.sh` again replaces the previous installation and keeps saved
profiles.

### Upgrading from an earlier version

Saved profiles are kept and converted to the current format automatically.
Profiles that were created as built-in defaults ("1000x800 centered" and
"600x400 lower right") are ordinary profiles after the upgrade and can be
deleted in the editor. Profile names that were duplicated or empty are
renamed (for example "Wide 2", "Unnamed profile") when loaded.

## Uninstall

```bash
./uninstall.sh            # keeps saved profiles
./uninstall.sh --purge    # also deletes ~/.config/window-sizer
```

## Usage

### Editor

Left-click the tray icon, or launch **Window Sizer** from the application menu.

* **Add** creates a profile; **Delete** removes the selected one. New profiles
  start as 1024 × 768 with the position **Center**. When no profiles exist,
  the right-hand side of the editor is empty.
* Reorder by dragging entries in the list. The window menu and tray menu use
  the same order.
* Each profile has a name, a width and a height in pixels (one field per
  row), and a position chosen from one drop-down:
  **Top, Bottom, Left, Right, Top left, Top right, Bottom left, Bottom right,
  Center, Custom.** Choosing **Custom** shows **X** and **Y** fields, each on
  its own row below the drop-down, for the position of the window's upper left
  corner; they are hidden for every other choice (the values are remembered
  while the profile exists). All labels and fields line up in two columns.
* Changes are saved automatically a moment after each edit, and when the
  editor is closed.
* Closing the editor hides it; the application keeps running in the tray.
  Quit from the tray menu.

Profile names must be unique, ignoring upper/lower case ("Wide" and "wide"
count as the same name), and cannot be empty. While the name field contains an
invalid name, a message appears under it and the last valid name stays in
effect; leaving the field restores that name. New profiles are named "New
profile", "New profile 2", and so on.

Position and size rules:

* Width and height are the size of the whole window frame, title bar and
  borders included.
* Named positions place the window within the screen's work area (the screen
  minus panels), on the screen the window is currently on. Left and Right are
  vertically centered; Top and Bottom are horizontally centered.
* **Center** follows the same rounding as Plasma's built-in window centering:
  when the leftover space is odd, the extra pixel goes below or to the right,
  so the window sits one pixel higher or further left than an exact center.
* **Custom** X and Y are measured from the upper left corner of the screen the
  window is on (panels are not subtracted). Negative values are allowed.
* Sizes larger than the work area are reduced to fit, and sizes below the
  window's own minimum are raised to that minimum.
* Windows that cannot be resized (fixed-size windows) are only moved when a
  profile is applied from the tray; the window menu does not list profiles for
  them. Fullscreen windows are skipped. Maximized and tiled windows are
  restored first, then placed.

### Window context menu

Right-click a window's title bar (or its taskbar entry) and choose
**Window Size**, then a profile. Entries follow the order set in the editor.

Right-clicking a window edge opens the same menu only if the window decoration
has an edge that can be clicked. The default Breeze theme draws no side
borders, so only the title bar and taskbar entry work there; a decoration with
visible borders, or a larger border size in Window Decorations settings, makes
edges clickable. This behavior is controlled by KWin, not by this project, and
has not been verified here.

### Tray icon

* **Left-click**: open the editor.
* **Right-click**: list of profiles. Choosing one applies it to the currently
  active window. This path is intended for windows whose context menu is not
  reachable (for example some applications that draw their own title bar).
  The active window is the one focused before the menu was opened; if the
  panel or another non-window item holds focus, the topmost regular window on
  the current virtual desktop is used instead.
* The menu also contains **Edit profiles...** and **Quit**.
* **Icon.** The application uses the icon-theme icon `transform-scale`, the
  same icon as **Resize** in a window's "More Actions" menu, for the tray icon,
  the editor window, tray notifications, the application-menu entry, and the
  autostart entry. It follows the active icon theme. If a theme lacks it, other
  window-related theme icons are tried, and a drawn window outline is the last
  fallback.

## How it works

```
 Editor / tray app (Python, PySide6)                KWin (compositor)
 ───────────────────────────────────                ─────────────────
 profiles.json  ──autosave──►  kwinrc [Script-window-sizer] profiles=<JSON>
                                                          │ readConfig()
                                        window-sizer script (JavaScript)
                                        registerUserActionsMenu() → "Window Size"
                                        placeWindow() sets window.frameGeometry

 tray menu click ──D-Bus──► loadScript(one-shot script) ─► placeWindow(active window)
```

* Moving and resizing other applications' windows has to be done by the
  compositor on Wayland, so all geometry changes happen inside KWin through its
  scripting API. External tools such as `xdotool` and `wmctrl` do not work for
  native Wayland windows.
* **Profile storage.** The editor saves `~/.config/window-sizer/profiles.json`
  (the single source of truth) and mirrors the same list into `kwinrc` as one
  JSON value, so the KWin script can read it without the tray application
  running. Each profile is stored as `name`, `width`, `height`, `position`
  (one of `top`, `bottom`, `left`, `right`, `top-left`, `top-right`,
  `bottom-left`, `bottom-right`, `center`, `custom`), `x`, and `y`. Edits are
  written about half a second after the last change, so typing a name does not
  trigger a save per keystroke. After a change, the tray application asks KWin
  to reload the script through D-Bus so the menu reflects the new list.
* **Window menu.** The script registers a callback with
  `registerUserActionsMenu()`. KWin calls it each time a window's context menu
  opens; the callback returns the submenu built from the current profiles.
* **Tray menu.** A tray icon cannot reach into another process's window on
  Wayland, so a small one-shot KWin script is generated, loaded through
  `org.kde.kwin.Scripting` over D-Bus (with `busctl`), run once, and unloaded.
  It contains the same `placeWindow()` code as the permanent script (the
  section marked `BEGIN SHARED` in `main.js`, copied to `kwin_shared.js` at
  install time).
* **Settings storage** needs no database: one JSON file plus one `kwinrc` key.

## Enabling and disabling the integration

"Integration" here means the KWin script that adds the window-menu entry. The
tray application works independently of it, and vice versa.

**Window menu (KWin script)**

Graphical: System Settings → Window Management → KWin Scripts → tick or untick
**Window Sizer** → Apply.

Command line:

```bash
# Disable
kwriteconfig6 --file kwinrc --group Plugins --key window-sizerEnabled false
busctl --user call org.kde.KWin /KWin org.kde.KWin reconfigure
busctl --user call org.kde.KWin /Scripting org.kde.kwin.Scripting unloadScript s window-sizer

# Enable
kwriteconfig6 --file kwinrc --group Plugins --key window-sizerEnabled true
busctl --user call org.kde.KWin /KWin org.kde.KWin reconfigure
```

If the menu entry does not appear or disappear immediately, log out and back
in; this always applies the setting.

**Tray application**

```bash
# Start now
~/.local/bin/window-sizer &

# Stop
pkill -f window-sizer/window_sizer.py

# Disable start at login
rm ~/.config/autostart/window-sizer-tray.desktop
#   (or: System Settings → Autostart)

# Enable start at login (re-run the installer, or create the entry again)
./install.sh
```

## Files and locations

| Path | Contents |
|---|---|
| `~/.config/window-sizer/profiles.json` | Saved profiles |
| `~/.config/kwinrc`, group `[Script-window-sizer]` | Copy of the profiles for the KWin script |
| `~/.local/share/kwin/scripts/window-sizer/` | Installed KWin script |
| `~/.local/share/window-sizer/` | Tray application (`window_sizer.py`, `kwin_shared.js`) |
| `~/.local/bin/window-sizer` | Launcher |
| `~/.local/share/applications/window-sizer.desktop` | Application-menu entry |
| `~/.config/autostart/window-sizer-tray.desktop` | Start at login |

## Troubleshooting

KWin script output appears in the user journal:

```bash
journalctl --user -f | grep -i window-sizer
```

A healthy start logs `window-sizer: loaded v1.1.0 (N profiles)`.

* **No "Window Size" entry.** The entry is only shown when at least one
  profile exists. Also confirm the script is enabled (see above) and that the
  `loaded` line appears. Entries are hidden for windows that cannot be resized
  and for non-regular windows (docks, desktop, etc.).
* **The menu arrow overlaps the "Window Size" text.** The script pads the
  label with non-breaking spaces to make room. If the overlap remains, raise
  `MENU_LABEL_PADDING` at the top of
  `kwin/window-sizer/contents/code/main.js`, then reinstall. This is a
  workaround for how KWin sizes script-provided menu items and has not been
  checked against every Plasma 6 release.
* **Edits in the editor do not show up in the window menu.** The script is
  reloaded after each autosave. If a stale list persists, run
  `./install.sh` again or log out and in. Check that the profile mirror was
  written: `kreadconfig6 --file kwinrc --group Script-window-sizer --key profiles`.
* **Left-click on the tray icon shows the menu instead of the editor.**
  Whether a click is delivered as "activate" or opens the menu is decided by
  the Plasma system tray. Use **Edit profiles...** in the menu, or launch
  **Window Sizer** from the application menu.
* **A profile does nothing for one application.** Some applications enforce
  their own size limits, and KWin window rules (System Settings → Window
  Management → Window Rules) can override sizes. Fixed-size windows can only be
  moved.
* **Profile applied from the tray hits the wrong window.** The target is the
  active window, or the topmost regular window if the active one is not a
  regular window. Activate the intended window before opening the tray menu.
* **Tray messages.** Errors from D-Bus, `kwriteconfig6`, or saving the profile
  file are printed to the terminal (when started from one) and shown as a tray
  notification.

## Limitations

* Plasma 6 only. The scripting API names differ on Plasma 5.
* Profiles apply only to the window and screen at the moment of use; there is
  no automatic or per-application assignment (KWin window rules do that).
* The tray application needs a StatusNotifierItem host (the Plasma system
  tray provides one).

## Development

```bash
# Edit files under kwin/ or app/, then:
./uninstall.sh && ./install.sh
```

The `loaded vX.Y.Z` line in the journal shows which script version is running.
The version constant is at the top of `main.js`.
