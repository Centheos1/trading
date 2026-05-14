// Phase 9C — Strategy diagnostics panel.
//
// Bound to the registered ``snapshotModel`` context property
// (Trading.Models 1.0 SnapshotModel).  Renders the layer-by-layer
// readout that ``StrategyDiagnosticsPanel`` (legacy QWidget) used to
// paint by hand.  All bindings emit through Q_PROPERTY notify signals
// so changes ripple to the UI in a single scene-graph frame.
import QtQuick
import QtQuick.Controls
import QtQuick.Layouts

Rectangle {
    id: diagnostics
    color: "#0c0e1c"
    border.color: "#1a1d33"
    border.width: 1
    radius: 4
    implicitHeight: 116

    property var snapshot: typeof snapshotModel !== "undefined"
        ? snapshotModel : null
    // Phase 9D — ``suppressionModel`` is registered as a context
    // property by ``ui.app._install_bridge_context``.  Use ``typeof``
    // so the standalone scaffold tests (no bridge) still load this
    // component cleanly.
    property var suppression: typeof suppressionModel !== "undefined"
        ? suppressionModel : null

    function _enumDisplay(value) {
        if (!value) return "\u2014";  // em-dash for empty
        var parts = String(value).split(".");
        return parts[parts.length - 1];
    }

    function _biasColor(bias) {
        var b = _enumDisplay(bias);
        if (b === "LONG")  return "#1ecc66";
        if (b === "SHORT") return "#ee4040";
        return "#b4b4c8";
    }

    function _regimeColor(regime) {
        var r = _enumDisplay(regime);
        if (r === "MEAN_REVERSION") return "#1ecc66";
        if (r === "BREAKOUT")       return "#64a0dc";
        if (r === "BREAKDOWN")      return "#ee4040";
        return "#b4b4c8";
    }

    function _riskColor(pct) {
        if (pct === null || pct === undefined) return "#b4b4c8";
        if (pct > 0.80) return "#ee4040";
        if (pct > 0.50) return "#e0c050";
        return "#1ecc66";
    }

    GridLayout {
        anchors.fill: parent
        anchors.margins: 6
        columns: 4
        columnSpacing: 6
        rowSpacing: 4

        // ---- Tide ----
        Text {
            color: "#788090"
            font.family: "Menlo"
            font.pixelSize: 10
            text: "Tide"
        }
        Text {
            Layout.fillWidth: true
            color: diagnostics._biasColor(
                diagnostics.snapshot ? diagnostics.snapshot.tideBias : "")
            font.family: "Menlo"
            font.pixelSize: 11
            font.bold: true
            text: diagnostics._enumDisplay(
                diagnostics.snapshot ? diagnostics.snapshot.tideBias : "")
        }

        // ---- Wave ----
        Text {
            color: "#788090"
            font.family: "Menlo"
            font.pixelSize: 10
            text: "Wave"
        }
        Text {
            Layout.fillWidth: true
            color: diagnostics._regimeColor(
                diagnostics.snapshot ? diagnostics.snapshot.waveRegime : "")
            font.family: "Menlo"
            font.pixelSize: 11
            font.bold: true
            text: diagnostics._enumDisplay(
                diagnostics.snapshot ? diagnostics.snapshot.waveRegime : "")
        }

        // ---- Trade state ----
        Text {
            color: "#788090"
            font.family: "Menlo"
            font.pixelSize: 10
            text: "Trade"
        }
        Text {
            Layout.fillWidth: true
            color: "#dcdce6"
            font.family: "Menlo"
            font.pixelSize: 11
            font.bold: true
            text: diagnostics._enumDisplay(
                diagnostics.snapshot ? diagnostics.snapshot.tradeState : "")
        }

        // ---- uPnL ----
        Text {
            color: "#788090"
            font.family: "Menlo"
            font.pixelSize: 10
            text: "uPnL"
        }
        Text {
            Layout.fillWidth: true
            color: {
                if (!diagnostics.snapshot) return "#b4b4c8";
                if (diagnostics.snapshot.unrealizedPnl > 0) return "#1ecc66";
                if (diagnostics.snapshot.unrealizedPnl < 0) return "#ee4040";
                return "#b4b4c8";
            }
            font.family: "Menlo"
            font.pixelSize: 11
            font.bold: true
            text: diagnostics.snapshot
                ? (diagnostics.snapshot.unrealizedPnl >= 0 ? "+" : "")
                  + diagnostics.snapshot.unrealizedPnl.toFixed(4)
                : "\u2014"
        }

        // ---- Archetype ----
        Text {
            color: "#788090"
            font.family: "Menlo"
            font.pixelSize: 10
            text: "Arch"
        }
        Text {
            Layout.fillWidth: true
            color: "#b4b4c8"
            font.family: "Menlo"
            font.pixelSize: 11
            text: diagnostics._enumDisplay(
                diagnostics.snapshot ? diagnostics.snapshot.tradeArchetype : "")
        }

        // ---- ES used ----
        Text {
            color: "#788090"
            font.family: "Menlo"
            font.pixelSize: 10
            text: "ES%"
        }
        Text {
            Layout.fillWidth: true
            color: diagnostics._riskColor(
                diagnostics.snapshot ? diagnostics.snapshot.riskBudgetPct : 0)
            font.family: "Menlo"
            font.pixelSize: 11
            font.bold: true
            text: diagnostics.snapshot
                ? (diagnostics.snapshot.riskBudgetPct * 100).toFixed(0) + "%"
                : "\u2014"
        }

        // ---- Ripples (Phase 9D — suppression metrics tooltip) ----
        Text {
            color: "#788090"
            font.family: "Menlo"
            font.pixelSize: 10
            text: "Ripples"
        }
        Text {
            objectName: "suppressionRow"
            Layout.fillWidth: true
            color: "#b4b4c8"
            font.family: "Menlo"
            font.pixelSize: 11
            text: diagnostics.suppression
                ? diagnostics.suppression.emitted
                  + " / -" + diagnostics.suppression.totalSuppressed
                : "\u2014"

            ToolTip.visible: ripplesHover.hovered && diagnostics.suppression
            ToolTip.delay: 200
            ToolTip.timeout: 6000
            ToolTip.text: diagnostics.suppression
                ? diagnostics.suppression.summary
                : ""

            HoverHandler {
                id: ripplesHover
            }
        }
    }
}
