/**
 * React wrapper around the WebGL2 HeatmapRenderer — Bookmap-style heatmap.
 *
 * Price range management
 * ----------------------
 * centrePrice / priceSpan are set once on the first BOOK_UPDATE (auto-range
 * from N closest bid/ask levels) and only change on Ctrl+Wheel or when
 * visibleLevels is adjusted.  Keeping centrePrice stable means every depth
 * column and every bubble share the same y-axis mapping — no drift.
 *
 * Controls
 * --------
 *   Drag          — scroll left/right through history
 *   Ctrl+Wheel    — zoom price axis (clears buffer)
 *   Plain Wheel   — scroll time axis
 */

import { useEffect, useMemo, useRef, useState } from 'react';
import { HeatmapRenderer, PRICE_LEVELS } from '../../charts/webgl/HeatmapRenderer';
import { DepthAggregator } from '../../engine/DepthAggregator';
import { useStreamClient } from '../../ws/useStreamClient';
import { useTradingStore } from '../../stores/trading';

export interface HeatmapChartProps {
  visibleLevels?: number;
  onRangeChange?: (priceMin: number, priceMax: number) => void;
}

// ---------------------------------------------------------------------------
// Price axis overlay — renders on top of the WebGL canvas
// ---------------------------------------------------------------------------

function pickInterval(span: number): number {
  const target = 8; // desired number of labels
  const raw = span / target;
  const mag = Math.pow(10, Math.floor(Math.log10(raw)));
  for (const m of [1, 2, 5, 10, 25, 50]) {
    if (span / (mag * m) <= target + 1) return mag * m;
  }
  return mag * 10;
}

function PriceAxis({
  priceMin,
  priceMax,
  currentPrice,
}: {
  priceMin: number;
  priceMax: number;
  currentPrice: number | null;
}): JSX.Element {
  const span = priceMax - priceMin;
  const interval = pickInterval(span);
  const p2y = (p: number): string => `${((priceMax - p) / span) * 100}%`;

  const first = Math.ceil(priceMin / interval) * interval;
  const labels: number[] = [];
  for (let p = first; p <= priceMax + 1e-9; p += interval) {
    labels.push(Math.round(p * 1e6) / 1e6);
  }

  return (
    <div
      style={{
        position: 'absolute',
        right: 0,
        top: 0,
        bottom: 0,
        width: 70,
        background: 'rgba(8, 11, 18, 0.82)',
        borderLeft: '1px solid #1d2330',
        pointerEvents: 'none',
        overflow: 'hidden',
      }}
    >
      {labels.map((price) => {
        const y = ((priceMax - price) / span) * 100;
        if (y < 0 || y > 100) return null;
        return (
          <div
            key={price}
            style={{
              position: 'absolute',
              right: 5,
              top: `${y}%`,
              transform: 'translateY(-50%)',
              fontSize: 10,
              fontFamily: 'monospace',
              color: '#5a6472',
              lineHeight: 1,
              whiteSpace: 'nowrap',
            }}
          >
            {price.toFixed(0)}
          </div>
        );
      })}

      {/* Tick marks */}
      {labels.map((price) => {
        const y = ((priceMax - price) / span) * 100;
        if (y < 0 || y > 100) return null;
        return (
          <div
            key={`tick-${price}`}
            style={{
              position: 'absolute',
              left: 0,
              top: `${y}%`,
              width: 4,
              height: 1,
              background: '#1d2330',
            }}
          />
        );
      })}

      {/* Current price badge */}
      {currentPrice !== null &&
        currentPrice >= priceMin &&
        currentPrice <= priceMax && (
          <div
            style={{
              position: 'absolute',
              left: 0,
              right: 0,
              top: p2y(currentPrice),
              transform: 'translateY(-50%)',
              background: '#d4a017',
              color: '#000',
              fontSize: 10,
              fontFamily: 'monospace',
              fontWeight: 700,
              textAlign: 'center',
              padding: '2px 3px',
              lineHeight: 1.3,
              zIndex: 2,
              borderRadius: 2,
            }}
          >
            {currentPrice.toFixed(1)}
          </div>
        )}
    </div>
  );
}

// ---------------------------------------------------------------------------
// HeatmapChart
// ---------------------------------------------------------------------------

