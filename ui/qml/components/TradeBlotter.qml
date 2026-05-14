// Phase 9C — Signal / trade blotter ListView.
//
// Backed by ``tradeBlotterModel`` (registered as a QML context
// property; falls back to the QML type for the standalone scaffold).
// Fixed-height 32 px delegates — the QML scene graph caches sized
// delegates so virtualisation kicks in for 500 rows without
// re-measuring layout.
//
// Category colour coding comes from the model's ``categoryColor`` role
// (hex string, set in :mod:`ui.models.trade_blotter_model`).
import QtQuick
import QtQuick.Controls
import QtQuick.Layouts

Rectangle {
    id: blotter
    color: "#0c0e1c"
    border.color: "#1a1d33"
    border.width: 1
    radius: 4

    property var listModel: typeof tradeBlotterModel !== "undefined"
        ? tradeBlotterModel : null

    Component.onCompleted: {
        if (!blotter.listModel) {
            console.warn(
                "TradeBlotter.qml: no ``tradeBlotterModel`` context property");
        }
    }

    ColumnLayout {
        anchors.fill: parent
        anchors.margins: 4
        spacing: 2

        // ---- header strip ----
        Rectangle {
            Layout.fillWidth: true
            Layout.preferredHeight: 22
            color: "#1a1a2e"

            RowLayout {
                anchors.fill: parent
                anchors.leftMargin: 6
                anchors.rightMargin: 6

                Text {
                    Layout.preferredWidth: 90
                    color: "#b4b4c8"
                    font.family: "Menlo"
                    font.pixelSize: 10
                    font.bold: true
                    text: "Time"
                }
                Text {
                    Layout.preferredWidth: 60
                    color: "#b4b4c8"
                    font.family: "Menlo"
                    font.pixelSize: 10
                    font.bold: true
                    text: "Layer"
                }
                Text {
                    Layout.preferredWidth: 100
                    color: "#b4b4c8"
                    font.family: "Menlo"
                    font.pixelSize: 10
                    font.bold: true
                    text: "Category"
                }
                Text {
                    Layout.preferredWidth: 160
                    color: "#b4b4c8"
                    font.family: "Menlo"
                    font.pixelSize: 10
                    font.bold: true
                    text: "Type"
                }
                Text {
                    Layout.preferredWidth: 36
                    color: "#b4b4c8"
                    font.family: "Menlo"
                    font.pixelSize: 10
                    font.bold: true
                    text: "Side"
                }
                Text {
                    Layout.preferredWidth: 70
                    color: "#b4b4c8"
                    font.family: "Menlo"
                    font.pixelSize: 10
                    font.bold: true
                    text: "Price"
                }
                Text {
                    Layout.preferredWidth: 65
                    color: "#b4b4c8"
                    font.family: "Menlo"
                    font.pixelSize: 10
                    font.bold: true
                    text: "PnL"
                }
                Text {
                    Layout.fillWidth: true
                    color: "#b4b4c8"
                    font.family: "Menlo"
                    font.pixelSize: 10
                    font.bold: true
                    text: "Description"
                }
            }
        }

        ListView {
            id: rowsView
            Layout.fillWidth: true
            Layout.fillHeight: true
            model: blotter.listModel
            clip: true
            spacing: 0
            // Auto-scroll to the bottom when new rows arrive — match the
            // QTableView.scrollToBottom() behaviour.
            onCountChanged: {
                if (rowsView.count > 0) {
                    rowsView.positionViewAtEnd();
                }
            }

            delegate: Rectangle {
                width: ListView.view.width
                height: 32  // Fixed height — AC #1 (delegate caching).
                color: (index % 2 === 0) ? "#0f0f19" : "#11111d"

                RowLayout {
                    anchors.fill: parent
                    anchors.leftMargin: 6
                    anchors.rightMargin: 6

                    Text {
                        Layout.preferredWidth: 90
                        color: "#b4b4c8"
                        font.family: "Menlo"
                        font.pixelSize: 11
                        text: model.timestampStr || ""
                    }
                    Text {
                        Layout.preferredWidth: 60
                        color: model.categoryColor || "#b4b4c8"
                        font.family: "Menlo"
                        font.pixelSize: 11
                        text: model.layer || ""
                    }
                    Text {
                        Layout.preferredWidth: 100
                        color: model.categoryColor || "#b4b4c8"
                        font.family: "Menlo"
                        font.pixelSize: 11
                        text: model.category || ""
                    }
                    Text {
                        Layout.preferredWidth: 160
                        color: model.categoryColor || "#b4b4c8"
                        font.family: "Menlo"
                        font.pixelSize: 11
                        text: model.signalType || ""
                        elide: Text.ElideRight
                    }
                    Text {
                        Layout.preferredWidth: 36
                        color: model.side === "BUY" ? "#1ec864"
                               : model.side === "SELL" ? "#ee4040"
                               : "#b4b4c8"
                        font.family: "Menlo"
                        font.pixelSize: 11
                        font.bold: model.side === "BUY" || model.side === "SELL"
                        text: model.side || ""
                    }
                    Text {
                        Layout.preferredWidth: 70
                        color: "#b4b4c8"
                        font.family: "Menlo"
                        font.pixelSize: 11
                        horizontalAlignment: Text.AlignRight
                        text: model.priceStr || ""
                    }
                    Text {
                        Layout.preferredWidth: 65
                        color: model.realizedPnl > 0 ? "#1ec864"
                               : model.realizedPnl < 0 ? "#ee4040"
                               : "#b4b4c8"
                        font.family: "Menlo"
                        font.pixelSize: 11
                        font.bold: true
                        horizontalAlignment: Text.AlignRight
                        text: model.realizedPnlStr || ""
                    }
                    Text {
                        Layout.fillWidth: true
                        color: "#a0a0b8"
                        font.family: "Menlo"
                        font.pixelSize: 11
                        text: model.description || ""
                        elide: Text.ElideRight
                    }
                }
            }

            ScrollBar.vertical: ScrollBar { active: true }
        }
    }
}
