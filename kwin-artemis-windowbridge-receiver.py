#!/usr/bin/env python3
"""KWin -> Artemis bridge: active window info + Night Light state.

Window half
    The KWin script calls WindowChanged(json) on org.kde.kwin.WindowBridge
    (/org/kde/kwin/WindowBridge) on every activation/title change; the payload is
    forwarded to Artemis' JSON module `kwin_window`.

Night Light half
    KWin publishes its Night Light state on org.kde.KWin
    /org/kde/KWin/NightLight. `currentTemperature` is the *animated* value, so it
    already reflects a transition in progress: while a transition runs KWin emits
    PropertiesChanged ~13x/s in 50 K steps. That state (plus derived values such as
    `warmth`, `colorHex` and the `mulRed/Green/Blue` byte-space tint multipliers
    for a multiply-blend layer) is published to the Artemis JSON module
    `kwin_nightlight`, throttled to one POST per 80 ms.

    Read-only on purpose: the temperature, schedule and enabled state belong to
    KWin and the Plasma applet, so nothing here writes to them.

D-Bus (same service object as the window bridge)
    GetNightLightState() -> s
    signal NightLightChanged(s)
"""

import configparser
import json
import math
import os
import threading
import time
from datetime import datetime
from pathlib import Path

import dbus
import dbus.mainloop.glib
import dbus.service
import requests
from gi.repository import GLib

BRIDGE_VERSION = "2.2.0"

ARTEMIS_BASE_URL = "http://localhost:9696"
WINDOW_MODULE_ID = "kwin_window"
NIGHTLIGHT_MODULE_ID = "kwin_nightlight"
WINDOW_SCHEMA_URL = f"{ARTEMIS_BASE_URL}/json-modules/{WINDOW_MODULE_ID}/schema"
WINDOW_DATA_URL = f"{ARTEMIS_BASE_URL}/json-modules/{WINDOW_MODULE_ID}/data"
NIGHTLIGHT_SCHEMA_URL = f"{ARTEMIS_BASE_URL}/json-modules/{NIGHTLIGHT_MODULE_ID}/schema"
NIGHTLIGHT_DATA_URL = f"{ARTEMIS_BASE_URL}/json-modules/{NIGHTLIGHT_MODULE_ID}/data"
JSON_MODULES_URL = f"{ARTEMIS_BASE_URL}/json-modules"

KWIN_SERVICE = "org.kde.KWin"
NIGHTLIGHT_PATH = "/org/kde/KWin/NightLight"
NIGHTLIGHT_IFACE = "org.kde.KWin.NightLight"
PROPS_IFACE = "org.freedesktop.DBus.Properties"

MIN_TEMPERATURE = 1000
MAX_TEMPERATURE = 6500
DEFAULT_DAY_TEMPERATURE = 6500
DEFAULT_NIGHT_TEMPERATURE = 4500

# Night Light is a gain applied to *linear* light, but the bytes Artemis passes
# around are gamma encoded: a linear gain g reaches the wire as g**(1/gamma).
# 2.2 is the curve KWin itself uses on both ends (the table is decoded with
# gamma22 and its sRGB description is gamma22, not the piecewise sRGB curve),
# so the byte-space factor is table**(1/2.2), not the raw table entry.
# KWB_TINT_GAMMA overrides the exponent for fitting a specific strip.
TINT_GAMMA = float(os.environ.get("KWB_TINT_GAMMA", "2.2"))

# KWin emits currentTemperature every ~70 ms during a transition; one Artemis
# POST per 80 ms keeps the module in sync without flooding the web server.
PUBLISH_INTERVAL_MS = 80
HEARTBEAT_SECONDS = 5

CONFIG_HOME = Path(os.environ.get("XDG_CONFIG_HOME") or (Path.home() / ".config"))
KWINRC = CONFIG_HOME / "kwinrc"

