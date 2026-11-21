// Window Sizer - KWin script (Plasma 6)
//
// Adds a "Window Size" submenu to the window context menu (title bar and
// taskbar entry). Profiles are read from kwinrc:
//
//     [Script-window-sizer]
//     profiles=<JSON array>
//
// The tray application writes that value. With no profiles, no menu entry is
// shown.
//
// The section between BEGIN SHARED and END SHARED is also reused by the tray
// application, which prepends it to a one-shot script when a profile is applied
// from the tray menu. Keep that section free of anything that depends on
// registerUserActionsMenu() or readConfig().

const VERSION = "1.1.0";

// Number of non-breaking spaces appended to the submenu label. KWin does not
// reserve room for the submenu arrow on script-provided entries, so without
// padding the arrow overlaps the text. Increase if the overlap persists.
const MENU_LABEL_PADDING = 6;

// ==== BEGIN SHARED ====
// Rectangle of the whole screen the window is on (panels included).
function screenRect(window) {
    try {
        const g = window.output.geometry;
        if (g && g.width > 0) {
            return g;
        }
    } catch (e) { }
    return workspace.clientArea(KWin.FullScreenArea, window);
}

// Resize and move a window according to a profile:
//   { width, height, position, x, y }
// where position is one of: top, bottom, left, right, top-left, top-right,
// bottom-left, bottom-right, center, custom.
//
// Sizes are frame sizes (title bar and borders included). Named positions are
// inside the work area (the screen minus panels). For "custom", x/y are
// measured from the top-left corner of the screen the window is on.
function placeWindow(window, profile) {
    if (!window || !window.normalWindow) {
        return false;
    }
    if (window.fullScreen) {
        console.warn("window-sizer: skipping fullscreen window");
        return false;
    }
    try {
        if (window.tile) {
            window.tile = null;
        }
    } catch (e) { }
    window.setMaximize(false, false);

    const area = workspace.clientArea(KWin.PlacementArea, window);
    const current = window.frameGeometry;

    // Fixed-size windows keep their size and are only moved.
    let w = current.width;
    let h = current.height;
    if (window.resizeable) {
        w = Math.max(Math.min(profile.width, area.width), window.minSize.width);
        h = Math.max(Math.min(profile.height, area.height), window.minSize.height);
    }

    let x;
    let y;
    if (profile.position === "custom") {
        const screen = screenRect(window);
        x = screen.x + profile.x;
        y = screen.y + profile.y;
    } else {
        // Centering matches KWin's built-in "center window": the leftover space
        // is halved with integer division, so an odd remainder puts the window
        // one pixel up/left instead of down/right.
        const left = area.x;
        const right = area.x + area.width - w;
        const centerX = area.x + Math.floor((area.width - w) / 2);
        const top = area.y;
        const bottom = area.y + area.height - h;
        const centerY = area.y + Math.floor((area.height - h) / 2);
        switch (profile.position) {
        case "top":          x = centerX; y = top;     break;
        case "bottom":       x = centerX; y = bottom;  break;
        case "left":         x = left;    y = centerY; break;
        case "right":        x = right;   y = centerY; break;
        case "top-left":     x = left;    y = top;     break;
        case "top-right":    x = right;   y = top;     break;
        case "bottom-left":  x = left;    y = bottom;  break;
        case "bottom-right": x = right;   y = bottom;  break;
        default:             x = centerX; y = centerY; break; // "center"
        }
    }

    window.frameGeometry = {
        x: Math.round(x),
        y: Math.round(y),
        width: Math.round(w),
        height: Math.round(h)
    };
    return true;
}
// ==== END SHARED ====

function normalizeProfile(p) {
    if (!p || typeof p !== "object") {
        return null;
    }
    const width = Number(p.width);
    const height = Number(p.height);
    if (!(width > 0) || !(height > 0)) {
        return null;
    }
    // Older versions stored { mode, preset } instead of { position }.
    const position = p.position || (p.mode === "custom" ? "custom" : p.preset) || "center";
    return {
        name: String(p.name === undefined ? "Unnamed profile" : p.name),
        width: width,
        height: height,
        position: String(position),
        x: Number(p.x) || 0,
        y: Number(p.y) || 0
    };
}

// Read on every menu open, so edits made in the tray application show up
// without reloading the script (when KWin re-reads kwinrc; the tray application
// also triggers a reload as a fallback).
function loadProfiles() {
    const raw = readConfig("profiles", "");
    if (!raw) {
        return [];
    }
    try {
        const parsed = JSON.parse(raw);
        if (Array.isArray(parsed)) {
            return parsed.map(normalizeProfile).filter(function (p) { return p !== null; });
        }
    } catch (e) {
        console.warn("window-sizer: invalid profiles in kwinrc: " + e);
    }
    return [];
}

// "&" starts a keyboard mnemonic in Qt menus; double it to show it literally.
function menuText(name) {
    return String(name).replace(/&/g, "&&");
}

registerUserActionsMenu(function (window) {
    if (!window.normalWindow || !window.resizeable) {
        return; // no entry for docks, dialogs of fixed size, etc.
    }
    const profiles = loadProfiles();
    if (profiles.length === 0) {
        return;
    }
    return {
        text: "Window Size" + "\u00A0".repeat(MENU_LABEL_PADDING),
        items: profiles.map(function (profile) {
            return {
                text: menuText(profile.name),
                triggered: function () {
                    try {
                        placeWindow(window, profile);
                    } catch (e) {
                        console.warn("window-sizer: failed to apply '" + profile.name + "': " + e);
                    }
                }
            };
        })
    };
});

console.warn("window-sizer: loaded v" + VERSION + " (" + loadProfiles().length + " profiles)");
