/**
 * Shell layout: main chart area on the left, sidebar always visible on the right.
 *
 *   ┌─────────────────────────────────────┬──────────────────┐
 *   │ main (supplied by App)              │ sidebar          │
 *   │                                     │  ControlPanel    │
 *   │                                     │  StrategyPanel   │
 *   │                                     │  BacktestPanel   │
 *   └─────────────────────────────────────┴──────────────────┘
 *
 * The sidebar is always visible and scrolls independently.
 * Chart arrangements are composed inside `main` by App.tsx.
 */

import { useCallback, useRef } from 'react';
import type { ReactNode } from 'react';

// ---------------------------------------------------------------------------
// Draggable divider
// ---------------------------------------------------------------------------

interface DividerProps {
  axis: 'h' | 'v';
  onDrag: (delta: number) => void;
}

export function Divider({ axis, onDrag }: DividerProps): JSX.Element {
  const drag = useRef<{ x: number; y: number } | null>(null);
  const onMouseMove = useCallback(
    (e: MouseEvent) => {
      if (drag.current === null) return;
      const delta = axis === 'h' ? e.clientX - drag.current.x : e.clientY - drag.current.y;
      onDrag(delta);
      drag.current = { x: e.clientX, y: e.clientY };
    },
    [axis, onDrag],
  );
  const stop = useCallback(() => {
    drag.current = null;
    document.removeEventListener('mousemove', onMouseMove);
    document.removeEventListener('mouseup', stop);
  }, [onMouseMove]);
  const start = useCallback(
    (e: React.MouseEvent) => {
      drag.current = { x: e.clientX, y: e.clientY };
      document.addEventListener('mousemove', onMouseMove);
      document.addEventListener('mouseup', stop);
    },
    [onMouseMove, stop],
  );
  return (
    <div
      onMouseDown={start}
      style={{
        background: '#1c2128',
        cursor: axis === 'h' ? 'col-resize' : 'row-resize',
        width: axis === 'h' ? 4 : '100%',
        height: axis === 'v' ? 4 : '100%',
        flexShrink: 0,
        zIndex: 1,
      }}
    />
  );
}

// ---------------------------------------------------------------------------
// Top-level layout shell
// ---------------------------------------------------------------------------

export interface LayoutProps {
  main: ReactNode;
  sidebar: ReactNode;
}

export function Layout({ main, sidebar }: LayoutProps): JSX.Element {
  return (
    <div
      style={{
        display: 'flex',
        height: '100%',
        width: '100%',
        overflow: 'hidden',
      }}
    >
      {/* Chart area */}
      <div style={{ flex: 1, minWidth: 0, display: 'flex', overflow: 'hidden' }}>
        {main}
      </div>

      {/* Sidebar — always visible, scrolls its own content */}
      <div
        style={{
          width: 300,
          minWidth: 240,
          flexShrink: 0,
          background: '#0e1117',
          borderLeft: '1px solid #21262d',
          overflowY: 'auto',
          display: 'flex',
          flexDirection: 'column',
        }}
      >
        {sidebar}
      </div>
    </div>
  );
}

// ---------------------------------------------------------------------------
// Titled frame around a single chart
// ---------------------------------------------------------------------------

export function ChartFrame({
  title,
  children,
}: {
  title: string;
  children: ReactNode;
}): JSX.Element {
  return (
    <div
      style={{
        height: '100%',
        width: '100%',
        display: 'flex',
        flexDirection: 'column',
        background: '#0d1117',
        minHeight: 0,
        minWidth: 0,
      }}
    >
      <div
        style={{
          padding: '3px 10px',
          fontSize: 11,
          letterSpacing: 1,
          textTransform: 'uppercase',
          color: '#7d8590',
          borderBottom: '1px solid #21262d',
          background: '#161b22',
          flexShrink: 0,
          userSelect: 'none',
        }}
      >
        {title}
      </div>
      <div style={{ flex: 1, minHeight: 0, minWidth: 0 }}>{children}</div>
    </div>
  );
}
