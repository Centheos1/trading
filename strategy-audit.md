# Audit of `strategy.new.md`

**Role.** Critical quantitative researcher and quantitative developer.  
**Compared against.** Historical `strategy.md` (V1 implementation contract) and the deployed architecture in `docs/implementation_plan.md` / live `strategy` service (Tide 60 s, Wave 5 s, C++ Ripple per event, Redis collector, NSGA-II optimiser, replay harness).  
**Scope.** Weaknesses in the *new* specification. This is not a rewrite.  
**Stance.** Tide / Wave / Ripple is a research architecture, not evidence. Hierarchical decomposition can be wrong, redundant, or unidentifiable given the data.

**Documents not modified:** `strategy.md`, `strategy.new.md`.

---

## Verdict (read this first)

`strategy.new.md` is a better *research charter* than the 2,400-line V1 spec: it separates prediction from decision in language, treats bounce/breakout as hypotheses, and refuses to hard-code utility. It is **not yet a strategy specification that could be implemented, backtested, or autonomously executed without a large number of silent choices**.

The most serious problems are not missing mermaid diagrams. They are:

1. **The mathematical core is internally inconsistent** (Gaussian diffusion vs Student-\(t\) returns; ES of log-returns vs dollar risk budgets; Fokker–Planck written for a Markov diffusion that the rest of the programme immediately abandons).
2. **The decision layer is named but not specified**, so prediction/decision/execution still collapse in any real system — and they *already collapse* in the live engine.
3. **First-passage and Fokker–Planck research cannot be identified on the stated initial dataset** (1-minute OHLC has no intra-bar path; no dual timestamps; almost no historical L2).
4. **The document both defers complexity and commits to a three-layer autonomous stack**, which is the same over-engineering failure mode as V1, with a new probabilistic costume.
5. **Operational contracts that made V1 runnable were dropped** (lifecycle, exits, intent schema, consumed-ES accounting, postures OBSERVE/PAPER/LIVE, cadences) without replacements. A bot cannot “eventually” execute on open questions.

Existing platform work (HMM A/B, NSGA-II Wave/Tide optimise, live wiring) is treated as “engineering substrate.” That is too generous. It is also a **contamination and overfitting surface** for the Master’s programme.

---

## 1. Concepts from the old strategy that were accidentally lost

These are not “implementation trivia.” They are objects the new spec still *implicitly* needs, or research baselines it claims to keep but no longer defines.

### 1.1 Objects required for any executable system

| Lost concept | Why it matters | Risk if omitted |
|---|---|---|
| Trade lifecycle FSM (SETUP→…→COOLDOWN) | Autonomy is a state machine over orders and positions, not a density | Unspecified partial fills, re-entry, and flatten behaviour |
| Exit taxonomy and **priority order** (risk-budget, invalidation, time, target, exhaustion) | First-passage \(\tau_{\text{target}},\tau_{\text{stop}}\) is empty without operational barrier rules | Stops and targets will be invented per experiment |
| Scale-in / scale-out / partial exits | Position sizing is not a single \(a_t\) | Size path unidentified |
| Execution **intent** schema (ENTRY/SCALE/EXIT, MARKET/LIMIT, urgency) | Live path already uses intents + `intent_risk_block_reason` | Research decisions cannot map onto the broker |
| Consumed vs remaining ES; flatten when `risk_multiplier == 0` | Tide is a **control loop**, not a snapshot | Budget constraint is not enforceable |
| One-trade-per-symbol and cooldown | Identifies overlapping bets | First-passage labels and P&L double-count |
| Maker/taker fees as first-class, distinct from “slippage” | Cost model in §7.4 is a sum of unlike terms | Backtests default to zero (see current optimiser reports) |
| Mark vs last vs mid vs microprice | \(P_t\) is undefined | Every equation in §4 is ambiguous |
| Perpetual **funding**, mark price, liquidation | Product is a USD-M perp in the live stack | \(R_p\) is not a spot log-return |
| Fill event contract (commission, is_maker, order_id) | Required to learn \(C_{\text{execution}}\) | Execution research has no labelled outcome |

### 1.2 Research baselines that disappeared while still being referenced

- **Liquidity map** (resting / traded / flow / structure / voids) and **hold / break / dest** scores. Phase 20 still exists as “logistic hold/break calibration.” That *is* a first-passage model on levels. The new spec mentions walls and voids as a feature laundry list and never reconnects them to \(\tau_{\text{target}},\tau_{\text{stop}}\).
- **Wall quality**, persistence, cancellation, VPIN, Tide- and Ripple-level LSI.
- **Permission matrix** as an *explicit baseline policy* (the document calls it crude, then does not table it as Benchmark 6).
- **Vol-regime ladder** LOW/NORMAL/HIGH/CRISIS and the deterministic risk multiplier. Needed as the nested Tide model before Student-\(t\) ES.
- **Funding rate and open interest** as Tide candidates (old open research Q7).
- **Cross-venue L1** (Oanda) as a Wave information set — implemented as config boosts, not evaluated as H3-style incremental information.
- **Confirmation window** (setup vs entry). Without it, “decision time” and “execution time” are the same bar — classic leakage.
- **Replay determinism / event-time clock** as a *research* requirement, not only an ops sentence in §14. The old spec made this an invariant of *all* decision logic.
- **Feature namespaces and snapshot contracts.** The new spec has no interface between layers. Experiments will not be comparable.
- **C++ hot-path vs Python slow-path split.** Irrelevant to the thesis *until* Ripple is live; fatal to “eventual autonomy” if ignored.

