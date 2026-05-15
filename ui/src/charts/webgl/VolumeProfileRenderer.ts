/**
 * Volume profile — horizontal bars showing traded volume per price level.
 *
 * Buy and sell volume are stacked, drawn as filled rectangles.  The POC
 * (point of control, max-volume row) is highlighted with a contrast
 * outline.  All geometry is rebuilt per frame; the dataset is small
 * (≤ ~512 rows from the C++ engine).
 */

import { GLRenderer } from './GLRenderer';
import { createBuffer, linkProgram } from './glUtils';
import type { VpBar } from '../../ws/messages';

const VS = `#version 300 es
precision highp float;
layout(location = 0) in vec2 a_pos;
uniform vec2 u_viewport;
void main() {
  vec2 ndc = a_pos / u_viewport * 2.0 - 1.0;
  gl_Position = vec4(ndc.x, -ndc.y, 0.0, 1.0);
}`;

const FS = `#version 300 es
precision highp float;
uniform vec4 u_color;
out vec4 outColor;
void main() {
  outColor = u_color;
}`;

const BUY_COLOR: [number, number, number, number] = [0.06, 0.79, 0.50, 0.85];
const SELL_COLOR: [number, number, number, number] = [0.92, 0.22, 0.26, 0.85];
const POC_COLOR: [number, number, number, number] = [1.0, 0.84, 0.10, 1.0];

export class VolumeProfileRenderer extends GLRenderer {
  private prog!: WebGLProgram;
  private vao!: WebGLVertexArrayObject;
  private buyBuf!: WebGLBuffer;
  private sellBuf!: WebGLBuffer;
  private pocBuf!: WebGLBuffer;
  private uColor!: WebGLUniformLocation;
  private uViewport!: WebGLUniformLocation;

  private bars: VpBar[] = [];
  private buyCount = 0;
  private sellCount = 0;
  private pocCount = 0;

  // When non-zero, these override the auto-scale so the VP price axis
  // matches the heatmap's visible range exactly.
  private extPriceMin = 0;
  private extPriceMax = 0;

  constructor(canvas: HTMLCanvasElement) {
    super(canvas);
    this.initGL();
  }

  setBars(bars: VpBar[]): void {
    this.bars = bars;
    this.invalidate();
  }

  /**
   * Lock the y-axis to a specific price range.
   * Pass (0, 0) to revert to auto-scale from the bar data.
   */
  setPriceRange(priceMin: number, priceMax: number): void {
    this.extPriceMin = priceMin;
    this.extPriceMax = priceMax;
    this.invalidate();
  }

  protected draw(): void {
    const gl = this.gl;
    gl.clearColor(0.05, 0.07, 0.10, 1.0);
    gl.clear(gl.COLOR_BUFFER_BIT);
    if (this.bars.length === 0) return;
    this.rebuild();

    gl.useProgram(this.prog);
    gl.uniform2f(this.uViewport, this.pixelWidth, this.pixelHeight);
    gl.bindVertexArray(this.vao);
    gl.enable(gl.BLEND);
    gl.blendFunc(gl.SRC_ALPHA, gl.ONE_MINUS_SRC_ALPHA);

    gl.bindBuffer(gl.ARRAY_BUFFER, this.buyBuf);
    gl.vertexAttribPointer(0, 2, gl.FLOAT, false, 0, 0);
    gl.uniform4f(this.uColor, ...BUY_COLOR);
    gl.drawArrays(gl.TRIANGLES, 0, this.buyCount);

    gl.bindBuffer(gl.ARRAY_BUFFER, this.sellBuf);
    gl.vertexAttribPointer(0, 2, gl.FLOAT, false, 0, 0);
    gl.uniform4f(this.uColor, ...SELL_COLOR);
    gl.drawArrays(gl.TRIANGLES, 0, this.sellCount);

    if (this.pocCount > 0) {
      gl.bindBuffer(gl.ARRAY_BUFFER, this.pocBuf);
      gl.vertexAttribPointer(0, 2, gl.FLOAT, false, 0, 0);
      gl.uniform4f(this.uColor, ...POC_COLOR);
      gl.drawArrays(gl.LINES, 0, this.pocCount);
    }
    gl.disable(gl.BLEND);
  }

