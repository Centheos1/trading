/**
 * Central Zustand store for live trading state.
 *
 * Holds:
 *   * connection status
 *   * last seen TRADE per symbol (for the candle in-bucket update)
 *   * candle history (in-memory, per-symbol, per-bucket-ms)
 *   * latest CVD value
 *   * latest volume profile bars
 *   * recent signals (rolling buffer, capped)
 *   * viewport state shared by all charts (scroll / zoom / price range)
 *
 * BOOK_UPDATE messages are intentionally NOT stored here.  They are
 * routed directly to the ``DepthAggregator``/``HeatmapRenderer`` because
 * the depth ring buffer lives on the GPU and copying it through React
 * state would defeat the architecture.  Components subscribe to the WS
 * client's BOOK_UPDATE events directly via ``useStreamClient()``.
 */

import { create } from 'zustand';
import type {
  CandleMsg,
  CvdUpdateMsg,
  HealthMsg,
  SignalMsg,
  TradeMsg,
  VpBar,
  VpUpdateMsg,
} from '../ws/messages';

/** A single OHLCV bucket. */
export interface Candle {
  ts_ms: number;
  o: number;
  h: number;
  l: number;
  c: number;
  vol: number;
  buy_vol: number;
  sell_vol: number;
}

const MAX_CANDLES = 1000;
const MAX_SIGNALS = 200;
const MAX_CVD_POINTS = 5000;

/** Shared viewport state — every chart subscribes to this. */
export interface ViewportState {
  /** Right-edge timestamp (live = ``Date.now()``). */
  rightEdgeMs: number;
  /** Number of milliseconds visible on the time axis. */
  timeWindowMs: number;
  /** Price-axis min / max (auto-scale when ``autoScalePrice`` is true). */
  priceMin: number;
  priceMax: number;
  autoScalePrice: boolean;
}

interface TradingState {
  connected: boolean;
  health: HealthMsg | null;
  symbol: string;
  bucketMs: number;
  lastTrade: TradeMsg | null;
  candles: Candle[];
  cvd: { ts_ms: number; value: number }[];
  vp: VpBar[];
  signals: SignalMsg[];
  viewport: ViewportState;

  // mutations
  setConnected: (c: boolean) => void;
  setSymbol: (s: string) => void;
  setBucketMs: (ms: number) => void;
  ingestTrade: (m: TradeMsg) => void;
  ingestCandle: (m: CandleMsg) => void;
  preloadCandles: (rows: Candle[]) => void;
  ingestCvd: (m: CvdUpdateMsg) => void;
  ingestVp: (m: VpUpdateMsg) => void;
  ingestSignal: (m: SignalMsg) => void;
  ingestHealth: (m: HealthMsg) => void;
  setViewport: (patch: Partial<ViewportState>) => void;
}

const DEFAULT_VIEWPORT: ViewportState = {
  rightEdgeMs: Date.now(),
  timeWindowMs: 80 * 60_000, // 80 one-minute buckets
  priceMin: 0,
  priceMax: 0,
  autoScalePrice: true,
};

export const useTradingStore = create<TradingState>((set, get) => ({
  connected: false,
  health: null,
  symbol: 'BTCUSDT',
  bucketMs: 60_000,
  lastTrade: null,
  candles: [],
  cvd: [],
  vp: [],
  signals: [],
  viewport: DEFAULT_VIEWPORT,

  setConnected: (c) => set({ connected: c }),
  setSymbol: (s) => set({ symbol: s, candles: [], cvd: [], vp: [], signals: [] }),
  setBucketMs: (ms) => set({ bucketMs: ms, candles: [] }),

  ingestTrade: (m) => {
    const state = get();
    if (m.symbol !== state.symbol) return;

    // Update the live (rightmost) candle in place.
    const bucketTs = Math.floor(m.ts_ms / state.bucketMs) * state.bucketMs;
    const candles = state.candles.slice();
    const last = candles[candles.length - 1];
    if (last !== undefined && last.ts_ms === bucketTs) {
      const updated: Candle = {
        ...last,
        h: Math.max(last.h, m.price),
        l: Math.min(last.l, m.price),
        c: m.price,
        vol: last.vol + m.qty,
        buy_vol: last.buy_vol + (m.side === 'BUY' ? m.qty : 0),
        sell_vol: last.sell_vol + (m.side === 'SELL' ? m.qty : 0),
      };
      candles[candles.length - 1] = updated;
    } else if (last === undefined || bucketTs > last.ts_ms) {
      candles.push({
        ts_ms: bucketTs,
        o: m.price,
        h: m.price,
        l: m.price,
        c: m.price,
        vol: m.qty,
        buy_vol: m.side === 'BUY' ? m.qty : 0,
        sell_vol: m.side === 'SELL' ? m.qty : 0,
      });
      if (candles.length > MAX_CANDLES) {
        candles.splice(0, candles.length - MAX_CANDLES);
      }
    }
    set({ lastTrade: m, candles });
  },

  ingestCandle: (m) => {
    const state = get();
    if (m.symbol !== state.symbol) return;
    if (m.bucket_ms !== state.bucketMs) return;
    const candles = state.candles.slice();
    const idx = candles.findIndex((c) => c.ts_ms === m.ts_ms);
    const candle: Candle = {
      ts_ms: m.ts_ms,
      o: m.o,
      h: m.h,
      l: m.l,
      c: m.c,
      vol: m.vol,
      buy_vol: m.buy_vol,
      sell_vol: m.sell_vol,
    };
    if (idx >= 0) {
      candles[idx] = candle;
    } else {
      candles.push(candle);
      candles.sort((a, b) => a.ts_ms - b.ts_ms);
      if (candles.length > MAX_CANDLES) {
        candles.splice(0, candles.length - MAX_CANDLES);
      }
    }
    set({ candles });
  },

  preloadCandles: (rows) => {
    const sorted = [...rows].sort((a, b) => a.ts_ms - b.ts_ms);
    set({ candles: sorted.slice(-MAX_CANDLES) });
  },

  ingestCvd: (m) => {
    const state = get();
    if (m.symbol !== state.symbol) return;
    const cvd = state.cvd.slice();
    cvd.push({ ts_ms: m.ts_ms, value: m.value });
    if (cvd.length > MAX_CVD_POINTS) {
      cvd.splice(0, cvd.length - MAX_CVD_POINTS);
    }
    set({ cvd });
  },

  ingestVp: (m) => {
    const state = get();
    if (m.symbol !== state.symbol) return;
    set({ vp: m.bars });
  },

  ingestSignal: (m) => {
    const state = get();
    if (m.symbol !== state.symbol) return;
    const signals = state.signals.slice();
    signals.push(m);
    if (signals.length > MAX_SIGNALS) {
      signals.splice(0, signals.length - MAX_SIGNALS);
    }
    set({ signals });
  },

  ingestHealth: (m) => set({ health: m }),

  setViewport: (patch) =>
    set((s) => ({ viewport: { ...s.viewport, ...patch } })),
}));
