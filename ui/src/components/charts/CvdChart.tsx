import { useEffect, useRef } from 'react';
import { CVDRenderer } from '../../charts/webgl/CVDRenderer';
import { useTradingStore } from '../../stores/trading';

export function CvdChart(): JSX.Element {
  const canvasRef = useRef<HTMLCanvasElement | null>(null);
  const rendererRef = useRef<CVDRenderer | null>(null);
  const cvd = useTradingStore((s) => s.cvd);

  useEffect(() => {
    const canvas = canvasRef.current;
    if (canvas === null) return;
    const r = new CVDRenderer(canvas);
    rendererRef.current = r;
    r.start();
    return () => {
      r.stop();
      rendererRef.current = null;
    };
  }, []);

  useEffect(() => {
    rendererRef.current?.setSeries(cvd);
  }, [cvd]);

  return (
    <canvas
      ref={canvasRef}
      style={{ width: '100%', height: '100%', display: 'block' }}
    />
  );
}
