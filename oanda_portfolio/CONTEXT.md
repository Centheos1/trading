# OANDA frontier

A separate daily portfolio strategy. Its notebook is still the working surface, and its promotion is independent of Tide / Wave / Ripple research.

## Language

**OANDA frontier**:
The daily multi-instrument strategy developed in `notebooks/11_oanda_frontier.ipynb`.
_Avoid_: Tide, Wave, Ripple, H6

**Composition weight**:
An asset's share of the fully invested mix, before account leverage is chosen.
_Avoid_: order size, margin usage

**Account exposure**:
Composition weight multiplied by the account's leverage. This is the economic position.
_Avoid_: composition weight, margin utilization
