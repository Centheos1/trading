/**
 * WebSocket client with auto-reconnect and msgpack decoding.
 *
 * Usage:
 *
 *     const client = new StreamClient('/stream');
 *     client.on('TRADE', (msg) => ...);
 *     client.on('BOOK_UPDATE', (msg) => ...);
 *     client.start();
 *
 * The client subscribes to all message types from the strategy service
 * (TRADE, BOOK_UPDATE, CANDLE, CVD_UPDATE, VP_UPDATE, SIGNAL, HEALTH).
 * Dispatch is O(1) — one handler list per message type.
 */

import { decode } from '@msgpack/msgpack';
import type { StreamMsg } from './messages';

type Handler<T extends StreamMsg = StreamMsg> = (msg: T) => void;

const BACKOFF_INITIAL_MS = 1000;
const BACKOFF_MAX_MS = 30_000;

export class StreamClient {
  private ws: WebSocket | null = null;
  private url: string;
  private handlers: Map<string, Set<Handler>> = new Map();
  private backoff = BACKOFF_INITIAL_MS;
  private stopped = false;
  private reconnectTimer: ReturnType<typeof setTimeout> | null = null;
  private statusHandlers: Set<(connected: boolean) => void> = new Set();

  constructor(url?: string) {
    // VITE_WS_URL is injected by Vite at dev-server startup from the
    // container environment.  In local Docker development it points
    // directly to the strategy service port (bypassing the Vite proxy,
    // which has known issues tunnelling WebSocket frames in Docker).
    // In production (nginx serves everything from one origin) this var
    // is not set, so we fall back to the same-origin path which nginx
    // proxies correctly.
    const proto = window.location.protocol === 'https:' ? 'wss' : 'ws';
    const fallback = `${proto}://${window.location.host}/stream`;
    this.url = url ?? import.meta.env.VITE_WS_URL ?? fallback;
  }

  /** Register a handler for one message type. */
  on<T extends StreamMsg['type']>(
    type: T,
    handler: Handler<Extract<StreamMsg, { type: T }>>,
  ): () => void {
    let set = this.handlers.get(type);
    if (!set) {
      set = new Set();
      this.handlers.set(type, set);
    }
    set.add(handler as Handler);
    return () => set!.delete(handler as Handler);
  }

  /** Listen for connect / disconnect events. */
  onStatus(handler: (connected: boolean) => void): () => void {
    this.statusHandlers.add(handler);
    return () => this.statusHandlers.delete(handler);
  }

  start(): void {
    this.stopped = false;
    this.connect();
  }

  stop(): void {
    this.stopped = true;
    if (this.reconnectTimer !== null) {
      clearTimeout(this.reconnectTimer);
      this.reconnectTimer = null;
    }
    if (this.ws !== null) {
      this.ws.close();
      this.ws = null;
    }
  }

  // ------------------------------------------------------------------

  private connect(): void {
    try {
      const ws = new WebSocket(this.url);
      ws.binaryType = 'arraybuffer';
      this.ws = ws;

      ws.onopen = () => {
        this.backoff = BACKOFF_INITIAL_MS;
        this.statusHandlers.forEach((h) => h(true));
      };

      ws.onmessage = (event) => {
        // Live data frames are always binary (msgpack).  Strings are
        // ignored on this socket today but reserved for control msgs
        // (e.g. server-initiated diagnostics).
        if (typeof event.data === 'string') {
          return;
        }
        try {
          const decoded = decode(new Uint8Array(event.data)) as StreamMsg;
          const handlers = this.handlers.get(decoded.type);
          if (handlers !== undefined) {
            handlers.forEach((h) => h(decoded));
          }
        } catch (err) {
          // eslint-disable-next-line no-console
          console.error('Failed to decode stream message', err);
        }
      };

      ws.onerror = () => {
        // ``onclose`` will be invoked right after.  No-op here.
      };

      ws.onclose = () => {
        this.ws = null;
        this.statusHandlers.forEach((h) => h(false));
        if (!this.stopped) {
          this.scheduleReconnect();
        }
      };
    } catch (err) {
      // eslint-disable-next-line no-console
      console.error('WebSocket connect threw', err);
      if (!this.stopped) {
        this.scheduleReconnect();
      }
    }
  }

  private scheduleReconnect(): void {
    const delay = this.backoff;
    this.backoff = Math.min(this.backoff * 2, BACKOFF_MAX_MS);
    this.reconnectTimer = setTimeout(() => this.connect(), delay);
  }
}
