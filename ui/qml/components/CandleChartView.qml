// Phase 9B/9C/9D — chart with embedded toolbar.
//
// Hosts the native ``CandleItem`` (``ui.items.candle_item.CandleItem``)
// which builds the body / wick / volume scene-graph nodes directly on
// the render thread.  Phase 9C wires the trade-overlay lines + ENTRY /
// EXIT triangle markers from ``snapshotModel`` (entry / stop / target
// + active trade state) per UI_STRATEGY_INTEGRATION_PLAN.md §22.4.
//
// Phase 9D adds the chart toolbar (timeframe ComboBox + overlay
// CheckBox row).  The legacy ``MainWindow._candle_combo`` was removed;
// this toolbar is now the SOLE candle-duration control.  Selections
// persist across restarts via ``Qt.labs.settings``.
import QtQuick
import QtQuick.Controls
import QtQuick.Layouts
import QtCore
import Trading.Items 1.0

Item {
    id: root
    property alias bucketMs: candle.bucketMs
    property alias visibleCandles: candle.visibleCandles
    property alias candleItem: candle
    property alias chartToolbar: toolbar
    property alias overlaySettings: overlaySettings

    // Snapshot model is exposed as a context property by
    // ``MainWindowBridge`` (real run) or by the standalone scaffold
    // tests (no overlay).  Use ``typeof`` so the default-binding case
    // resolves to the always-false overlay state.
    property var snapshot: typeof snapshotModel !== "undefined"
        ? snapshotModel : null
    property var bridge: typeof mainWindowBridge !== "undefined"
        ? mainWindowBridge : null

    // Phase 9D — persistent overlay enable flags.  The Settings group
    // shares a namespace with the QML toolbar so subsequent launches
    // restore the user's last toggle state without any extra wiring.
    Settings {
        id: overlaySettings
        category: "Phase9D/ChartOverlays"
        property bool sma: true
        property bool ema: true
        property bool vwap: true
        property bool structural: true
        property bool volprofile: true
    }

    function applyOverlayState() {
        if (!root.bridge) return;
        root.bridge.setOverlayEnabled("sma", overlaySettings.sma);
        root.bridge.setOverlayEnabled("ema", overlaySettings.ema);
        root.bridge.setOverlayEnabled("vwap", overlaySettings.vwap);
        root.bridge.setOverlayEnabled("structural", overlaySettings.structural);
        root.bridge.setOverlayEnabled("volprofile", overlaySettings.volprofile);
    }

    Component.onCompleted: {
        // Replay the persisted state into the bridge as soon as it's
        // available so the legacy candle widget mirrors the QML toolbar
        // immediately on startup.
        applyOverlayState();
    }
    onBridgeChanged: applyOverlayState()

    ColumnLayout {
        anchors.fill: parent
        spacing: 0

        Rectangle {
            id: toolbar
            objectName: "candleChartToolbar"
            Layout.fillWidth: true
            Layout.preferredHeight: 32
            color: "#0c0e1c"
            border.color: "#1a1d33"
            border.width: 1

            RowLayout {
                anchors.fill: parent
                anchors.leftMargin: 6
                anchors.rightMargin: 6
                spacing: 8

                Label {
                    text: "Candle"
                    color: "#8d92ad"
                    font.family: "Menlo"
                    font.pixelSize: 11
                }
                ComboBox {
                    id: candleDurationCombo
                    objectName: "candleDurationCombo"
                    Layout.preferredWidth: 90
                    flat: true
                    model: [
                        { label: "1 min",  ms: 60000   },
                        { label: "5 min",  ms: 300000  },
                        { label: "15 min", ms: 900000  },
                        { label: "30 min", ms: 1800000 },
                        { label: "1 hr",   ms: 3600000 }
                    ]
                    textRole: "label"
                    valueRole: "ms"
                    currentIndex: 0
                    onActivated: function(index) {
                        var ms = candleDurationCombo.currentValue;
                        if (root.bridge && typeof ms === "number" && ms > 0) {
                            root.bridge.setCandleBucketMs(ms);
                        }
                    }
                }

                Item { Layout.preferredWidth: 12 }

                Label {
                    text: "Overlays"
                    color: "#8d92ad"
                    font.family: "Menlo"
                    font.pixelSize: 11
                }
                CheckBox {
                    objectName: "overlayChkSma"
                    text: "SMA"
                    checked: overlaySettings.sma
                    onToggled: {
                        overlaySettings.sma = checked;
                        if (root.bridge) root.bridge.setOverlayEnabled("sma", checked);
                    }
                }
                CheckBox {
                    objectName: "overlayChkEma"
                    text: "EMA"
                    checked: overlaySettings.ema
                    onToggled: {
                        overlaySettings.ema = checked;
                        if (root.bridge) root.bridge.setOverlayEnabled("ema", checked);
                    }
                }
                CheckBox {
                    objectName: "overlayChkVwap"
                    text: "VWAP"
                    checked: overlaySettings.vwap
                    onToggled: {
                        overlaySettings.vwap = checked;
                        if (root.bridge) root.bridge.setOverlayEnabled("vwap", checked);
                    }
                }
                CheckBox {
                    objectName: "overlayChkStructural"
                    text: "H/L"
                    checked: overlaySettings.structural
                    onToggled: {
                        overlaySettings.structural = checked;
                        if (root.bridge) root.bridge.setOverlayEnabled("structural", checked);
                    }
                }
                CheckBox {
                    objectName: "overlayChkVolProfile"
                    text: "VP"
                    checked: overlaySettings.volprofile
                    onToggled: {
                        overlaySettings.volprofile = checked;
                        if (root.bridge) root.bridge.setOverlayEnabled("volprofile", checked);
                    }
                }

                Item { Layout.fillWidth: true }
            }
        }

        Item {
            id: chartContainer
            Layout.fillWidth: true
            Layout.fillHeight: true

            CandleItem {
                id: candle
                objectName: "candleItem"
                anchors.fill: parent

                // Bind overlay state to the snapshot model when one is
                // registered.  ``tradeOverlayActive`` follows the lifecycle
                // label per AC #4; the price fields just track the snapshot.
                tradeOverlayActive: root.snapshot
                    ? root.snapshot.tradeState === "ACTIVE"
                      || root.snapshot.tradeState === "EXPAND"
                      || root.snapshot.tradeState === "CONFIRM"
                      || root.snapshot.tradeState === "OPEN"
                      || root.snapshot.tradeState === "FILLED"
                      || root.snapshot.tradeState === "IN_TRADE"
                    : false
                entryPrice: root.snapshot ? root.snapshot.entryPrice : 0
                stopPrice: root.snapshot ? root.snapshot.stopPrice : 0
                targetPrice: root.snapshot ? root.snapshot.targetPrice : 0
            }

            Rectangle {
                anchors.fill: parent
                color: "transparent"
                border.color: "#1a1d33"
                border.width: 1
            }
        }
    }
}
