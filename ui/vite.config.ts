import { defineConfig } from 'vite';
import react from '@vitejs/plugin-react';

// In docker-compose the strategy service is reachable as ``strategy:8000``
// from inside the UI container, but from the host browser it must be
// ``localhost:8000``.  We proxy /api and /stream so the browser only
// ever talks to the UI origin.

export default defineConfig({
  plugins: [react()],
  server: {
    host: '0.0.0.0',
    port: 3000,
    strictPort: true,
    proxy: {
      '/api': {
        // STRATEGY_HTTP_URL is a server-side env var (no VITE_ prefix) so
        // it is never exposed to the browser bundle.
        target: process.env.STRATEGY_HTTP_URL || 'http://localhost:8000',
        changeOrigin: true,
      },
      '/stream': {
        // Target is the base origin only — Vite appends the matched path
        // (/stream) automatically.  STRATEGY_WS_URL must NOT include /stream.
        target: process.env.STRATEGY_WS_URL || 'ws://localhost:8000',
        ws: true,
        changeOrigin: true,
      },
    },
  },
  build: {
    target: 'es2022',
    sourcemap: true,
  },
});
