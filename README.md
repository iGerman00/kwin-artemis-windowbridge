# KWin Artemis WindowBridge

KWin 6 ↔ Artemis RGB bridge. The focused window and the Night Light state go into Artemis as JSON modules; the Night Light temperature can be driven back over HTTP or D-Bus.

```
KWin script ──WindowChanged(json)──▶ receiver ──HTTP──▶ Artemis (kwin_window, kwin_nightlight)
KWin NightLight D-Bus ──PropertiesChanged──▶ receiver
                     HTTP 127.0.0.1:8484 ────┘  set temperature / enabled / mode
```

- `metadata.json`, `contents/code/main.js` — KWin script. Emits a 59-field snapshot of the active window on activation and on caption, caption-normal, minimized and fullscreen changes.
- `kwin-artemis-windowbridge-receiver.py` — owns the `org.kde.kwin.WindowBridge` D-Bus name, forwards windows to Artemis, watches `org.kde.KWin.NightLight`, publishes its state and serves the control API.

## Requirements

- KDE Plasma 6 / KWin 6
- `python3` with `dbus-python`, `PyGObject` (`gi`), `requests`
- Artemis RGB with the Web API plugin enabled (`http://localhost:9696`)

## Install

```sh
./install.sh            # --no-reload skips the KWin reload
```

Installs the KWin script to `~/.local/share/kwin/scripts/kwin-artemis-windowbridge`, the receiver to `~/.scripts/` (override with `KWB_RECEIVER_DIR`), writes an autostart entry, enables the script in `kwinrc` and reloads it.

## Artemis data

`kwin_window` — the window snapshot: caption, resourceName/Class, pid, geometry, output, flags, desktops.

`kwin_nightlight` — Night Light state. `currentTemperature` is KWin's *animated* value: it follows transitions in 50 K steps (~13 updates/s), so no interpolation is needed. Derived fields:

- `warmth` — 0 at the day temperature, 1 at the configured night temperature
- `colorHex`, `colorRed`/`colorGreen`/`colorBlue` — blackbody colour of `currentTemperature`
- `enabled`, `running`, `active`, `inhibited`, `daylight`, `modeName`, `targetTemperature`, `dayTemperature`, `nightTemperature`, `transitionActive`, `transitionRemainingMs`, transition timestamps, `previewActive`

## Control

Base URL `http://127.0.0.1:8484` (override with `KWB_NIGHTLIGHT_PORT`).

| Call | Effect |
| --- | --- |
| `GET /nightlight` | state |
| `POST /nightlight {"temperature": 3000}` | persistent `[NightColor] NightTemperature` |
| `POST /nightlight {"enabled": false}` | `[NightColor] Active` |
| `POST /nightlight {"mode": "constant"}` | `0` = constant, `1` = darklight |
| `POST /nightlight {"temperature": 3000, "applyNow": true}` | constant + enabled + temperature |
| `POST /nightlight/preview {"temperature": 3000}` | 15 s preview, then KWin reverts |
| `POST /nightlight/preview/stop` | cancel preview |
| `GET /health` | liveness |

D-Bus equivalents on `/org/kde/kwin/WindowBridge`: `GetNightLightState`, `SetNightLightTemperature`, `PreviewNightLightTemperature`, `StopNightLightPreview`, signal `NightLightChanged`.

## Notes

- KWin has no D-Bus setter for the configured temperature. Persistent changes write `kwinrc [NightColor]` with `kwriteconfig6 --notify`; without `--notify` KWin keeps its cached settings and nothing moves.
- In `darklight` mode the night temperature only applies at night; use `applyNow` (or `mode=constant`) to force it now.
- `preview()` shows a "Color Temperature Preview" OSD and reverts after 15 s.
- KWin scripts cannot subscribe to D-Bus properties and have no timers, so the Night Light half lives in the receiver.
