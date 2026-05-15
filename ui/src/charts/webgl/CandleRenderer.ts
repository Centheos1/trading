/**
 * WebGL2 candlestick renderer with client-side indicators.
 *
 * Each candle generates 4 quads (2 triangles each = 6 verts each):
 *
 *   * body rect       (open ↔ close)
 *   * wick rect       (high ↔ low)
 *   * volume rect     (bottom 20 % of the chart)
 *   * (optional indicator line vertices in a separate buffer)
 *
 * Bull / bear are routed to separate VBO regions so each draw call has
 * a uniform color.  Geometry is rebuilt whenever the candle list or
 * viewport changes — for ~80 visible candles this is well under one
 * millisecond.
 *
 * Indicators (SMA, VWAP, EMA) are computed on the client from the
 * stored OHLCV array.  Toggling an indicator does NOT require a
 * backend round-trip.
 */

import { GLRenderer } from './GLRenderer';
import { createBuffer, linkProgram } from './glUtils';
import type { Candle } from '../../stores/trading';

// ---------------------------------------------------------------------------
// Constants
// ---------------------------------------------------------------------------

const BULL_COLOR: [number, number, number] = [0.06, 0.79, 0.50];
const BEAR_COLOR: [number, number, number] = [0.92, 0.22, 0.26];
const BULL_VOL: [number, number, number, number] = [0.06, 0.79, 0.50, 0.35];
const BEAR_VOL: [number, number, number, number] = [0.92, 0.22, 0.26, 0.35];
const SMA_COLOR: [number, number, number] = [1.00, 0.82, 0.20];
const EMA_COLOR: [number, number, number] = [0.55, 0.74, 1.00];
const VWAP_COLOR: [number, number, number] = [0.78, 0.42, 0.92];

const VOLUME_ZONE_FRAC = 0.22;
const CANDLE_GAP_FRAC = 0.25;

// ---------------------------------------------------------------------------
// Shaders — one program for filled quads (candles + volume), one for lines.
// ---------------------------------------------------------------------------

const QUAD_VS = `#version 300 es
precision highp float;
layout(location = 0) in vec2 a_pos;       // pixel coords
uniform vec2 u_viewport;
out float v_alpha;
void main() {
  vec2 ndc = a_pos / u_viewport * 2.0 - 1.0;
  gl_Position = vec4(ndc.x, -ndc.y, 0.0, 1.0);  // y-flip → top-left origin
  v_alpha = 1.0;
}`;

const QUAD_FS = `#version 300 es
precision highp float;
uniform vec4 u_color;
in  float v_alpha;
out vec4  outColor;
void main() {
  outColor = vec4(u_color.rgb, u_color.a * v_alpha);
}`;

const LINE_VS = `#version 300 es
precision highp float;
layout(location = 0) in vec2 a_pos;
uniform vec2 u_viewport;
void main() {
  vec2 ndc = a_pos / u_viewport * 2.0 - 1.0;
  gl_Position = vec4(ndc.x, -ndc.y, 0.0, 1.0);
}`;

const LINE_FS = `#version 300 es
precision highp float;
uniform vec3 u_color;
out vec4 outColor;
void main() {
  outColor = vec4(u_color, 0.95);
}`;

// ---------------------------------------------------------------------------
// Renderer
// ---------------------------------------------------------------------------

export interface CandleIndicators {
  sma?: number;     // period (default off)
  ema?: number;     // period
  vwap?: boolean;   // session anchored
}

export class CandleRenderer extends GLRenderer {
  private quadProg!: WebGLProgram;
  private lineProg!: WebGLProgram;
  private bullBuf!: WebGLBuffer;
  private bearBuf!: WebGLBuffer;
  private bullVolBuf!: WebGLBuffer;
  private bearVolBuf!: WebGLBuffer;
  private lineBuf!: WebGLBuffer;
  private vao!: WebGLVertexArrayObject;
  private lineVao!: WebGLVertexArrayObject;

  private uQuadColor!: WebGLUniformLocation;
  private uQuadViewport!: WebGLUniformLocation;
  private uLineColor!: WebGLUniformLocation;
  private uLineViewport!: WebGLUniformLocation;

