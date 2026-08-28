// Camera popup overlay — a Wayland layer-shell surface that plays a camera
// stream in a corner, ABOVE everything (including fullscreen apps), without
// stealing focus. This is the reliable approach on KDE Plasma Bigscreen, whose
// shell forces normal windows to maximized/non-movable — a layer-shell surface
// self-positions and is immune to that (and renders above fullscreen by design).
//
// Config is passed via environment variables (read in Component.onCompleted):
//   CAM_POPUP_STREAM_URL  stream URL (anything QtMultimedia/ffmpeg can play)
//   CAM_POPUP_WIDTH/HEIGHT   size in logical px
//   CAM_POPUP_CORNER      top-right | top-left | bottom-right | bottom-left
//   CAM_POPUP_MARGIN      gap from the screen edges, px
//   CAM_POPUP_ANIM        slide | fade | none
//   CAM_POPUP_ANIM_MS     animation duration, ms
import QtQuick
import QtMultimedia
import org.kde.layershell 1.0 as LayerShell

Window {
    id: win
    visible: true
    color: "black"

    function env(name, def) {
        var v = Qt.application.arguments; // not used; env via helper below
        return def;
    }

    // These are injected by string substitution when the launcher writes the
    // file (see camera_popup.py). Defaults here are just for standalone testing.
    property string streamUrl: "%STREAM_URL%"
    property string snapshotUrl: "%SNAPSHOT_URL%"
    property int popWidth: %WIDTH%
    property int popHeight: %HEIGHT%
    property int popMargin: %MARGIN%
    property string corner: "%CORNER%"
    property string anim: "%ANIM%"
    property int animMs: %ANIM_MS%
    property int probeSize: %PROBE_SIZE%

    width: popWidth
    height: popHeight

    // ---- layer-shell placement: overlay layer, anchored to the chosen corner ----
    LayerShell.Window.layer: LayerShell.Window.LayerOverlay
    LayerShell.Window.anchors: {
        var a = 0;
        a |= (corner.indexOf("bottom") >= 0) ? LayerShell.Window.AnchorBottom
                                             : LayerShell.Window.AnchorTop;
        a |= (corner.indexOf("left") >= 0) ? LayerShell.Window.AnchorLeft
                                           : LayerShell.Window.AnchorRight;
        return a;
    }
    LayerShell.Window.margins {
        top: popMargin; bottom: popMargin; left: popMargin; right: popMargin
    }
    // Never take keyboard focus — the foreground app keeps it, and you can still
    // switch apps underneath with this floating on top.
    LayerShell.Window.keyboardInteractivity: LayerShell.Window.KeyboardInteractivityNone
    LayerShell.Window.exclusionZone: 0

    MediaPlayer {
        id: player
        source: win.streamUrl
        videoOutput: vout
        audioOutput: AudioOutput { muted: true }
        Component.onCompleted: {
            // Shrink ffmpeg's stream-probe size so it commits to decoding sooner
            // (default analysis of the fragmented-mp4 costs ~0.7s of the startup).
            // QPlaybackOptions is a value type: read, mutate, write back.
            // CAM_POPUP_PROBE_SIZE bytes; <=0 leaves the default.
            if (win.probeSize > 0) {
                var o = player.playbackOptions;
                o.probeSize = win.probeSize;
                player.playbackOptions = o;
            }
            play();
        }
        onErrorOccurred: (err, str) => console.log("campopup MediaPlayer error:", err, str)
        // Hide the still-snapshot placeholder once live video is actually
        // rendering, so the popup shows a picture INSTANTLY (snapshot loads in
        // ~0.1s) instead of black while the h264 stream connects/buffers (~3-5s).
        onPlayingChanged: if (playing) hideSnap.start()
        onPositionChanged: if (position > 0 && snap.opacity > 0) hideSnap.start()
    }

    // Content lives in a child Item so we can animate its position/opacity
    // (a Window has no transform/opacity we can slide).
    Item {
        id: content
        anchors.fill: parent
        VideoOutput {
            id: vout
            anchors.fill: parent
            // Fill the window and crop the overscan (the camera stream is
            // ultra-wide 8:3, so a 16:9 window would otherwise letterbox).
            fillMode: VideoOutput.PreserveAspectCrop
        }
        // Instant still-frame placeholder shown on top of the (initially black)
        // video until the first live frame arrives, then it fades out.
        Image {
            id: snap
            anchors.fill: parent
            source: win.snapshotUrl
            fillMode: Image.PreserveAspectCrop
            cache: false
            asynchronous: true
            visible: opacity > 0
            opacity: source == "" ? 0 : 1
        }
        NumberAnimation {
            id: hideSnap
            target: snap
            property: "opacity"
            to: 0.0
            duration: 250
        }
    }

    // ---- entrance animation ----
    Component.onCompleted: {
        if (anim === "fade") {
            content.opacity = 0.0;
            fadeIn.start();
        } else if (anim === "slide") {
            content.x = (corner.indexOf("left") >= 0)
                        ? -win.popWidth - win.popMargin
                        : win.popWidth + win.popMargin;
            slideIn.start();
        }
    }

    NumberAnimation {
        id: slideIn
        target: content
        property: "x"
        to: 0
        duration: win.animMs
        easing.type: Easing.OutCubic
    }
    NumberAnimation {
        id: fadeIn
        target: content
        property: "opacity"
        from: 0.0; to: 1.0
        duration: win.animMs
    }
}