### 1.3 Institutional knowledge that should have been inherited as warnings

- V1 audit: Tide/Wave snapshots unit-tested but **not wired**; live ran on `DefaultTideSnapshot` / `DefaultWaveSnapshot`. The new promotion rules (§22) do not mention wiring tests.
- Phase 9 knobs (`liquidation_equity_frac`, `slippage_bps`) exist and are **defaulted to 0.0 in published Wave optimise reports**. “Evaluate after realistic costs” is already being violated by the current toolchain.
- Phase 7V HMM A/B on the order of **tens of observations**, mixed verdict, in-sample BIC on the same window. The new spec does not freeze this as *non-confirmatory* or sequester those dates from the untouched test set.

---

## 2. Concepts from the current Tide / Wave / Ripple research direction that are missing

The new direction is probabilistic, hierarchical, and dual-objective. Relative to *that* brief, the spec is still missing the hard parts.

| Gap | Why it is not optional |
|---|---|
| Definition of the **action set** \(\mathcal{A}\) | \(\max_a \mathbb{E}[U(R_p\mid a)]\) is undefined if \(a\) is “maybe a size, maybe an archetype, maybe a hedge” |
| Mapping from Wave objects to **barriers** | First-passage needs \(A,B\) (or a grid). “Experimental choice” is not a model |
| **Filtered vs smoothed** HMM posteriors | \(\pi_t\) from the forward–backward algorithm uses the future. Live/trading must use filtered \(\pi_{t\mid t}\) only |
| **Which layer owns \(\pi_t\)** | Tide “regime probabilities as risk inputs”; Wave publishes \(\pi_t\); historical HMM is **Ripple** liquidity-state. Three different latent processes are being called one HMM |
| **GARCH / realised-vol** as the actual next model after Gaussian | The live Tide already uses realised vol. Jumping to Fokker–Planck skips the model that 1-minute data can actually identify |
| **What \(\mathcal{F}_t\) excludes** | Same-bar high/low, current-bar VWAP, close-to-close vs open-to-close, next-funding known in advance |
| **Universe and numeraire** | BTCUSDT perp vs multi-asset PCA; USDT P&L vs AUD wealth. H3 and H13 require this |
| **Overlapping forecasts** | Rolling \(p(R_{t:t+h})\) every minute with horizon \(h\gg 1\) produces dependent scores; no HAC / block bootstrap / unique-decision clock |
| **Promotion gate that matches the live postures** | OBSERVE / PAPER / LIVE already exist. Research “observation vs armed” is not bound to them |
| **Source-of-truth conflict** | `AGENTS.md` / `implementation_plan.md` still rank **`strategy.md` first**. A new canonical spec that does not rewire this *will not be implemented* |
| **Identifiability of incremental layers** | If Tide, Wave, and Ripple share returns, vol, and \(\pi_t\), ablations are collinear. No orthogonalisation protocol |

---

## 3. Assumptions presented as facts that should be hypotheses

The document is careful in §18 and sloppy in “current design” sections. These are **architectural commitments that smuggle theory**.

| Statement (paraphrase) | Why it is not a fact |
|---|---|
| Markets have a useful Tide/Wave/Ripple hierarchy | Could be one model at one horizon; extra layers can be p-hacking machines |
| Prediction, decision, and execution *should* be separate modules | Valid as software hygiene; not a unique statistical identification strategy |
| Work in \(X_t=\ln P_t\) | Discrete ticks, spreads, and funding are not log-diffusions. Log vs simple return is a hypothesis (especially with leverage) |
| Start from \(dX_t=\mu_t dt+\sigma_t dW_t\) | 1-minute crypto returns are closer to a weakly dependent heavy-tailed discrete-time series. Diffusion is a modelling hypothesis, and a strong one |
| Fokker–Planck / flux are the right language for first passage | For discrete barriers and OHLC, Kaplan–Meier / empirical hitting frequencies may dominate PDE methods |
| ES is the preferred live constraint because it is coherent | ES is **not elicitable**; it is hard to backtest; estimation error can make VaR or stress notional *better operationally*. Coherence ≠ implementability |
| Student-\(t\) is the correct second rung | Alternatives: skew-\(t\), mixture, jump, realised-vol + Gaussian, EVT peaks-over-threshold. “\(t\) next” is fashion unless nested against these |
| HMM is the latent-state default | Change-point, MS-GARCH, simple vol bins already exist as Tide `vol_regime`. H2 should not privilege HMM |
| RORAC belongs at allocation time | RORAC is a ratio; maximising ratios is a different (often pathological) programme than constrained expected utility |
| Ripple is the only layer that submits orders | The live **risk gate** already rejects/reduces size. Execution bridge is a decision maker |
| Dual timestamps “must” exist for the programme | True for live L2; **false for the five-year 1-minute archive** |
| Composition of validated components is the path to a bot | Interactions can destroy edges (H10). Stated, then the architecture still assumes composition |
| “None of the economic hypotheses are validated” | True in a strict sense; false as *sociology of the project* — optimiser reports and HMM A/B will be treated as evidence unless explicitly burned or sequestered |

**Elicitability footnote (should be in the spec as a hypothesis, not ignored).** VaR is elicitable; ES is not. That is a reason *not* to treat ES as the unique professional choice for both estimation *and* constraint.