  private bullCount = 0;
  private bearCount = 0;
  private bullVolCount = 0;
  private bearVolCount = 0;

  /** Each entry is the GL line buffer offset/length + colour. */
  private overlays: Array<{
    offset: number;
    count: number;
    color: [number, number, number];
  }> = [];

  private candles: Candle[] = [];
  private visibleCount = 80;
  private indicators: CandleIndicators = { sma: 20, ema: 12, vwap: true };

  constructor(canvas: HTMLCanvasElement) {
    super(canvas);
    this.initGL();
  }

  setCandles(rows: Candle[]): void {
    this.candles = rows;
    this.invalidate();
  }

  setVisibleCount(n: number): void {
    this.visibleCount = Math.max(20, Math.min(500, n));
    this.invalidate();
  }

  setIndicators(patch: Partial<CandleIndicators>): void {
    this.indicators = { ...this.indicators, ...patch };
    this.invalidate();
  }

  // ------------------------------------------------------------------

  protected draw(): void {
    const gl = this.gl;
    const w = this.pixelWidth;
    const h = this.pixelHeight;

    gl.clearColor(0.05, 0.07, 0.10, 1.0);
    gl.clear(gl.COLOR_BUFFER_BIT);

    if (this.candles.length === 0) return;
    this.rebuildGeometry();

    gl.useProgram(this.quadProg);
    gl.uniform2f(this.uQuadViewport, w, h);
    gl.bindVertexArray(this.vao);

    // Bull bodies + wicks
    gl.bindBuffer(gl.ARRAY_BUFFER, this.bullBuf);
    gl.vertexAttribPointer(0, 2, gl.FLOAT, false, 0, 0);
    gl.uniform4f(this.uQuadColor, ...BULL_COLOR, 1.0);
    gl.drawArrays(gl.TRIANGLES, 0, this.bullCount);

    // Bear bodies + wicks
    gl.bindBuffer(gl.ARRAY_BUFFER, this.bearBuf);
    gl.vertexAttribPointer(0, 2, gl.FLOAT, false, 0, 0);
    gl.uniform4f(this.uQuadColor, ...BEAR_COLOR, 1.0);
    gl.drawArrays(gl.TRIANGLES, 0, this.bearCount);

    // Volume bars
    gl.enable(gl.BLEND);
    gl.blendFunc(gl.SRC_ALPHA, gl.ONE_MINUS_SRC_ALPHA);
    gl.bindBuffer(gl.ARRAY_BUFFER, this.bullVolBuf);
    gl.vertexAttribPointer(0, 2, gl.FLOAT, false, 0, 0);
    gl.uniform4f(this.uQuadColor, ...BULL_VOL);
    gl.drawArrays(gl.TRIANGLES, 0, this.bullVolCount);

    gl.bindBuffer(gl.ARRAY_BUFFER, this.bearVolBuf);
    gl.vertexAttribPointer(0, 2, gl.FLOAT, false, 0, 0);
    gl.uniform4f(this.uQuadColor, ...BEAR_VOL);
    gl.drawArrays(gl.TRIANGLES, 0, this.bearVolCount);
    gl.disable(gl.BLEND);

    // Indicator lines
    if (this.overlays.length > 0) {
      gl.useProgram(this.lineProg);
      gl.uniform2f(this.uLineViewport, w, h);
      gl.bindVertexArray(this.lineVao);
      gl.bindBuffer(gl.ARRAY_BUFFER, this.lineBuf);
      gl.vertexAttribPointer(0, 2, gl.FLOAT, false, 0, 0);
      for (const ov of this.overlays) {
        gl.uniform3f(this.uLineColor, ...ov.color);
        gl.drawArrays(gl.LINE_STRIP, ov.offset, ov.count);
      }
    }
  }

  // ------------------------------------------------------------------
  // Geometry rebuild
  // ------------------------------------------------------------------

