/// <reference types="vite/client" />

interface ImportMetaEnv {
  readonly VITE_STRATEGY_HTTP_URL?: string;
  readonly VITE_STRATEGY_WS_URL?: string;
}

interface ImportMeta {
  readonly env: ImportMetaEnv;
}