WINDOW_SCHEMA = {
    "$schema": "http://json-schema.org/draft-04/schema#",
    "title": "KWin Window Information",
    "description": "Full window data from KWin window manager",
    "type": "object",
    "properties": {
        # === BASIC WINDOW INFO ===
        "caption": {
            "type": "string",
            "title": "Window Title",
            "description": "The title/caption displayed in the window"
        },
        "captionNormal": {
            "type": "string",
            "title": "Normal Caption",
            "description": "The normal caption without modifications"
        },
        "resourceName": {
            "type": "string",
            "title": "Resource Name",
            "description": "X11 resource name (usually executable name)"
        },
        "resourceClass": {
            "type": "string",
            "title": "Resource Class",
            "description": "X11 resource class (usually application name)"
        },
        "windowRole": {
            "type": "string",
            "title": "Window Role",
            "description": "X11 window role hint"
        },
        "desktopFileName": {
            "type": "string",
            "title": "Desktop File",
            "description": "Associated .desktop file name"
        },
        "internalId": {
            "type": "string",
            "title": "Internal ID",
            "description": "KWin's internal UUID for this window"
        },
        "pid": {
            "type": "integer",
            "title": "Process ID",
            "description": "Process ID of the application"
        },
        "stackingOrder": {
            "type": "integer",
            "title": "Stacking Order",
            "description": "Z-order position in the window stack"
        },

        # === WINDOW STATE ===
        "active": {
            "type": "boolean",
            "title": "Active",
            "description": "Whether this window currently has focus"
        },
        "managed": {
            "type": "boolean",
            "title": "Managed",
            "description": "Whether KWin manages this window"
        },
        "deleted": {
            "type": "boolean",
            "title": "Deleted",
            "description": "Whether this window has been deleted"
        },
        "fullScreen": {
            "type": "boolean",
            "title": "Full Screen",
            "description": "Whether the window is in fullscreen mode"
        },
        "minimized": {
            "type": "boolean",
            "title": "Minimized",
            "description": "Whether the window is minimized"
        },
        "shade": {
            "type": "boolean",
            "title": "Shaded",
            "description": "Whether the window is shaded (rolled up)"
        },
        "hidden": {
            "type": "boolean",
            "title": "Hidden",
            "description": "Whether the window is hidden"
        },

        # === WINDOW BEHAVIOR ===
        "keepAbove": {
            "type": "boolean",
            "title": "Keep Above",
            "description": "Whether the window is set to stay above others"
        },
        "keepBelow": {
            "type": "boolean",
            "title": "Keep Below",
            "description": "Whether the window is set to stay below others"
        },
        "onAllDesktops": {
            "type": "boolean",
            "title": "On All Desktops",
            "description": "Whether the window appears on all virtual desktops"
        },
        "skipTaskbar": {
            "type": "boolean",
            "title": "Skip Taskbar",
            "description": "Whether the window is excluded from the taskbar"
        },
        "skipPager": {
            "type": "boolean",
            "title": "Skip Pager",
            "description": "Whether the window is excluded from the pager"
        },
        "skipSwitcher": {
            "type": "boolean",
            "title": "Skip Switcher",
            "description": "Whether the window is excluded from Alt+Tab"
        },

        # === WINDOW CAPABILITIES ===
        "closeable": {
            "type": "boolean",
            "title": "Closeable",
            "description": "Whether the window can be closed"
        },
        "minimizable": {
            "type": "boolean",
            "title": "Minimizable",
            "description": "Whether the window can be minimized"
        },
        "maximizable": {
            "type": "boolean",
            "title": "Maximizable",
            "description": "Whether the window can be maximized"
        },
        "moveable": {
            "type": "boolean",
            "title": "Moveable",
            "description": "Whether the window can be moved"
        },
        "resizeable": {
            "type": "boolean",
            "title": "Resizeable",
            "description": "Whether the window can be resized"
        },

        # === WINDOW TYPES ===
        "normalWindow": {
            "type": "boolean",
            "title": "Normal Window",
            "description": "Whether this is a normal application window"
        },
        "dialog": {
            "type": "boolean",
            "title": "Dialog",
            "description": "Whether this is a dialog window"
        },
        "notification": {
            "type": "boolean",
            "title": "Notification",
            "description": "Whether this is a notification window"
        },
        "dock": {
            "type": "boolean",
            "title": "Dock/Panel",
            "description": "Whether this is a dock or panel window"
        },
        "tooltip": {
            "type": "boolean",
            "title": "Tooltip",
            "description": "Whether this is a tooltip window"
        },

        # === INTERACTION STATE ===
        "move": {
            "type": "boolean",
            "title": "Being Moved",
            "description": "Whether the window is currently being moved"
        },
        "resize": {
            "type": "boolean",
            "title": "Being Resized",
            "description": "Whether the window is currently being resized"
        },
        "modal": {
            "type": "boolean",
            "title": "Modal",
            "description": "Whether this is a modal window"
        },
        "demandsAttention": {
            "type": "boolean",
            "title": "Demands Attention",
            "description": "Whether the window is demanding attention"
        },
        "unresponsive": {
            "type": "boolean",
            "title": "Unresponsive",
            "description": "Whether the application is unresponsive"
        },

        # === APPEARANCE ===
        "opacity": {
            "type": "number",
            "title": "Opacity",
            "description": "Window opacity (0.0 to 1.0)"
        },
        "noBorder": {
            "type": "boolean",
            "title": "No Border",
            "description": "Whether the window has no decoration border"
        },

        # === FRAME GEOMETRY ===
        "frameX": {
            "type": "integer",
            "title": "Frame X",
            "description": "Frame X coordinate"
        },
        "frameY": {
            "type": "integer",
            "title": "Frame Y",
            "description": "Frame Y coordinate"
        },
        "frameWidth": {
            "type": "integer",
            "title": "Frame Width",
            "description": "Frame width including decorations"
        },
        "frameHeight": {
            "type": "integer",
            "title": "Frame Height",
            "description": "Frame height including decorations"
        },

        # === CLIENT GEOMETRY ===
        "clientX": {
            "type": "integer",
            "title": "Client X",
            "description": "Client area X coordinate"
        },
        "clientY": {
            "type": "integer",
            "title": "Client Y",
            "description": "Client area Y coordinate"
        },
        "clientWidth": {
            "type": "integer",
            "title": "Client Width",
            "description": "Client area width (content only)"
        },
        "clientHeight": {
            "type": "integer",
            "title": "Client Height",
            "description": "Client area height (content only)"
        },

        # === POSITION & SIZE ===
        "x": {
            "type": "integer",
            "title": "X Position",
            "description": "Window X position"
        },
        "y": {
            "type": "integer",
            "title": "Y Position",
            "description": "Window Y position"
        },
        "width": {
            "type": "integer",
            "title": "Width",
            "description": "Window width"
        },
        "height": {
            "type": "integer",
            "title": "Height",
            "description": "Window height"
        },

        # === OUTPUT INFO ===
        "outputName": {
            "type": "string",
            "title": "Output Name",
            "description": "Name of the output/monitor this window is on"
        },
        "outputManufacturer": {
            "type": "string",
            "title": "Output Manufacturer",
            "description": "Manufacturer of the output/monitor"
        },
        "outputModel": {
            "type": "string",
            "title": "Output Model",
            "description": "Model of the output/monitor"
        },
        "outputWidth": {
            "type": "integer",
            "title": "Output Width",
            "description": "Width of the output/monitor"
        },
        "outputHeight": {
            "type": "integer",
            "title": "Output Height",
            "description": "Height of the output/monitor"
        },

        # === DESKTOP INFO ===
        "desktopCount": {
            "type": "integer",
            "title": "Desktop Count",
            "description": "Number of virtual desktops this window is on"
        },
        "desktopNames": {
            "type": "string",
            "title": "Desktop Names",
            "description": "Names of virtual desktops (comma-separated)"
        },
        "activityCount": {
            "type": "integer",
            "title": "Activity Count",
            "description": "Number of activities this window is on"
        }
    },
    "required": ["caption", "active", "pid"]
}

