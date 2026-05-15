/**
 * Bookmap-style heatmap with a scrollable historical depth buffer.
 *
 * Architecture (see ``plan.md §5``):
 *
 *   * One ``GL_R32F`` texture of size ``MAX_COLS × PRICE_LEVELS``.
 *     Each texel stores a signed float (positive = bid depth,
 *     negative = ask depth) — single channel because the bid/ask
 *     split is encoded in the sign.
 *
 *   * A CPU ring buffer ``Float32Array`` of the same size persists the
 *     same data so historical columns can be re-uploaded after a
 *     ``WebGLContextLost`` event.
 *
 *   * Every BOOK_UPDATE -> DepthAggregator.aggregate() -> one column
 *     written via ``texSubImage2D``.  ``writeCol`` increments mod
 *     ``MAX_COLS`` so the texture is a ring; the shader unwraps it.
 *
 *   * The shader maps depth → color via a configurable LUT uniform.
 *     Bid blue and ask red are the defaults; change them client-side
 *     without touching the backend.
 *
 *   * Bubbles (TRADE messages) are stored in a separate VBO and drawn
 *     as point sprites on top of the depth texture, at the exact
 *     column/price where the trade occurred.
 *
 * Scroll & zoom:
 *
 *   * ``setScrollOffset(cols)`` — shifts the view left by ``cols``
 *     columns (negative = back in time).  Pinned to 0 (live) by default.
 *   * ``setTimeZoom(colsPerPixel)`` — compresses columns horizontally.
 *   * ``setPriceRange(min, max)`` — re-maps the y axis.
 */

import { GLRenderer } from './GLRenderer';
import {
  createBuffer,
  createR32FTexture,
  linkProgram,
} from './glUtils';
import type { DepthColumn } from '../../engine/DepthAggregator';

// ---------------------------------------------------------------------------
// Compile-time constants — the ring buffer footprint.
// ---------------------------------------------------------------------------

const MAX_COLS = 4096; // ~17 hours @ 100 ms cadence
const PRICE_LEVELS = 512;
const MAX_BUBBLES = 16_384;

// ---------------------------------------------------------------------------
// Shaders
// ---------------------------------------------------------------------------

const DEPTH_VERT = `#version 300 es
precision highp float;
layout(location = 0) in vec2 a_pos;   // -1..1 NDC quad
out vec2 v_uv;
void main() {
  // UV runs 0..1 over the viewport.
  v_uv = (a_pos + vec2(1.0)) * 0.5;
  gl_Position = vec4(a_pos, 0.0, 1.0);
}`;

const DEPTH_FRAG = `#version 300 es
precision highp float;
in vec2 v_uv;
out vec4 outColor;

uniform sampler2D u_depth;
uniform float     u_writeCol;
uniform float     u_maxCols;
uniform float     u_priceLevels;
uniform float     u_scrollCols;
uniform float     u_visibleCols;
// Log-scale intensity: t = log2(1 + |depth|*scale) / log2(1+scale).
// 500 maps 0.001 BTC→6%, 0.1 BTC→63%, 1 BTC→100%.
uniform float     u_intensityScale;
uniform vec3      u_bgColor;

// Bookmap-style multi-stop hot gradient.
// Bids: dark navy → blue → cyan → yellow → hot-red
// Asks: dark maroon → red → orange → yellow → white-hot
vec3 depthColor(float t, bool isBid) {
  if (t < 0.001) return u_bgColor;
  if (isBid) {
    if      (t < 0.30) return mix(vec3(0.01, 0.04, 0.20), vec3(0.04, 0.20, 0.78), t / 0.30);
    else if (t < 0.55) return mix(vec3(0.04, 0.20, 0.78), vec3(0.00, 0.74, 0.94), (t-0.30)/0.25);
    else if (t < 0.75) return mix(vec3(0.00, 0.74, 0.94), vec3(1.00, 0.88, 0.00), (t-0.55)/0.20);
    else               return mix(vec3(1.00, 0.88, 0.00), vec3(1.00, 0.12, 0.05), (t-0.75)/0.25);
  } else {
    if      (t < 0.30) return mix(vec3(0.20, 0.02, 0.04), vec3(0.78, 0.06, 0.10), t / 0.30);
    else if (t < 0.55) return mix(vec3(0.78, 0.06, 0.10), vec3(0.94, 0.44, 0.00), (t-0.30)/0.25);
    else if (t < 0.75) return mix(vec3(0.94, 0.44, 0.00), vec3(1.00, 0.88, 0.00), (t-0.55)/0.20);
    else               return mix(vec3(1.00, 0.88, 0.00), vec3(1.00, 0.96, 0.82), (t-0.75)/0.25);
  }
}

void main() {
  float colsFromRight = (1.0 - v_uv.x) * u_visibleCols + u_scrollCols;
  float col = mod(u_writeCol - 1.0 - colsFromRight + u_maxCols, u_maxCols);
  float row = floor((1.0 - v_uv.y) * u_priceLevels);
  vec2 sampleUV = vec2((col + 0.5) / u_maxCols, (row + 0.5) / u_priceLevels);

  float depth = texture(u_depth, sampleUV).r;
  float t = clamp(
    log2(1.0 + abs(depth) * u_intensityScale) / log2(1.0 + u_intensityScale),
    0.0, 1.0
  );
  outColor = vec4(depthColor(t, depth >= 0.0), 1.0);
}`;

