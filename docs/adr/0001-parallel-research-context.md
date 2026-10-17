# Research proceeds beside the frozen V1

The deployed historical V1 already encodes bounce/breakout behaviour, while the current strategy is an unvalidated research programme. Replacing V1 in place would destroy the baseline and let untested hypotheses inherit its live path. The research roadmap is therefore the root `strategy.md`, the old specification is archived at `docs/history/strategy_v1.md`, and new work is a separate Python package under `research/`. Notebooks orchestrate tested modules. Confirmatory inputs are immutable dataset manifests. The first research slice is BTCUSDT log-return evaluation and event-time replay, not a broker deployment.

V1 remains runnable. An in-place refactor, a notebook-only implementation, and treating the OANDA frontier as Tide were rejected because each collapses a boundary this programme needs to keep.
