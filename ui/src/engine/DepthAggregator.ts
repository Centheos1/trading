/**
 * Receives raw BOOK_UPDATE messages from the strategy service and
 * aggregates them into a 1D depth array sized for the display grid.
 *
 * This is the TypeScript replacement for the deleted Python
 * ``OrderFlowViewModel``.  Crucially:
 *
 *   * Tick size and price range are owned by the UI service.  Changing
 *     either reconfigures this aggregator and re-renders from the local
 *     ring buffer — there is NO backend round-trip.
 *
 *   * The output is a ``Float32Array`` of length ``priceLevels``:
 *
 *         depth[i]  > 0   →  bid liquidity at row i
 *         depth[i]  < 0   →  ask liquidity at row i
 *         depth[i] == 0   →  empty
 *
 *     Row 0 is the top of the chart (highest price); row N-1 is the
 *     bottom (lowest price).  This matches Bookmap's convention.
 *
 *   * It is rendering-agnostic — it just produces a depth column.  The
 *     HeatmapRenderer uploads that column into its ring buffer.
 */

import type { BookUpdateMsg } from '../ws/messages';

export interface DepthGridConfig {
  /** Number of vertical price levels in the display grid. */
  priceLevels: number;
  /** Price quantisation (e.g. 5.0 = $5 buckets). */
  tickSize: number;
  /** Centre price (e.g. last trade or mid).  Range = centre ± span/2. */
  centrePrice: number;
  /** Total price span (priceLevels × tickSize is a reasonable default). */
  priceSpan: number;
}

export interface DepthColumn {
  ts_ms: number;
  /** Length == ``config.priceLevels`` */
  depth: Float32Array;
  /** Top-of-grid price (inclusive) for this column. */
  topPrice: number;
  /** Bottom-of-grid price (inclusive) for this column. */
  bottomPrice: number;
}

export class DepthAggregator {
  private config: DepthGridConfig;

  constructor(config: DepthGridConfig) {
    this.config = { ...config };
  }

  /** Replace the display grid configuration. */
  setConfig(patch: Partial<DepthGridConfig>): void {
    this.config = { ...this.config, ...patch };
  }

  getConfig(): DepthGridConfig {
    return { ...this.config };
  }

  /** Aggregate one BOOK_UPDATE into a single depth column. */
  aggregate(msg: BookUpdateMsg): DepthColumn {
    const { priceLevels, centrePrice, priceSpan } = this.config;
    // ``tickSize`` is part of the config schema but is implicit in
    // ``priceSpan / priceLevels``; we keep the field so the UI can
    // round display labels to the user-selected tick.
    const depth = new Float32Array(priceLevels);

    // The grid runs from ``topPrice`` (row 0) down to ``bottomPrice``
    // (row priceLevels-1).  ``priceSpan`` overrides
    // ``priceLevels × tickSize`` when provided so panning the price
    // axis on the heatmap zooms the price scale without re-bucketing.
    const halfSpan = priceSpan / 2.0;
    const topPrice = centrePrice + halfSpan;
    const bottomPrice = centrePrice - halfSpan;
    const pxPerTick = priceLevels / priceSpan;

    // Bids increment positively; asks decrement (so the renderer can
    // distinguish sign in a single channel).
    for (const [price, size] of msg.bids) {
      const row = Math.floor((topPrice - price) * pxPerTick);
      if (row < 0 || row >= priceLevels) continue;
      depth[row] += size;
    }
    for (const [price, size] of msg.asks) {
      const row = Math.floor((topPrice - price) * pxPerTick);
      if (row < 0 || row >= priceLevels) continue;
      depth[row] -= size;
    }

    return {
      ts_ms: msg.ts_ms,
      depth,
      topPrice,
      bottomPrice,
    };
  }
}
