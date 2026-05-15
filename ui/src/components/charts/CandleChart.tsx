import { useEffect, useRef } from 'react';
import { CandleRenderer } from '../../charts/webgl/CandleRenderer';
import { useTradingStore } from '../../stores/trading';

export function CandleChart(): JSX.Element {
  const canvasRef = useRef<HTMLCanvasElement | null>(null);
  const rendererRef = useRef<CandleRenderer | null>(null);
  const candles = useTradingStore((s) => s.candles);

  useEffect(() => {
    const canvas = canvasRef.current;
    if (canvas === null) return;
    const r = new CandleRenderer(canvas);
    rendererRef.current = r;
    r.start();
    return () => {
      r.stop();
      rendererRef.current = null;
    };
  }, []);

  useEffect(() => {
    rendererRef.current?.setCandles(candles);
  }, [candles]);

  return (
    <canvas
      ref={canvasRef}
      style={{ width: '100%', height: '100%', display: 'block' }}
    />
  );
}
