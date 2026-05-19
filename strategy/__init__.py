"""Strategy service.

Headless trading engine that:
  * subscribes to raw trades and order book updates on Redis
  * runs the C++ ``orderflow_engine`` to produce signals, CVD, volume profile
  * exposes a FastAPI REST API for backtesting, optimisation, and live
    strategy parameters
"""
