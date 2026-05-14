// Phase 9C — Live position card.
//
// Bound to the registered ``positionModel`` context property
// (Trading.Models 1.0 PositionModel).  Renders:
//   * trade-state badge (top-left)
//   * entry / stop / target rows
//   * R:R ratio
//   * unrealised PnL (with green/red colour)
//   * session PnL
//
// Solid background colour (#0c0e1c) — NO ``opacity`` / fade animations
// per the NICE DCV guidance in UI_STRATEGY_INTEGRATION_PLAN.md §22.4
// AC #3 (semi-transparent layers stall the H.264 encoder).
import QtQuick
import QtQuick.Layouts

Rectangle {
    id: card
    color: "#0c0e1c"
    border.color: "#1a1d33"
    border.width: 1
    radius: 4
    implicitHeight: 168

    // ``positionModel`` is exposed via the QML root contextProperty by
    // ``MainWindowBridge`` (and registered as a QML type for the
    // standalone scaffold).  Fall back to a null model when running
    // the stub harness — every binding then resolves to its default.
    property var model: typeof positionModel !== "undefined" ? positionModel : null

    Component.onCompleted: {
        if (!card.model) {
            console.warn("PositionCard.qml: no ``positionModel`` context property");
        }
    }

    ColumnLayout {
        anchors.fill: parent
        anchors.margins: 8
        spacing: 4

        // ---- header: trade-state badge + archetype ----
        RowLayout {
            Layout.fillWidth: true
            spacing: 6

            Rectangle {
                id: stateBadge
                Layout.alignment: Qt.AlignVCenter
                implicitWidth: stateText.implicitWidth + 12
                implicitHeight: 20
                radius: 3
                color: card.model && card.model.active ? "#1a4a2e" : "#1f2235"
                border.color: card.model && card.model.active ? "#30a050" : "#2a2d44"
                border.width: 1
                Text {
                    id: stateText
                    anchors.centerIn: parent
                    color: card.model && card.model.active ? "#7fdc9a" : "#788090"
                    font.family: "Menlo"
                    font.pixelSize: 10
                    font.bold: true
                    text: card.model && card.model.tradeStateLabel
                        ? card.model.tradeStateLabel
                        : "FLAT"
                }
            }

            Text {
                Layout.alignment: Qt.AlignVCenter
                color: "#788090"
                font.family: "Menlo"
                font.pixelSize: 10
                text: card.model && card.model.tradeArchetype
                    ? card.model.tradeArchetype
                    : ""
            }

            Item { Layout.fillWidth: true }

            Text {
                Layout.alignment: Qt.AlignVCenter
                color: card.model && card.model.tradeSide === "LONG" ? "#1ecc66"
                       : card.model && card.model.tradeSide === "SHORT" ? "#ee4040"
                       : "#788090"
                font.family: "Menlo"
                font.pixelSize: 10
                font.bold: true
                text: card.model && card.model.tradeSide
                    ? card.model.tradeSide
                    : ""
            }
        }

        // ---- entry / stop / target rows ----
        Loader {
            Layout.fillWidth: true
            sourceComponent: card.model && card.model.active
                ? activeTradeRows
                : placeholderRows
        }

        Component {
            id: placeholderRows
            Text {
                color: "#5a6280"
                font.family: "Menlo"
                font.pixelSize: 11
                horizontalAlignment: Text.AlignHCenter
                width: parent ? parent.width : implicitWidth
                text: "No active trade"
            }
        }

        Component {
            id: activeTradeRows
            ColumnLayout {
                spacing: 2
                width: parent ? parent.width : implicitWidth

                Repeater {
                    model: [
                        { label: "Entry",  value: card.model.entryPrice,  color: "#dcdce6" },
                        { label: "Stop",   value: card.model.stopPrice,   color: "#ee4040" },
                        { label: "Target", value: card.model.targetPrice, color: "#1ecc66" },
                    ]
                    delegate: RowLayout {
                        Layout.fillWidth: true
                        Text {
                            Layout.preferredWidth: 56
                            color: "#788090"
                            font.family: "Menlo"
                            font.pixelSize: 11
                            text: modelData.label
                        }
                        Text {
                            Layout.fillWidth: true
                            color: modelData.color
                            font.family: "Menlo"
                            font.pixelSize: 11
                            font.bold: true
                            horizontalAlignment: Text.AlignRight
                            text: modelData.value > 0
                                ? modelData.value.toFixed(2)
                                : "\u2014"
                        }
                    }
                }
            }
        }

        Rectangle {
            Layout.fillWidth: true
            Layout.preferredHeight: 1
            color: "#1a1d33"
        }

        // ---- R:R + uPnL row ----
        RowLayout {
            Layout.fillWidth: true

            Text {
                color: "#788090"
                font.family: "Menlo"
                font.pixelSize: 11
                text: "R:R"
            }
            Text {
                Layout.fillWidth: true
                color: "#dcdce6"
                font.family: "Menlo"
                font.pixelSize: 11
                font.bold: true
                horizontalAlignment: Text.AlignRight
                text: card.model && card.model.rrRatio !== 0
                    ? card.model.rrRatio.toFixed(2)
                    : "\u2014"
            }
        }

        RowLayout {
            Layout.fillWidth: true

            Text {
                color: "#788090"
                font.family: "Menlo"
                font.pixelSize: 11
                text: "uPnL"
            }
            Text {
                Layout.fillWidth: true
                color: card.model && card.model.unrealizedPnl > 0 ? "#1ecc66"
                       : card.model && card.model.unrealizedPnl < 0 ? "#ee4040"
                       : "#dcdce6"
                font.family: "Menlo"
                font.pixelSize: 11
                font.bold: true
                horizontalAlignment: Text.AlignRight
                text: card.model
                    ? (card.model.unrealizedPnl >= 0 ? "+" : "")
                      + card.model.unrealizedPnl.toFixed(4)
                    : "\u2014"
            }
        }

        RowLayout {
            Layout.fillWidth: true

            Text {
                color: "#788090"
                font.family: "Menlo"
                font.pixelSize: 11
                text: "Session"
            }
            Text {
                Layout.fillWidth: true
                color: card.model && card.model.sessionPnl > 0 ? "#1ecc66"
                       : card.model && card.model.sessionPnl < 0 ? "#ee4040"
                       : "#dcdce6"
                font.family: "Menlo"
                font.pixelSize: 11
                font.bold: true
                horizontalAlignment: Text.AlignRight
                text: card.model
                    ? (card.model.sessionPnl >= 0 ? "+" : "")
                      + card.model.sessionPnl.toFixed(2)
                    : "\u2014"
            }
        }
    }
}
