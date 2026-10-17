# Context Map

## Contexts

- [Tide / Wave / Ripple research](./research/CONTEXT.md) — the current strategy programme. It discovers whether a BTCUSDT forecast and decision process has value.
- [OANDA frontier](./oanda_portfolio/CONTEXT.md) — a separate daily portfolio strategy. It is still being developed in its own notebook and is not on the research promotion path.
- [Historical V1](./docs/history/strategy_v1.md) — the frozen bounce/breakout trading system. It is a benchmark and an operational baseline, not the current strategy.

## Relationships

- **Research ↔ Historical V1**: the research programme may use V1 as a nested benchmark. It does not inherit V1 orders, permissions, or promotion status.
- **Research ↔ OANDA frontier**: the contexts may share storage and engineering standards. A result in one cannot validate a hypothesis in the other.
- **OANDA frontier ↔ Historical V1**: neither is an implementation of the other.
