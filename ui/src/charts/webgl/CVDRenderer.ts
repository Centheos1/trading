/**
 * CVD (cumulative volume delta) line chart.
 *
 * One LINE_STRIP over the last N CVD points, with a baseline zero
 * line and price-axis labels rendered in HTML overlay (not in this
 * file).  Geometry is rebuilt every frame because the dataset is
 * small (≤ ~5000 points).
 */

import { GLRenderer } from './GLRenderer';
import { createBuffer, linkProgram } from './glUtils';

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

export class CVDRenderer extends GLRenderer {
  private prog!: WebGLProgram;
  private vao!: WebGLVertexArrayObject;
  private buf!: WebGLBuffer;
  private uColor!: WebGLUniformLocation;
  private uViewport!: WebGLUniformLocation;

  private series: { ts_ms: number; value: number }[] = [];
  private vertCount = 0;
  private baselineOffset = 0;
  private baselineCount = 0;

  constructor(canvas: HTMLCanvasElement) {
    super(canvas);
    this.initGL();
  }

  setSeries(series: { ts_ms: number; value: number }[]): void {
    this.series = series;
    this.invalidate();
  }

  protected draw(): void {
    const gl = this.gl;
    gl.clearColor(0.05, 0.07, 0.10, 1.0);
    gl.clear(gl.COLOR_BUFFER_BIT);
    if (this.series.length < 2) return;
    this.rebuild();

    gl.useProgram(this.prog);
    gl.uniform2f(this.uViewport, this.pixelWidth, this.pixelHeight);
    gl.bindVertexArray(this.vao);

    // Baseline zero in faint white.
    gl.uniform4f(this.uColor, 0.85, 0.85, 0.85, 0.25);
    gl.drawArrays(gl.LINES, this.baselineOffset, this.baselineCount);

    // CVD line — green when positive, red when negative (single colour
    // here; the gradient is purely cosmetic and could be in shader).
    const last = this.series[this.series.length - 1]!.value;
    if (last >= 0) {
      gl.uniform4f(this.uColor, 0.06, 0.79, 0.50, 1.0);
    } else {
      gl.uniform4f(this.uColor, 0.92, 0.22, 0.26, 1.0);
    }
    gl.drawArrays(gl.LINE_STRIP, 0, this.vertCount);
  }

  private rebuild(): void {
    const gl = this.gl;
    const w = this.pixelWidth;
    const h = this.pixelHeight;
    const margin = 14;
    const cw = w - margin * 2;
    const ch = h - margin * 2;

    let vmin = Infinity, vmax = -Infinity;
    for (const p of this.series) {
      if (p.value < vmin) vmin = p.value;
      if (p.value > vmax) vmax = p.value;
    }
    if (vmin === vmax) {
      vmin -= 1;
      vmax += 1;
    } else {
      const pad = (vmax - vmin) * 0.10;
      vmin -= pad;
      vmax += pad;
    }
    const vr = vmax - vmin;
    const v2y = (v: number) => margin + (1 - (v - vmin) / vr) * ch;

    const verts: number[] = [];
    for (let i = 0; i < this.series.length; i++) {
      const p = this.series[i]!;
      const x = margin + (i / (this.series.length - 1)) * cw;
      verts.push(x, v2y(p.value));
    }
    this.vertCount = this.series.length;

    // Baseline = y(0)
    const yZero = v2y(0);
    this.baselineOffset = this.vertCount;
    this.baselineCount = 2;
    verts.push(margin, yZero, margin + cw, yZero);

    gl.bindBuffer(gl.ARRAY_BUFFER, this.buf);
    gl.bufferData(gl.ARRAY_BUFFER, new Float32Array(verts), gl.DYNAMIC_DRAW);
  }

  private initGL(): void {
    const gl = this.gl;
    this.prog = linkProgram(gl, VS, FS);
    this.uColor = this.must(gl.getUniformLocation(this.prog, 'u_color'));
    this.uViewport = this.must(gl.getUniformLocation(this.prog, 'u_viewport'));
    this.vao = this.must(gl.createVertexArray());
    gl.bindVertexArray(this.vao);
    this.buf = createBuffer(gl, gl.ARRAY_BUFFER, null, gl.DYNAMIC_DRAW);
    gl.enableVertexAttribArray(0);
    gl.vertexAttribPointer(0, 2, gl.FLOAT, false, 0, 0);
    gl.bindVertexArray(null);
  }

  private must<T>(v: T | null): T {
    if (v === null) throw new Error('WebGL allocation returned null');
    return v;
  }
}
