/**
 * Backtest & optimisation control panel.
 *
 * The two flows are:
 *
 *   * Backtest — POST /api/backtest, runs synchronously in a worker
 *     thread on the strategy service, returns JSON result.
 *   * Optimise — POST /api/optimise starts a job; the panel polls
 *     GET /api/optimise/{job_id} until ``status === 'completed'``.
 */

import { useState } from 'react';
import { api, type BacktestResult, type OptimiseStatus } from '../api/rest';
import { useTradingStore } from '../stores/trading';
import { Section, Row, inputStyle } from './ControlPanel';

function isoToMs(s: string): number {
  return new Date(s).getTime();
}
function msToIso(ms: number): string {
  return new Date(ms).toISOString().slice(0, 10);
}

export function BacktestPanel(): JSX.Element {
  const symbol = useTradingStore((s) => s.symbol);
  const [strategy, setStrategy] = useState('orderflow');
  const [tf, setTf] = useState('1m');
  const [from, setFrom] = useState(msToIso(Date.now() - 7 * 86400_000));
  const [to, setTo] = useState(msToIso(Date.now()));
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const [result, setResult] = useState<BacktestResult['result'] | null>(null);
  const [optStatus, setOptStatus] = useState<OptimiseStatus | null>(null);

  const runBacktest = async (): Promise<void> => {
    setBusy(true);
    setError(null);
    setResult(null);
    try {
      const res = await api.runBacktest({
        strategy,
        symbol,
        tf,
        from_time: isoToMs(from),
        to_time: isoToMs(to),
        params: {},
      });
      setResult(res.result);
    } catch (e) {
      setError(String(e));
    } finally {
      setBusy(false);
    }
  };

  const runOptimise = async (): Promise<void> => {
    setBusy(true);
    setError(null);
    setOptStatus(null);
    try {
      const { job_id } = await api.startOptimise({
        strategy,
        symbol,
        tf,
        from_time: isoToMs(from),
        to_time: isoToMs(to),
        params: {},
        population_size: 30,
        generations: 10,
      });
      while (true) {
        const status = await api.getOptimise(job_id);
        setOptStatus(status);
        if (status.status !== 'running') break;
        await new Promise((r) => setTimeout(r, 1000));
      }
    } catch (e) {
      setError(String(e));
    } finally {
      setBusy(false);
    }
  };

  return (
    <Section title="Backtest / optimise">
      {error !== null && (
        <div style={{ color: '#f6465d', fontSize: 11, marginBottom: 6 }}>
          {error}
        </div>
      )}
      <Row label="Strategy">
        <select
          value={strategy}
          onChange={(e) => setStrategy(e.target.value)}
          style={inputStyle}
        >
          <option value="orderflow">orderflow</option>
          <option value="obv">obv</option>
          <option value="ichimoku">ichimoku</option>
          <option value="sup_res">sup/res</option>
        </select>
      </Row>
      <Row label="Timeframe">
        <input value={tf} onChange={(e) => setTf(e.target.value)} style={inputStyle} />
      </Row>
      <Row label="From">
        <input
          type="date"
          value={from}
          onChange={(e) => setFrom(e.target.value)}
          style={inputStyle}
        />
      </Row>
      <Row label="To">
        <input
          type="date"
          value={to}
          onChange={(e) => setTo(e.target.value)}
          style={inputStyle}
        />
      </Row>
      <div style={{ display: 'flex', gap: 8, marginTop: 6 }}>
        <button onClick={runBacktest} disabled={busy} style={btnStyle}>
          {busy ? 'Running…' : 'Run backtest'}
        </button>
        <button onClick={runOptimise} disabled={busy} style={btnStyle}>
          Optimise
        </button>
      </div>

      {result !== null && (
        <div style={{ marginTop: 10, fontSize: 12 }}>
          <Row label="PnL">{result.pnl.toFixed(2)}</Row>
          <Row label="Max DD">{result.max_drawdown.toFixed(2)}</Row>
          <Row label="Trades">{result.num_trades}</Row>
          <Row label="Sharpe">{result.sharpe_ratio.toFixed(2)}</Row>
          <Row label="CAGR">{result.cagr.toFixed(2)}</Row>
        </div>
      )}
      {optStatus !== null && (
        <div style={{ marginTop: 10, fontSize: 12 }}>
          <Row label="Job">{optStatus.job_id.slice(0, 8)}…</Row>
          <Row label="Status">{optStatus.status}</Row>
          <Row label="Progress">{(optStatus.progress * 100).toFixed(0)}%</Row>
        </div>
      )}
    </Section>
  );
}

const btnStyle: React.CSSProperties = {
  ...inputStyle,
  cursor: 'pointer',
  flex: 1,
  textAlign: 'center',
  padding: '6px 8px',
};
