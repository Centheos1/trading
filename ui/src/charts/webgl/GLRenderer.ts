/**
 * Base class for all WebGL2 chart renderers in the app.
 *
 * Each subclass owns one canvas, one ``WebGL2RenderingContext``, and
 * one ``requestAnimationFrame`` callback.  The base class manages:
 *
 *   * DPI-aware canvas sizing via ``ResizeObserver``
 *   * a render loop that calls :meth:`draw` once per frame *and*
 *     only when ``invalidate()`` has been called since the last frame
 *
 * Frames are intentionally on-demand: a static chart costs zero work
 * per frame.  Live updates call ``invalidate()`` (from BOOK_UPDATE /
 * TRADE handlers) which queues exactly one redraw on the next RAF.
 */

import { getGL2 } from './glUtils';

export abstract class GLRenderer {
  protected readonly canvas: HTMLCanvasElement;
  protected readonly gl: WebGL2RenderingContext;
  private resizeObs: ResizeObserver | null = null;
  private dirty = true;
  private rafId = 0;
  private running = false;
  private dpr = 1;

  constructor(canvas: HTMLCanvasElement) {
    this.canvas = canvas;
    this.gl = getGL2(canvas);
  }

  start(): void {
    if (this.running) return;
    this.running = true;
    this.resizeObs = new ResizeObserver(() => this.handleResize());
    this.resizeObs.observe(this.canvas);
    this.handleResize();
    this.tick();
  }

  stop(): void {
    this.running = false;
    if (this.rafId !== 0) {
      cancelAnimationFrame(this.rafId);
      this.rafId = 0;
    }
    if (this.resizeObs !== null) {
      this.resizeObs.disconnect();
      this.resizeObs = null;
    }
  }

  /** Mark the renderer dirty — a single draw is scheduled on next RAF. */
  invalidate(): void {
    this.dirty = true;
  }

  protected abstract draw(): void;

  /** Hook for subclasses that need to react to canvas resize. */
  protected onResize(_w: number, _h: number): void {
    /* no-op */
  }

  // ------------------------------------------------------------------

  private tick = (): void => {
    if (!this.running) return;
    if (this.dirty) {
      this.dirty = false;
      try {
        this.draw();
      } catch (err) {
        // eslint-disable-next-line no-console
        console.error('GLRenderer draw threw', err);
      }
    }
    this.rafId = requestAnimationFrame(this.tick);
  };

  private handleResize(): void {
    const rect = this.canvas.getBoundingClientRect();
    // When the canvas is inside a visibility:hidden container its layout
    // rect still has non-zero dimensions (unlike display:none), but guard
    // anyway so we never write a 0×0 canvas size to the WebGL context.
    if (rect.width === 0 || rect.height === 0) return;
    this.dpr = window.devicePixelRatio || 1;
    const w = Math.max(1, Math.floor(rect.width * this.dpr));
    const h = Math.max(1, Math.floor(rect.height * this.dpr));
    if (this.canvas.width !== w || this.canvas.height !== h) {
      this.canvas.width = w;
      this.canvas.height = h;
      this.gl.viewport(0, 0, w, h);
      this.onResize(w, h);
      this.invalidate();
    }
  }

  protected get pixelWidth(): number {
    return this.canvas.width;
  }

  protected get pixelHeight(): number {
    return this.canvas.height;
  }
}