---

## 4. Mathematical inconsistencies

### 4.1 Gaussian diffusion vs Student-\(t\) returns vs Fokker–Planck

The SDE \(dX_t=\mu_t dt+\sigma_t dW_t\) with locally integrable \(\mu,\sigma\) produces **conditionally Gaussian** increments of \(X\) (or mixtures of Gaussians if \((\mu,\sigma)\) are stochastic). It does **not** produce Student-\(t\) increments over a fixed \(h\) unless you change the process (variance-mixture, subordination, jumps).

The Fokker–Planck equation in §4.3 is the forward equation for *that* diffusion. Rung 2 of §4.6 (“Student-\(t\) return model”) **leaves the SDE/FP setting**. Rung 4 then returns to “conditional diffusion” as if the \(t\) model were a diffusion. This is not a nested ladder; it is two incompatible probability models stapled together.

**Consequence.** Likelihood-ratio tests along §4.6 are not nested. Calibration comparisons are still valid; calling it a “progression” is not.

### 4.2 Time-varying \(\mu_t,\sigma_t\) and a 1-D Fokker–Planck

If \(\mu_t=f(Z_t,O_t)\) and \(O_t\) is an exogenous microstructure process, the pair \((X_t,O_t)\) is the Markov state (if anything is). The scalar FP for \(p(x,t)\) is then either:

- a **conditional** equation given a frozen path of \((\mu,\sigma)\), or
- **wrong**.

Flux \(J\) as written does not justify first-passage probabilities under order-flow conditioning without stating which of those two interpretations is meant.

### 4.3 ES of losses vs ES of returns vs a dollar budget

§5.2 defines VaR/ES on a **loss** \(L\ge 0\). §5.3 constrains \(\mathrm{ES}_\alpha(R_p)\le B_t\). \(R_p\) is introduced as a **log return**. Those objects live in different spaces:

- Left-tail ES of a log-return is typically **negative** (a return quantile).
- \(B_t\) is discussed as a **budget** (historically USDT).
- Portfolio value change \(\Delta V \approx V\cdot(e^{R}-1)\) is not \(R\).
- Levered perp P&L is marked on **notional**, with funding, not on \(\ln P\).

Euler decomposition in §5.3 requires positive homogeneity of \(\rho(\mathbf{w})\). Homogeneity holds for dollar P&L of linear positions, **not** for ES of log-returns as a function of “action” \(a\) if \(a\) is leverage.

V1 was clearer (if crude): \(\mathrm{ES}_i \approx Q_i P_i \hat\sigma_i \sqrt{\Delta t}\cdot \phi(z_\alpha)/(1-\alpha)\). The new spec dropped the only internally consistent units.

### 4.4 Student-\(t\) moments

If \(\nu\le 2\), variance is infinite; if \(\nu\le 1\), mean (and standard ES constructions) break. Regime-conditioned \(t\) mixtures can have **undefined ES** at the same \(\alpha\) used for Gaussian. No domain restriction appears.

### 4.5 ES is not linear in regimes

Even with finite moments, \(\mathrm{ES}(\sum \pi_k R^{(k)}) \neq \sum \pi_k \mathrm{ES}(R^{(k)})\). “Regime-conditioned Student-\(t\)” plus a single ES constraint needs the **mixture distribution**, not a probability-weighted average of per-state ES. The spec never says this. Tide will be implemented wrong.

### 4.6 First passage: \(X\) vs \(P\), continuous vs bars

\(\tau\) is “to a target or stop in \(X\) (or in \(P\))” — these are different barriers. Additive stops in price are **geometric** in \(X\). On 1-minute OHLC, the continuous hitting time is **not observed**. Using bar high/low as if they were continuous first passage **upward-biases** hit rates (classic barrier bias). Using close-only **downward-biases** them. Neither is stated.

Two-barrier probabilities for GBM have formulas; with estimated \(\mu_t,\sigma_t\) they are plug-in estimators with huge error at short horizon. PDE language conceals that.

### 4.7 \(C_{\text{execution}}\) is not an equation

Spread, commission, slippage, impact, funding, and latency are not commensurate. Funding is a **carry** over holding time, not an entry cost. Latency is a **timing operator** on the fill, not a scalar addend. Summing them hides double counting (spread vs slippage vs impact).

### 4.8 Options / FX decomposition

\(R_p = R_{\mathrm{BTC}} + H(\mathrm{FX}_T) - C_{\text{option}}\) mixes a (log?) BTC return, an FX hedge payoff, and an option **premium** without a numeraire, without stating whether the option is on BTC or FX, and overloads \(P\) for both physical measure and price. §13 is right to defer; the formula should not be in a canonical spec in that form.

### 4.9 RORAC is undefined

H6 and §8.2 never write RORAC. Standard form (expected excess / ES capital) needs: horizon, after-cost numerator, whether ES is ex ante or ex post, and what happens when ES \(\to 0\). Ratio maximisation encourages **small-denominator artefacts**.

### 4.10 Notation collisions

- \(P\) is price and physical measure.
- \(\sigma\) is volatility and (in the old spec) the logistic function.
- \(\pi_t\) is HMM posterior and (old) wall persistence \(\pi_p\).
- \(D_t\) dispersion vs depth — old spec disambiguated; new spec uses \(D_t\) once and \(\mathcal{D}\) never.

### 4.11 Schrödinger

