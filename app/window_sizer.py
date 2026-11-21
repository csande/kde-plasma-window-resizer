#!/usr/bin/env python3
"""Window Sizer: tray application and profile editor for KDE Plasma 6.

* Left-click on the tray icon opens the profile editor (changes are saved automatically).
* Right-click lists the saved profiles; choosing one applies it to the active window.
* Profiles live in ~/.config/window-sizer/profiles.json and are mirrored into
  kwinrc so the companion KWin script can show them in the window context menu.

Moving and resizing windows is done by KWin itself (the only approach that works
on both Wayland and X11): this program loads a small one-shot KWin script over
D-Bus (via busctl) that applies the profile.
"""
from __future__ import annotations

import argparse
import itertools
import json
import os
import re
import subprocess
import sys
import tempfile
from dataclasses import dataclass, field, replace
from pathlib import Path

from PySide6.QtCore import Qt, QTimer
from PySide6.QtGui import QAction, QColor, QGuiApplication, QIcon, QPainter, QPen, QPixmap
from PySide6.QtNetwork import QLocalServer, QLocalSocket
from PySide6.QtWidgets import (
    QAbstractItemView,
    QApplication,
    QComboBox,
    QFormLayout,
    QGroupBox,
    QHBoxLayout,
    QLabel,
    QLineEdit,
    QListWidget,
    QListWidgetItem,
    QMenu,
    QPushButton,
    QSpinBox,
    QSystemTrayIcon,
    QVBoxLayout,
    QWidget,
)

APP_NAME = "Window Sizer"
APP_ID = "window-sizer"

KWIN_SCRIPT_ID = "window-sizer"
KWIN_CONFIG_GROUP = f"Script-{KWIN_SCRIPT_ID}"
KWIN_CONFIG_KEY = "profiles"

_config_home = Path(os.environ.get("XDG_CONFIG_HOME") or Path.home() / ".config")
_data_home = Path(os.environ.get("XDG_DATA_HOME") or Path.home() / ".local" / "share")
PROFILES_FILE = _config_home / APP_ID / "profiles.json"
KWIN_SCRIPT_MAIN = _data_home / "kwin" / "scripts" / KWIN_SCRIPT_ID / "contents" / "code" / "main.js"

# Order matters: this is the order of the position drop-down.
POSITIONS = [
    ("top", "Top"),
    ("bottom", "Bottom"),
    ("left", "Left"),
    ("right", "Right"),
    ("top-left", "Top left"),
    ("top-right", "Top right"),
    ("bottom-left", "Bottom left"),
    ("bottom-right", "Bottom right"),
    ("center", "Center"),
    ("custom", "Custom"),
]
POSITION_KEYS = {key for key, _ in POSITIONS}

DEFAULT_WIDTH = 1024
DEFAULT_HEIGHT = 768

_uid_counter = itertools.count(1)


# --------------------------------------------------------------------------- #
# Profiles
# --------------------------------------------------------------------------- #

def _clamp_int(value, default: int, low: int, high: int) -> int:
    try:
        number = int(value)
    except (TypeError, ValueError):
        return default
    return max(low, min(high, number))


def unique_name(base: str, taken) -> str:
    """Return `base`, or `base 2`, `base 3`, ... so that it is not in `taken`.

    Comparison ignores case, so "Wide" and "wide" count as the same name.
    """
    used = {name.casefold() for name in taken}
    if base.casefold() not in used:
        return base
    counter = 2
    while f"{base} {counter}".casefold() in used:
        counter += 1
    return f"{base} {counter}"


