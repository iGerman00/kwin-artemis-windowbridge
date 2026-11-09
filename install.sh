#!/usr/bin/env bash
# Install KWin Artemis WindowBridge: KWin script, D-Bus receiver, autostart entry.
set -euo pipefail

SRC="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd)"
DATA_HOME="${XDG_DATA_HOME:-$HOME/.local/share}"
CONFIG_HOME="${XDG_CONFIG_HOME:-$HOME/.config}"
SCRIPT_ID="kwin-artemis-windowbridge"
KWIN_SCRIPTS="$DATA_HOME/kwin/scripts/$SCRIPT_ID"
RECEIVER_DIR="${KWB_RECEIVER_DIR:-$HOME/.scripts}"
RECEIVER="$RECEIVER_DIR/kwin-artemis-windowbridge-receiver.py"
AUTOSTART="$CONFIG_HOME/autostart/kwin-artemis-windowbridge-receiver.py.desktop"
PORT="${KWB_NIGHTLIGHT_PORT:-8484}"
PY="${PYTHON:-/usr/bin/python3}"
RELOAD=1

case "${1:-}" in
    "") ;;
    --no-reload) RELOAD=0 ;;
    *) echo "usage: $0 [--no-reload]" >&2; exit 2 ;;
esac

for tool in kwriteconfig6 qdbus6; do
    command -v "$tool" >/dev/null || { echo "missing: $tool" >&2; exit 1; }
done
for mod in dbus gi requests; do
    "$PY" -c "import $mod" 2>/dev/null || { echo "missing Python module '$mod' for $PY" >&2; exit 1; }
done

install -d "$KWIN_SCRIPTS/contents/code" "$RECEIVER_DIR" "$(dirname "$AUTOSTART")"
install -m644 "$SRC/metadata.json" "$KWIN_SCRIPTS/metadata.json"
install -m644 "$SRC/contents/code/main.js" "$KWIN_SCRIPTS/contents/code/main.js"
install -m755 "$SRC/kwin-artemis-windowbridge-receiver.py" "$RECEIVER"

cat > "$AUTOSTART" <<EOF
[Desktop Entry]
Type=Application
Name=kwin-artemis-windowbridge-receiver.py
Exec=$RECEIVER
Terminal=false
StartupNotify=true
X-KDE-AutostartScript=true
EOF

kwriteconfig6 --file kwinrc --group Plugins --key "${SCRIPT_ID}Enabled" true --notify
systemctl --user daemon-reload 2>/dev/null || true

if [ "$RELOAD" = 1 ]; then
    qdbus6 org.kde.KWin /Scripting org.kde.kwin.Scripting.unloadScript "$SCRIPT_ID" >/dev/null 2>&1 || true
    qdbus6 org.kde.KWin /Scripting org.kde.kwin.Scripting.loadScript \
        "$KWIN_SCRIPTS/contents/code/main.js" "$SCRIPT_ID" >/dev/null
    qdbus6 org.kde.KWin /Scripting org.kde.kwin.Scripting.start >/dev/null
fi

UNIT="$(systemctl --user list-units --all --plain --no-legend 2>/dev/null \
    | awk '/receiver\.py@autostart\.service/ {print $1; exit}' || true)"
if [ -n "$UNIT" ]; then
    systemctl --user restart "$UNIT"
else
    pkill -f "$RECEIVER" 2>/dev/null || true
    setsid "$PY" "$RECEIVER" >/dev/null 2>&1 < /dev/null &
fi

sleep 1
STATE="$(qdbus6 org.kde.kwin.WindowBridge /org/kde/kwin/WindowBridge \
    org.kde.kwin.WindowBridge.GetNightLightState 2>/dev/null || true)"
if [ -n "$STATE" ]; then
    echo "receiver: $(printf '%s' "$STATE" | grep -o '"currentTemperature": [0-9]*' || true)"
else
    echo "receiver: no answer on D-Bus yet (journalctl --user -b | grep windowbridge)" >&2
fi
echo "KWin script: $KWIN_SCRIPTS"
echo "receiver:    $RECEIVER"
echo "control API: http://127.0.0.1:$PORT/nightlight"