NIGHTLIGHT_SCHEMA = {
    "$schema": "http://json-schema.org/draft-04/schema#",
    "title": "KWin Night Light",
    "description": "KWin Night Light state, including the animated current colour temperature",
    "type": "object",
    "properties": {
        "available": {
            "type": "boolean",
            "title": "Available",
            "description": "Whether KWin exposes the Night Light D-Bus interface"
        },
        "enabled": {
            "type": "boolean",
            "title": "Enabled",
            "description": "Night Light enabled in KWin settings ([NightColor] Active)"
        },
        "running": {
            "type": "boolean",
            "title": "Running",
            "description": "Night Light is currently applying a temperature"
        },
        "active": {
            "type": "boolean",
            "title": "Active",
            "description": "Enabled, running and not inhibited"
        },
        "inhibited": {
            "type": "boolean",
            "title": "Inhibited",
            "description": "Temporarily blocked, e.g. by fullscreen video"
        },
        "daylight": {
            "type": "boolean",
            "title": "Daylight",
            "description": "Night Light currently targets the day temperature"
        },
        "mode": {
            "type": "integer",
            "title": "Mode",
            "description": "0 = constant, 1 = darklight (schedule based)"
        },
        "modeName": {
            "type": "string",
            "title": "Mode Name",
            "description": "constant or darklight"
        },
        "currentTemperature": {
            "type": "integer",
            "title": "Current Temperature (K)",
            "description": "Live animated colour temperature, follows transitions"
        },
        "targetTemperature": {
            "type": "integer",
            "title": "Target Temperature (K)",
            "description": "Temperature the transition is heading to"
        },
        "dayTemperature": {
            "type": "integer",
            "title": "Day Temperature (K)",
            "description": "Configured day temperature ([NightColor] DayTemperature)"
        },
        "nightTemperature": {
            "type": "integer",
            "title": "Night Temperature (K)",
            "description": "Configured night temperature ([NightColor] NightTemperature)"
        },
        "warmth": {
            "type": "number",
            "title": "Warmth (0 day .. 1 night)",
            "description": "Derived: 0 at the day temperature, 1 at the configured night temperature"
        },
        "colorHex": {
            "type": "string",
            "title": "Approximate Colour",
            "description": "Blackbody colour of currentTemperature as #RRGGBB"
        },
        "colorRed": {
            "type": "integer",
            "title": "Colour Red",
            "description": "Red channel of colorHex (0-255)"
        },
        "colorGreen": {
            "type": "integer",
            "title": "Colour Green",
            "description": "Green channel of colorHex (0-255)"
        },
        "colorBlue": {
            "type": "integer",
            "title": "Colour Blue",
            "description": "Blue channel of colorHex (0-255)"
        },
        "gainRed": {
            "type": "number",
            "title": "Gain Red (0..1)",
            "description": "KWin's exact red channel factor at currentTemperature"
        },
        "gainGreen": {
            "type": "number",
            "title": "Gain Green (0..1)",
            "description": "KWin's exact green channel factor at currentTemperature"
        },
        "gainBlue": {
            "type": "number",
            "title": "Gain Blue (0..1)",
            "description": "KWin's exact blue channel factor at currentTemperature"
        },
        "mulRed": {
            "type": "integer",
            "title": "Tint Red",
            "description": "Red multiplier for a multiply-blend layer: 255 * gainRed^(1/tintGamma). "
                           "Gains are linear-light, encoded bytes need the root."
        },
        "mulGreen": {
            "type": "integer",
            "title": "Tint Green",
            "description": "Green multiplier for a multiply-blend layer: 255 * gainGreen^(1/tintGamma)"
        },
        "mulBlue": {
            "type": "integer",
            "title": "Tint Blue",
            "description": "Blue multiplier for a multiply-blend layer: 255 * gainBlue^(1/tintGamma)"
        },
        "tintGamma": {
            "type": "number",
            "title": "Tint Gamma",
            "description": "Exponent used for mulRed/mulGreen/mulBlue (KWB_TINT_GAMMA, default 2.2)"
        },
        "transitionActive": {
            "type": "boolean",
            "title": "Transition Active",
            "description": "currentTemperature has not reached targetTemperature yet"
        },
        "transitionRemainingMs": {
            "type": "integer",
            "title": "Transition Remaining (ms)",
            "description": "Estimated time left in the running transition"
        },
        "previousTransitionDateTime": {
            "type": "integer",
            "title": "Previous Transition Start",
            "description": "Unix time the last/current transition started (0 when none)"
        },
        "previousTransitionDuration": {
            "type": "integer",
            "title": "Previous Transition Duration (ms)",
            "description": "Length of the last/current transition"
        },
        "scheduledTransitionDateTime": {
            "type": "integer",
            "title": "Next Transition Start",
            "description": "Unix time the next scheduled transition starts (0 when none)"
        },
        "scheduledTransitionDuration": {
            "type": "integer",
            "title": "Next Transition Duration (ms)",
            "description": "Length of the next scheduled transition"
        },
        "updatedAt": {
            "type": "string",
            "title": "Updated At",
            "description": "Time this payload was built (ISO 8601)"
        }
    },
    "required": ["currentTemperature", "warmth"]
}