const BUBBLE_VERT = `#version 300 es
precision highp float;
layout(location = 0) in float a_col;   // historical column index (0..MAX_COLS)
layout(location = 1) in float a_price; // absolute market price of the trade
layout(location = 2) in float a_qty;   // absolute size — controls radius
layout(location = 3) in float a_side;  // -1 = sell, +1 = buy

uniform float u_writeCol;
uniform float u_maxCols;
uniform float u_scrollCols;
uniform float u_visibleCols;
uniform float u_bubbleScale;
// Price-axis mapping — same reference as the depth aggregator so bubbles
// always sit at the correct y position regardless of centrePrice drift.
uniform float u_centrePrice;
uniform float u_priceSpan;

out float v_side;
out float v_alpha;

void main() {
  // X: project column to NDC the same way as the depth shader.
  float liveCol = mod(u_writeCol - 1.0 + u_maxCols, u_maxCols);
  float cyclic = mod(liveCol - a_col + u_maxCols, u_maxCols);
  float colsFromRight = cyclic + u_scrollCols;
  float xNorm = 1.0 - colsFromRight / u_visibleCols;

  // Y: map absolute price to normalised [0,1] using current centre/span.
  // centrePrice maps to 0.5; topPrice(+span/2) maps to 1.0.
  float yNorm = (a_price - u_centrePrice) / u_priceSpan + 0.5;

  float ndcX = xNorm * 2.0 - 1.0;
  float ndcY = yNorm * 2.0 - 1.0;

  v_side  = a_side;
  v_alpha = clamp(xNorm, 0.0, 1.0); // fade off-screen bubbles
  gl_Position = vec4(ndcX, ndcY, 0.0, 1.0);
  // Log₂ scaling works across BTC's 4-order-of-magnitude qty range.
  // qty * 1000 maps 0.001 BTC → log2(2)=1, 1 BTC → log2(1001)≈10.
  gl_PointSize = max(5.0, log2(1.0 + a_qty * 1000.0) * u_bubbleScale);
}`;

const BUBBLE_FRAG = `#version 300 es
precision highp float;
in  float v_side;
in  float v_alpha;
out vec4  outColor;
void main() {
  // Soft-edged circle.
  vec2 uv = gl_PointCoord * 2.0 - 1.0;
  float r = dot(uv, uv);
  if (r > 1.0) discard;
  float edge = smoothstep(1.0, 0.7, r);
  vec3 col = v_side > 0.0
      ? vec3(0.05, 0.80, 0.45)   // BUY  — green
      : vec3(0.92, 0.25, 0.30);  // SELL — red
  outColor = vec4(col, edge * v_alpha);
}`;

// ---------------------------------------------------------------------------
// Renderer
// ---------------------------------------------------------------------------


interface BubbleSlot {
  col: number;
  price: number; // absolute market price — y is computed in the shader
  qty: number;
  side: number;
}

export class HeatmapRenderer extends GLRenderer {
  // GL objects
  private depthProg!: WebGLProgram;
  private bubbleProg!: WebGLProgram;
  private depthTex!: WebGLTexture;
  private quadVao!: WebGLVertexArrayObject;
  private bubbleVao!: WebGLVertexArrayObject;
  private bubbleColBuf!: WebGLBuffer;
  private bubblePriceBuf!: WebGLBuffer;
  private bubbleQtyBuf!: WebGLBuffer;
  private bubbleSideBuf!: WebGLBuffer;