Correctly demoted. Still a landmine: it will attract implementation effort. It should be **forbidden on the critical path**, not “experimental extension.”

---

## 5. Architectural inconsistencies

### 5.1 Three documents, two canons

| Document | Canonical? |
|---|---|
| `strategy.new.md` | Claims to win if others contradict it |
| `strategy.md` | Still “canonical source of truth” for agents |
| `implementation_plan.md` | Maps modules to **V1 snapshots** (`TideSnapshot` bias, `WaveSnapshot` permissions, `RippleIntent`) |

Until this is resolved, developers will implement bounce/breakout permissions and researchers will write Fokker–Planck. The live bot will match neither.

### 5.2 The hierarchy both is and is not a hierarchy

§2.1: hierarchy is not causal superiority.  
§3 / §16: Tide → Wave → Decision → Ripple is the **control flow**.  
Live engine: Tide/Wave snapshots **pushed into** Ripple, which **emits intents**, which the **execution bridge** may block.

That is the old “no layer may override the one above” architecture with a new Decision box that **does not exist in code**. If Decision can reject Ripple’s timing, Ripple is not the unique order submitter. If Decision cannot, Decision is commentary.

### 5.3 Two research stacks, one runtime

| Spec | Runtime |
|---|---|
| Wave outputs \(p(R\mid\mathcal{F}_t)\) | `WaveEngine` → regime enum + permission bitfield on 5 s |
| Tide outputs probabilistic ES + \(\pi_t\) | `TideEngine` → vol_regime, risk_multiplier, es_budget on 60 s |
| Decision layer | **Missing.** Closest: `intent_risk_block_reason` + size throttle |
| Ripple refines first passage | `RippleEngine` **is** the strategy: walls, archetypes, lifecycle |

Calling the runtime “substrate” hides that **the only autonomously executable policy today is the V1 policy**. The new spec provides no adapter.

### 5.4 Cadence vs continuous time

FP/SDE language is continuous. Production is 60 s / 5 s / event. Research dataset is 60 s bars. No Nyquist / information-clock section. Conditioning Wave on Ripple \(O_t\) at 1-minute resolution is not microstructure research; it is **bar-aggregated book stats**, a different estimator with different leakage.

### 5.5 Portfolio Tide vs single-symbol Ripple

Euler cells and multi-asset PCA require a universe. V1 and live: **one symbol, one trade**. H3/H5/H6 are portfolio hypotheses on a single-name engine. Either Tide is overspecified or the runtime is underspecified.

### 5.6 News in two diagrams

