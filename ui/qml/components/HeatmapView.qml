// Phase 9B — chart bridge.
//
// Wraps the native ``HeatmapItem`` (QQuickPaintedItem-backed
// ui.items.heatmap_item).  Bridge tier: paint() forwards to
// ``HeatmapWidget.render()``; Phase 9B-final upgrades this to a
// ``QSGTexture`` quad without changing the QML surface.
//
// TODO Phase 9B-final: drop the bridge layer and bind directly to the
// scene-graph node.
import QtQuick
import Trading.Items 1.0

Item {
    HeatmapItem {
        id: heatmap
        objectName: "heatmapItem"
        anchors.fill: parent
    }

    Rectangle {
        anchors.fill: parent
        color: "transparent"
        border.color: "#1a1d33"
        border.width: 1
    }
}
