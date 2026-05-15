/**
 * Top-of-sidebar control panel — symbol picker, candle bucket size,
 * connection status, basic display preferences for the heatmap.
 *
 * Display-side controls (tick size, intensity, colour gradient) are
 * applied locally; strategy-side controls are forwarded via REST.
 */

import { useTradingStore } from '../stores/trading';

export type ViewMode = 'candle' | 'heatmap';

interface ControlPanelProps {
  viewMode: ViewMode;
  setViewMode: (m: ViewMode) => void;
  heatmapLevels: number;
  setHeatmapLevels: (n: number) => void;
}

export function ControlPanel({
  viewMode,
  setViewMode,
  heatmapLevels,
  setHeatmapLevels,
}: ControlPanelProps): JSX.Element {
  const connected = useTradingStore((s) => s.connected);
  const health = useTradingStore((s) => s.health);
  const symbol = useTradingStore((s) => s.symbol);
  const setSymbol = useTradingStore((s) => s.setSymbol);
  const bucketMs = useTradingStore((s) => s.bucketMs);
  const setBucketMs = useTradingStore((s) => s.setBucketMs);

  return (
    <>
    <div
      style={{
        display: 'flex',
        padding: '8px 12px',
        gap: 6,
        borderBottom: '1px solid #21262d',
        background: '#161b22',
      }}
    >
      {(['candle', 'heatmap'] as ViewMode[]).map((m) => (
        <button
          key={m}
          onClick={() => setViewMode(m)}
          style={{
            flex: 1,
            padding: '5px 0',
            fontSize: 12,
            fontWeight: 600,
            letterSpacing: 0.5,
            textTransform: 'uppercase',
            borderRadius: 4,
            border: 'none',
            cursor: 'pointer',
            background: viewMode === m ? '#1f6feb' : '#21262d',
            color: viewMode === m ? '#ffffff' : '#7d8590',
            transition: 'background 0.15s',
          }}
        >
          {m === 'candle' ? 'Candles' : 'Heatmap'}
        </button>
      ))}
    </div>
    <Section title="Connection">
      <Row label="Status">
        <span style={{ color: connected ? '#0ecb81' : '#f6465d' }}>
          {connected ? 'connected' : 'disconnected'}
        </span>
      </Row>
      {health !== null && (
        <>
          <Row label="Engine">{health.engine_state}</Row>
          <Row label="Symbols">{(health.symbols ?? []).join(', ') || '—'}</Row>
        </>
      )}
      <Row label="Symbol">
        <input
          value={symbol}
          onChange={(e) => setSymbol(e.target.value.toUpperCase())}
          style={inputStyle}
        />
      </Row>
      <Row label="Bucket">
        <select
          value={bucketMs}
          onChange={(e) => setBucketMs(parseInt(e.target.value, 10))}
          style={inputStyle}
        >
          <option value={1_000}>1 s</option>
          <option value={5_000}>5 s</option>
          <option value={15_000}>15 s</option>
          <option value={60_000}>1 m</option>
          <option value={300_000}>5 m</option>
          <option value={900_000}>15 m</option>
          <option value={3_600_000}>1 h</option>
        </select>
      </Row>
    </Section>
    <Section title="Heatmap">
      <Row label="Depth levels">
        <input
          type="number"
          min={5}
          max={50}
          step={1}
          value={heatmapLevels}
          onChange={(e) => {
            const v = parseInt(e.target.value, 10);
            if (Number.isFinite(v) && v >= 5 && v <= 50) setHeatmapLevels(v);
          }}
          style={{ ...inputStyle, minWidth: 56 }}
        />
      </Row>
      <div style={{ fontSize: 10, color: '#484f58', marginTop: 4, lineHeight: 1.4 }}>
        Levels shown each side of the market.<br />
        Ctrl+Wheel to zoom · Drag to scroll history.
      </div>
    </Section>
    </>
  );
}

// ---------------------------------------------------------------------------
// shared bits
// ---------------------------------------------------------------------------

export function Section({
  title,
  children,
}: {
  title: string;
  children: React.ReactNode;
}): JSX.Element {
  return (
    <div
      style={{
        padding: '10px 12px',
        borderBottom: '1px solid #21262d',
      }}
    >
      <h3
        style={{
          margin: 0,
          marginBottom: 8,
          fontSize: 11,
          textTransform: 'uppercase',
          letterSpacing: 1.2,
          color: '#8b949e',
        }}
      >
        {title}
      </h3>
      {children}
    </div>
  );
}

export function Row({
  label,
  children,
}: {
  label: string;
  children: React.ReactNode;
}): JSX.Element {
  return (
    <div
      style={{
        display: 'flex',
        justifyContent: 'space-between',
        alignItems: 'center',
        margin: '4px 0',
        fontSize: 12,
      }}
    >
      <span style={{ color: '#7d8590' }}>{label}</span>
      <span>{children}</span>
    </div>
  );
}

export const inputStyle: React.CSSProperties = {
  background: '#0d1117',
  border: '1px solid #30363d',
  color: '#e6edf3',
  padding: '3px 6px',
  fontSize: 12,
  borderRadius: 4,
  minWidth: 80,
};