  // Uniform locations (depth pass)
  private uDepthWriteCol!: WebGLUniformLocation;
  private uDepthScroll!: WebGLUniformLocation;
  private uDepthVisible!: WebGLUniformLocation;
  private uDepthIntensity!: WebGLUniformLocation;
  private uDepthBg!: WebGLUniformLocation;

  // Uniforms (bubble pass)
  private uBubWriteCol!: WebGLUniformLocation;
  private uBubScroll!: WebGLUniformLocation;
  private uBubVisible!: WebGLUniformLocation;
  private uBubScale!: WebGLUniformLocation;
  private uBubCentre!: WebGLUniformLocation;
  private uBubSpan!: WebGLUniformLocation;

  // CPU-side ring buffer mirroring the depth texture.
  private ringBuf = new Float32Array(MAX_COLS * PRICE_LEVELS);
  private writeCol = 0;
  private populatedCols = 0;

  // Bubble VBO state (CPU-side staging arrays — re-uploaded on add).
  private bubbles: BubbleSlot[] = [];
  private bubblesDirty = false;

  // Current price-axis range (set by HeatmapChart after auto-range / zoom).
  // Used as shader uniforms so every bubble is positioned at its absolute
  // price regardless of how centrePrice has shifted since the trade.
  private centrePrice = 0;
  private priceSpan = 1;

  // Viewport
  private scrollCols = 0;
  // Log-scale tuning: log2(1+depth*scale)/log2(1+scale).
  // 500 maps 0.001→6%, 0.1→63%, 1.0 BTC→100%.
  private intensityScale = 500;
  private bgColor: [number, number, number] = [0.02, 0.04, 0.07];
  private bubbleScale = 3.0;

  constructor(canvas: HTMLCanvasElement) {
    super(canvas);
    this.initGL();
  }

  // -------------------------------------------------------- public API

  /** Push one aggregated depth column (BOOK_UPDATE → DepthAggregator). */
  pushColumn(col: DepthColumn): void {
    if (col.depth.length !== PRICE_LEVELS) {
      // Re-bucket on the fly if the aggregator emits a different size.
      const resized = new Float32Array(PRICE_LEVELS);
      const ratio = col.depth.length / PRICE_LEVELS;
      for (let i = 0; i < PRICE_LEVELS; i++) {
        resized[i] = col.depth[Math.floor(i * ratio)] ?? 0;
      }
      col = { ...col, depth: resized };
    }

    // 1. CPU ring buffer.
    const offset = this.writeCol * PRICE_LEVELS;
    this.ringBuf.set(col.depth, offset);

    // 2. GPU texture — one column strip.
    const gl = this.gl;
    gl.bindTexture(gl.TEXTURE_2D, this.depthTex);
    gl.texSubImage2D(
      gl.TEXTURE_2D,
      0,
      this.writeCol, 0,
      1, PRICE_LEVELS,
      gl.RED, gl.FLOAT,
      col.depth,
    );

    this.writeCol = (this.writeCol + 1) % MAX_COLS;
    this.populatedCols = Math.min(this.populatedCols + 1, MAX_COLS);
    this.invalidate();
  }

  /**
   * Record a trade bubble at its absolute market price.
   * The y-position is computed in the vertex shader using the current
   * centrePrice / priceSpan uniforms, so bubbles stay at the correct
   * screen position even as the visible range shifts over time.
   */
  pushBubble(price: number, qty: number, side: 'BUY' | 'SELL'): void {
    const col = (this.writeCol - 1 + MAX_COLS) % MAX_COLS;
    this.bubbles.push({
      col,
      price,
      qty: Math.max(qty, 0.01),
      side: side === 'BUY' ? 1 : -1,
    });
    if (this.bubbles.length > MAX_BUBBLES) {
      this.bubbles.splice(0, this.bubbles.length - MAX_BUBBLES);
    }
    this.bubblesDirty = true;
    this.invalidate();
  }

  /** Update the price-axis reference used by the bubble shader. */
  setCentrePrice(centrePrice: number, priceSpan: number): void {
    this.centrePrice = centrePrice;
    this.priceSpan = Math.max(priceSpan, 1);
    this.invalidate();
  }

  setScrollOffset(cols: number): void {
    this.scrollCols = Math.max(0, cols);
    this.invalidate();
  }

  setIntensityScale(v: number): void {
    this.intensityScale = Math.max(1, v);
    this.invalidate();
  }

