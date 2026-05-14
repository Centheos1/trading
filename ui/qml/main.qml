// Phase 9A — QML scaffold.
//
// Root ApplicationWindow (OrderFlow shell) plus two additional detachable
// top-level Window items (Chart, Strategy).  All three open at startup,
// matching Phase 9A acceptance criterion #1 ("python -m ui.app opens three
// QML windows without Python exceptions").
//
// All visual content is stubbed for 9A — concrete components arrive in
// Phase 9B (chart widgets) and 9C (strategy dashboard).  The
// ``gpuWarning`` property on the root window is toggled by
// ``ui.app._check_gpu`` when software rendering is detected so the banner
// can surface inline without bouncing through a Python signal.
//
// Solid panel backgrounds + no animations comply with the NICE DCV
// optimisation rules (``AGENT_STRATEGY_RULES.md §22.6``).
import QtQuick
import QtQuick.Controls
import QtQuick.Layouts
import QtQuick.Window

import "components" as Components

ApplicationWindow {
    id: orderFlowWindow

    objectName: "orderFlowWindow"
    title: "OrderFlow Trading — OrderFlow"
    visible: true
    width: 1280
    height: 800
    color: "#0c0e1c"

    // Software-rendering banner toggle.  ``ui.app._check_gpu`` sets this
    // to true when QSGRendererInterface reports Software / Unknown.
    property bool gpuWarning: false

    header: Rectangle {
        color: "#13162a"
        height: 36
        Label {
            anchors.verticalCenter: parent.verticalCenter
            anchors.left: parent.left
            anchors.leftMargin: 12
            text: "OrderFlow — Heatmap · CVD · Volume Profile"
            color: "#a8aec3"
            font.family: "Menlo"
            font.pixelSize: 12
        }
    }

    // Persistent in-app banner; visible only when GPU verification fails.
    Rectangle {
        id: gpuWarningBanner
        objectName: "gpuWarningBanner"
        anchors.top: parent.top
        anchors.left: parent.left
        anchors.right: parent.right
        height: visible ? 28 : 0
        visible: orderFlowWindow.gpuWarning
        color: "#7a1f1f"
        z: 100

        Label {
            anchors.centerIn: parent
            color: "#ffffff"
            font.family: "Menlo"
            font.pixelSize: 11
            text: "GPU acceleration unavailable — Qt scene graph is running in SOFTWARE mode."
        }
    }

    ColumnLayout {
        anchors.fill: parent
        anchors.topMargin: gpuWarningBanner.height
        spacing: 1

        Components.HeatmapView {
            Layout.fillWidth: true
            Layout.fillHeight: true
        }

        RowLayout {
            Layout.fillWidth: true
            Layout.preferredHeight: 180
            spacing: 1

            Components.CvdView {
                Layout.fillWidth: true
                Layout.fillHeight: true
            }
            Components.VolumeProfileView {
                Layout.preferredWidth: 220
                Layout.fillHeight: true
            }
        }
    }

    // -----------------------------------------------------------------
    // Detached chart window
    // -----------------------------------------------------------------
    Window {
        id: chartWindow
        objectName: "chartWindow"
        title: "OrderFlow Trading — Chart"
        visible: true
        width: 1100
        height: 720
        color: "#0c0e1c"

        Components.CandleChartView {
            anchors.fill: parent
        }
    }

    // -----------------------------------------------------------------
    // Detached strategy dashboard window
    // -----------------------------------------------------------------
    Window {
        id: strategyWindow
        objectName: "strategyWindow"
        title: "OrderFlow Trading — Strategy"
        visible: true
        width: 760
        height: 720
        color: "#0c0e1c"

        Components.StrategyDashboard {
            anchors.fill: parent
        }
    }
}
