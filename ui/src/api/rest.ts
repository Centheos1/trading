/**
 * Thin REST client for the strategy service.  All endpoints go through
 * the Vite/nginx proxy so the browser only ever talks to the UI origin.
 */

const BASE = '/api';

async function jsonFetch<T>(path: string, init: RequestInit = {}): Promise<T> {
  const res = await fetch(`${BASE}${path}`, {
    headers: { 'Content-Type': 'application/json', ...(init.headers ?? {}) },
    ...init,
  });
  if (!res.ok) {
    const text = await res.text().catch(() => '(no body)');
    throw new Error(`${res.status} ${res.statusText}: ${text}`);
  }
  return (await res.json()) as T;
}

export interface HealthResponse {
  status: string;
  engine: {
    symbols: string[];
    bucket_ms?: number;
    book_emit_hz?: number;
    engine_running?: boolean;
  };
}

export interface CandleRow {
  ts_ms: number;
  o: number;
  h: number;
  l: number;
  c: number;
  vol: number;
  buy_vol: number;
  sell_vol: number;
}

export interface CandlesResponse {
  symbol: string;
  candles: CandleRow[];
}

export const api = {
  health: () => jsonFetch<HealthResponse>('/health'),
  getCandles: (symbol: string, limit = 200) =>
    jsonFetch<CandlesResponse>(`/candles?symbol=${encodeURIComponent(symbol)}&limit=${limit}`),
  getStrategy: () => jsonFetch<Record<string, unknown>>('/strategy'),
  patchStrategy: (patch: Record<string, unknown>) =>
    jsonFetch<Record<string, unknown>>('/strategy', {
      method: 'PATCH',
      body: JSON.stringify(patch),
    }),
  runBacktest: (req: BacktestRequest) =>
    jsonFetch<BacktestResult>('/backtest', {
      method: 'POST',
      body: JSON.stringify(req),
    }),
  startOptimise: (req: OptimiseRequest) =>
    jsonFetch<{ job_id: string; status: string }>('/optimise', {
      method: 'POST',
      body: JSON.stringify(req),
    }),
  getOptimise: (jobId: string) =>
    jsonFetch<OptimiseStatus>(`/optimise/${jobId}`),
};

export interface BacktestRequest {
  strategy: string;
  exchange?: string;
  symbol: string;
  tf: string;
  from_time: number;
  to_time: number;
  params: Record<string, unknown>;
}

export interface BacktestResult {
  status: string;
  result: {
    pnl: number;
    max_drawdown: number;
    num_trades: number;
    sharpe_ratio: number;
    cagr: number;
  };
}

export interface OptimiseRequest extends BacktestRequest {
  population_size?: number;
  generations?: number;
}

export interface OptimiseStatus {
  job_id: string;
  status: string;
  progress: number;
  result: unknown;
  error: string | null;
}
