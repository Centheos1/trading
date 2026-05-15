/**
 * React hook that owns one ``StreamClient`` for the lifetime of the
 * app and pipes its messages into the Zustand store.
 *
 * Components that need the raw BOOK_UPDATE / TRADE stream for the
 * WebGL renderers can call ``useStreamClient()`` to get the same
 * client instance and register their own handlers — the depth ring
 * buffer must stay out of React state.
 */

import { useEffect, useRef } from 'react';
import { StreamClient } from './client';
import { useTradingStore } from '../stores/trading';
import { api } from '../api/rest';

let SHARED: StreamClient | null = null;

export function useStreamClient(): StreamClient {
  if (SHARED === null) {
    SHARED = new StreamClient();
  }
  return SHARED;
}

/** Mount-once effect that wires the shared client to the store. */
export function useStreamWiring(): void {
  const client = useStreamClient();

  const setConnected = useTradingStore((s) => s.setConnected);
  const ingestTrade = useTradingStore((s) => s.ingestTrade);
  const ingestCandle = useTradingStore((s) => s.ingestCandle);
  const ingestCvd = useTradingStore((s) => s.ingestCvd);
  const ingestVp = useTradingStore((s) => s.ingestVp);
  const ingestSignal = useTradingStore((s) => s.ingestSignal);
  const ingestHealth = useTradingStore((s) => s.ingestHealth);
  const preloadCandles = useTradingStore((s) => s.preloadCandles);
  const symbol = useTradingStore((s) => s.symbol);

  // Track whether a preload is already in-flight so reconnects don't
  // queue duplicate fetches.
  const preloadedRef = useRef(false);

  useEffect(() => {
    preloadedRef.current = false;

    // No wired-guard here — React StrictMode intentionally calls the
    // cleanup then re-runs the effect.  The guard was preventing
    // client.start() from being called after StrictMode's cleanup
    // (which calls client.stop()), leaving the client permanently stopped.
    const offs: Array<() => void> = [];

    const handleStatus = (connected: boolean) => {
      setConnected(connected);
      if (connected && !preloadedRef.current) {
        preloadedRef.current = true;
        // Fetch recent candle history so the chart is populated immediately
        // rather than waiting up to ``bucketMs`` for the first candle close.
        api.getCandles(symbol, 200).then((res) => {
          if (res.candles.length > 0) {
            preloadCandles(res.candles);
          }
        }).catch(() => {
          // Best-effort — live TRADE messages will build candles regardless.
        });
      }
    };

    offs.push(client.onStatus(handleStatus));
    offs.push(client.on('TRADE', ingestTrade));
    offs.push(client.on('CANDLE', ingestCandle));
    offs.push(client.on('CVD_UPDATE', ingestCvd));
    offs.push(client.on('VP_UPDATE', ingestVp));
    offs.push(client.on('SIGNAL', ingestSignal));
    offs.push(client.on('HEALTH', ingestHealth));
    client.start();

    return () => {
      offs.forEach((off) => off());
      client.stop();
    };
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, []);
}
