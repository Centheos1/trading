import { useEffect, useRef } from 'react';
import { VolumeProfileRenderer } from '../../charts/webgl/VolumeProfileRenderer';
import { useTradingStore } from '../../stores/trading';

interface VolumeProfileChartProps {
  /**
   * When provided, locks the y-axis to this price range so the VP aligns
   * with the heatmap.  Pass (0, 0) or omit to auto-scale from bar data.
   */
  priceMin?: number;
  priceMax?: number;
}

export function VolumeProfileChart({
  priceMin = 0,
  priceMax = 0,
}: VolumeProfileChartProps): JSX.Element {
  const canvasRef = useRef<HTMLCanvasElement | null>(null);
  const rendererRef = useRef<VolumeProfileRenderer | null>(null);
  const vp = useTradingStore((s) => s.vp);

  useEffect(() => {
    const canvas = canvasRef.current;
    if (canvas === null) return;
    const r = new VolumeProfileRenderer(canvas);
    rendererRef.current = r;
    r.start();
    return () => {
      r.stop();
      rendererRef.current = null;
    };
  }, []);

  useEffect(() => {
    rendererRef.current?.setBars(vp);
  }, [vp]);

  useEffect(() => {
    rendererRef.current?.setPriceRange(priceMin, priceMax);
  }, [priceMin, priceMax]);

  return (
    <canvas
      ref={canvasRef}
      style={{ width: '100%', height: '100%', display: 'block' }}
    />
  );
}