def log(msg):
    print(f"[{datetime.now().strftime('%H:%M:%S')}] {msg}", flush=True)


def clamp(value, low, high):
    return max(low, min(high, value))


# KWin's white-point table: src/3rdparty/colortemperature.h (Ingo Thies 2013, the
# redshift colorramp), 1000-6500 K in 100 K steps, linear interpolation between
# entries.
#
# The entries are gamma-corrected numbers and KWin does not multiply pixels by
# them: it decodes them (gamma 2.2) to move the output's white point, and the
# compositor then converts into that description with a Bradford chromatic
# adaptation. The net change to the panel's encoded bytes is much gentler than
# the raw entry. Measured against the panel, the byte-space multiplier is
# entry**(1/2.2): 2500 K behaves like a linear gain of 0.644, so the wire sees
# 209/255 green, not 164/255. Treating the entry as an encoded multiplier (the
# obvious reading) comes out 2.2x too warm.
#
# colorRed/Green/Blue publish the raw entries; mulRed/Green/Blue publish the
# byte-space multipliers a multiply-blend layer needs.
BLACKBODY_1000_6500 = (
    (1.00000000, 0.18172716, 0.00000000),  # 1000 K
    (1.00000000, 0.25503671, 0.00000000),  # 1100 K
    (1.00000000, 0.30942099, 0.00000000),  # 1200 K
    (1.00000000, 0.35357379, 0.00000000),  # 1300 K
    (1.00000000, 0.39091524, 0.00000000),  # 1400 K
    (1.00000000, 0.42322816, 0.00000000),  # 1500 K
    (1.00000000, 0.45159884, 0.00000000),  # 1600 K
    (1.00000000, 0.47675916, 0.00000000),  # 1700 K
    (1.00000000, 0.49923747, 0.00000000),  # 1800 K
    (1.00000000, 0.51943421, 0.00000000),  # 1900 K
    (1.00000000, 0.54360078, 0.08679949),  # 2000 K
    (1.00000000, 0.56618736, 0.14065513),  # 2100 K
    (1.00000000, 0.58734976, 0.18362641),  # 2200 K
    (1.00000000, 0.60724493, 0.22137978),  # 2300 K
    (1.00000000, 0.62600248, 0.25591950),  # 2400 K
    (1.00000000, 0.64373109, 0.28819679),  # 2500 K
    (1.00000000, 0.66052319, 0.31873863),  # 2600 K
    (1.00000000, 0.67645822, 0.34786758),  # 2700 K
    (1.00000000, 0.69160518, 0.37579588),  # 2800 K
    (1.00000000, 0.70602449, 0.40267128),  # 2900 K
    (1.00000000, 0.71976951, 0.42860152),  # 3000 K
    (1.00000000, 0.73288760, 0.45366838),  # 3100 K
    (1.00000000, 0.74542112, 0.47793608),  # 3200 K
    (1.00000000, 0.75740814, 0.50145662),  # 3300 K
    (1.00000000, 0.76888303, 0.52427322),  # 3400 K
    (1.00000000, 0.77987699, 0.54642268),  # 3500 K
    (1.00000000, 0.79041843, 0.56793692),  # 3600 K
    (1.00000000, 0.80053332, 0.58884417),  # 3700 K
    (1.00000000, 0.81024551, 0.60916971),  # 3800 K
    (1.00000000, 0.81957693, 0.62893653),  # 3900 K
    (1.00000000, 0.82854786, 0.64816570),  # 4000 K
    (1.00000000, 0.83717703, 0.66687674),  # 4100 K
    (1.00000000, 0.84548188, 0.68508786),  # 4200 K
    (1.00000000, 0.85347859, 0.70281616),  # 4300 K
    (1.00000000, 0.86118227, 0.72007777),  # 4400 K
    (1.00000000, 0.86860704, 0.73688797),  # 4500 K
    (1.00000000, 0.87576611, 0.75326132),  # 4600 K
    (1.00000000, 0.88267187, 0.76921169),  # 4700 K
    (1.00000000, 0.88933596, 0.78475236),  # 4800 K
    (1.00000000, 0.89576933, 0.79989606),  # 4900 K
    (1.00000000, 0.90198230, 0.81465502),  # 5000 K
    (1.00000000, 0.90963069, 0.82838210),  # 5100 K
    (1.00000000, 0.91710889, 0.84190889),  # 5200 K
    (1.00000000, 0.92441842, 0.85523742),  # 5300 K
    (1.00000000, 0.93156127, 0.86836903),  # 5400 K
    (1.00000000, 0.93853986, 0.88130458),  # 5500 K
    (1.00000000, 0.94535695, 0.89404470),  # 5600 K
    (1.00000000, 0.95201559, 0.90658983),  # 5700 K
    (1.00000000, 0.95851906, 0.91894041),  # 5800 K
    (1.00000000, 0.96487079, 0.93109690),  # 5900 K
    (1.00000000, 0.97107439, 0.94305985),  # 6000 K
    (1.00000000, 0.97713351, 0.95482993),  # 6100 K
    (1.00000000, 0.98305189, 0.96640795),  # 6200 K
    (1.00000000, 0.98883326, 0.97779486),  # 6300 K
    (1.00000000, 0.99448139, 0.98899179),  # 6400 K
    (1.00000000, 1.00000000, 1.00000000),  # 6500 K
)


