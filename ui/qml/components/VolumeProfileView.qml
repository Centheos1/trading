// Phase 9B — chart bridge.  Hosts ``VolumeProfileItem``.
//
// TODO Phase 9B-final: replace with QSGGeometryNode histogram.
import QtQuick
import Trading.Items 1.0

Item {
    VolumeProfileItem {
        id: vp
        objectName: "volumeProfileItem"
        anchors.fill: parent
    }

    Rectangle {
        anchors.fill: parent
        color: "transparent"
        border.color: "#1a1d33"
        border.width: 1
    }
}
