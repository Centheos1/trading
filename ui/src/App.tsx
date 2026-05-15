/**
 * Top-level component.
 *
 * Two view modes toggled by the "Candles / Heatmap" buttons in the sidebar.
 * BOTH layouts are always mounted and absolutely stacked in the same space.
 * Only the active one is visible (visibility: visible) — the inactive one is
 * visibility: hidden so:
 *
 *  ① Its canvas retains non-zero layout dimensions (unlike display:none),
 *    so ResizeObserver keeps firing correctly.
 *  ② The WebGL context, GPU texture, and CPU ring buffer are all preserved.
 *  ③ BOOK_UPDATE messages continue to fill the HeatmapRenderer ring buffer
 *    even while the user is looking at the candle chart — no data loss on
 *    switching back.
 *
 *  CANDLE MODE  (visibility: visible)
 *  ┌──────────────────────────────────────┬──────┬──────────┐
 *  │ Candlestick + SMA/EMA/VWAP overlays │  VP  │ Sidebar  │
 *  └──────────────────────────────────────┴──────┴──────────┘
 *
 *  HEATMAP MODE  (visibility: visible)
 *  ┌──────────────────────────────────────┬──────┬──────────┐
 *  │ Heatmap                (flex: 1)     │  VP  │ Sidebar  │
 *  ├──────────────────────────────────────┘      │          │
 *  │ CVD (fixed height)     │     spacer         │          │
 *  └──────────────────────────────────────────────┴──────────┘
 */

import { useCallback, useState } from 'react';
import { Layout, ChartFrame, Divider } from './components/Layout';
import { ControlPanel, type ViewMode } from './components/ControlPanel';
import { StrategyPanel } from './components/StrategyPanel';
import { BacktestPanel } from './components/BacktestPanel';
import { HeatmapChart } from './components/charts/HeatmapChart';
import { CandleChart } from './components/charts/CandleChart';
import { CvdChart } from './components/charts/CvdChart';
import { VolumeProfileChart } from './components/charts/VolumeProfileChart';
import { useStreamWiring } from './ws/useStreamClient';

const DIVIDER_PX = 4;
const VP_DEFAULT = 140;
const CVD_DEFAULT = 160;
const HEATMAP_LEVELS_DEFAULT = 20;

export function App(): JSX.Element {
  useStreamWiring();

  const [viewMode, setViewMode] = useState<ViewMode>('candle');
  const [vpWidth, setVpWidth] = useState(VP_DEFAULT);
  const [cvdH, setCvdH] = useState(CVD_DEFAULT);
  const [heatmapLevels, setHeatmapLevels] = useState(HEATMAP_LEVELS_DEFAULT);

  const onVpDrag = (d: number): void =>
    setVpWidth((w) => Math.max(80, Math.min(300, w - d)));
  const onCvdDrag = (d: number): void =>
    setCvdH((h) => Math.max(60, Math.min(400, h + d)));

  const sidebar = (
    <>
      <ControlPanel
        viewMode={viewMode}
        setViewMode={setViewMode}
        heatmapLevels={heatmapLevels}
        setHeatmapLevels={setHeatmapLevels}
      />
      <StrategyPanel />
      <BacktestPanel />
      <div
        style={{
          padding: '10px 12px',
          fontSize: 10,
          color: '#484f58',
          textAlign: 'center',
          marginTop: 'auto',
        }}
      >
        Distributed Trading Stack · React/WebGL2 · Headless Strategy
      </div>
    </>
  );

  // Both layouts live in the DOM simultaneously. Use an absolutely-positioned
  // wrapper so they occupy the same rectangle; visibility toggles which is seen.
  const main = (
    <div style={{ flex: 1, position: 'relative', overflow: 'hidden', minWidth: 0 }}>
      <ModePane visible={viewMode === 'candle'}>
        <CandleLayout vpWidth={vpWidth} onVpDrag={onVpDrag} />
      </ModePane>
      <ModePane visible={viewMode === 'heatmap'}>
        <HeatmapLayout
          vpWidth={vpWidth}
          onVpDrag={onVpDrag}
          cvdH={cvdH}
          onCvdDrag={onCvdDrag}
          heatmapLevels={heatmapLevels}
        />
      </ModePane>
    </div>
  );

  return <Layout main={main} sidebar={sidebar} />;
}

// ---------------------------------------------------------------------------
// Invisible wrapper that preserves canvas layout dimensions
// ---------------------------------------------------------------------------