§12: news → Tide/Wave/**Ripple**.  
§16 diagram: news → Tide and Wave only.  
Minor, but it shows layer ownership is not decided.

---

## 6. Prediction / decision / execution boundaries that remain unclear

This was the headline principle. It is not operationalised.

**What is a prediction?** Density, first-passage probability, ES, \(\pi_t\), factor residual — all listed. No required output schema, no horizon, no update clock, no statement whether predictions are **conditional on current position**.

**What is a decision?** \(a_t\) may be “do nothing.” Is \(a_t\) target weight, delta notional, a pair \((\text{side}, Q)\), a barrier pair, or “arm Ripple”? Can Decision choose the **archetype**? If yes, Wave/Ripple research is not separable. If no, H9 lives entirely in Ripple and Decision is a sizer.

**What is execution?** §7 mixes (i) modelling \(O_t\) as **predictive** of \(R\), and (ii) minimising \(C_{\text{execution}}\) given \(a\). Those are different likelihoods, different labels, different data. H7 vs H8 are distinct; the layer is not.

**Who may touch orders?**

- Spec: Ripple submits; Decision approves; Tide constrains.
- Code: Ripple proposes; risk function **mutates or blocks**; broker executes; Wave permissions can force exit in V1.

**When is information allowed?** Confirmation windows, order latency, and “signal on close, trade on next open” are unspecified. Without them, prediction and execution share a timestamp.

**In-trade predictions.** Does Wave keep publishing \(p(R\mid\mathcal{F}_t)\) for an open position (manage/exit), or only for entry? Exit is where first-passage actually lives. The spec is entry-centric.

**Failure: uncalibrated model.** §19 item 9 requires a failure path; §14 lists fallback. No mapping: e.g. “if PIT calibration rejects at 1%, Decision output is \(a=0\) and Tide \(B_t\) shrinks.” Without that, autonomy cannot fail closed in a defined way.

---

## 7. Data requirements that cannot currently be satisfied

| Requirement in `strategy.new.md` | Current reality |
|---|---|
| ~5 years 1-minute **price** data | Plausible starting set for L1 research; **not** a book; typically **OHLCV**, not a point process |
| Dual timestamps (exchange + local receipt) on all research data | Historical klines: **exchange bar time only**. Live health code tracks exchange ts; dual-clock research is **prospective** |
| Raw depth, snapshots, sequence IDs, session metadata | Phase 16P collector **in progress**; archive is not a five-year L2 panel |
| First-passage on continuous \(X_t\) | Intra-bar path **unobserved** on 1-minute bars |
| Microstructure \(\mu_t=f(Z_t,O_t)\) | \(O_t\) not in the initial dataset |
| Fill, slippage, impact, latency **outcomes** | Need own orders or a structural model; historical public trades \(\neq\) *your* fill |
| News earliest-available timestamps | No specified archive; LLM scrape time will be used unless a vendor clock exists |
| Cross-asset PCA / dispersion / absorption ratio | Phase 16Q OHLCV collection **in progress**; universe not frozen |
| AUD FX path for H13 | Not in the BTC 1-minute book; must be a separate series with its own leakage rules |
| Funding, OI, liquidations | Not in the new data section; live product needs them |
| Regenerable derived datasets from raw events | True **going forward** if 16P lands; **false** for the 1-minute research sample |

**Implication.** H7, H8, H9, FP-with-\(O_t\), and calibrated fill models are **not** part of the initial empirical programme. The spec says this in places and then keeps them in the “current architecture” diagram as if they were parallel workstreams rather than a **later sample**.

Using 1-minute **proxies** for execution cost (e.g. 1-bar range as “spread”) is a different hypothesis and must be labelled as such — otherwise H8 is fake.

---

## 8. Potential look-ahead and leakage risks

The spec lists leakage as a concern and does not pin the actual mechanisms this project will hit.

### 8.1 First-passage and bars

- Using **bar high/low** to label \(\tau_{\text{target}}<\tau_{\text{stop}}\) for a decision taken at the **same bar’s open or close**.
- Ambiguous **order of hits** when both target and stop lie in the same bar’s range (unidentifiable without ticks).
- Defining barriers from **future** swing points or a volume profile that includes the forecast interval.

### 8.2 HMM / PCA / factors

- **Smoothed** \(\gamma_t = \mathbb{P}(S_t\mid \mathcal{F}_T)\) in backtests; live is \(\mathbb{P}(S_t\mid\mathcal{F}_t)\). This alone can create a phantom H2.
- Full-sample PCA eigenvectors or \(\beta\) (H3).
- Standardising features with the **whole** train+test mean/variance.
- HMM trained on the evaluation window (Phase 7V pattern).

### 8.3 Wave features

- Current-bar VWAP, RSI, MACD using the bar that has not closed.
- Trend efficiency \(\eta\) including the decision bar’s \(\Delta P\).
- Cross-asset bars with **different close times** treated as simultaneous.
- Horizon labels in `wave_cli.py` (`look-ahead in bars`) — the tooling already thinks in leaked labels; the spec does not constrain it.

### 8.4 Ripple / L2

- Rebuilding books with **snapshot after updates** or ignoring `U`/`u` sequence (classic).
- CVD/OFI computed from public trades as if they were the strategy’s fills.
- Hold/break scores fitted on the **outcome** of the same approach.
- Microprice treated as a **tradable** entry price.

### 8.5 News / LLM

- Publication timestamp vs **exchange-available** timestamp.
- Model weights trained on text that **describes** the price move.
- “Surprise” defined using a forecast that itself used the outcome.
- Inference **duration**: Decision at \(t\) using an LLM result finished at \(t+\Delta\).

### 8.6 Research process leakage (the largest)

- “Formal proposal **after** initial experimentation reveals which hypotheses are promising” **is peeking**. The untouched test set is already contaminated unless a **pre-registered holdout** is locked *before* that exploration.
- NSGA-II / multi-objective Pareto on Wave/Tide (existing `/api/optimise`) is a **search over the same economic metrics** later used as “confirmation.”
- Reading this audit, prior reports, and smoke campaigns is part of the information set of the researchers. Pretending H1–H13 are pre-experimental is false.

---

## 9. Backtesting weaknesses

### 9.1 Spec-level

- No **bar-vs-event** backtest policy. Mixing 1-minute Wave research with event-time Ripple P&L in one “system Sharpe” is invalid (Open Q10 is asked, not solved).
- No fill model: next-bar open, conservative stop-first, or L2 replay.
- No **overlapping-position** accounting.
- No **funding** cash-flow in \(R_p\).
- Capacity/impact “sensitivity” mentioned; no model (square-root? participation rate?).
- Benchmarks 1–9 are **policy classes**, not implementations (lookbacks, costs, long-only vs long-short). “Buy-and-hold” of a fully collateralised perp is not buy-and-hold of spot BTC, and not an AUD investor’s hold.
- Random baseline vs buy-and-hold: different **exposure**. Comparing a 10% utilised ES strategy to 100% buy-and-hold without a **leverage/vol-matched** benchmark is how people invent skill.

### 9.2 Platform-level (the spec ignores a loaded gun)

- Wave/Tide backtests historically run with **zero slippage and zero liquidation floor** unless flags are set.
- Orderflow backtest and Wave backtest are **different simulators**. Incremental “Ripple adds value” can be a simulator artefact.
- Replay harness tests **determinism**, not **realism**.
- Paper broker \(\neq\) live queue position.
- Optimiser `num_trades` as a Pareto axis encourages **hyperactive** policies that look good in-sample.

### 9.3 First-passage backtests

Without a tick path, any reported \(\mathbb{P}(\tau_{\text{target}}<\tau_{\text{stop}})\) on 1-minute data is an **interval-censored** estimator. Treating it as the live object (event-time barriers on L2) is a **domain shift**, not a confirmation.

---

## 10. Statistical validation weaknesses

The experiment contract is a checklist, not a statistics section.

| Weakness | Detail |
|---|---|
| Composite null | “No incremental value” — value in which metric? Sharpe, Δ log-likelihood, ES violations, turnover-adjusted IR? |
| No primary metric | Academic vs practical will cherrypick |
| No power / MDE | H7 on a short prospective L2 sample will be inconclusive; the spec does not say what happens then |
| Dependent observations | Minute returns, overlapping \(h\)-horizons, clustered vol — i.i.d. LR and binomial VaR tests are invalid as stated |
| Calibration tests | PIT/reliability need a **probability integral transform under the forecast’s own filtration**; HMM and expanding windows break this |
| ES backtesting unnamed | Acerbi–Szekely, McNeil–Frey, etc. are not interchangeable with “ES backtesting” |
| VaR exceedances | Clustering in crypto tails; Kupiec unconditional coverage is a toy |
| Label noise | Same-bar barrier ambiguity attenuates Brier scores and can **favour** miscalibrated models |
| HMM | Label switching, short regimes, BIC on tiny \(n\) (already happened) |
| No scoring rule for decisions | Log-likelihood of \(R\) can improve while **economic** decisions worsen (spec says this in §10.6, then H1 is still a calibration hypothesis that can “pass” the programme without H4) |

H4 is the only hypothesis that approximates a **trading** claim. H1–H3 can consume the entire Master’s calendar and never touch H4.

---

## 11. Multiple-testing and overfitting risks

**The family is already huge.** H1–H13 × horizons \(h\) × barrier widths × \(\alpha\) × symbols × feature subsets × HMM \(K\) × PCA \(k\) × train/validate schemes. A single “multiple-testing rule” in a table does not control this unless **the family is frozen**.

Specific project amplifiers:

1. **Proposal-after-exploration** (explicit data-dependent hypothesis selection). Needs a two-stage design: exploration set vs confirmatory set, **dates written down now**.
2. **NSGA-II** over strategy parameters using the same P&L metrics (deflated Sharpe / PBO / CSCV not mentioned).
3. **Ablation path** in §10.5 is a **garden of forking paths** if any rung can be skipped “if not sacred.”
4. **HMM \(K\) via BIC** on the evaluation sample (already in Phase 7V).
5. **Feature laundry lists** (§6.2, §7.1) invite stepwise inclusion.
6. **“Nested complexity”** still allows **non-nested** ML (Benchmark 7) with unbounded capacity.
7. Researcher degrees of freedom in **cost assumptions**: any H4/H10 can be rescued by lowering `slippage_bps`.

Without a **pre-registered analysis plan** (primary horizon, primary symbol, primary economic metric, one-sided tests, correction method — Bonferroni is too harsh, uncorrected is a fiction, something like SPA/Reality Check or a small frozen family is required), the academic objective is not defensible.

---

## 12. Components that are unnecessary or premature

| Component | Why premature |
|---|---|
| Fokker–Planck / flux / Schrödinger | Not identifiable on OHLC; not needed for empirical hitting frequencies or discrete-time densities |
| Microstructure-conditioned diffusion as architecture | No L2 panel |
| Jump–Lévy “rung 6” in the canonical ladder | Residual diagnostics come after a working discrete-time model |
| Euler sleeve×asset×cell | Single-symbol live; no second strategy sleeve |
| Options / \(Q\)-measure language | Admitted future; still occupies canonical math |
| LLM event schema with 10 fields | Zero timestamp-safe archive; each field is another overfitting axis |
| RORAC as an architectural placement | Undefined; ratio objectives are a known trap |
| Full utility maximisation \(\max \mathbb{E}[U]\) | \(U\) explicitly unset; then the decision layer cannot exist except as a stub |
| Hierarchical “current design” as a **commitment** | Evidence has not shown two layers beat one |

A Master’s programme that starts with **one series, one horizon, two nested densities, a costed long/short-or-flat rule** is a strategy. Starting with Tide+Wave+Ripple+FP+HMM+PCA+ES+RORAC+LLM is a **catalogue**.

---

## 13. Components that should be deferred

Agree with much of §20. Tighten and add:

**Defer until 1-minute density/decision work exists**

- All of Ripple-as-alpha (H7–H9), FP with \(O_t\), wall taxonomies, liquidity map as a **forecasting** object.

**Defer until a costed Wave rule exists**

- Tide ES as a *binding optimiser* (H5). A vol target or notional cap is enough. Estimating ES well enough to bind (Open Q5) is a thesis by itself.

**Defer until live L2 has length and dual clocks**

- Fill probability, latency effects, queue, impact.

**Defer indefinitely for the degree (keep as ops, not science)**

- Schrödinger.
- LLM-as-feature until a **vendor timestamped** news dataset is paid for and frozen.
- FX overlay except a **simple AUD mark-to-market of USDT P&L** (accounting, not a hedge strategy). That is cheaper than H13 as written.
- Multi-venue arb, options book.

**Do not defer (the spec under-weights these)**

- Event-time replay contracts for *whatever* policy is live.
- Explicit next-bar/tick fill rules for 1-minute research.
- Cost defaults **on**, not off, in every reported backtest.
- Holdout calendar lock.

---

## 14. Research questions that are still poorly defined

Open questions in §21 are real but several are not yet questions (they are missing definitions).

| Topic | What is missing |
|---|---|
| H1 Gaussian vs \(t\) | Which \(h\)? Which \(\nu\) estimation? Conditional vs unconditional \(t\)? On **close-to-close** or **open-to-close**? |
| H2 \(\pi_t\) | Filtered vs smoothed? States named a priori or data-driven? Same states as Tide vol_regime? |
| H3 PCA | Universe? Rolling window? Factors as **contemporaneous** \(r_M\) (not tradable without lag) vs lagged? |
| First passage | Barriers in ticks, in \(\sigma\), in dollars? Time-stopped at \(h\) or open-ended? Intra-bar rule? |
| H4 “decision quality” | Utility unset → the hypothesis is not falsifiable |
| H5 ES constraint | Improves left tail “without fully destroying mean” — not a null; any inner bound works |
| H6 RORAC | Formula, horizon, denominator floor |
| H7 “\(O_t\) improves…” | Which functional? Which lag of book features? Conditioning set exactly? |
| H8 execution | Holding “parent decision fixed” — parent is unspecified |
| H9 archetypes | V1 bounce/breakout need **frozen** parameters or they are a new search |
| H10 full stack | Not testable until every layer has a frozen implementation |
| H11 structural break | No break definition (fee change, listing, 2020, 2022, ETF, etc.) |
| H12 news | No corpus |
| Non-stationarity vs regime | No test statistic that distinguishes “\(\pi_t\) moved” from “emissions broke” |
| Model uncertainty | Listed as a distinction; **no procedure** (Bayesian model average, sandwich, stress, cVaR over models) |
| Benchmark 7 “simple ML” | Unbounded |

**Poorly defined at the programme level:** What is the **minimum publishable unit** if H4 fails? The spec says rejection is allowed; it does not specify the fallback thesis (e.g. “calibration of 1-minute BTC tails under temporal leakage controls”). Without that, failed trading results will be dressed as architecture papers.

---

## 15. Over-engineered relative to available evidence

Evidence on hand is roughly: a V1 deterministic stack that can run live in OBSERVE; Wave/Tide **parameter searches** with often-zero costs; HMM A/B that is **not** a statistical validation; incomplete L2 history; 1-minute prices.

Relative to that:

- Three latent regimes (Tide vol, Wave HMM, Ripple liquidity HMM) is **unidentifiable** and un-ablatable.
- Continuous-time PDE story on minute bars is ornament.
- Portfolio Euler + utility + RORAC + ES elicitability issues **before** a single costed univariate rule.
- Dual objectives (Master’s + profitable bot) with a **shared** unfrozen test set will overfit both.
- “Current design” commits to the hierarchy **before** Benchmarks 3–6 are even specified.

Skeptical prior: **one** well-specified discrete-time model of \(R_{t:t+h}\) on BTC 1-minute, with a lagged information set, a next-bar execution assumption, non-zero fees, and a vol-targeted benchmark, would already be a heavy empirical paper. Tide/Wave/Ripple is not justified by that prior.

---

## 16. Under-specified areas

Where the spec is too thin to constrain work (the opposite of §15):

| Area | What must be specified later — but *should have been sketched now* |
|---|---|
| \(P_t\) | Close, mark, last, mid, microprice |
| Clock | Decision at bar close \(t\), trade at \(t+1\) open, or same-bar fantasy |
| \(\mathcal{A}\) and \(U\) or a **provisional** decision rule (e.g. long iff \(\mathbb{P}(\tau_+<\tau_-)>p^*\) and ES ok) | Otherwise Decision is a blank |
| Units of \(B_t\) and ES | Return vs dollar vs margin |
| Primary symbol, venue, contract | BTCUSDT perp assumed in code, not in spec |
| Train/validate/test **dates** | “Untouched” is theatre until dates exist |
| Cost schedule | Maker, taker, funding, min slippage — numeric **research defaults**, not production magic |
| HMM filtration | Filter only |
| Intra-bar barrier rule | Conservative (assume stop first) as default |
| Layer interfaces | Replacements for Tide/Wave/Ripple snapshots |
| Mapping to live postures | OBSERVE/PAPER/LIVE |
| Model-break policy | Numeric |
| Multi-testing | Frozen family + method |
| Numeraire | USDT vs AUD |
| Position/lifecycle | Even a simplified: flat / desired / flattening |
| Stale data | Thresholds exist in stream health; not in the spec |

Under-specification plus over-architecture is how V1 grew 2,400 lines of **defaults that became the strategy**.

---

## 17. What could prevent eventual autonomous execution

1. **No executable policy.** Densities do not place orders. Without a frozen mapping \(p(\cdot)\mapsto\) intents, the bot cannot be armed except by keeping V1 Ripple — which the spec says not to treat as the product.
2. **No lifecycle / exit / flatten semantics.** Autonomy dies on partial fills, disconnects, and “what if ES says reduce but Wave still likes the trade.” V1 had this; the new spec deleted it.
3. **Source-of-truth split.** Agents will keep implementing `strategy.md`.
4. **Decision layer absent in code** and unspecified, while live risk already mutates orders — undefined authority.
5. **Uncalibrated ES as a hard live limit.** Estimation noise → flicker flatten/unflatten, or limits so wide they never bind. False precision §5.5 warns about this and offers no controller (hysteresis, smoothed ES, notional caps).
6. **L2 and 1-minute research never join.** Live Ripple needs books; the thesis may never produce a Ripple that is *allowed* to trade (H7 untested). Result: live stays on unvalidated bounce/breakout.
7. **Latency and dual clocks** not in the 1-minute model; live decisions on a **different information set** than the backtest.
8. **Default-zero costs** in the research stack → policies that cannot pay take fees.
9. **Safety list without contracts.** Stale book, sequence gap, NaN features, HMM degeneracy, broker reject, position mismatch: V1/live have fragments; the new spec lists slogans. Autonomy requires **priority**: e.g. any quality fault → cancel working orders → flatten or hold.
10. **Perp specifics.** Liquidation, mark-price stops, funding at 00:00/08:00/16:00 UTC, reduce-only, price bands — absent. A “correct” Wave signal can still blow up on mark-last divergence.
11. **Capacity to fail closed vs research models that always emit \(\pi_t\).** If the model cannot output “I don’t know,” the bot will trade noise after breaks (H11).
12. **Operational cadence vs PDE fantasy.** A 100 µs Ripple path cannot wait for a Fokker–Planck solve; if research couples them, live will approximate with something untested.
13. **Audit of prior wiring failure not encoded.** Promotion after §19 without a **wired-live** test repeats 2026-05.
14. **Academic peeking.** If the confirmatory window is spent in exploration, there is no honest go-live metric left — only more in-sample architecture.

---

## Topic-focused notes (requested)

### Gaussian vs Student-\(t\) risk

Viable **discrete-time** hypothesis (H1) if nested, horizon-fixed, and ES/VaR computed in **consistent units**. Invalid as a continuation of the Brownian SDE. Must restrict \(\nu\) and compare to **skew** and **GARCH-Gaussian**. Coherence is not a reason to skip elicitation / backtestability.

### HMM regime probabilities

Three possible HMMs (Tide, Wave, Ripple) are conflated. Phase 7V is not evidence. **Smoothed posteriors are look-ahead.** H2 must specify filtered \(\pi_{t\mid t}\), lagged one decision clock, and a non-HMM nested alternative (`vol_regime` bins).

### PCA / factor modelling

H3 is empty without universe and **lag**. Contemporaneous \(\beta r_M\) is not an implementable forecast. Single-name BTC research does not need PCA; a BTC-only programme should say so. Absorption ratio / dispersion need a panel that 16Q has not finished.

### Fokker–Planck

Ornamental on the initial dataset. At best a **computational method** for first-passage *after* a diffusion is justified. Not a modelling primitive. Flux is unused in any decision rule.

### First-passage probabilities

The right *economic* object for target/stop trading — and currently **unidentified** on OHLC. Must specify barriers, time-stop \(h\), intra-bar convention (default: **stop first**), and that 1-minute estimates are **not** the live L2 object. Connect to the old hold/break scores or drop them.

### Order-flow conditioning

Cannot be an initial architecture commitment. H7 requires a prospective sample, conditioning on Wave **filtered** state, and a clear split between predictive increment vs execution increment (H8).

### Probability calibration

Right requirement; incomplete. Needs scoring rules by object (log-score for densities, Brier/log for barrier events), **dependence-robust** tests, and a rule that **miscalibration disables sizing** (link to autonomy). Sharpness without economic value must not promote layers.

### ES / RORAC

ES constraint is underspecified in units and statistically awkward (non-elicitability, mixture non-linearity). RORAC is undefined and premature. V1 parametric ES throttle is the actual nested benchmark and is not listed as such.

### Position sizing

Correctly called a decision problem; then left blank. Without a provisional rule, every experiment will invent a sizer and overfit. Suggest freezing a **dumb** sizer (vol target or fixed notional) for H1–H4.

### Execution costs

Equation is not a model. Platform defaults undermine the spec. Funding omitted from data. H8 needs **own-fill** data or a conservative structural cost, not public CVD.

### FX hedging

Correctly deferred; formula is wrong. Immediate need is **reporting numeraire**, not a hedge book.

### LLM / news

Correct interface (no direct orders). Still premature and leakage-dominant. Do not let event fields become Wave features until a frozen, timestamped corpus exists.

### Non-stationarity

Distinction regime vs structural change is good; **no test**. Crypto has obvious candidates (halvings, ETF, fee changes). Walk-forward is mentioned; embargo, purge, and **model retirement** are not.

### Model uncertainty

Named, not operationalised. Live ES will be a point number. Need at least: parameter bootstrap bands, a stress model, and “don’t know” → \(a=0\).

### L2 data collection

Right direction (raw events, regenerate derived, quality counts). Dual-clock and sequence-gap research **does not apply retroactively** to 1-minute. Do not blend samples in one Sharpe.

### Event-time replay

Demoted to one sentence. For autonomy it is a **hard invariant** of the V1 engine. The new spec should state: no wall-clock in simulated decisions; live and replay share the same intent function. Research bar backtests are a **different**, leakier simulator and must be labelled.

### Benchmark construction

Ladder is conceptually right and **implementation-empty**. Missing vol-matched and cost-matched variants. Missing V1 permission-matrix policy as an explicit rung. Random and buy-and-hold are not comparable exposures.

### Ablation testing

Path is a menu, not a design. Collinear layers will produce spurious “HMM adds 2 bp.” Ablate **orthogonalised residuals** or nested models, not stacked feature bundles.

---

## Recommended freeze (audit only — not a rewrite)

If the specification is revised later, the highest-leverage fixes are: (i) lock a confirmatory calendar before more exploration; (ii) put **units, clocks, \(P_t\), intra-bar rules, and a dumb sizer** in the spec; (iii) demote FP/Ripple/LLM/FX from “current architecture” to “sample-dependent later”; (iv) restore lifecycle/intent/cost/safety as **execution contracts** without restoring bounce/breakout as truth; (v) reconcile `strategy.md` vs `strategy.new.md` vs the live engine so autonomy has one policy.

Until then, treat `strategy.new.md` as a **direction document with unresolved identification**, not as a canonical contract that code must obey.
