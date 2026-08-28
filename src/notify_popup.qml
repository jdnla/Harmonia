// HA notification toast — a Wayland layer-shell overlay that shows a titled text
// notification (with optional icon/image) in a screen corner and auto-dismisses.
// Plasma Bigscreen provides NO org.freedesktop.Notifications server, so we render
// our own. Same layer-shell approach as the camera popup: self-positions in a
// corner, renders above fullscreen apps, never takes keyboard focus.
//
// Styled to fit the Plasma/Breeze look and driven by config (see notify_popup.py
// / config.env: NOTIFY_* — colors, fonts, radius, timeout, corner, size). Text
// is base64 so arbitrary characters/newlines survive substitution.
import QtQuick
import QtQuick.Effects
import org.kde.layershell 1.0 as LayerShell

Window {
    id: win
    visible: true
    color: "transparent"

    // --- substituted config ---
    property string b64Title: "%TITLE_B64%"
    property string b64Body:  "%BODY_B64%"
    property string imageUrl: "%IMAGE_URL%"
    property int popWidth:  %WIDTH%
    property int popHeight: %HEIGHT%
    property int popMargin: %MARGIN%
    property string corner: "%CORNER%"
    property int timeoutMs: %TIMEOUT_MS%
    property int animMs:    %ANIM_MS%
    // styling (all overridable via NOTIFY_* config)
    property color accent:    "%ACCENT%"
    property color bgColor:   "%BG_COLOR%"
    property color titleColor:"%TITLE_COLOR%"
    property color bodyColor: "%BODY_COLOR%"
    property int   radius:    %RADIUS%
    property int   titleSize: %TITLE_SIZE%
    property int   bodySize:  %BODY_SIZE%
    property string fontFamily: "%FONT%"
    property bool  showProgress: %SHOW_PROGRESS%
    property bool  showShadow:   %SHOW_SHADOW%

    function b64decode(s) { if (!s) return ""; try { return Qt.atob(s); } catch (e) { return s; } }

    readonly property int pad: showShadow ? 24 : 2
    width: popWidth + pad * 2
    height: popHeight + pad * 2

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
        top: popMargin - pad; bottom: popMargin - pad
        left: popMargin - pad; right: popMargin - pad
    }
    LayerShell.Window.keyboardInteractivity: LayerShell.Window.KeyboardInteractivityNone
    LayerShell.Window.exclusionZone: 0

    Item {
        id: content
        anchors.fill: parent

        Rectangle {
            id: card
            x: win.pad; y: win.pad
            width: win.popWidth; height: win.popHeight
            radius: win.radius
            color: win.bgColor
            border.color: "#22ffffff"
            border.width: 1

            // soft drop shadow (optional)
            layer.enabled: win.showShadow
            layer.effect: MultiEffect {
                shadowEnabled: true
                shadowColor: "#aa000000"
                shadowBlur: 0.6
                shadowVerticalOffset: 4
                shadowHorizontalOffset: 0
            }

            Rectangle {
                id: accentBar
                anchors { left: parent.left; top: parent.top; bottom: parent.bottom }
                width: 5; radius: 3; color: win.accent
            }

            Row {
                anchors.fill: parent
                anchors.leftMargin: accentBar.width + 16
                anchors.rightMargin: 18
                anchors.topMargin: 14
                anchors.bottomMargin: 14
                spacing: 16

                // Optional icon/image. Rounded via a clipping Rectangle (NOT a
                // MultiEffect mask — the mask effect renders grey on this GPU).
                Rectangle {
                    id: imgWrap
                    visible: win.imageUrl.length > 0 && img.status !== Image.Error
                    width: visible ? Math.min(parent.height, 110) : 0
                    height: Math.min(parent.height, 110)
                    anchors.verticalCenter: parent.verticalCenter
                    radius: 8
                    color: "#22ffffff"
                    clip: true
                    Image {
                        id: img
                        anchors.fill: parent
                        fillMode: Image.PreserveAspectCrop
                        source: win.imageUrl
                        asynchronous: true
                        cache: false
                    }
                }

                Column {
                    width: parent.width - (imgWrap.visible ? imgWrap.width + parent.spacing : 0)
                    anchors.verticalCenter: parent.verticalCenter
                    spacing: 4

                    Text {
                        width: parent.width
                        text: win.b64decode(win.b64Title)
                        color: win.titleColor
                        font.family: win.fontFamily
                        font.pixelSize: win.titleSize
                        font.weight: Font.DemiBold
                        elide: Text.ElideRight
                        maximumLineCount: 1
                        visible: text.length > 0
                    }
                    Text {
                        width: parent.width
                        text: win.b64decode(win.b64Body)
                        color: win.bodyColor
                        font.family: win.fontFamily
                        font.pixelSize: win.bodySize
                        lineHeight: 1.15
                        wrapMode: Text.WordWrap
                        elide: Text.ElideRight
                        maximumLineCount: 3
                    }
                }
            }

            Rectangle {
                id: progress
                visible: win.showProgress
                anchors { left: accentBar.right; bottom: parent.bottom }
                height: 3; radius: 2
                color: win.accent; opacity: 0.7
                width: parent.width - accentBar.width
            }
        }
    }

    property int slideFrom: (corner.indexOf("left") >= 0)
                            ? -(popWidth + popMargin + pad)
                            :  (popWidth + popMargin + pad)
    Component.onCompleted: {
        content.opacity = 0; content.x = slideFrom;
        slideIn.start();
        if (showProgress) progressAnim.start();
        dismiss.start();
    }
    ParallelAnimation {
        id: slideIn
        NumberAnimation { target: content; property: "x"; to: 0; duration: win.animMs; easing.type: Easing.OutCubic }
        NumberAnimation { target: content; property: "opacity"; to: 1; duration: win.animMs }
    }
    NumberAnimation {
        id: progressAnim
        target: progress; property: "width"
        from: card.width - accentBar.width; to: 0
        duration: win.timeoutMs; easing.type: Easing.Linear
    }
    SequentialAnimation {
        id: dismiss
        PauseAnimation { duration: win.timeoutMs }
        ParallelAnimation {
            NumberAnimation { target: content; property: "x"; to: win.slideFrom; duration: win.animMs; easing.type: Easing.InCubic }
            NumberAnimation { target: content; property: "opacity"; to: 0; duration: win.animMs }
        }
        ScriptAction { script: Qt.quit() }
    }
}