function ModePane({
  visible,
  children,
}: {
  visible: boolean;
  children: React.ReactNode;
}): JSX.Element {
  return (
    <div
      style={{
        position: 'absolute',
        inset: 0,
        display: 'flex',
        // visibility:hidden keeps the element in the layout flow so the
        // canvas always has a non-zero bounding rect and ResizeObserver
        // keeps working.  The WebGL context + GPU buffers are preserved.
        visibility: visible ? 'visible' : 'hidden',
        // Block pointer events on the inactive pane so the active one
        // receives all mouse/wheel events.
        pointerEvents: visible ? 'auto' : 'none',
      }}
    >
      {children}
    </div>
  );
}

// ---------------------------------------------------------------------------
// Candle mode  ─  full-height candle chart | VP (price-axis aligned)
// ---------------------------------------------------------------------------

interface CandleLayoutProps {
  vpWidth: number;
  onVpDrag: (delta: number) => void;
}

function CandleLayout({ vpWidth, onVpDrag }: CandleLayoutProps): JSX.Element {
  return (
    <div
      style={{
        flex: 1,
        display: 'flex',
        minHeight: 0,
        minWidth: 0,
      }}
    >
      <div style={{ flex: 1, minWidth: 0 }}>
        <ChartFrame title="Candles · SMA · VWAP">
          <CandleChart />
        </ChartFrame>
      </div>

      <Divider axis="h" onDrag={onVpDrag} />

      {/* Volume Profile — price-axis aligned with candles */}
      <div style={{ width: vpWidth, flexShrink: 0 }}>
        <ChartFrame title="Vol Profile">
          <VolumeProfileChart />
        </ChartFrame>
      </div>
    </div>
  );
}

// ---------------------------------------------------------------------------
// Heatmap mode  ─  [Heatmap | VP] over [CVD | spacer]
// ---------------------------------------------------------------------------

interface HeatmapLayoutProps {
  vpWidth: number;
  onVpDrag: (delta: number) => void;
  cvdH: number;
  onCvdDrag: (delta: number) => void;
  heatmapLevels: number;
}

function HeatmapLayout({
  vpWidth,
  onVpDrag,
  cvdH,
  onCvdDrag,
  heatmapLevels,
}: HeatmapLayoutProps): JSX.Element {
  // Track the heatmap's visible price range so the VP can align to it.
  const [priceMin, setPriceMin] = useState(0);
  const [priceMax, setPriceMax] = useState(0);
  const handleRangeChange = useCallback((lo: number, hi: number) => {
    setPriceMin(lo);
    setPriceMax(hi);
  }, []);

  return (
    <div
      style={{
        flex: 1,
        display: 'flex',
        flexDirection: 'column',
        minHeight: 0,
        minWidth: 0,
      }}
    >
      {/* ── Top: Heatmap | VP (price-axis aligned) ─────────────────── */}
      <div style={{ flex: 1, display: 'flex', minHeight: 0 }}>
        <div style={{ flex: 1, minWidth: 0 }}>
          <ChartFrame title="Heatmap">
            <HeatmapChart
              visibleLevels={heatmapLevels}
              onRangeChange={handleRangeChange}
            />
          </ChartFrame>
        </div>

        <Divider axis="h" onDrag={onVpDrag} />

        {/* VP shares the heatmap's price range → price axes align */}
        <div style={{ width: vpWidth, flexShrink: 0 }}>
          <ChartFrame title="Vol Profile">
            <VolumeProfileChart priceMin={priceMin} priceMax={priceMax} />
          </ChartFrame>
        </div>
      </div>

      <Divider axis="v" onDrag={onCvdDrag} />

      {/* ── Bottom: CVD | spacer (time-axis aligned with heatmap) ───── */}
      {/*
       * The spacer is vpWidth + DIVIDER_PX wide so the CVD chart has
       * exactly the same width as the heatmap above — time axes stay
       * aligned when the VP panel is resized.
       */}
      <div style={{ height: cvdH, flexShrink: 0, display: 'flex', minHeight: 0 }}>
        <div style={{ flex: 1, minWidth: 0 }}>
          <ChartFrame title="CVD">
            <CvdChart />
          </ChartFrame>
        </div>
        <div
          style={{
            width: vpWidth + DIVIDER_PX,
            flexShrink: 0,
            background: '#0d1117',
            borderLeft: '1px solid #21262d',
          }}
        />
      </div>
    </div>
  );
}