@dataclass
class Profile:
    name: str = "New profile"
    width: int = DEFAULT_WIDTH
    height: int = DEFAULT_HEIGHT
    position: str = "center"   # one of POSITION_KEYS
    x: int = 0                 # used when position == "custom"
    y: int = 0
    # Runtime identity for the editor list (not persisted).
    uid: int = field(default_factory=lambda: next(_uid_counter), compare=False, repr=False)

    def to_dict(self) -> dict:
        return {
            "name": self.name,
            "width": self.width,
            "height": self.height,
            "position": self.position,
            "x": self.x,
            "y": self.y,
        }

    @classmethod
    def from_dict(cls, data: dict) -> "Profile":
        position = data.get("position")
        if position not in POSITION_KEYS:
            # Files written by earlier versions used { "mode": ..., "preset": ... }.
            legacy = "custom" if data.get("mode") == "custom" else data.get("preset")
            position = legacy if legacy in POSITION_KEYS else "center"
        return cls(
            name=str(data.get("name", "")),
            width=_clamp_int(data.get("width"), DEFAULT_WIDTH, 50, 20000),
            height=_clamp_int(data.get("height"), DEFAULT_HEIGHT, 50, 20000),
            position=position,
            x=_clamp_int(data.get("x"), 0, -20000, 20000),
            y=_clamp_int(data.get("y"), 0, -20000, 20000),
        )