def kelvin_to_rgb(kelvin):
    """KWin-exact white point (0..1 channel factors) for a colour temperature."""
    if kelvin >= MAX_TEMPERATURE:
        return (1.0, 1.0, 1.0)
    kelvin = clamp(int(kelvin), MIN_TEMPERATURE, MAX_TEMPERATURE)
    index = (kelvin - MIN_TEMPERATURE) // 100
    blend = (kelvin % 100) / 100.0
    low = BLACKBODY_1000_6500[index]
    high = BLACKBODY_1000_6500[index + 1]
    return tuple(low[c] * (1.0 - blend) + high[c] * blend for c in range(3))

def read_nightcolor_config():
    """Read [NightColor] from kwinrc. KWin reloads it through KConfigWatcher."""
    values = {
        "enabled": False,
        "mode": 1,
        "dayTemperature": DEFAULT_DAY_TEMPERATURE,
        "nightTemperature": DEFAULT_NIGHT_TEMPERATURE,
    }
    if not KWINRC.exists():
        return values
    parser = configparser.RawConfigParser(strict=False, interpolation=None)
    parser.optionxform = str  # keep KConfig key case
    try:
        parser.read(KWINRC)
    except Exception:
        return values
    if not parser.has_section("NightColor"):
        return values

    def raw(key, default):
        return parser.get("NightColor", key, fallback=default)

    try:
        values["enabled"] = str(raw("Active", "false")).strip().lower() in ("1", "true", "yes", "on")
    except Exception:
        pass
    try:
        values["mode"] = 0 if int(raw("Mode", 1)) == 0 else 1
    except Exception:
        pass
    try:
        values["dayTemperature"] = clamp(int(raw("DayTemperature", DEFAULT_DAY_TEMPERATURE)),
                                         MIN_TEMPERATURE, MAX_TEMPERATURE)
    except Exception:
        pass
    try:
        values["nightTemperature"] = clamp(int(raw("NightTemperature", DEFAULT_NIGHT_TEMPERATURE)),
                                           MIN_TEMPERATURE, MAX_TEMPERATURE)
    except Exception:
        pass
    return values