export function HeatmapChart({
  visibleLevels = 20,
  onRangeChange,
}: HeatmapChartProps): JSX.Element {
  const canvasRef = useRef<HTMLCanvasElement | null>(null);
  const rendererRef = useRef<HeatmapRenderer | null>(null);
  const aggregatorRef = useRef<DepthAggregator | null>(null);
  const dragStartRef = useRef<{ x: number; scroll: number } | null>(null);
  const autoRangedRef = useRef(false);
  const prevLevelsRef = useRef(visibleLevels);
  const onRangeChangeRef = useRef(onRangeChange);
  useEffect(() => { onRangeChangeRef.current = onRangeChange; });

  // Local state for the price axis overlay.
  const [axisRange, setAxisRange] = useState<{ min: number; max: number } | null>(null);
  // lastTrade used for the current-price line display only (not for aggregator).
  const lastTrade = useTradingStore((s) => s.lastTrade);

  const client = useStreamClient();

  useEffect(() => {
    aggregatorRef.current = new DepthAggregator({
      priceLevels: PRICE_LEVELS,
      tickSize: 1,
      centrePrice: 0,
      priceSpan: 500,
    });
    autoRangedRef.current = false;
  }, []);

  useEffect(() => {
    if (prevLevelsRef.current === visibleLevels) return;
    prevLevelsRef.current = visibleLevels;
    autoRangedRef.current = false;
    rendererRef.current?.clearBuffer();
    setAxisRange(null);
  }, [visibleLevels]);

  // Helper: notify parent + update local axis range state.
  const notifyRange = (min: number, max: number): void => {
    setAxisRange({ min, max });
    onRangeChangeRef.current?.(min, max);
  };

  useEffect(() => {
    const canvas = canvasRef.current;
    if (canvas === null) return;
    const renderer = new HeatmapRenderer(canvas);
    rendererRef.current = renderer;
    renderer.start();

    const offBook = client.on('BOOK_UPDATE', (msg) => {
      const agg = aggregatorRef.current;
      if (agg === null) return;

      // Skip empty book snapshots — the C++ engine hasn't been seeded yet.
      // Pushing all-zero columns before the price axis is initialised wastes
      // ring-buffer history and keeps the heatmap blank even after real data
      // arrives (centrePrice would stay at 0, mapping BTC ~100k out of range).
      if (msg.bids.length === 0 || msg.asks.length === 0) return;

      if (!autoRangedRef.current && msg.bids.length > 0 && msg.asks.length > 0) {
        const sortedBids = [...msg.bids].sort(
          ([a]: [number, number], [b]: [number, number]) => b - a,
        );
        const sortedAsks = [...msg.asks].sort(
          ([a]: [number, number], [b]: [number, number]) => a - b,
        );
        const n = Math.min(visibleLevels, sortedBids.length, sortedAsks.length);
        if (n > 0) {
          const bestBid = sortedBids[0][0];
          const bestAsk = sortedAsks[0][0];
          const worstBid = sortedBids[n - 1][0];
          const worstAsk = sortedAsks[n - 1][0];
          const span = Math.max(worstAsk - worstBid, 1);
          const newCentre = (bestBid + bestAsk) / 2;
          const newSpan = span * 1.3;
          agg.setConfig({ centrePrice: newCentre, priceSpan: newSpan });
          renderer.setCentrePrice(newCentre, newSpan);
          autoRangedRef.current = true;
          notifyRange(newCentre - newSpan / 2, newCentre + newSpan / 2);
        }
      }

      renderer.pushColumn(agg.aggregate(msg));
    });

    const offTrade = client.on('TRADE', (msg) => {
      if (!autoRangedRef.current) return;
      renderer.pushBubble(msg.price, msg.qty, msg.side);
    });

    return () => {
      offBook();
      offTrade();
      renderer.stop();
      rendererRef.current = null;
    };
  }, [client]); // eslint-disable-line react-hooks/exhaustive-deps

  const handlers = useMemo(
    () => ({
      onMouseDown: (e: React.MouseEvent<HTMLCanvasElement>) => {
        const r = rendererRef.current;
        if (r === null) return;
        dragStartRef.current = { x: e.clientX, scroll: r.scrollOffset };
      },
      onMouseMove: (e: React.MouseEvent<HTMLCanvasElement>) => {
        const r = rendererRef.current;
        const s = dragStartRef.current;
        if (r === null || s === null) return;
        r.setScrollOffset(Math.max(0, s.scroll - (e.clientX - s.x)));
      },
      onMouseUp: () => { dragStartRef.current = null; },
      onWheel: (e: React.WheelEvent<HTMLCanvasElement>) => {
        e.preventDefault();
        const r = rendererRef.current;
        const agg = aggregatorRef.current;
        if (r === null || agg === null) return;
        if (e.ctrlKey || e.metaKey) {
          const factor = e.deltaY > 0 ? 1.15 : 0.87;
          const cfg = agg.getConfig();
          const newSpan = Math.max(5, cfg.priceSpan * factor);
          agg.setConfig({ priceSpan: newSpan });
          r.setCentrePrice(cfg.centrePrice, newSpan);
          r.clearBuffer();
          autoRangedRef.current = true;
          notifyRange(cfg.centrePrice - newSpan / 2, cfg.centrePrice + newSpan / 2);
        } else {
          r.setScrollOffset(Math.max(0, r.scrollOffset + e.deltaY * 0.5));
        }
      },
    }),
    [], // eslint-disable-line react-hooks/exhaustive-deps
  );

  const currentPrice = lastTrade?.price ?? null;

  return (
    <div style={{ position: 'relative', width: '100%', height: '100%', overflow: 'hidden' }}>
      {/* WebGL heatmap — takes full width; price axis overlays the right edge */}
      <canvas
        ref={canvasRef}
        style={{ width: '100%', height: '100%', display: 'block', cursor: 'crosshair' }}
        {...handlers}
      />

      {/* Current price horizontal hairline */}
      {axisRange !== null && currentPrice !== null &&
        currentPrice >= axisRange.min && currentPrice <= axisRange.max && (
          <div
            style={{
              position: 'absolute',
              left: 0,
              right: 70,
              top: `${((axisRange.max - currentPrice) / (axisRange.max - axisRange.min)) * 100}%`,
              height: 1,
              background: 'rgba(212, 160, 23, 0.6)',
              pointerEvents: 'none',
            }}
          />
        )}

      {/* Price axis */}
      {axisRange !== null && (
        <PriceAxis
          priceMin={axisRange.min}
          priceMax={axisRange.max}
          currentPrice={currentPrice}
        />
      )}
    </div>
  );
}