  private rebuildGeometry(): void {
    const gl = this.gl;
    const visible = this.candles.slice(-this.visibleCount);
    const n = visible.length;
    if (n === 0) return;

    const px = 60;            // left margin
    const py = 10;            // top margin
    const pw = this.pixelWidth - px - 70;  // right margin for axis
    const ph = this.pixelHeight - py - 28;
    const volZone = ph * VOLUME_ZONE_FRAC;
    const chartH = ph - volZone;
    const step = pw / Math.max(n, 1);
    const candleW = step * (1 - CANDLE_GAP_FRAC);
    const halfW = candleW * 0.5;

    let pmin = Infinity, pmax = -Infinity, vmax = 0;
    for (const c of visible) {
      if (c.h > pmax) pmax = c.h;
      if (c.l < pmin) pmin = c.l;
      if (c.vol > vmax) vmax = c.vol;
    }
    const span = Math.max(pmax - pmin, 1e-6);
    pmin -= span * 0.04;
    pmax += span * 0.04;
    const pr = pmax - pmin;

    const p2y = (p: number) => py + (1 - (p - pmin) / pr) * chartH;

    // Collect verts per bucket.
    const bullV: number[] = [];
    const bearV: number[] = [];
    const bullVolV: number[] = [];
    const bearVolV: number[] = [];

    const pushRect = (
      arr: number[],
      x0: number, y0: number, x1: number, y1: number,
    ): void => {
      arr.push(x0, y0, x1, y0, x1, y1, x0, y0, x1, y1, x0, y1);
    };

    for (let i = 0; i < n; i++) {
      const c = visible[i]!;
      const xm = px + (i + 0.5) * step;
      const bull = c.c >= c.o;
      const yo = p2y(c.o);
      const yc = p2y(c.c);
      const yh = p2y(c.h);
      const yl = p2y(c.l);
      const bodyTop = Math.min(yo, yc);
      const bodyBot = Math.max(yo, yc);
      // Body
      const arr = bull ? bullV : bearV;
      pushRect(arr, xm - halfW, bodyTop, xm + halfW, Math.max(bodyBot, bodyTop + 1));
      // Wick (1 px wide)
      pushRect(arr, xm - 0.5, yh, xm + 0.5, yl);

      // Volume
      const volH = vmax > 0 ? (c.vol / vmax) * volZone : 0;
      const volTop = py + ph - volH;
      const volArr = bull ? bullVolV : bearVolV;
      pushRect(volArr, xm - halfW, volTop, xm + halfW, py + ph);
    }

    // Upload candle quads.
    gl.bindBuffer(gl.ARRAY_BUFFER, this.bullBuf);
    gl.bufferData(gl.ARRAY_BUFFER, new Float32Array(bullV), gl.DYNAMIC_DRAW);
    this.bullCount = bullV.length / 2;

    gl.bindBuffer(gl.ARRAY_BUFFER, this.bearBuf);
    gl.bufferData(gl.ARRAY_BUFFER, new Float32Array(bearV), gl.DYNAMIC_DRAW);
    this.bearCount = bearV.length / 2;

    gl.bindBuffer(gl.ARRAY_BUFFER, this.bullVolBuf);
    gl.bufferData(gl.ARRAY_BUFFER, new Float32Array(bullVolV), gl.DYNAMIC_DRAW);
    this.bullVolCount = bullVolV.length / 2;

    gl.bindBuffer(gl.ARRAY_BUFFER, this.bearVolBuf);
    gl.bufferData(gl.ARRAY_BUFFER, new Float32Array(bearVolV), gl.DYNAMIC_DRAW);
    this.bearVolCount = bearVolV.length / 2;

    // Indicator lines.
    const lineV: number[] = [];
    this.overlays = [];

    const pushOverlay = (
      ys: number[],
      color: [number, number, number],
    ): void => {
      const offset = lineV.length / 2;
      for (let i = 0; i < ys.length; i++) {
        const x = px + (i + 0.5) * step;
        lineV.push(x, ys[i]!);
      }
      this.overlays.push({ offset, count: ys.length, color });
    };

    if (this.indicators.sma) {
      const sma = computeSMA(visible, this.indicators.sma);
      pushOverlay(sma.map(p => Number.isFinite(p) ? p2y(p) : NaN), SMA_COLOR);
    }
    if (this.indicators.ema) {
      const ema = computeEMA(visible, this.indicators.ema);
      pushOverlay(ema.map(p => Number.isFinite(p) ? p2y(p) : NaN), EMA_COLOR);
    }
    if (this.indicators.vwap) {
      const vwap = computeVWAP(visible);
      pushOverlay(vwap.map(p => Number.isFinite(p) ? p2y(p) : NaN), VWAP_COLOR);
    }

    gl.bindBuffer(gl.ARRAY_BUFFER, this.lineBuf);
    gl.bufferData(gl.ARRAY_BUFFER, new Float32Array(lineV), gl.DYNAMIC_DRAW);
  }