class NightLight:
    """Reads KWin Night Light state; never writes to it."""

    def __init__(self, bus, on_publish):
        self._bus = bus
        self._on_publish = on_publish
        self._props = {"available": False}
        self._config = read_nightcolor_config()
        self._config_stamp = self._config_mtime()
        self._flush_source = 0
        self._last_signature = None
        self._owner_initialized = False
        self._kwin_obj = bus.get_object(KWIN_SERVICE, NIGHTLIGHT_PATH, introspect=False)
        self._props_iface = dbus.Interface(self._kwin_obj, PROPS_IFACE)
        bus.watch_name_owner(KWIN_SERVICE, self._on_kwin_owner_changed)
        bus.add_signal_receiver(
            self._on_properties_changed,
            signal_name="PropertiesChanged",
            dbus_interface=PROPS_IFACE,
            path=NIGHTLIGHT_PATH,
            bus_name=KWIN_SERVICE,
        )
        self.refresh()

    # ---------- reading ----------

    @staticmethod
    def _config_mtime():
        try:
            st = KWINRC.stat()
            return (st.st_mtime_ns, st.st_size)
        except OSError:
            return None

    def _reload_config(self, force=False):
        stamp = self._config_mtime()
        if force or stamp != self._config_stamp:
            self._config = read_nightcolor_config()
            self._config_stamp = stamp
        return self._config

    def refresh(self):
        try:
            raw = self._props_iface.GetAll(NIGHTLIGHT_IFACE)
            self._props = {str(k): v for k, v in raw.items()}
            self._props["available"] = True
        except dbus.DBusException as exc:
            self._props = {"available": False}
            log(f"⚠️ Night Light D-Bus read failed: {exc.get_dbus_name() or exc}")
        self._reload_config(force=True)
        self._publish()

    def _on_kwin_owner_changed(self, owner):
        if not self._owner_initialized:
            # watch_name_owner fires once for the current owner; refresh() already ran.
            self._owner_initialized = True
            return
        if owner:
            log("🔁 KWin restarted, re-reading Night Light state")
            self.refresh()

    def _on_properties_changed(self, interface_name, changed_properties, invalidated_properties):
        if str(interface_name) != NIGHTLIGHT_IFACE:
            return
        for key, value in changed_properties.items():
            self._props[str(key)] = value
        self._props["available"] = True
        self._schedule_publish()

    def _schedule_publish(self):
        if self._flush_source:
            return
        self._flush_source = GLib.timeout_add(PUBLISH_INTERVAL_MS, self._flush)

    def _flush(self):
        self._flush_source = 0
        self._publish()
        return False

    def _int_prop(self, name, default=0):
        try:
            return int(self._props.get(name, default))
        except (TypeError, ValueError):
            return default

    def _bool_prop(self, name, default=False):
        value = self._props.get(name, default)
        try:
            return bool(value)
        except Exception:
            return default

    def state(self):
        cfg = self._reload_config()
        current = self._int_prop("currentTemperature", cfg["nightTemperature"])
        target = self._int_prop("targetTemperature", current)
        day = clamp(int(cfg["dayTemperature"]), MIN_TEMPERATURE, MAX_TEMPERATURE)
        night = clamp(int(cfg["nightTemperature"]), MIN_TEMPERATURE, MAX_TEMPERATURE)
        enabled = self._bool_prop("enabled", cfg["enabled"])
        running = self._bool_prop("running", False)
        inhibited = self._bool_prop("inhibited", False)
        daylight = self._bool_prop("daylight", False)

        span = day - night
        warmth = clamp((day - current) / span, 0.0, 1.0) if span >= 100 else 0.0
        gain_red, gain_green, gain_blue = kelvin_to_rgb(current)
        red = int(gain_red * 255.0 + 0.5)
        green = int(gain_green * 255.0 + 0.5)
        blue = int(gain_blue * 255.0 + 0.5)
        # Byte-space equivalents of the linear gains, for a multiply-blend layer.
        mul_red = int(clamp(gain_red ** (1.0 / TINT_GAMMA) * 255.0 + 0.5, 0, 255))
        mul_green = int(clamp(gain_green ** (1.0 / TINT_GAMMA) * 255.0 + 0.5, 0, 255))
        mul_blue = int(clamp(gain_blue ** (1.0 / TINT_GAMMA) * 255.0 + 0.5, 0, 255))

        transition_active = current != target
        remaining = 0
        if transition_active:
            started = self._int_prop("previousTransitionDateTime", 0)
            duration = self._int_prop("previousTransitionDuration", 0)
            if started and duration:
                elapsed_ms = (time.time() - started) * 1000.0
                remaining = int(clamp(duration - elapsed_ms, 0, duration))
            else:
                remaining = 0

        return {
            "available": self._bool_prop("available", False),
            "enabled": enabled,
            "running": running,
            "active": enabled and running and not inhibited,
            "inhibited": inhibited,
            "daylight": daylight,
            "mode": int(cfg["mode"]),
            "modeName": "constant" if int(cfg["mode"]) == 0 else "darklight",
            "currentTemperature": current,
            "targetTemperature": target,
            "dayTemperature": day,
            "nightTemperature": night,
            "warmth": round(warmth, 4),
            "colorHex": f"#{red:02X}{green:02X}{blue:02X}",
            "colorRed": red,
            "colorGreen": green,
            "colorBlue": blue,
            "gainRed": round(gain_red, 6),
            "gainGreen": round(gain_green, 6),
            "gainBlue": round(gain_blue, 6),
            "mulRed": mul_red,
            "mulGreen": mul_green,
            "mulBlue": mul_blue,
            "tintGamma": TINT_GAMMA,
            "transitionActive": transition_active,
            "transitionRemainingMs": remaining,
            "previousTransitionDateTime": self._int_prop("previousTransitionDateTime", 0),
            "previousTransitionDuration": self._int_prop("previousTransitionDuration", 0),
            "scheduledTransitionDateTime": self._int_prop("scheduledTransitionDateTime", 0),
            "scheduledTransitionDuration": self._int_prop("scheduledTransitionDuration", 0),
            "updatedAt": datetime.now().astimezone().isoformat(timespec="seconds"),
        }

    def _publish(self):
        state = self.state()
        try:
            self._on_publish(state)
        except Exception as exc:  # noqa: BLE001 - publishing must never kill the watcher
            log(f"❌ Night Light publish error: {exc}")
        signature = (
            state["enabled"], state["running"], state["inhibited"], state["mode"],
            state["targetTemperature"], state["daylight"], state["previousTransitionDateTime"],
        )
        if signature != self._last_signature:
            self._last_signature = signature
            log(
                "🌙 Night Light: enabled={} running={} inhibited={} mode={} target={}K "
                "current={}K daylight={}".format(
                    state["enabled"], state["running"], state["inhibited"], state["modeName"],
                    state["targetTemperature"], state["currentTemperature"], state["daylight"]
                )
            )