  private rebuild(): void {
    const gl = this.gl;
    const w = this.pixelWidth;
    const h = this.pixelHeight;
    const marginX = 8;
    const marginY = 10;
    const cw = w - marginX * 2;
    const ch = h - marginY * 2;

    // Use the externally-provided heatmap range when available so the VP
    // y-axis stays pixel-perfect with the heatmap.  Fall back to the bars'
    // own range for the candle-chart VP (no external range supplied).
    const useExternal = this.extPriceMin < this.extPriceMax;
    let pmin: number, pmax: number;
    if (useExternal) {
      pmin = this.extPriceMin;
      pmax = this.extPriceMax;
    } else {
      pmin = Infinity; pmax = -Infinity;
      for (const b of this.bars) {
        if (b.price < pmin) pmin = b.price;
        if (b.price > pmax) pmax = b.price;
      }
    }

    let vmax = 0, pocPrice = 0;
    for (const b of this.bars) {
      if (b.total > vmax) { vmax = b.total; pocPrice = b.price; }
    }
    if (vmax <= 0) return;

    const pr = Math.max(pmax - pmin, 1e-6);
    const p2y = (p: number) => marginY + (1 - (p - pmin) / pr) * ch;

    // Bar height: when using an external range derive it from price spacing
    // so bars are correctly proportioned; otherwise divide chart evenly.
    let rowH: number;
    if (useExternal && this.bars.length > 1) {
      const prices = this.bars.map((b) => b.price).sort((a, b) => a - b);
      const ticks: number[] = [];
      for (let i = 1; i < prices.length; i++) ticks.push(prices[i] - prices[i - 1]);
      ticks.sort((a, b) => a - b);
      const medTick = ticks[Math.floor(ticks.length / 2)] ?? 1;
      rowH = Math.max(1, (medTick / pr) * ch);
    } else {
      rowH = ch / Math.max(1, this.bars.length);
    }

    const buyV: number[] = [];
    const sellV: number[] = [];
    const pocV: number[] = [];

    const pushRect = (
      arr: number[],
      x0: number, y0: number, x1: number, y1: number,
    ): void => {
      arr.push(x0, y0, x1, y0, x1, y1, x0, y0, x1, y1, x0, y1);
    };

    for (const b of this.bars) {
      // Skip bars entirely outside the visible range.
      if (b.price < pmin || b.price > pmax) continue;
      const y = p2y(b.price);
      const y0 = y - rowH * 0.45;
      const y1 = y + rowH * 0.45;
      const wBuy = (b.buy / vmax) * cw * 0.5;
      const wSell = (b.sell / vmax) * cw * 0.5;
      // Buy: left of vertical centre.  Sell: right.
      const cx = marginX + cw * 0.5;
      pushRect(buyV, cx - wBuy, y0, cx, y1);
      pushRect(sellV, cx, y0, cx + wSell, y1);
      if (Math.abs(b.price - pocPrice) < 1e-9) {
        pocV.push(marginX, y, marginX + cw, y);
      }
    }

    gl.bindBuffer(gl.ARRAY_BUFFER, this.buyBuf);
    gl.bufferData(gl.ARRAY_BUFFER, new Float32Array(buyV), gl.DYNAMIC_DRAW);
    this.buyCount = buyV.length / 2;

    gl.bindBuffer(gl.ARRAY_BUFFER, this.sellBuf);
    gl.bufferData(gl.ARRAY_BUFFER, new Float32Array(sellV), gl.DYNAMIC_DRAW);
    this.sellCount = sellV.length / 2;

    gl.bindBuffer(gl.ARRAY_BUFFER, this.pocBuf);
    gl.bufferData(gl.ARRAY_BUFFER, new Float32Array(pocV), gl.DYNAMIC_DRAW);
    this.pocCount = pocV.length / 2;
  }

  private initGL(): void {
    const gl = this.gl;
    this.prog = linkProgram(gl, VS, FS);
    this.uColor = this.must(gl.getUniformLocation(this.prog, 'u_color'));
    this.uViewport = this.must(gl.getUniformLocation(this.prog, 'u_viewport'));
    this.vao = this.must(gl.createVertexArray());
    gl.bindVertexArray(this.vao);
    this.buyBuf = createBuffer(gl, gl.ARRAY_BUFFER, null, gl.DYNAMIC_DRAW);
    this.sellBuf = createBuffer(gl, gl.ARRAY_BUFFER, null, gl.DYNAMIC_DRAW);
    this.pocBuf = createBuffer(gl, gl.ARRAY_BUFFER, null, gl.DYNAMIC_DRAW);
    gl.enableVertexAttribArray(0);
    gl.bindVertexArray(null);
  }

  private must<T>(v: T | null): T {
    if (v === null) throw new Error('WebGL allocation returned null');
    return v;
  }
}
