#!/usr/bin/env python3
"""KWin -> Artemis bridge: active window info + Night Light state/control.

Window half (unchanged)
    The KWin script calls WindowChanged(json) on org.kde.kwin.WindowBridge
    (/org/kde/kwin/WindowBridge) on every activation/title change; the payload is
    forwarded to Artemis' JSON module `kwin_window`.

Night Light half (new in 2.0)
    KWin publishes its Night Light state on org.kde.KWin
    /org/kde/KWin/NightLight. `currentTemperature` is the *animated* value, so it
    already reflects a transition in progress: while a transition runs KWin emits
    PropertiesChanged ~13x/s in 50 K steps. That state (plus derived values such as
    `warmth` and `colorHex`) is published to the Artemis JSON module
    `kwin_nightlight`, throttled to one POST per 80 ms.

    KWin's D-Bus interface has no setter for the configured temperature, so:
      - persistent change  -> write kwinrc [NightColor] (KConfigWatcher reloads it
                              in KWin and quick-adjusts the screen)
      - momentary change   -> NightLight.preview(K), which KWin reverts after 15 s
                              and which also shows a "Color Temperature Preview" OSD

Control surface (loopback only)
    HTTP 127.0.0.1:8484 (override with KWB_NIGHTLIGHT_PORT)
        GET  /nightlight                      -> state
        POST /nightlight                      {"temperature": 3000}
             keys: temperature | nightTemperature  (1000-6500 K)
                   enabled                        (bool, [NightColor] Active)
                   mode                           ("constant" | "darklight")
                   applyNow                       (bool: mode=constant + enabled,
                                                   so the temperature applies now)
             -> {"ok": true, "applied": bool, "wrote": {...}, "state": {...}}
        POST /nightlight/preview              {"temperature": 3000}   # 15 s
        POST /nightlight/preview/stop
        GET  /health

    D-Bus (same service object as the window bridge)
        GetNightLightState() -> s
        SetNightLightTemperature(u) -> s
        PreviewNightLightTemperature(u)
        StopNightLightPreview()
        signal NightLightChanged(s)

    Range note: KWin clamps the temperature to 1000..6500 K. In "darklight" mode
    the configured night temperature only takes effect at night (day target is
    DayTemperature); pass applyNow/mode=constant to force it immediately.
"""

import configparser
import json
import math
import os
import subprocess
import threading
import time
from datetime import datetime
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

import dbus
import dbus.mainloop.glib
import dbus.service
import requests
from gi.repository import GLib

BRIDGE_VERSION = "2.0.0"

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
PREVIEW_SECONDS = 15

# KWin emits currentTemperature every ~70 ms during a transition; one Artemis
# POST per 80 ms keeps the module in sync without flooding the web server.
PUBLISH_INTERVAL_MS = 80
HEARTBEAT_SECONDS = 5

