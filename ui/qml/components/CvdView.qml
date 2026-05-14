// Phase 9B — chart bridge.  Hosts ``CvdItem``.
//
// TODO Phase 9B-final: replace with QSGGeometryNode bars + line strip.
import QtQuick
import Trading.Items 1.0

Item {
    CvdItem {
        id: cvd
        objectName: "cvdItem"
        anchors.fill: parent
    }

    Rectangle {
        anchors.fill: parent
        color: "transparent"
        border.color: "#1a1d33"
        border.width: 1
    }
}
