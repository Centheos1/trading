# Tide / Wave / Ripple research

The current strategy programme. It asks whether distributions, risk constraints, and execution can be shown to add value before any of them is allowed to trade.

## Language

**Tide**:
The market and portfolio state that constrains how much risk may be open.
_Avoid_: macro bias, trade signal

**Wave**:
A conditional distribution over future opportunity.
_Avoid_: long/short/neutral, permission matrix

**Ripple**:
Microstructure information and the execution of an action that Decision has already authorised.
_Avoid_: entry signal, strategy

**Decision**:
The authorisation of a change in exposure, including an explicit decision to do nothing.
_Avoid_: order, signal

**Target exposure**:
The exposure authorised by a Decision. Execution turns it into orders.
_Avoid_: order intent, position size as an order

**NO-TRADE**:
A Decision that no exposure change has sufficient expected value under the current constraints.
_Avoid_: flat signal, neutral regime

**Forecast**:
A distribution over future log returns, or a declared functional of that distribution.
_Avoid_: price target, trade

**Horizon**:
The future interval covered by a Forecast. The initial family is 5, 15, and 60 minutes.
_Avoid_: bar count when a timestamp is missing

**Label availability**:
The time at which a Horizon's outcome is known. It is the decision time plus the Horizon.
_Avoid_: decision time, bar time

**Sealed test**:
The final calendar block that cannot be used to choose features, models, thresholds, or utilities.
_Avoid_: validation set, out-of-sample sample used during development

**Dataset manifest**:
The immutable identity of the exact stored inputs, time bounds, and quality exclusions used by an Experiment.
_Avoid_: latest data, file path

**Experiment**:
One registered hypothesis test with a stable identifier. A changed question is a new Experiment.
_Avoid_: notebook, backtest run

**Baseline**:
The simpler nested model or policy that a candidate must beat on a pre-registered comparison.
_Avoid_: buy-and-hold when a different nested control was declared

**Historical V1**:
The frozen bounce/breakout system. It can be a Baseline. It is not this strategy.
_Avoid_: current strategy, production strategy
