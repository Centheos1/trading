/**
 * Small WebGL2 helpers shared by all renderers.
 *
 * Keep this file free of state — every function is a pure helper that
 * either compiles a shader, links a program, or creates a GPU buffer.
 */

export function getGL2(canvas: HTMLCanvasElement): WebGL2RenderingContext {
  const gl = canvas.getContext('webgl2', {
    antialias: false,
    preserveDrawingBuffer: false,
    powerPreference: 'high-performance',
  });
  if (gl === null) {
    throw new Error(
      'WebGL2 is required.  Update your browser or enable hardware acceleration.',
    );
  }
  return gl;
}

export function compileShader(
  gl: WebGL2RenderingContext,
  type: GLenum,
  source: string,
): WebGLShader {
  const shader = gl.createShader(type);
  if (shader === null) throw new Error('createShader returned null');
  gl.shaderSource(shader, source);
  gl.compileShader(shader);
  if (!gl.getShaderParameter(shader, gl.COMPILE_STATUS)) {
    const info = gl.getShaderInfoLog(shader);
    gl.deleteShader(shader);
    throw new Error(`Shader compile error: ${info ?? '(no log)'}`);
  }
  return shader;
}

export function linkProgram(
  gl: WebGL2RenderingContext,
  vsSrc: string,
  fsSrc: string,
): WebGLProgram {
  const vs = compileShader(gl, gl.VERTEX_SHADER, vsSrc);
  const fs = compileShader(gl, gl.FRAGMENT_SHADER, fsSrc);
  const prog = gl.createProgram();
  if (prog === null) throw new Error('createProgram returned null');
  gl.attachShader(prog, vs);
  gl.attachShader(prog, fs);
  gl.linkProgram(prog);
  if (!gl.getProgramParameter(prog, gl.LINK_STATUS)) {
    const info = gl.getProgramInfoLog(prog);
    gl.deleteProgram(prog);
    throw new Error(`Program link error: ${info ?? '(no log)'}`);
  }
  gl.deleteShader(vs);
  gl.deleteShader(fs);
  return prog;
}

export function createBuffer(
  gl: WebGL2RenderingContext,
  target: GLenum,
  data: BufferSource | null,
  usage: GLenum,
): WebGLBuffer {
  const buf = gl.createBuffer();
  if (buf === null) throw new Error('createBuffer returned null');
  gl.bindBuffer(target, buf);
  if (data !== null) {
    gl.bufferData(target, data, usage);
  }
  return buf;
}

/** Allocate a single-channel float texture of the given size. */
export function createR32FTexture(
  gl: WebGL2RenderingContext,
  width: number,
  height: number,
): WebGLTexture {
  const tex = gl.createTexture();
  if (tex === null) throw new Error('createTexture returned null');
  gl.bindTexture(gl.TEXTURE_2D, tex);
  // EXT_color_buffer_float is required to render INTO an R32F texture
  // but is NOT required just to sample from one — and we only sample.
  gl.texImage2D(
    gl.TEXTURE_2D,
    /* level   */ 0,
    /* iformat */ gl.R32F,
    width,
    height,
    /* border  */ 0,
    /* format  */ gl.RED,
    /* type    */ gl.FLOAT,
    /* data    */ null,
  );
  gl.texParameteri(gl.TEXTURE_2D, gl.TEXTURE_MIN_FILTER, gl.NEAREST);
  gl.texParameteri(gl.TEXTURE_2D, gl.TEXTURE_MAG_FILTER, gl.NEAREST);
  gl.texParameteri(gl.TEXTURE_2D, gl.TEXTURE_WRAP_S, gl.CLAMP_TO_EDGE);
  gl.texParameteri(gl.TEXTURE_2D, gl.TEXTURE_WRAP_T, gl.CLAMP_TO_EDGE);
  return tex;
}