  // ------------------------------------------------------------------

  private initGL(): void {
    const gl = this.gl;
    this.quadProg = linkProgram(gl, QUAD_VS, QUAD_FS);
    this.lineProg = linkProgram(gl, LINE_VS, LINE_FS);

    this.uQuadColor = this.must(gl.getUniformLocation(this.quadProg, 'u_color'));
    this.uQuadViewport = this.must(gl.getUniformLocation(this.quadProg, 'u_viewport'));
    this.uLineColor = this.must(gl.getUniformLocation(this.lineProg, 'u_color'));
    this.uLineViewport = this.must(gl.getUniformLocation(this.lineProg, 'u_viewport'));

    this.vao = this.must(gl.createVertexArray());
    gl.bindVertexArray(this.vao);
    this.bullBuf = createBuffer(gl, gl.ARRAY_BUFFER, null, gl.DYNAMIC_DRAW);
    this.bearBuf = createBuffer(gl, gl.ARRAY_BUFFER, null, gl.DYNAMIC_DRAW);
    this.bullVolBuf = createBuffer(gl, gl.ARRAY_BUFFER, null, gl.DYNAMIC_DRAW);
    this.bearVolBuf = createBuffer(gl, gl.ARRAY_BUFFER, null, gl.DYNAMIC_DRAW);
    gl.enableVertexAttribArray(0);

    this.lineVao = this.must(gl.createVertexArray());
    gl.bindVertexArray(this.lineVao);
    this.lineBuf = createBuffer(gl, gl.ARRAY_BUFFER, null, gl.DYNAMIC_DRAW);
    gl.enableVertexAttribArray(0);

    gl.bindVertexArray(null);
  }

  private must<T>(v: T | null): T {
    if (v === null) throw new Error('WebGL allocation returned null');
    return v;
  }
}

// ---------------------------------------------------------------------------
// Indicator helpers (pure functions — no GL state)
// ---------------------------------------------------------------------------

function computeSMA(candles: Candle[], period: number): number[] {
  const out: number[] = new Array(candles.length).fill(NaN);
  let sum = 0;
  for (let i = 0; i < candles.length; i++) {
    sum += candles[i]!.c;
    if (i >= period) sum -= candles[i - period]!.c;
    if (i >= period - 1) out[i] = sum / period;
  }
  return out;
}

function computeEMA(candles: Candle[], period: number): number[] {
  const out: number[] = new Array(candles.length).fill(NaN);
  if (candles.length === 0) return out;
  const k = 2.0 / (period + 1.0);
  let prev = candles[0]!.c;
  out[0] = prev;
  for (let i = 1; i < candles.length; i++) {
    prev = candles[i]!.c * k + prev * (1 - k);
    out[i] = prev;
  }
  return out;
}

function computeVWAP(candles: Candle[]): number[] {
  const out: number[] = new Array(candles.length).fill(NaN);
  let cumPV = 0;
  let cumV = 0;
  for (let i = 0; i < candles.length; i++) {
    const c = candles[i]!;
    const typical = (c.h + c.l + c.c) / 3.0;
    cumPV += typical * c.vol;
    cumV += c.vol;
    out[i] = cumV > 0 ? cumPV / cumV : NaN;
  }
  return out;
}