class ArtemisPublisher(threading.Thread):
    """Latest-wins background sender so a slow Artemis never blocks D-Bus."""

    def __init__(self):
        super().__init__(name="artemis-publisher", daemon=True)
        self._cond = threading.Condition()
        self._payload = None
        self._stop = False
        self._last_error = 0.0
        self._error_reported = None

    def publish(self, payload):
        with self._cond:
            self._payload = payload
            self._cond.notify()

    def stop(self):
        with self._cond:
            self._stop = True
            self._cond.notify()

    def run(self):
        while True:
            with self._cond:
                while self._payload is None and not self._stop:
                    self._cond.wait()
                if self._stop:
                    return
                payload, self._payload = self._payload, None
            try:
                response = requests.post(NIGHTLIGHT_DATA_URL, json=payload, timeout=2)
                if response.status_code not in (200, 201, 204):
                    raise RuntimeError(f"HTTP {response.status_code}: {response.text[:200]}")
            except Exception as exc:  # noqa: BLE001 - keep retrying, log sparingly
                now = time.time()
                if now - self._last_error > 30 or str(exc) != self._error_reported:
                    log(f"❌ Artemis night light POST failed: {exc}")
                    self._last_error = now
                    self._error_reported = str(exc)


class KWinBridge(dbus.service.Object):
    def __init__(self, bus, nightlight, publisher):
        bus_name = dbus.service.BusName("org.kde.kwin.WindowBridge", bus=bus)
        super().__init__(bus_name, "/org/kde/kwin/WindowBridge")
        self.nightlight = nightlight
        self.publisher = publisher
        log("🎧 D-Bus service ready")

    @dbus.service.method('org.kde.kwin.WindowBridge', in_signature='s', out_signature='')
    def WindowChanged(self, json_data):
        try:
            window_data = json.loads(json_data)
            caption = window_data.get('caption', 'Unknown')
            log(f"📨 Window: {caption}")
            resp = requests.post(WINDOW_DATA_URL, json=window_data, timeout=2)
            if resp.status_code in [200, 201, 204]:
                log(f"✅ Sent to Artemis ({resp.status_code})")
            else:
                log(f"❌ Artemis error ({resp.status_code}): {resp.text}")
        except Exception as e:
            log(f"❌ Error: {e}")

    @dbus.service.signal('org.kde.kwin.WindowBridge', signature='s')
    def NightLightChanged(self, json_data):
        pass

    def _publish_nightlight_state(self, state):
        self.publisher.publish(state)
        try:
            self.NightLightChanged(json.dumps(state))
        except dbus.DBusException as exc:
            log(f"⚠️ Night Light signal failed: {exc.get_dbus_name() or exc}")

    @dbus.service.method('org.kde.kwin.WindowBridge', in_signature='', out_signature='s')
    def GetNightLightState(self):
        return json.dumps(self.nightlight.state())


