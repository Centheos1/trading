// Phase 9C — Strategy dashboard composition.
//
// Stacks ``PositionCard``, ``StrategyDiagnostics``, and ``TradeBlotter``
// vertically in the right-hand dock.  All three children pull their
// data from the registered context properties (``positionModel``,
// ``snapshotModel``, ``tradeBlotterModel``); the dashboard itself owns
// no state.
import QtQuick
import QtQuick.Layouts

Rectangle {
    color: "#0c0e1c"

    ColumnLayout {
        anchors.fill: parent
        anchors.margins: 8
        spacing: 8

        PositionCard {
            Layout.fillWidth: true
        }

        StrategyDiagnostics {
            Layout.fillWidth: true
        }

        TradeBlotter {
            Layout.fillWidth: true
            Layout.fillHeight: true
        }
    }
}