  setBubbleScale(s: number): void {
    this.bubbleScale = Math.max(0.5, s);
    this.invalidate();
  }

  /**
   * Wipe the ring buffer and GPU texture.
   * Call this when the price range is reconfigured so stale columns at the
   * old scale do not corrupt the new view.
   */
  clearBuffer(): void {
    this.ringBuf.fill(0);
    this.writeCol = 0;
    this.populatedCols = 0;
    this.bubbles = [];
    this.bubblesDirty = true;
    this.centrePrice = 0;
    this.priceSpan = 1;
    const gl = this.gl;
    gl.bindTexture(gl.TEXTURE_2D, this.depthTex);
    gl.texImage2D(
      gl.TEXTURE_2D, 0, gl.R32F,
      MAX_COLS, PRICE_LEVELS, 0,
      gl.RED, gl.FLOAT,
      this.ringBuf,
    );
    this.invalidate();
  }

  /** Visible-cols default sizing matches the canvas pixel width. */
  private get visibleCols(): number {
    return Math.max(64, this.pixelWidth);
  }

  // ------------------------------------------------------ public getters

  get scrollOffset(): number {
    return this.scrollCols;
  }

  get heatIntensity(): number {
    return this.intensityScale;
  }

  // ------------------------------------------------------------------

  protected draw(): void {
    const gl = this.gl;
    gl.viewport(0, 0, this.pixelWidth, this.pixelHeight);
    gl.clearColor(...this.bgColor, 1.0);
    gl.clear(gl.COLOR_BUFFER_BIT);

    // Depth pass — full-screen quad textured with the ring buffer.
    gl.useProgram(this.depthProg);
    gl.activeTexture(gl.TEXTURE0);
    gl.bindTexture(gl.TEXTURE_2D, this.depthTex);
    gl.uniform1f(this.uDepthWriteCol, this.writeCol);
    gl.uniform1f(this.uDepthScroll, this.scrollCols);
    gl.uniform1f(this.uDepthVisible, this.visibleCols);
    gl.uniform1f(this.uDepthIntensity, this.intensityScale);
    gl.uniform3f(this.uDepthBg, ...this.bgColor);
    gl.bindVertexArray(this.quadVao);
    gl.drawArrays(gl.TRIANGLE_STRIP, 0, 4);

    // Bubble pass — point sprites with alpha blending.
    if (this.bubbles.length > 0 && this.centrePrice !== 0) {
      if (this.bubblesDirty) {
        this.uploadBubbles();
        this.bubblesDirty = false;
      }
      gl.useProgram(this.bubbleProg);
      gl.enable(gl.BLEND);
      gl.blendFunc(gl.SRC_ALPHA, gl.ONE_MINUS_SRC_ALPHA);
      gl.uniform1f(this.uBubWriteCol, this.writeCol);
      gl.uniform1f(this.uBubScroll, this.scrollCols);
      gl.uniform1f(this.uBubVisible, this.visibleCols);
      gl.uniform1f(this.uBubScale, this.bubbleScale);
      gl.uniform1f(this.uBubCentre, this.centrePrice);
      gl.uniform1f(this.uBubSpan, this.priceSpan);
      gl.bindVertexArray(this.bubbleVao);
      gl.drawArrays(gl.POINTS, 0, this.bubbles.length);
      gl.disable(gl.BLEND);
    }
  }

  // ------------------------------------------------------------------

