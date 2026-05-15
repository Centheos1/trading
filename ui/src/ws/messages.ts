/**
 * TypeScript mirrors of the msgpack payloads emitted by
 * ``strategy/engine/stream_emitter.py``.
 *
 * Keep this file 1:1 with the Python side — the discriminated union is
 * the contract between the two services.  When you add a message type,
 * add the matching factory in stream_emitter.py and update the union
 * below.
 */

export interface TradeMsg {
  type: 'TRADE';
  symbol: string;
  ts_ms: number;
  price: number;
  qty: number;
  side: 'BUY' | 'SELL';
}

export interface BookUpdateMsg {
  type: 'BOOK_UPDATE';
  symbol: string;
  ts_ms: number;
  /** [price, size] pairs, best-first (bids descending, asks ascending). */
  bids: [number, number][];
  asks: [number, number][];
}

export interface CandleMsg {
  type: 'CANDLE';
  symbol: string;
  ts_ms: number;
  bucket_ms: number;
  o: number;
  h: number;
  l: number;
  c: number;
  vol: number;
  buy_vol: number;
  sell_vol: number;
}

export interface CvdUpdateMsg {
  type: 'CVD_UPDATE';
  symbol: string;
  ts_ms: number;
  value: number;
}

export interface VpBar {
  price: number;
  total: number;
  buy: number;
  sell: number;
}

export interface VpUpdateMsg {
  type: 'VP_UPDATE';
  symbol: string;
  ts_ms: number;
  bars: VpBar[];
}

export interface SignalMsg {
  type: 'SIGNAL';
  symbol: string;
  ts_ms: number;
  signal_type: string;
  price: number;
  stop: number;
  target: number;
  strength: number;
}

export interface HealthMsg {
  type: 'HEALTH';
  ts_ms: number;
  lag_ms: number;
  symbols: string[];
  engine_state: string;
}

export type StreamMsg =
  | TradeMsg
  | BookUpdateMsg
  | CandleMsg
  | CvdUpdateMsg
  | VpUpdateMsg
  | SignalMsg
  | HealthMsg;
