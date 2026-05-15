/**
 * Live strategy parameter editor.
 *
 * All fields are forwarded to the strategy service via
 * ``PATCH /api/strategy``.  Changes take effect on the next signal
 * evaluation without restarting the service or interrupting the
 * WebSocket stream.
 */

import { useEffect, useState } from 'react';
import { api } from '../api/rest';
import { useTradingStore } from '../stores/trading';
import { Section, Row, inputStyle } from './ControlPanel';

interface StrategyParams {
  imbalance_threshold?: number;
  stacked_imbalance_levels?: number;
  absorption_volume_ratio?: number;
  absorption_window_ms?: number;
  cvd_divergence_lookback?: number;
  exhaustion_lookback_bars?: number;
  exhaustion_price_threshold?: number;
  poc_rejection_distance?: number;
  poc_rejection_window_ms?: number;
  signal_strength_min?: number;
}

const FIELDS: { key: keyof StrategyParams; label: string; step?: number }[] = [
  { key: 'imbalance_threshold', label: 'Imbalance threshold', step: 0.1 },
  { key: 'stacked_imbalance_levels', label: 'Stacked levels', step: 1 },
  { key: 'absorption_volume_ratio', label: 'Absorption ratio', step: 0.1 },
  { key: 'absorption_window_ms', label: 'Absorption ms', step: 100 },
  { key: 'cvd_divergence_lookback', label: 'CVD lookback', step: 1 },
  { key: 'exhaustion_lookback_bars', label: 'Exhaustion bars', step: 1 },
  { key: 'exhaustion_price_threshold', label: 'Exhaustion px %', step: 0.001 },
  { key: 'poc_rejection_distance', label: 'POC reject dist', step: 0.1 },
  { key: 'poc_rejection_window_ms', label: 'POC window ms', step: 100 },
  { key: 'signal_strength_min', label: 'Min signal strength', step: 0.05 },
];

export function StrategyPanel(): JSX.Element {
  const [params, setParams] = useState<StrategyParams>({});
  const [pending, setPending] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const signals = useTradingStore((s) => s.signals);

  useEffect(() => {
    api
      .getStrategy()
      .then((p) => setParams(p as StrategyParams))
      .catch((e) => setError(String(e)));
  }, []);

  const update = (key: keyof StrategyParams, value: number): void => {
    setPending(true);
    setError(null);
    const patch = { [key]: value };
    setParams((p) => ({ ...p, ...patch }));
    api
      .patchStrategy(patch)
      .then((p) => setParams(p as StrategyParams))
      .catch((e) => setError(String(e)))
      .finally(() => setPending(false));
  };

  return (
    <>
      <Section title={`Strategy parameters${pending ? ' …' : ''}`}>
        {error !== null && (
          <div style={{ color: '#f6465d', fontSize: 11, marginBottom: 6 }}>
            {error}
          </div>
        )}
        {FIELDS.map(({ key, label, step }) => (
          <Row key={key} label={label}>
            <input
              type="number"
              step={step ?? 0.01}
              value={params[key] ?? ''}
              onChange={(e) => {
                const v = parseFloat(e.target.value);
                if (Number.isFinite(v)) update(key, v);
              }}
              style={inputStyle}
            />
          </Row>
        ))}
      </Section>

      <Section title={`Recent signals (${signals.length})`}>
        <div style={{ maxHeight: 180, overflowY: 'auto', fontSize: 11 }}>
          {signals.slice().reverse().map((s, i) => (
            <div
              key={`${s.ts_ms}-${i}`}
              style={{
                padding: '3px 0',
                borderBottom: '1px solid #21262d',
                color: s.signal_type.includes('BUY') ? '#0ecb81' : '#f6465d',
              }}
            >
              <span style={{ color: '#7d8590', marginRight: 6 }}>
                {new Date(s.ts_ms).toLocaleTimeString()}
              </span>
              {s.signal_type} @ {s.price.toFixed(2)}
            </div>
          ))}
          {signals.length === 0 && (
            <div style={{ color: '#7d8590' }}>no signals yet</div>
          )}
        </div>
      </Section>
    </>
  );
}
