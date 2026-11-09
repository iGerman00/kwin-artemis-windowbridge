// KWin Artemis WindowBridge: Sends window info on activation AND title changes

// Set to true only while debugging: logs window captions/ids to the journal.
var DEBUG = false;

function log(msg) {
    if (DEBUG) print(msg);
}

function extractWindowData(client) {
    if (!client) return null;
    try {
        var output = client.output || {};
        var outputGeom = output.geometry || {};
        var desktops = client.desktops || [];
        var desktopNames = desktops.map(function(d) { return d.name || ""; }).join(", ");
        var frameGeom = client.frameGeometry || {};
        var bufferGeom = client.bufferGeometry || {};
        var clientGeom = client.clientGeometry || {};
        var iconGeom = client.iconGeometry || {};
        var minSize = client.minSize || {};
        var maxSize = client.maxSize || {};

        return {
            caption: client.caption || "",
            captionNormal: client.captionNormal || "",
            resourceName: client.resourceName || "",
            resourceClass: client.resourceClass || "",
            windowRole: client.windowRole || "",
            desktopFileName: client.desktopFileName || "",
            internalId: client.internalId || "",
            pid: client.pid || 0,
            stackingOrder: client.stackingOrder || 0,
            active: client.active || false,
            managed: client.managed || false,
            deleted: client.deleted || false,
            fullScreen: client.fullScreen || false,
            minimized: client.minimized || false,
            shade: client.shade || false,
            hidden: client.hidden || false,
            keepAbove: client.keepAbove || false,
            keepBelow: client.keepBelow || false,
            onAllDesktops: client.onAllDesktops || false,
            skipTaskbar: client.skipTaskbar || false,
            skipPager: client.skipPager || false,
            skipSwitcher: client.skipSwitcher || false,
            skipsCloseAnimation: client.skipsCloseAnimation || false,
            closeable: client.closeable || false,
            minimizable: client.minimizable || false,
            maximizable: client.maximizable || false,
            moveable: client.moveable || false,
            moveableAcrossScreens: client.moveableAcrossScreens || false,
            resizeable: client.resizeable || false,
            fullScreenable: client.fullScreenable || false,
            normalWindow: client.normalWindow || false,
            dialog: client.dialog || false,
            notification: client.notification || false,
            dock: client.dock || false,
            tooltip: client.tooltip || false,
            move: client.move || false,
            resize: client.resize || false,
            modal: client.modal || false,
            demandsAttention: client.demandsAttention || false,
            unresponsive: client.unresponsive || false,
            opacity: client.opacity || 1.0,
            noBorder: client.noBorder || false,
            frameX: frameGeom.x || 0,
            frameY: frameGeom.y || 0,
            frameWidth: frameGeom.width || 0,
            frameHeight: frameGeom.height || 0,
            clientX: clientGeom.x || 0,
            clientY: clientGeom.y || 0,
            clientWidth: clientGeom.width || 0,
            clientHeight: clientGeom.height || 0,
            x: client.x || 0,
            y: client.y || 0,
            width: client.width || 0,
            height: client.height || 0,
            iconX: iconGeom.x || 0,
            iconY: iconGeom.y || 0,
            iconWidth: iconGeom.width || 0,
            iconHeight: iconGeom.height || 0,
            minWidth: minSize.width || 0,
            minHeight: minSize.height || 0,
            maxWidth: maxSize.width || 0,
            maxHeight: maxSize.height || 0,
            outputName: output.name || "",
            outputManufacturer: output.manufacturer || "",
            outputModel: output.model || "",
            outputSerialNumber: output.serialNumber || "",
            outputX: outputGeom.x || 0,
            outputY: outputGeom.y || 0,
            outputWidth: outputGeom.width || 0,
            outputHeight: outputGeom.height || 0,
            devicePixelRatio: output.devicePixelRatio || 1.0,
            desktopCount: desktops.length || 0,
            desktopNames: desktopNames,
            activityCount: (client.activities || []).length || 0,
            windowType: client.windowType || 0,
            maximizeMode: client.maximizeMode || 0,
            layer: client.layer || 0,
            hasApplicationMenu: client.hasApplicationMenu || false,
            applicationMenuActive: client.applicationMenuActive || false,
            providesContextHelp: client.providesContextHelp || false,
            inputMethod: client.inputMethod || false
        };
    } catch (e) {
        print("❌ Error extracting window data: " + e);
        return null;
    }
}

function sendWindowInfo(client, reason) {
    if (!client) {
        return;
    }
    var windowData = extractWindowData(client);
    if (!windowData) {
        return;
    }
    try {
        // Add reason for the update
        windowData.updateReason = reason || "unknown";
        
        var jsonString = JSON.stringify(windowData);
        callDBus(
            "org.kde.kwin.WindowBridge",
            "/org/kde/kwin/WindowBridge",
            "org.kde.kwin.WindowBridge",
            "WindowChanged",
            jsonString
        );
        log("Sent: " + reason);
    } catch (e) {
        print("❌ Error sending window data: " + e);
    }
}

// Track windows we've already connected caption listeners to
var trackedWindows = [];

function connectCaptionListener(client) {
    // Avoid connecting multiple times to the same window
    if (trackedWindows.indexOf(client) !== -1) {
        return;
    }
    
    try {
        // Connect to caption change signal
        client.captionChanged.connect(function() {
            sendWindowInfo(client, "caption_changed");
        });
        
        // Connect to other useful property changes
        client.captionNormalChanged.connect(function() {
            sendWindowInfo(client, "caption_normal_changed");
        });
        
        // Optional: also track minimized/maximized state changes
        client.minimizedChanged.connect(function() {
            sendWindowInfo(client, "minimized_changed");
        });
        
        client.fullScreenChanged.connect(function() {
            sendWindowInfo(client, "fullscreen_changed");
        });
        
        trackedWindows.push(client);
        log("Connected listener: " + (client.resourceClass || "unknown"));
        
    } catch (e) {
        print("❌ Error connecting caption listener: " + e);
    }
}

function removeFromTracked(client) {
    var index = trackedWindows.indexOf(client);
    if (index !== -1) {
        trackedWindows.splice(index, 1);
    }
}

// Handle window activation (focus changes)
function handleWindowActivated(client) {
    sendWindowInfo(client, "window_activated");
    if (client) {
        connectCaptionListener(client);
    }
}

function handleWindowAdded(client) {
    if (client) {
        connectCaptionListener(client);
        // send info immediately for new windows?
        // sendWindowInfo(client, "window_added");
    }
}

function handleWindowRemoved(client) {
    removeFromTracked(client);
}

function connectExistingWindows() {
    var clients = workspace.stackingOrder;
    for (var i = 0; i < clients.length; i++) {
        connectCaptionListener(clients[i]);
    }
    log("Connected to " + clients.length + " existing windows");
}

workspace.windowActivated.connect(handleWindowActivated);
workspace.windowAdded.connect(handleWindowAdded);
workspace.windowRemoved.connect(handleWindowRemoved);

connectExistingWindows();

// Shortcut to stop everything
// registerShortcut("KWInArtemisStopBridge", "Stop KWin Artemis WindowBridge", "Meta+Shift+Q", function() {
//     workspace.windowActivated.disconnect(handleWindowActivated);
//     workspace.windowAdded.disconnect(handleWindowAdded);
//     workspace.windowRemoved.disconnect(handleWindowRemoved);
//     print("🛑 Window bridge stopped.");
// });

print("✅ KWin Artemis WindowBridge loaded (tracking activation + caption changes)");