  private initGL(): void {
    const gl = this.gl;

    // Programs
    this.depthProg = linkProgram(gl, DEPTH_VERT, DEPTH_FRAG);
    this.bubbleProg = linkProgram(gl, BUBBLE_VERT, BUBBLE_FRAG);

    // Uniforms
    this.uDepthWriteCol = this.must(gl.getUniformLocation(this.depthProg, 'u_writeCol'));
    gl.useProgram(this.depthProg);
    gl.uniform1i(this.must(gl.getUniformLocation(this.depthProg, 'u_depth')), 0);
    gl.uniform1f(this.must(gl.getUniformLocation(this.depthProg, 'u_maxCols')), MAX_COLS);
    gl.uniform1f(this.must(gl.getUniformLocation(this.depthProg, 'u_priceLevels')), PRICE_LEVELS);
    this.uDepthScroll = this.must(gl.getUniformLocation(this.depthProg, 'u_scrollCols'));
    this.uDepthVisible = this.must(gl.getUniformLocation(this.depthProg, 'u_visibleCols'));
    this.uDepthIntensity = this.must(gl.getUniformLocation(this.depthProg, 'u_intensityScale'));
    this.uDepthBg = this.must(gl.getUniformLocation(this.depthProg, 'u_bgColor'));

    gl.useProgram(this.bubbleProg);
    gl.uniform1f(this.must(gl.getUniformLocation(this.bubbleProg, 'u_maxCols')), MAX_COLS);
    this.uBubWriteCol = this.must(gl.getUniformLocation(this.bubbleProg, 'u_writeCol'));
    this.uBubScroll = this.must(gl.getUniformLocation(this.bubbleProg, 'u_scrollCols'));
    this.uBubVisible = this.must(gl.getUniformLocation(this.bubbleProg, 'u_visibleCols'));
    this.uBubScale = this.must(gl.getUniformLocation(this.bubbleProg, 'u_bubbleScale'));
    this.uBubCentre = this.must(gl.getUniformLocation(this.bubbleProg, 'u_centrePrice'));
    this.uBubSpan = this.must(gl.getUniformLocation(this.bubbleProg, 'u_priceSpan'));

    // Full-screen quad (two triangles via TRIANGLE_STRIP).
    this.quadVao = this.must(gl.createVertexArray());
    gl.bindVertexArray(this.quadVao);
    createBuffer(
      gl,
      gl.ARRAY_BUFFER,
      new Float32Array([-1, -1, 1, -1, -1, 1, 1, 1]),
      gl.STATIC_DRAW,
    );
    gl.enableVertexAttribArray(0);
    gl.vertexAttribPointer(0, 2, gl.FLOAT, false, 0, 0);

    // Depth ring-buffer texture.
    this.depthTex = createR32FTexture(gl, MAX_COLS, PRICE_LEVELS);

    // Bubble VBOs — one Float32Array per attribute to avoid interleaving
    // re-uploads when only some attributes change.
    this.bubbleVao = this.must(gl.createVertexArray());
    gl.bindVertexArray(this.bubbleVao);
    this.bubbleColBuf   = this.makeBubbleAttr(0, 1); // a_col
    this.bubblePriceBuf = this.makeBubbleAttr(1, 1); // a_price (absolute)
    this.bubbleQtyBuf   = this.makeBubbleAttr(2, 1); // a_qty
    this.bubbleSideBuf  = this.makeBubbleAttr(3, 1); // a_side

    gl.bindVertexArray(null);
  }

  private makeBubbleAttr(location: number, size: number): WebGLBuffer {
    const gl = this.gl;
    const buf = this.must(gl.createBuffer());
    gl.bindBuffer(gl.ARRAY_BUFFER, buf);
    gl.bufferData(gl.ARRAY_BUFFER, MAX_BUBBLES * 4, gl.DYNAMIC_DRAW);
    gl.enableVertexAttribArray(location);
    gl.vertexAttribPointer(location, size, gl.FLOAT, false, 0, 0);
    return buf;
  }

  private uploadBubbles(): void {
    const gl = this.gl;
    const n = this.bubbles.length;
    const cols   = new Float32Array(n);
    const prices = new Float32Array(n);
    const qtys   = new Float32Array(n);
    const sides  = new Float32Array(n);
    for (let i = 0; i < n; i++) {
      const b = this.bubbles[i]!;
      cols[i]   = b.col;
      prices[i] = b.price;
      qtys[i]   = b.qty;
      sides[i]  = b.side;
    }
    gl.bindBuffer(gl.ARRAY_BUFFER, this.bubbleColBuf);
    gl.bufferSubData(gl.ARRAY_BUFFER, 0, cols);
    gl.bindBuffer(gl.ARRAY_BUFFER, this.bubblePriceBuf);
    gl.bufferSubData(gl.ARRAY_BUFFER, 0, prices);
    gl.bindBuffer(gl.ARRAY_BUFFER, this.bubbleQtyBuf);
    gl.bufferSubData(gl.ARRAY_BUFFER, 0, qtys);
    gl.bindBuffer(gl.ARRAY_BUFFER, this.bubbleSideBuf);
    gl.bufferSubData(gl.ARRAY_BUFFER, 0, sides);
  }

  private must<T>(v: T | null): T {
    if (v === null) throw new Error('WebGL allocation returned null');
    return v;
  }
}

export { MAX_COLS, PRICE_LEVELS };
