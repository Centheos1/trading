"""Strategy service.

Headless trading engine that:
  * subscribes to raw trades and order book updates on Redis
  * runs the C++ ``orderflow_engine`` to produce signals, CVD, volume profile
  * exposes a FastAPI WebSocket stream (``/stream``) of raw market data and
    a REST API for backtesting, optimisation, and live strategy parameters

The service has no rendering logic.  All display decisions (tick size,
price range, colour gradients, indicator parameters) live in the UI
service.
"""