def register_schema(url, schema):
    try:
        response = requests.post(url, json=schema, timeout=5)
        if response.status_code in (200, 201):
            return True
        log(f"❌ Schema registration failed ({url}): {response.status_code} {response.text[:200]}")
    except Exception as exc:  # noqa: BLE001
        log(f"❌ Schema registration error ({url}): {exc}")
    return False


def module_exists(module_id):
    try:
        response = requests.get(JSON_MODULES_URL, timeout=5)
        if response.status_code != 200:
            return False, False
        modules = response.json()
        return True, any(m.get("ModuleId") == module_id for m in modules)
    except Exception:  # noqa: BLE001 - Artemis not up yet
        return False, False


def main():
    log(f"🚀 Starting KWin → Artemis bridge {BRIDGE_VERSION}")
    dbus.mainloop.glib.DBusGMainLoop(set_as_default=True)
    bus = dbus.SessionBus()

    publisher = ArtemisPublisher()
    publisher.start()

    state = {"window_schema_ok": False, "nightlight_schema_ok": False}
    module_log = {"at": 0.0}

    def ensure_modules(force_nightlight=False):
        up, has_window = module_exists(WINDOW_MODULE_ID)
        if not up:
            if time.time() - module_log["at"] > 30:
                module_log["at"] = time.time()
                log("🕒 Artemis not reachable yet, retrying every 5 s")
            return False
        if not state["window_schema_ok"]:
            if has_window:
                state["window_schema_ok"] = True
                log(f"✅ Schema exists for '{WINDOW_MODULE_ID}'")
            else:
                state["window_schema_ok"] = register_schema(WINDOW_SCHEMA_URL, WINDOW_SCHEMA)
        if force_nightlight or not state["nightlight_schema_ok"]:
            # Always (re)register at startup: this is what adds/updates the schema
            # fields used by the Artemis data model.
            state["nightlight_schema_ok"] = register_schema(NIGHTLIGHT_SCHEMA_URL, NIGHTLIGHT_SCHEMA)
            if state["nightlight_schema_ok"]:
                log(f"✅ Schema registered for '{NIGHTLIGHT_MODULE_ID}'")
        return state["window_schema_ok"] and state["nightlight_schema_ok"]

    nightlight = NightLight(bus, on_publish=lambda payload: None)
    service = KWinBridge(bus, nightlight, publisher)
    nightlight._on_publish = service._publish_nightlight_state  # wire after service exists

    ensure_modules(force_nightlight=True)
    publisher.publish(nightlight.state())

    def heartbeat():
        if not ensure_modules():
            return True
        publisher.publish(nightlight.state())
        return True

    GLib.timeout_add_seconds(HEARTBEAT_SECONDS, heartbeat)

    log("✅ KWin Artemis bridge ready")
    loop = GLib.MainLoop()
    try:
        loop.run()
    except KeyboardInterrupt:
        log("👋 Stopping.")
    finally:
        publisher.stop()


if __name__ == '__main__':
    main()