CONTROL_HOST = "127.0.0.1"
CONTROL_PORT = int(os.environ.get("KWB_NIGHTLIGHT_PORT", "8484"))

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
        "previewActive": {
            "type": "boolean",
            "title": "Preview Active",
            "description": "A preview requested through this bridge is running"
        },
        "previewTemperature": {
            "type": "integer",
            "title": "Preview Temperature (K)",
            "description": "Temperature of the running preview (0 when none)"
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


def kelvin_to_rgb(kelvin):
    """Tanner Helland blackbody approximation, normalised to a full-scale channel."""
    t = clamp(kelvin, MIN_TEMPERATURE, MAX_TEMPERATURE) / 100.0
    if t <= 66:
        red = 255.0
        green = 99.4708025861 * math.log(t) - 161.1195681661
        blue = 0.0 if t <= 19 else 138.5177312231 * math.log(t - 10) - 305.0447927307
    else:
        red = 329.698727446 * ((t - 60) ** -0.1332047592)
        green = 288.1221695283 * ((t - 60) ** -0.0755148492)
        blue = 255.0
    return tuple(int(clamp(round(c), 0, 255)) for c in (red, green, blue))


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


def run_in_mainloop(fn, *args, timeout=6.0):
    """Execute fn on the GLib main loop (where the D-Bus connection lives)."""
    box = {}
    done = threading.Event()

    def job():
        try:
            box["value"] = fn(*args)
        except BaseException as exc:  # noqa: BLE001 - forwarded to the caller
            box["error"] = exc
        finally:
            done.set()
        return False

    GLib.idle_add(job)
    if not done.wait(timeout):
        raise TimeoutError("main loop did not answer in time")
    if "error" in box:
        raise box["error"]
    return box["value"]


class NightLight:
    """Reads KWin Night Light state and applies temperature changes."""

    def __init__(self, bus, on_publish):
        self._bus = bus
        self._on_publish = on_publish
        self._props = {"available": False}
        self._config = read_nightcolor_config()
        self._config_stamp = self._config_mtime()
        self._preview_temperature = 0
        self._preview_until = 0.0
        self._flush_source = 0
        self._last_signature = None
        self._owner_initialized = False
        self._kwin_obj = bus.get_object(KWIN_SERVICE, NIGHTLIGHT_PATH, introspect=False)
        self._props_iface = dbus.Interface(self._kwin_obj, PROPS_IFACE)
        self._nl_iface = dbus.Interface(self._kwin_obj, NIGHTLIGHT_IFACE)
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

    def preview_active(self):
        return self._preview_temperature > 0 and time.time() < self._preview_until

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
        red, green, blue = kelvin_to_rgb(current)

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

        preview_active = self.preview_active()
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
            "transitionActive": transition_active,
            "transitionRemainingMs": remaining,
            "previousTransitionDateTime": self._int_prop("previousTransitionDateTime", 0),
            "previousTransitionDuration": self._int_prop("previousTransitionDuration", 0),
            "scheduledTransitionDateTime": self._int_prop("scheduledTransitionDateTime", 0),
            "scheduledTransitionDuration": self._int_prop("scheduledTransitionDuration", 0),
            "previewActive": preview_active,
            "previewTemperature": self._preview_temperature if preview_active else 0,
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

    # ---------- control ----------

    def _write_config(self, key, value):
        try:
            result = subprocess.run(
                ["kwriteconfig6", "--file", "kwinrc", "--group", "NightColor",
                 "--key", key, str(value), "--notify"],
                capture_output=True, text=True, timeout=5,
            )
        except FileNotFoundError as exc:
            raise RuntimeError("kwriteconfig6 not found") from exc
        except subprocess.TimeoutExpired as exc:
            raise RuntimeError("kwriteconfig6 timed out") from exc
        if result.returncode != 0:
            raise RuntimeError(
                f"kwriteconfig6 failed for {key}: {(result.stderr or result.stdout).strip()}"
            )

    def _live_prop(self, name):
        """Read a property straight from KWin (not the signal-fed cache)."""
        return int(self._props_iface.Get(NIGHTLIGHT_IFACE, name))

    def _wait_for_target(self, expected_target, timeout=2.0):
        """Wait until KWin accepted the new target; the animation continues after."""
        deadline = time.time() + timeout
        while time.time() < deadline:
            try:
                if self._live_prop("targetTemperature") == expected_target:
                    return True
            except dbus.DBusException:
                return False
            time.sleep(0.05)
        return False

    def apply(self, temperature=None, enabled=None, mode=None, apply_now=False):
        """Persistent change: writes [NightColor] and lets KWin animate to it.

        kwriteconfig6 --notify makes KWin's KConfigWatcher re-read kwinrc; without
        --notify the file changes but KWin keeps its cached settings.
        """
        writes = []
        if mode is not None:
            if mode not in ("constant", "darklight"):
                raise ValueError("mode must be 'constant' or 'darklight'")
            writes.append(("Mode", 0 if mode == "constant" else 1))
        if apply_now:
            writes.append(("Mode", 0))
            writes.append(("Active", "true"))
        if temperature is not None:
            writes.append(("NightTemperature", int(temperature)))
        if enabled is not None:
            writes.append(("Active", "true" if enabled else "false"))
        if not writes:
            raise ValueError("nothing to set: pass temperature, enabled, mode or applyNow")

        merged = {}
        for key, value in writes:
            merged[key] = value
        for key, value in merged.items():
            self._write_config(key, value)
        self._reload_config(force=True)

        if temperature is not None:
            applied = self._wait_for_target(int(temperature))
        else:
            time.sleep(0.25)
            applied = True
        self.refresh()
        state = self.state()
        result = {"applied": applied, "wrote": merged, "state": state}
        if temperature is not None and not applied:
            result["note"] = (
                "Night Light is in daylight, so the night temperature takes effect at the "
                "next transition; pass applyNow (or mode=constant) to force it now."
            )
        return result

    def preview(self, temperature):
        temperature = clamp(int(temperature), MIN_TEMPERATURE, MAX_TEMPERATURE)
        self._nl_iface.preview(dbus.UInt32(temperature))
        self._preview_temperature = temperature
        self._preview_until = time.time() + PREVIEW_SECONDS
        GLib.timeout_add_seconds(PREVIEW_SECONDS + 1, self._clear_preview_flag)
        GLib.timeout_add(PUBLISH_INTERVAL_MS, self._flush_once)
        return {"previewTemperature": temperature, "previewSeconds": PREVIEW_SECONDS,
                "state": self.state()}

    def stop_preview(self):
        self._nl_iface.stopPreview()
        self._clear_preview_flag()
        GLib.timeout_add(PUBLISH_INTERVAL_MS, self._flush_once)
        return {"state": self.state()}

    def _clear_preview_flag(self):
        self._preview_temperature = 0
        self._preview_until = 0.0
        return False

    def _flush_once(self):
        self._publish()
        return False


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

    @dbus.service.method('org.kde.kwin.WindowBridge', in_signature='u', out_signature='s')
    def SetNightLightTemperature(self, temperature):
        return json.dumps(self.nightlight.apply(temperature=int(temperature)))

    @dbus.service.method('org.kde.kwin.WindowBridge', in_signature='u', out_signature='')
    def PreviewNightLightTemperature(self, temperature):
        self.nightlight.preview(int(temperature))

    @dbus.service.method('org.kde.kwin.WindowBridge', in_signature='', out_signature='')
    def StopNightLightPreview(self):
        self.nightlight.stop_preview()


class ControlHandler(BaseHTTPRequestHandler):
    server_version = f"kwin-artemis-bridge/{BRIDGE_VERSION}"
    protocol_version = "HTTP/1.1"

    def log_message(self, fmt, *args):  # noqa: A003 - silence per-request logging
        pass

    def _send(self, code, payload):
        body = json.dumps(payload).encode()
        self.send_response(code)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def _read_json(self):
        length = int(self.headers.get("Content-Length") or 0)
        if length <= 0:
            return {}
        if length > 65536:
            raise ValueError("request body too large")
        raw = self.rfile.read(length)
        if not raw.strip():
            return {}
        return json.loads(raw)

    def _nightlight(self):
        return self.server.nightlight  # type: ignore[attr-defined]

    def do_GET(self):
        path = self.path.split("?", 1)[0].rstrip("/") or "/"
        try:
            if path == "/health":
                self._send(200, {"ok": True, "version": BRIDGE_VERSION,
                                 "port": CONTROL_PORT,
                                 "nightlight": run_in_mainloop(self._nightlight().state)})
            elif path == "/nightlight":
                self._send(200, run_in_mainloop(self._nightlight().state))
            else:
                self._send(404, {"ok": False, "error": f"unknown path {path}"})
        except Exception as exc:  # noqa: BLE001
            self._send(500, {"ok": False, "error": str(exc)})

    def do_POST(self):
        path = self.path.split("?", 1)[0].rstrip("/")
        try:
            body = self._read_json()
            nightlight = self._nightlight()
            if path == "/nightlight":
                if "temperature" in body and "nightTemperature" in body:
                    raise ValueError("pass either temperature or nightTemperature, not both")
                temperature = body.get("nightTemperature", body.get("temperature"))
                if temperature is not None:
                    temperature = int(temperature)
                    if not MIN_TEMPERATURE <= temperature <= MAX_TEMPERATURE:
                        raise ValueError(
                            f"temperature must be {MIN_TEMPERATURE}..{MAX_TEMPERATURE} K"
                        )
                enabled = body.get("enabled")
                mode = body.get("mode")
                apply_now = bool(body.get("applyNow", body.get("apply_now", False)))

                def do_apply():
                    return nightlight.apply(
                        temperature=temperature,
                        enabled=None if enabled is None else bool(enabled),
                        mode=mode,
                        apply_now=apply_now,
                    )

                result = run_in_mainloop(do_apply, timeout=15)
                result["ok"] = True
                self._send(200, result)
            elif path == "/nightlight/preview":
                if "temperature" not in body:
                    raise ValueError("pass temperature")
                result = run_in_mainloop(nightlight.preview, int(body["temperature"]))
                result["ok"] = True
                self._send(200, result)
            elif path in ("/nightlight/preview/stop", "/nightlight/stop-preview"):
                result = run_in_mainloop(nightlight.stop_preview)
                result["ok"] = True
                self._send(200, result)
            else:
                self._send(404, {"ok": False, "error": f"unknown path {path}"})
        except (ValueError, TypeError) as exc:
            self._send(400, {"ok": False, "error": str(exc)})
        except Exception as exc:  # noqa: BLE001
            self._send(500, {"ok": False, "error": str(exc)})


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

    try:
        server = ThreadingHTTPServer((CONTROL_HOST, CONTROL_PORT), ControlHandler)
        server.daemon_threads = True
        server.nightlight = nightlight  # type: ignore[attr-defined]
        threading.Thread(target=server.serve_forever, name="nightlight-http", daemon=True).start()
        log(f"🌐 Night Light control API on http://{CONTROL_HOST}:{CONTROL_PORT}/nightlight")
    except OSError as exc:
        log(f"❌ HTTP control server unavailable on port {CONTROL_PORT}: {exc}")

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