class ProfileStore:
    """Reads and writes the profile list as JSON."""

    def __init__(self, path: Path = PROFILES_FILE) -> None:
        self.path = path

    def load(self) -> list[Profile]:
        try:
            data = json.loads(self.path.read_text(encoding="utf-8"))
            items = [item for item in data["profiles"] if isinstance(item, dict)]
        except FileNotFoundError:
            return []
        except (ValueError, KeyError, TypeError, OSError):
            # Corrupt file: keep a copy for inspection and start empty.
            try:
                self.path.replace(self.path.with_suffix(".json.bak"))
            except OSError:
                pass
            return []

        profiles: list[Profile] = []
        for item in items:
            profile = Profile.from_dict(item)
            # Names must be non-empty and unique, whatever the file contains.
            profile.name = unique_name(profile.name.strip() or "Unnamed profile",
                                       (p.name for p in profiles))
            profiles.append(profile)
        return profiles

    def save(self, profiles: list[Profile]) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        payload = {"version": 2, "profiles": [p.to_dict() for p in profiles]}
        tmp = self.path.with_suffix(".json.tmp")
        tmp.write_text(json.dumps(payload, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")
        os.replace(tmp, self.path)


# --------------------------------------------------------------------------- #
# KWin bridge
# --------------------------------------------------------------------------- #

_SHARED_RE = re.compile(r"// ==== BEGIN SHARED ====\n(.*?)// ==== END SHARED ====", re.S)

ONE_SHOT_TEMPLATE = """
(function () {
    const PROFILE = %(profile)s;

    function onCurrentDesktop(w) {
        try {
            const current = workspace.currentDesktop.id;
            return w.desktops.length === 0 || w.desktops.some(function (d) { return d.id === current; });
        } catch (e) {
            return true;
        }
    }
    function usable(w) {
        return w && w.normalWindow && !w.minimized && onCurrentDesktop(w);
    }

    // Prefer the active window. If the tray menu moved activation elsewhere
    // (for example to the panel), fall back to the topmost regular window.
    let target = workspace.activeWindow;
    if (!usable(target)) {
        target = null;
        const stack = workspace.stackingOrder;
        for (let i = stack.length - 1; i >= 0; i--) {
            if (usable(stack[i])) {
                target = stack[i];
                break;
            }
        }
    }
    if (!target) {
        console.warn("window-sizer: no window to resize");
        return;
    }
    try {
        placeWindow(target, PROFILE);
        console.warn("window-sizer: applied '" + PROFILE.name + "' to " + target.resourceClass);
    } catch (e) {
        console.warn("window-sizer: failed to apply profile: " + e);
    }
})();
"""


def _run(cmd: list[str], timeout: float = 5.0) -> tuple[bool, str]:
    try:
        result = subprocess.run(cmd, capture_output=True, text=True, timeout=timeout)
    except (OSError, subprocess.TimeoutExpired) as exc:
        return False, str(exc)
    return result.returncode == 0, (result.stdout or result.stderr).strip()


def _busctl(path: str, interface: str, method: str, signature: str = "", *args: str) -> tuple[bool, str]:
    cmd = ["busctl", "--user", "call", "org.kde.KWin", path, interface, method]
    if signature:
        cmd += [signature, *args]
    return _run(cmd)


def _find_shared_js() -> Path | None:
    here = Path(__file__).resolve().parent
    candidates = [
        here / "kwin_shared.js",
        here.parent / "kwin" / KWIN_SCRIPT_ID / "contents" / "code" / "main.js",
        KWIN_SCRIPT_MAIN,
    ]
    return next((c for c in candidates if c.is_file()), None)


class KWinBridge:
    """Talks to KWin through D-Bus (busctl) and kwinrc (kwriteconfig6)."""

    def __init__(self) -> None:
        self._counter = itertools.count(1)

    # -- configuration mirror ------------------------------------------------

    def _read_mirror(self) -> str:
        ok, out = _run(["kreadconfig6", "--file", "kwinrc", "--group", KWIN_CONFIG_GROUP,
                        "--key", KWIN_CONFIG_KEY])
        return out if ok else ""

    def _script_enabled(self) -> bool:
        ok, out = _run(["kreadconfig6", "--file", "kwinrc", "--group", "Plugins",
                        "--key", f"{KWIN_SCRIPT_ID}Enabled"])
        return ok and out.strip().lower() == "true"

    def sync_profiles(self, profiles: list[Profile]) -> str | None:
        """Mirror profiles into kwinrc and refresh the KWin script.

        Returns an error message, or None on success (including "nothing to do").
        """
        payload = json.dumps([p.to_dict() for p in profiles], separators=(",", ":"), ensure_ascii=False)
        if self._read_mirror() == payload:
            return None
        ok, out = _run(["kwriteconfig6", "--file", "kwinrc", "--group", KWIN_CONFIG_GROUP,
                        "--key", KWIN_CONFIG_KEY, payload])
        if not ok:
            return f"Could not write profiles to kwinrc: {out}"
        _busctl("/KWin", "org.kde.KWin", "reconfigure")
        if KWIN_SCRIPT_MAIN.is_file() and self._script_enabled():
            self._reload_installed_script()
        return None

    def _reload_installed_script(self) -> None:
        """Unload and load the installed script so it re-reads its configuration."""
        _busctl("/Scripting", "org.kde.kwin.Scripting", "unloadScript", "s", KWIN_SCRIPT_ID)
        self._load_and_run(KWIN_SCRIPT_MAIN, KWIN_SCRIPT_ID)

    # -- script loading ------------------------------------------------------

    def _load_and_run(self, path: Path, name: str) -> str | None:
        ok, out = _busctl("/Scripting", "org.kde.kwin.Scripting", "loadScript", "ss", str(path), name)
        match = re.search(r"\bi\s+(-?\d+)", out)
        if not ok or not match:
            return f"KWin refused to load the script: {out}"
        script_id = int(match.group(1))
        if script_id < 0:
            return "KWin reported that a script with this name is already loaded."
        # The object path of a loaded script differs between Plasma versions.
        for obj in (f"/Scripting/Script{script_id}", f"/{script_id}"):
            ok, _ = _busctl(obj, "org.kde.kwin.Script", "run")
            if ok:
                return None
        return "KWin loaded the script but could not run it."

    def apply_to_active_window(self, profile: Profile) -> str | None:
        shared_path = _find_shared_js()
        if shared_path is None:
            return "Could not find the shared KWin script code (kwin_shared.js)."
        match = _SHARED_RE.search(shared_path.read_text(encoding="utf-8"))
        if not match:
            return "The shared section is missing from the KWin script."

        source = match.group(1) + ONE_SHOT_TEMPLATE % {"profile": json.dumps(profile.to_dict(), ensure_ascii=False)}
        runtime = Path(os.environ.get("XDG_RUNTIME_DIR") or tempfile.gettempdir())
        serial = next(self._counter)
        name = f"window-sizer-oneshot-{os.getpid()}-{serial}"
        path = runtime / f"{name}.js"
        path.write_text(source, encoding="utf-8")

        error = self._load_and_run(path, name)

        def cleanup() -> None:
            _busctl("/Scripting", "org.kde.kwin.Scripting", "unloadScript", "s", name)
            path.unlink(missing_ok=True)

        QTimer.singleShot(2000, cleanup)
        return error


# --------------------------------------------------------------------------- #
# Icon
# --------------------------------------------------------------------------- #

def app_icon() -> QIcon:
    # "transform-scale" is the icon of the Resize entry in the window's
    # "More Actions" menu. The others are fallbacks for themes without it.
    for name in ("transform-scale", "preferences-system-windows", "view-restore", "window"):
        icon = QIcon.fromTheme(name)
        if not icon.isNull():
            return icon
    pixmap = QPixmap(64, 64)
    pixmap.fill(Qt.GlobalColor.transparent)
    painter = QPainter(pixmap)
    painter.setRenderHint(QPainter.RenderHint.Antialiasing)
    painter.setPen(QPen(QColor("#3daee9"), 5))
    painter.drawRoundedRect(8, 12, 48, 40, 5, 5)
    painter.drawLine(8, 24, 56, 24)
    painter.end()
    return QIcon(pixmap)


# --------------------------------------------------------------------------- #
# Editor window
# --------------------------------------------------------------------------- #

class EditorWindow(QWidget):
    """Profile editor. Every change is saved automatically (after a short pause)."""

    SAVE_DELAY_MS = 500

    def __init__(self, profiles: list[Profile], on_save) -> None:
        super().__init__()
        self._on_save = on_save
        self.profiles = [replace(p) for p in profiles]
        self._loading = False

        self._save_timer = QTimer(self)
        self._save_timer.setSingleShot(True)
        self._save_timer.setInterval(self.SAVE_DELAY_MS)
        self._save_timer.timeout.connect(self._do_save)

        self.setWindowTitle(f"{APP_NAME} \u2013 Profiles")
        self.setWindowIcon(app_icon())
        self.resize(720, 420)
        self._build_ui()
        self._rebuild_list()

    # -- construction --------------------------------------------------------

    def _build_ui(self) -> None:
        root = QVBoxLayout(self)
        body = QHBoxLayout()
        root.addLayout(body, 1)

        # Left: profile list and list actions.
        left = QVBoxLayout()
        body.addLayout(left, 2)
        left.addWidget(QLabel("Profiles (drag to reorder)"))
        self.list = QListWidget()
        self.list.setDragDropMode(QAbstractItemView.DragDropMode.InternalMove)
        self.list.setDefaultDropAction(Qt.DropAction.MoveAction)
        self.list.setSelectionMode(QAbstractItemView.SelectionMode.SingleSelection)
        self.list.currentRowChanged.connect(self._on_selection_changed)
        self.list.model().rowsMoved.connect(self._on_rows_moved)
        left.addWidget(self.list, 1)

        buttons = QHBoxLayout()
        left.addLayout(buttons)
        self.add_button = self._button("Add", "list-add", self._add)
        self.delete_button = self._button("Delete", "list-remove", self._delete)
        buttons.addWidget(self.add_button)
        buttons.addWidget(self.delete_button)

        # Right: form for the selected profile. The pane itself always keeps its
        # space; the form inside it is hidden while no profile exists.
        right_pane = QWidget()
        body.addWidget(right_pane, 3)
        right_layout = QVBoxLayout(right_pane)
        right_layout.setContentsMargins(0, 0, 0, 0)
        self.form_pane = QWidget()
        right_layout.addWidget(self.form_pane)
        right_layout.addStretch(1)
        # Close sits at the bottom of the right pane, level with Add and Delete.
        close_row = QHBoxLayout()
        close_row.addStretch(1)
        close_button = QPushButton("Close")
        close_button.clicked.connect(self.close)
        close_row.addWidget(close_button)
        right_layout.addLayout(close_row)
        self.close_button = close_button
        pane_layout = QVBoxLayout(self.form_pane)
        pane_layout.setContentsMargins(0, 0, 0, 0)
        # Caption above the box, left-aligned like "Profiles (drag to reorder)".
        pane_layout.addWidget(QLabel("Selected profile"))
        form_box = QGroupBox()
        pane_layout.addWidget(form_box)
        form_layout = QVBoxLayout(form_box)
        form = QFormLayout()
        self.form = form
        form_layout.addLayout(form)

        name_box = QVBoxLayout()
        self.name_edit = QLineEdit()
        self.name_edit.textChanged.connect(self._on_name_changed)
        self.name_edit.editingFinished.connect(self._on_name_finished)
        self.name_error = QLabel()
        self.name_error.setStyleSheet("color: #da4453;")   # Breeze "negative" red
        self.name_error.setWordWrap(True)
        self.name_error.hide()
        name_box.addWidget(self.name_edit)
        name_box.addWidget(self.name_error)
        form.addRow("Name", name_box)

        # Every field is a direct row of the form, so all labels and fields
        # share the same two columns and line up.
        self.width_spin = self._spin(50, 20000)
        self.height_spin = self._spin(50, 20000)
        form.addRow("Width", self.width_spin)
        form.addRow("Height", self.height_spin)

        self.position_combo = QComboBox()
        for key, label in POSITIONS:
            self.position_combo.addItem(label, key)
        self.position_combo.currentIndexChanged.connect(self._on_form_changed)
        form.addRow("Position", self.position_combo)

        # X and Y are rows of their own below the drop-down, shown only when
        # "Custom" is selected.
        self.x_spin = self._spin(-20000, 20000)
        self.y_spin = self._spin(-20000, 20000)
        form.addRow("X", self.x_spin)
        form.addRow("Y", self.y_spin)

        form_layout.addStretch(1)

    def _button(self, text: str, icon_name: str, slot) -> QPushButton:
        button = QPushButton(text)
        icon = QIcon.fromTheme(icon_name)
        if not icon.isNull():
            button.setIcon(icon)
        button.clicked.connect(slot)
        return button

    def _spin(self, low: int, high: int) -> QSpinBox:
        spin = QSpinBox()
        spin.setRange(low, high)
        spin.setSuffix(" px")
        spin.setFixedWidth(130)
        spin.valueChanged.connect(self._on_form_changed)
        return spin

    # -- saving --------------------------------------------------------------

    def _schedule_save(self) -> None:
        self._save_timer.start()   # restarts the countdown on every change

    def _do_save(self) -> None:
        self._save_timer.stop()
        self._on_save([replace(p) for p in self.profiles])

    def flush(self) -> None:
        """Save immediately if a change is waiting for the autosave timer."""
        if self._save_timer.isActive():
            self._do_save()

    # -- state helpers -------------------------------------------------------

    def _current(self) -> Profile | None:
        item = self.list.currentItem()
        if item is None:
            return None
        uid = item.data(Qt.ItemDataRole.UserRole)
        return next((p for p in self.profiles if p.uid == uid), None)

    def _name_error(self, text: str, profile: Profile) -> str | None:
        name = text.strip()
        if not name:
            return "The name cannot be empty."
        others = {p.name.casefold() for p in self.profiles if p.uid != profile.uid}
        if name.casefold() in others:
            return "Another profile already uses this name."
        return None

    def _rebuild_list(self, select_uid: int | None = None) -> None:
        self.list.blockSignals(True)
        self.list.clear()
        row = 0
        for index, profile in enumerate(self.profiles):
            item = QListWidgetItem(profile.name)
            item.setData(Qt.ItemDataRole.UserRole, profile.uid)
            self.list.addItem(item)
            if profile.uid == select_uid:
                row = index
        self.list.blockSignals(False)
        if self.profiles:
            self.list.setCurrentRow(row)
        self._on_selection_changed(self.list.currentRow())

    def _update_enabled(self) -> None:
        has_profile = self._current() is not None
        self.form_pane.setVisible(has_profile)
        self.delete_button.setEnabled(has_profile)
        show_custom = has_profile and self.position_combo.currentData() == "custom"
        self.form.setRowVisible(self.x_spin, show_custom)
        self.form.setRowVisible(self.y_spin, show_custom)

    # -- slots ---------------------------------------------------------------

    def _on_selection_changed(self, _row: int) -> None:
        profile = self._current()
        self._loading = True
        self.name_error.hide()
        if profile is not None:
            self.name_edit.setText(profile.name)
            self.width_spin.setValue(profile.width)
            self.height_spin.setValue(profile.height)
            self.position_combo.setCurrentIndex(max(0, self.position_combo.findData(profile.position)))
            self.x_spin.setValue(profile.x)
            self.y_spin.setValue(profile.y)
        else:
            self.name_edit.clear()
            self.width_spin.setValue(DEFAULT_WIDTH)
            self.height_spin.setValue(DEFAULT_HEIGHT)
            self.position_combo.setCurrentIndex(self.position_combo.findData("center"))
            self.x_spin.setValue(0)
            self.y_spin.setValue(0)
        self._loading = False
        self._update_enabled()

    def _on_name_changed(self, text: str) -> None:
        if self._loading:
            return
        profile = self._current()
        if profile is None:
            return
        error = self._name_error(text, profile)
        if error:
            # Keep the last valid name until the text becomes valid again.
            self.name_error.setText(error)
            self.name_error.show()
            return
        self.name_error.hide()
        profile.name = text.strip()
        self.list.currentItem().setText(profile.name)
        self._schedule_save()

    def _on_name_finished(self) -> None:
        """When editing ends on an invalid name, restore the last valid one."""
        profile = self._current()
        if profile is None or not self._name_error(self.name_edit.text(), profile):
            return
        self._loading = True
        self.name_edit.setText(profile.name)
        self._loading = False
        self.name_error.hide()

    def _on_form_changed(self, *_args) -> None:
        if self._loading:
            return
        profile = self._current()
        if profile is None:
            return
        profile.width = self.width_spin.value()
        profile.height = self.height_spin.value()
        profile.position = self.position_combo.currentData() or "center"
        profile.x = self.x_spin.value()
        profile.y = self.y_spin.value()
        self._update_enabled()
        self._schedule_save()

    def _on_rows_moved(self, *_args) -> None:
        by_uid = {p.uid: p for p in self.profiles}
        order = [self.list.item(i).data(Qt.ItemDataRole.UserRole) for i in range(self.list.count())]
        self.profiles = [by_uid[uid] for uid in order if uid in by_uid]
        self._schedule_save()

    def _add(self) -> None:
        profile = Profile(name=unique_name("New profile", (p.name for p in self.profiles)))
        self.profiles.append(profile)
        self._rebuild_list(select_uid=profile.uid)
        self._schedule_save()
        self.name_edit.setFocus()
        self.name_edit.selectAll()

    def _delete(self) -> None:
        row = self.list.currentRow()
        if not 0 <= row < len(self.profiles):
            return
        del self.profiles[row]
        select = self.profiles[min(row, len(self.profiles) - 1)].uid if self.profiles else None
        self._rebuild_list(select_uid=select)
        self._schedule_save()

    # -- closing -------------------------------------------------------------

    def closeEvent(self, event) -> None:  # noqa: N802 (Qt naming)
        self.flush()
        event.ignore()          # the application keeps running in the tray
        self.hide()


# --------------------------------------------------------------------------- #
# Tray application
# --------------------------------------------------------------------------- #

class TrayApp:
    def __init__(self, app: QApplication) -> None:
        self.app = app
        self.store = ProfileStore()
        self.bridge = KWinBridge()
        self.profiles = self.store.load()

        self.editor = EditorWindow(self.profiles, self.save_profiles)
        app.aboutToQuit.connect(self.editor.flush)

        self.menu = QMenu()
        self.tray = QSystemTrayIcon(app_icon())
        self.tray.setToolTip(APP_NAME)
        self.tray.setContextMenu(self.menu)
        self.tray.activated.connect(self._on_activated)
        self.rebuild_menu()
        self.tray.show()

        # Make sure the KWin side matches the saved profiles (no-op if it already does).
        QTimer.singleShot(0, self._initial_sync)

    # -- profiles ------------------------------------------------------------

    def _initial_sync(self) -> None:
        self._report(self.bridge.sync_profiles(self.profiles))

    def save_profiles(self, profiles: list[Profile]) -> None:
        self.profiles = profiles
        try:
            self.store.save(profiles)
        except OSError as exc:
            self._report(f"Could not save profiles: {exc}")
            return
        self._report(self.bridge.sync_profiles(profiles))
        self.rebuild_menu()

    def apply_profile(self, profile: Profile) -> None:
        self._report(self.bridge.apply_to_active_window(profile))

    # -- UI ------------------------------------------------------------------

    def rebuild_menu(self) -> None:
        self.menu.clear()
        if self.profiles:
            for profile in self.profiles:
                action = QAction(profile.name, self.menu)
                action.triggered.connect(lambda _checked=False, p=profile: self.apply_profile(p))
                self.menu.addAction(action)
        else:
            empty = QAction("(No profiles)", self.menu)
            empty.setEnabled(False)
            self.menu.addAction(empty)
        self.menu.addSeparator()
        self.menu.addAction("Edit profiles\u2026", self.show_editor)
        self.menu.addAction("Quit", self.quit)

    def show_editor(self) -> None:
        self.editor.show()
        self.editor.raise_()
        self.editor.activateWindow()

    def quit(self) -> None:
        self.editor.flush()
        self.app.quit()

    def _on_activated(self, reason: QSystemTrayIcon.ActivationReason) -> None:
        if reason == QSystemTrayIcon.ActivationReason.Trigger:   # left click
            self.show_editor()

    def _report(self, message: str | None) -> None:
        if message:
            print(f"{APP_ID}: {message}", file=sys.stderr)
            self.tray.showMessage(APP_NAME, message, QSystemTrayIcon.MessageIcon.Warning, 6000)


# --------------------------------------------------------------------------- #
# Entry point
# --------------------------------------------------------------------------- #

def acquire_single_instance(open_editor: bool) -> QLocalServer | None:
    """Return a listening server if this is the first instance, otherwise None
    after asking the running instance to show its editor (when requested)."""
    name = f"{APP_ID}-{os.getuid()}"
    socket = QLocalSocket()
    socket.connectToServer(name)
    if socket.waitForConnected(300):
        socket.write(b"editor" if open_editor else b"ping")
        socket.flush()
        socket.waitForBytesWritten(300)
        socket.disconnectFromServer()
        return None
    QLocalServer.removeServer(name)   # clear a stale socket file
    server = QLocalServer()
    server.listen(name)
    return server


def main() -> int:
    parser = argparse.ArgumentParser(description="Window Sizer tray application")
    parser.add_argument("--editor", action="store_true", help="open the profile editor on startup")
    args = parser.parse_args()

    QGuiApplication.setDesktopFileName(APP_ID)
    app = QApplication(sys.argv[:1])
    app.setApplicationName(APP_NAME)
    app.setQuitOnLastWindowClosed(False)

    server = acquire_single_instance(args.editor)
    if server is None:
        return 0   # another instance is running and has been notified

    tray_app = TrayApp(app)

    def on_connection() -> None:
        connection = server.nextPendingConnection()
        if connection is None:
            return
        connection.waitForReadyRead(200)
        if b"editor" in bytes(connection.readAll()):
            tray_app.show_editor()
        connection.disconnectFromServer()

    server.newConnection.connect(on_connection)

    if args.editor or not QSystemTrayIcon.isSystemTrayAvailable():
        if not QSystemTrayIcon.isSystemTrayAvailable():
            print(f"{APP_ID}: no system tray available; running with the editor only", file=sys.stderr)
        tray_app.show_editor()

    return app.exec()


if __name__ == "__main__":
    sys.exit(main())
