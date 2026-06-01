"""Wave-layer factor research helpers.

All heavy computation for notebook 10 (Wave PCA / K-means market-beta
research) lives here so the notebook stays readable.  Nothing in this
module touches any production trading path; it is pure research code.

Functions
---------
score_asset           – per-asset corr / lead-lag / stability scores
score_universe        – vectorised scoring over every candidate asset
select_universe       – final ~30-asset universe selection logic
rolling_pca           – sliding-window PCA on a returns panel
pca_loadings_table    – tidy DataFrame of latest PC loadings
kmeans_sweep          – K-means over k range, returns inertia + silhouette
cluster_analytics     – per-cluster stats relative to BTC/ETH
compute_residuals     – OLS factor model → btc/eth residual z-scores
forward_predictive_test – correlation / hit-rate / quantile tests
wave_feature_table    – candidate Wave feature catalogue as a DataFrame
"""

from __future__ import annotations

from typing import Dict, List, Optional, Sequence, Tuple

import numpy as np
import pandas as pd
from sklearn.decomposition import PCA
from sklearn.cluster import KMeans
from sklearn.metrics import silhouette_score
from sklearn.preprocessing import StandardScaler
from scipy import stats


# ──────────────────────────────────────────────────────────────────────────────
# 1.  Universe scoring
# ──────────────────────────────────────────────────────────────────────────────

def _min_max_norm(s: pd.Series) -> pd.Series:
    """Min-max normalise a Series to [0, 1], handling zero-range gracefully."""
    lo, hi = s.min(), s.max()
    if hi == lo:
        return pd.Series(np.zeros(len(s)), index=s.index)
    return (s - lo) / (hi - lo)


def score_asset(
    asset_rets: pd.Series,
    btc_rets: pd.Series,
    eth_rets: pd.Series,
    cfg: dict,
) -> dict:
    """Compute a full scoring dict for one candidate asset.

    Parameters
    ----------
    asset_rets : aligned 1-D return Series (NaN already handled upstream).
    btc_rets, eth_rets : same index as asset_rets.
    cfg : notebook CFG dict; uses ``rolling_corr_win`` and ``lag_horizons``.

    Returns
    -------
    dict with keys:
        corr_btc, corr_eth,
        abs_corr_btc, abs_corr_eth,
        roll_mean_corr, roll_std_corr, stable_corr_score,
        lag_corr_{h}m_btc, lag_corr_{h}m_eth  (one per horizon),
        predictive_corr_score, data_quality_score
    """
    win = cfg.get("rolling_corr_win", 60)
    horizons = cfg.get("lag_horizons", [1, 5, 15, 30])

    # Align to shared valid index (drop any row where any of the three is NaN)
    df = pd.concat(
        [asset_rets.rename("a"), btc_rets.rename("btc"), eth_rets.rename("eth")],
        axis=1,
    ).dropna()

    n_valid = len(df)
    n_total = max(len(asset_rets), 1)
    data_quality_score = n_valid / n_total

    if n_valid < max(win, 30):
        # Not enough data to compute meaningful correlations.
        return {
            "corr_btc": np.nan,
            "corr_eth": np.nan,
            "abs_corr_btc": np.nan,
            "abs_corr_eth": np.nan,
            "roll_mean_corr": np.nan,
            "roll_std_corr": np.nan,
            "stable_corr_score": np.nan,
            "predictive_corr_score": np.nan,
            "data_quality_score": data_quality_score,
            **{f"lag_corr_{h}m_btc": np.nan for h in horizons},
            **{f"lag_corr_{h}m_eth": np.nan for h in horizons},
        }

    a = df["a"]
    btc = df["btc"]
    eth = df["eth"]

    # Contemporaneous
    corr_btc = float(a.corr(btc))
    corr_eth = float(a.corr(eth))

    # Rolling correlation to BTC (used for stability)
    roll_corr = a.rolling(win).corr(btc).dropna()
    roll_mean_corr = float(roll_corr.mean())
    roll_std_corr = float(roll_corr.std()) if len(roll_corr) > 1 else np.nan

    # Stability score: mean(|rolling_corr|) / std(rolling_corr)
    # Higher = more consistently correlated (stable direction + magnitude)
    if roll_std_corr and roll_std_corr > 0:
        stable_corr_score = float(roll_corr.abs().mean() / roll_std_corr)
    else:
        stable_corr_score = float(roll_corr.abs().mean()) * 10.0  # near-zero std → very stable

    # Lead/lag: corr(asset[t], BTC[t+h]) — does asset LEAD BTC by h bars?
    lag_scores: dict = {}
    for h in horizons:
        # Forward-shift target: asset at t vs BTC at t+h
        btc_fwd = btc.shift(-h)
        eth_fwd = eth.shift(-h)
        valid = df.assign(btc_fwd=btc_fwd, eth_fwd=eth_fwd).dropna()
        if len(valid) > 30:
            lag_scores[f"lag_corr_{h}m_btc"] = float(valid["a"].corr(valid["btc_fwd"]))
            lag_scores[f"lag_corr_{h}m_eth"] = float(valid["a"].corr(valid["eth_fwd"]))
        else:
            lag_scores[f"lag_corr_{h}m_btc"] = np.nan
            lag_scores[f"lag_corr_{h}m_eth"] = np.nan

    # Predictive score: max |lead/lag corr| across all BTC/ETH/horizon combos
    lag_values = [v for v in lag_scores.values() if not np.isnan(v)]
    predictive_corr_score = float(np.max(np.abs(lag_values))) if lag_values else np.nan

    return {
        "corr_btc": corr_btc,
        "corr_eth": corr_eth,
        "abs_corr_btc": abs(corr_btc),
        "abs_corr_eth": abs(corr_eth),
        "roll_mean_corr": roll_mean_corr,
        "roll_std_corr": roll_std_corr,
        "stable_corr_score": stable_corr_score,
        "predictive_corr_score": predictive_corr_score,
        "data_quality_score": data_quality_score,
        **lag_scores,
    }


def score_universe(
    returns: pd.DataFrame,
    btc_col: str,
    eth_col: str,
    cfg: dict,
) -> pd.DataFrame:
    """Score every non-target column in *returns* against BTC and ETH.

    Parameters
    ----------
    returns   : aligned log-return panel (all assets).
    btc_col   : column name for BTC.
    eth_col   : column name for ETH.
    cfg       : notebook CFG dict.

    Returns
    -------
    pd.DataFrame indexed by asset symbol with all score columns plus
    ``combined_score``.
    """
    weights = cfg.get("score_weights", {
        "max_abs_corr":    0.30,
        "stable_corr":     0.30,
        "predictive_corr": 0.30,
        "data_quality":    0.10,
    })

    btc_rets = returns[btc_col]
    eth_rets = returns[eth_col]
    candidates = [c for c in returns.columns if c not in (btc_col, eth_col)]

    rows = []
    for sym in candidates:
        row = score_asset(returns[sym], btc_rets, eth_rets, cfg)
        row["symbol"] = sym
        rows.append(row)

    if not rows:
        return pd.DataFrame()

    df = pd.DataFrame(rows).set_index("symbol")

    # max_abs_corr: best of abs BTC / abs ETH correlation
    df["max_abs_corr_btc_eth"] = df[["abs_corr_btc", "abs_corr_eth"]].max(axis=1)

    # Normalise component scores before combining
    norm_max_abs  = _min_max_norm(df["max_abs_corr_btc_eth"].fillna(0))
    norm_stable   = _min_max_norm(df["stable_corr_score"].fillna(0))
    norm_pred     = _min_max_norm(df["predictive_corr_score"].fillna(0))
    norm_quality  = _min_max_norm(df["data_quality_score"].fillna(0))

    df["combined_score"] = (
        weights["max_abs_corr"]    * norm_max_abs
        + weights["stable_corr"]   * norm_stable
        + weights["predictive_corr"] * norm_pred
        + weights["data_quality"]  * norm_quality
    )

    return df.sort_values("combined_score", ascending=False)


def select_universe(
    scores_df: pd.DataFrame,
    all_available: Sequence[str],
    cfg: dict,
    btc_col: str = "BTCUSDT",
    eth_col: str = "ETHUSDT",
) -> pd.DataFrame:
    """Build the final research universe.

    Selection order
    ---------------
    1. Always include BTC and ETH.
    2. Core crypto bucket (``cfg["core_crypto"]``) — those present in data.
    3. Core macro/Oanda bucket (``cfg["core_macro"]``) — nearest match.
    4. Fill remaining slots with highest ``combined_score`` assets,
       excluding already-selected ones.

    Parameters
    ----------
    scores_df     : output of :func:`score_universe`.
    all_available : full list of loaded asset symbols (incl. BTC/ETH).
    cfg           : notebook CFG dict.
    btc_col, eth_col : symbol names used for BTC and ETH.

    Returns
    -------
    pd.DataFrame with columns [symbol, selection_reason, combined_score].
    """
    target_n = cfg.get("target_n_assets", 30)
    core_crypto = cfg.get("core_crypto", [])
    core_macro  = cfg.get("core_macro", [])

    available_set = set(all_available)
    selected: List[dict] = []
    seen: set = set()

    def _add(sym: str, reason: str) -> None:
        if sym in seen or sym not in available_set:
            return
        score = (
            float(scores_df.loc[sym, "combined_score"])
            if sym in scores_df.index
            else np.nan
        )
        selected.append({"symbol": sym, "selection_reason": reason, "combined_score": score})
        seen.add(sym)

    # 1. Anchors
    _add(btc_col, "anchor: BTC target")
    _add(eth_col, "anchor: ETH target")

    # 2. Core crypto
    for sym in core_crypto:
        if sym not in (btc_col, eth_col):
            _add(sym, "core_crypto_bucket")

    # 3. Core macro / Oanda — try exact match then common name variants
    _oanda_variants: Dict[str, List[str]] = {
        "NAS100_USD":  ["NAS100_USD", "NAS100USD", "NASDAQ100"],
        "SPX500_USD":  ["SPX500_USD", "SPX500USD", "SP500"],
        "XAU_USD":     ["XAU_USD", "XAUUSD", "GOLD"],
        "EUR_USD":     ["EUR_USD", "EURUSD"],
        "USD_JPY":     ["USD_JPY", "USDJPY"],
        "GBP_USD":     ["GBP_USD", "GBPUSD"],
        "AUD_USD":     ["AUD_USD", "AUDUSD"],
        "BCO_USD":     ["BCO_USD", "BCOUSD", "WTICOUSD", "USOIL"],
    }
    for macro_sym in core_macro:
        variants = _oanda_variants.get(macro_sym, [macro_sym])
        for v in variants:
            if v in available_set:
                _add(v, "core_macro_bucket")
                break

    # 4. Fill remaining slots from highest combined_score
    remaining = [
        s for s in scores_df.index
        if s not in seen and s in available_set
    ]
    for sym in remaining:
        if len(selected) >= target_n:
            break
        _add(sym, "combined_score_fill")

    out = pd.DataFrame(selected)
    if out.empty:
        return out
    return out.reset_index(drop=True)


# ──────────────────────────────────────────────────────────────────────────────
# 2.  PCA helpers
# ──────────────────────────────────────────────────────────────────────────────

def rolling_pca(
    returns_df: pd.DataFrame,
    window: int,
    n_components: int = 5,
    *,
    standardize: bool = True,
) -> dict:
    """Compute a rolling PCA on *returns_df*.

    For each timestep t >= window, fit PCA on returns_df[t-window:t].
    Returns a dict of time-indexed DataFrames/Series.

    Parameters
    ----------
    returns_df   : (T, N) log-return panel, no NaN (drop/fill upstream).
    window       : look-back window in bars.
    n_components : number of PCs to retain.
    standardize  : if True, standardise within each window before fitting.

    Returns
    -------
    dict with:
        ``explained_variance``  – (T, n_components) DataFrame of per-PC EV ratios
        ``cumulative_variance`` – (T,) Series for cum-EV of first n_components PCs
        ``pc1_variance``        – (T,) Series, EV ratio of PC1
        ``loadings``            – (T, N) DataFrame of PC1 loadings over time
        ``loadings_all``        – dict[int, (T, N) DataFrame], one per PC index
        ``pc_scores``           – (T, n_components) DataFrame of PC scores at t
        ``btc_loading``         – (T,) PC1 loading for BTC column
        ``eth_loading``         – (T,) PC1 loading for ETH column
    """
    T, N = returns_df.shape
    cols = returns_df.values  # (T, N) numpy array
    index = returns_df.index
    assets = list(returns_df.columns)
    n_comp = min(n_components, N)

    ev_rows = []
    pc1_load_rows = []
    all_load_rows: Dict[int, list] = {i: [] for i in range(n_comp)}
    pc_score_rows = []
    valid_idx = []

    for t in range(window, T + 1):
        window_data = cols[t - window: t]
        # Drop columns that are all-NaN in this window
        col_mask = ~np.all(np.isnan(window_data), axis=0)
        wd = window_data[:, col_mask]
        # Row-wise drop NaN
        row_mask = ~np.any(np.isnan(wd), axis=1)
        wd = wd[row_mask]

        if wd.shape[0] < n_comp + 5:
            continue

        if standardize:
            scaler = StandardScaler()
            wd = scaler.fit_transform(wd)

        n_c = min(n_comp, wd.shape[1])
        pca = PCA(n_components=n_c)
        pca.fit(wd)

        # Explained variance ratios
        ev = np.zeros(n_comp)
        ev[:n_c] = pca.explained_variance_ratio_[:n_c]
        ev_rows.append(ev)

        # PC1 loadings mapped back to original columns
        components = pca.components_  # (n_c, n_active_cols)
        active_assets = [a for a, m in zip(assets, col_mask) if m]

        load1 = np.zeros(len(assets))
        for j, a in enumerate(active_assets):
            load1[assets.index(a)] = components[0, j]
        pc1_load_rows.append(load1)

        for ci in range(n_comp):
            load_i = np.zeros(len(assets))
            if ci < n_c:
                for j, a in enumerate(active_assets):
                    load_i[assets.index(a)] = components[ci, j]
            all_load_rows[ci].append(load_i)

        # PC score at time t-1 (last row of window)
        last_row = cols[t - 1: t, :]
        last_row_active = last_row[:, col_mask]
        if not np.any(np.isnan(last_row_active)):
            if standardize:
                last_std = scaler.transform(last_row_active)
            else:
                last_std = last_row_active
            score = pca.transform(last_std)[0]
            padded = np.zeros(n_comp)
            padded[:len(score)] = score
        else:
            padded = np.zeros(n_comp)
        pc_score_rows.append(padded)

        valid_idx.append(index[t - 1])

    if not valid_idx:
        return {
            "explained_variance": pd.DataFrame(),
            "cumulative_variance": pd.Series(dtype=float),
            "pc1_variance": pd.Series(dtype=float),
            "loadings": pd.DataFrame(),
            "loadings_all": {},
            "pc_scores": pd.DataFrame(),
            "btc_loading": pd.Series(dtype=float),
            "eth_loading": pd.Series(dtype=float),
        }

    comp_names = [f"PC{i+1}" for i in range(n_comp)]
    ev_df = pd.DataFrame(np.array(ev_rows), index=valid_idx, columns=comp_names)
    cum_ev = ev_df.cumsum(axis=1).iloc[:, -1].rename("cumulative_ev")
    pc1_ev = ev_df["PC1"].rename("pc1_variance")

    load_df = pd.DataFrame(np.array(pc1_load_rows), index=valid_idx, columns=assets)
    load_all = {
        ci: pd.DataFrame(np.array(all_load_rows[ci]), index=valid_idx, columns=assets)
        for ci in range(n_comp)
    }
    pc_scores_df = pd.DataFrame(np.array(pc_score_rows), index=valid_idx, columns=comp_names)

    btc_col_name = next((a for a in assets if "BTC" in a.upper()), None)
    eth_col_name = next((a for a in assets if "ETH" in a.upper()), None)

    return {
        "explained_variance":  ev_df,
        "cumulative_variance": cum_ev,
        "pc1_variance":        pc1_ev,
        "loadings":            load_df,
        "loadings_all":        load_all,
        "pc_scores":           pc_scores_df,
        "btc_loading":         load_df[btc_col_name] if btc_col_name else pd.Series(dtype=float),
        "eth_loading":         load_df[eth_col_name] if eth_col_name else pd.Series(dtype=float),
    }


def pca_loadings_table(
    pca_result: dict,
    n_pcs: int = 3,
    top_n: int = 10,
) -> pd.DataFrame:
    """Return the latest snapshot of PC loadings as a tidy DataFrame.

    Parameters
    ----------
    pca_result : output of :func:`rolling_pca`.
    n_pcs      : how many PCs to include.
    top_n      : show top N assets by abs(loading) per PC.

    Returns
    -------
    DataFrame indexed by asset with one column per PC.
    """
    rows = {}
    for ci in range(n_pcs):
        ldf = pca_result["loadings_all"].get(ci)
        if ldf is None or ldf.empty:
            continue
        latest = ldf.iloc[-1]
        rows[f"PC{ci+1}"] = latest

    if not rows:
        return pd.DataFrame()

    tbl = pd.DataFrame(rows)
    # Sort by abs(PC1) descending and take top_n
    tbl = tbl.reindex(tbl["PC1"].abs().sort_values(ascending=False).index)
    return tbl.head(top_n)


# ──────────────────────────────────────────────────────────────────────────────
# 3.  K-means helpers
# ──────────────────────────────────────────────────────────────────────────────

def kmeans_sweep(
    features: np.ndarray,
    k_range: Sequence[int],
    random_seed: int = 42,
    n_init: int = 20,
) -> pd.DataFrame:
    """Fit K-means for each k in *k_range* and return inertia + silhouette.

    Parameters
    ----------
    features    : (N_assets, M_features) array — e.g. latest PCA loadings.
    k_range     : iterable of k values to try (skip k >= N_assets).
    random_seed : random state for reproducibility.
    n_init      : number of K-means initialisations per k.

    Returns
    -------
    DataFrame with columns [k, inertia, silhouette].
    """
    rows = []
    n_samples = len(features)
    for k in k_range:
        if k >= n_samples:
            continue
        km = KMeans(n_clusters=k, random_state=random_seed, n_init=n_init)
        labels = km.fit_predict(features)
        sil = (
            float(silhouette_score(features, labels))
            if len(set(labels)) > 1
            else np.nan
        )
        rows.append({"k": k, "inertia": km.inertia_, "silhouette": sil})
    return pd.DataFrame(rows)


def cluster_analytics(
    returns: pd.DataFrame,
    labels: np.ndarray,
    btc_col: str,
    eth_col: str,
    lag_horizons: Sequence[int] = (1, 5, 15, 30),
) -> pd.DataFrame:
    """Compute per-cluster statistics relative to BTC and ETH.

    Parameters
    ----------
    returns     : aligned log-return panel (all universe assets).
    labels      : cluster assignment array aligned to returns.columns.
    btc_col, eth_col : symbol names for BTC/ETH.
    lag_horizons : lead/lag horizons in bars.

    Returns
    -------
    DataFrame indexed by cluster_id with stats columns.
    """
    assets = list(returns.columns)
    btc_rets = returns[btc_col]
    eth_rets = returns[eth_col]

    cluster_ids = sorted(set(labels))
    rows = []
    for cid in cluster_ids:
        members = [a for a, lbl in zip(assets, labels) if lbl == cid]
        if not members:
            continue

        # Mean cluster return series (equal-weight)
        cluster_ret = returns[members].mean(axis=1)

        # Intra-cluster stats
        mean_ret  = float(cluster_ret.mean())
        vol       = float(cluster_ret.std())
        sharpe    = mean_ret / vol if vol > 0 else np.nan

        # Contemporaneous correlation to BTC/ETH
        corr_btc = float(cluster_ret.corr(btc_rets))
        corr_eth = float(cluster_ret.corr(eth_rets))

        # Lead/lag: cluster leads BTC by h bars?
        lead_lag_btc = {}
        lead_lag_eth = {}
        for h in lag_horizons:
            btc_fwd = btc_rets.shift(-h)
            eth_fwd = eth_rets.shift(-h)
            df_h = pd.concat(
                [cluster_ret.rename("c"), btc_fwd.rename("btc"), eth_fwd.rename("eth")],
                axis=1,
            ).dropna()
            lead_lag_btc[h] = float(df_h["c"].corr(df_h["btc"])) if len(df_h) > 10 else np.nan
            lead_lag_eth[h] = float(df_h["c"].corr(df_h["eth"])) if len(df_h) > 10 else np.nan

        # Cluster leader: asset with highest abs PC1-loading contribution
        # (fall back to highest mean return if PCA info not available)
        leader = max(
            members,
            key=lambda m: abs(float(returns[m].corr(cluster_ret))),
        )

        row: dict = {
            "cluster_id": cid,
            "n_members": len(members),
            "members": ", ".join(members),
            "leader": leader,
            "mean_return": mean_ret,
            "volatility": vol,
            "sharpe": sharpe,
            "corr_btc": corr_btc,
            "corr_eth": corr_eth,
        }
        for h in lag_horizons:
            row[f"lead_lag_btc_h{h}"] = lead_lag_btc[h]
            row[f"lead_lag_eth_h{h}"] = lead_lag_eth[h]

        rows.append(row)

    return pd.DataFrame(rows).set_index("cluster_id")


# ──────────────────────────────────────────────────────────────────────────────
# 4.  BTC/ETH residual model
# ──────────────────────────────────────────────────────────────────────────────

def compute_residuals(
    btc_rets: pd.Series,
    eth_rets: pd.Series,
    factor_returns: pd.DataFrame,
    rolling_window: int = 120,
) -> pd.DataFrame:
    """OLS factor model → rolling BTC/ETH residual z-scores.

    For each point t, fits OLS: target = β₀ + Σ βᵢ factorᵢ using a
    rolling window of *rolling_window* bars (expanding until that size is
    reached).  Residual = actual - predicted; z-score uses a rolling
    standard deviation of the same length.

    Additionally computes:
    - ``residual_breadth``: fraction of universe assets returning positively
      relative to the BTC/ETH contemporaneous return.
    - ``market_beta_pressure``: normalised score [-1, +1] combining BTC and
      ETH z-scores and residual breadth.

    Parameters
    ----------
    btc_rets, eth_rets : aligned 1-D return Series.
    factor_returns     : (T, K) DataFrame of factor (PC or cluster) returns,
                         same index as btc_rets.
    rolling_window     : look-back for OLS fit and z-score window.

    Returns
    -------
    DataFrame with columns:
        btc_actual, eth_actual,
        btc_expected, eth_expected,
        btc_residual, eth_residual,
        btc_residual_z, eth_residual_z,
        market_beta_pressure
    """
    # Align everything
    combined = pd.concat(
        [btc_rets.rename("btc"), eth_rets.rename("eth"), factor_returns],
        axis=1,
    ).dropna()

    if len(combined) < rolling_window + 5:
        return pd.DataFrame()

    factor_cols = list(factor_returns.columns)
    T = len(combined)

    btc_exp  = np.full(T, np.nan)
    eth_exp  = np.full(T, np.nan)

    for t in range(rolling_window, T):
        win_data = combined.iloc[t - rolling_window: t]
        X = win_data[factor_cols].values
        # Add intercept
        X_c = np.column_stack([np.ones(len(X)), X])
        y_btc = win_data["btc"].values
        y_eth = win_data["eth"].values

        try:
            coef_btc, *_ = np.linalg.lstsq(X_c, y_btc, rcond=None)
            coef_eth, *_ = np.linalg.lstsq(X_c, y_eth, rcond=None)
        except np.linalg.LinAlgError:
            continue

        x_now = np.array([1.0] + list(combined[factor_cols].iloc[t].values))
        btc_exp[t] = float(x_now @ coef_btc)
        eth_exp[t] = float(x_now @ coef_eth)

    out = pd.DataFrame(
        {
            "btc_actual":   combined["btc"].values,
            "eth_actual":   combined["eth"].values,
            "btc_expected": btc_exp,
            "eth_expected": eth_exp,
        },
        index=combined.index,
    )
    out["btc_residual"] = out["btc_actual"] - out["btc_expected"]
    out["eth_residual"] = out["eth_actual"] - out["eth_expected"]

    # Rolling z-score
    def _rolling_z(s: pd.Series, w: int) -> pd.Series:
        mu  = s.rolling(w, min_periods=w // 2).mean()
        sig = s.rolling(w, min_periods=w // 2).std()
        return (s - mu) / sig.replace(0, np.nan)

    out["btc_residual_z"] = _rolling_z(out["btc_residual"], rolling_window)
    out["eth_residual_z"] = _rolling_z(out["eth_residual"], rolling_window)

    # market_beta_pressure: tanh-normalised average of the two z-scores
    avg_z = (out["btc_residual_z"].fillna(0) + out["eth_residual_z"].fillna(0)) / 2.0
    out["market_beta_pressure"] = np.tanh(avg_z / 2.0)  # maps to (-1, +1)

    return out


# ──────────────────────────────────────────────────────────────────────────────
# 5.  Forward predictive tests
# ──────────────────────────────────────────────────────────────────────────────

def forward_predictive_test(
    signal: pd.Series,
    target_rets: pd.Series,
    horizons: Sequence[int] = (1, 5, 15, 30),
    n_quantiles: int = 5,
    bootstrap_n: int = 1000,
    random_seed: int = 42,
) -> dict:
    """Test whether *signal[t]* predicts *target_rets[t+h]*.

    Parameters
    ----------
    signal      : predictor (e.g. btc_residual_z or PC1 score).
    target_rets : BTC or ETH log returns.
    horizons    : forward horizons in bars.
    n_quantiles : number of signal quantile buckets.
    bootstrap_n : number of bootstrap samples for CI on directional hit rate.
    random_seed : for reproducibility.

    Returns
    -------
    dict keyed by horizon h, each a dict with:
        ``pearson_r``, ``pearson_p``,
        ``hit_rate``,  ``hit_rate_ci`` (95% CI tuple),
        ``quantile_mean_returns`` (Series indexed by quantile),
        ``t_stat``
    """
    rng = np.random.default_rng(random_seed)
    results = {}

    for h in horizons:
        fwd = target_rets.shift(-h)
        df  = pd.concat(
            [signal.rename("sig"), fwd.rename("fwd")], axis=1
        ).dropna()

        if len(df) < 50:
            results[h] = None
            continue

        sig_arr = df["sig"].values
        fwd_arr = df["fwd"].values

        # Pearson correlation
        r, p = stats.pearsonr(sig_arr, fwd_arr)

        # Directional hit rate: sign(signal) == sign(future return)
        hit = (np.sign(sig_arr) == np.sign(fwd_arr)).mean()

        # Bootstrap 95% CI on hit rate
        boot_hits = np.array([
            (np.sign(rng.choice(sig_arr, size=len(sig_arr), replace=True))
             == np.sign(rng.choice(fwd_arr, size=len(fwd_arr), replace=True))
             ).mean()
            for _ in range(bootstrap_n)
        ])
        ci = (float(np.percentile(boot_hits, 2.5)), float(np.percentile(boot_hits, 97.5)))

        # t-stat: testing mean future return when signal is positive vs negative
        pos_mask = sig_arr > 0
        neg_mask = sig_arr <= 0
        if pos_mask.sum() > 5 and neg_mask.sum() > 5:
            t_stat, _ = stats.ttest_ind(fwd_arr[pos_mask], fwd_arr[neg_mask])
        else:
            t_stat = np.nan

        # Quantile mean returns
        try:
            q_labels = pd.qcut(df["sig"], n_quantiles, labels=False, duplicates="drop")
            qmr = df.groupby(q_labels)["fwd"].mean()
            qmr.index = [f"Q{i+1}" for i in qmr.index]
        except Exception:
            qmr = pd.Series(dtype=float)

        results[h] = {
            "pearson_r":             float(r),
            "pearson_p":             float(p),
            "hit_rate":              float(hit),
            "hit_rate_ci":           ci,
            "quantile_mean_returns": qmr,
            "t_stat":                float(t_stat) if not np.isnan(t_stat) else np.nan,
            "n_obs":                 len(df),
        }

    return results


def summarise_predictive_tests(test_results: dict, label: str = "") -> pd.DataFrame:
    """Format :func:`forward_predictive_test` output as a summary DataFrame.

    Parameters
    ----------
    test_results : output of :func:`forward_predictive_test`.
    label        : optional prefix for the index (e.g. "btc_residual_z").

    Returns
    -------
    DataFrame with one row per horizon.
    """
    rows = []
    for h, res in test_results.items():
        if res is None:
            rows.append({"horizon_bars": h, "pearson_r": np.nan,
                         "pearson_p": np.nan, "hit_rate": np.nan, "t_stat": np.nan})
            continue
        ci_lo, ci_hi = res["hit_rate_ci"]
        rows.append({
            "signal": label,
            "horizon_bars": h,
            "pearson_r":   round(res["pearson_r"], 4),
            "pearson_p":   round(res["pearson_p"], 4),
            "hit_rate":    round(res["hit_rate"], 4),
            "hit_rate_ci_lo": round(ci_lo, 4),
            "hit_rate_ci_hi": round(ci_hi, 4),
            "t_stat":      round(res["t_stat"], 3) if not np.isnan(res["t_stat"]) else np.nan,
            "n_obs":       res["n_obs"],
        })
    return pd.DataFrame(rows)


# ──────────────────────────────────────────────────────────────────────────────
# 6.  Wave feature catalogue
# ──────────────────────────────────────────────────────────────────────────────

def wave_feature_table() -> pd.DataFrame:
    """Return the candidate Wave feature catalogue as a tidy DataFrame.

    Each row represents a proposed feature that could later be wired into
    the production Wave engine.  This is research output only.
    """
    rows = [
        {
            "feature_name":       "wave.pc1_return",
            "description":        "Return of the first principal component across the selected universe.",
            "formula":            "Dot product of latest PC1 loadings with universe log-returns at time t.",
            "update_cadence":     "Every Wave tick (~5 s / configurable)",
            "required_inputs":    "Universe log-returns, rolling PCA loadings",
            "expected_range":     "Unbounded; typically ±3σ of asset returns",
            "wave_permission_use":"High positive → reinforce BREAKOUT permission; high negative → BREAKDOWN",
        },
        {
            "feature_name":       "wave.pc1_variance_explained",
            "description":        "Fraction of universe variance explained by PC1 in the current window.",
            "formula":            "PCA.explained_variance_ratio_[0] from rolling fit.",
            "update_cadence":     "Every Wave tick",
            "required_inputs":    "Universe returns, rolling PCA",
            "expected_range":     "[0, 1]; typical crypto: 0.25–0.65",
            "wave_permission_use":"High → cohesive market, amplify directional permissions; Low → suppress.",
        },
        {
            "feature_name":       "wave.market_cohesion",
            "description":        "Degree to which the universe moves together. Inverse of fragmentation.",
            "formula":            "Weighted average pairwise correlation of universe assets in rolling window.",
            "update_cadence":     "Every Wave tick",
            "required_inputs":    "Universe returns",
            "expected_range":     "[-1, 1]; healthy crypto market ~0.4–0.7",
            "wave_permission_use":"High → trust directional signals; Low → increase mean-reversion weight.",
        },
        {
            "feature_name":       "wave.correlation_regime",
            "description":        "Discrete label for the current correlation environment.",
            "formula":            "Thresholded market_cohesion: HIGH (>0.6) / NORMAL / LOW (<0.3) / COLLAPSE (<0)",
            "update_cadence":     "Every Wave tick",
            "required_inputs":    "wave.market_cohesion",
            "expected_range":     "Categorical: {HIGH, NORMAL, LOW, COLLAPSE}",
            "wave_permission_use":"COLLAPSE → disable breakout; HIGH → enable breakout/breakdown.",
        },
        {
            "feature_name":       "wave.cluster_leader_return",
            "description":        "Return of the leading asset in the dominant cluster at time t.",
            "formula":            "Return of the cluster_leader asset identified by K-means + loadings.",
            "update_cadence":     "Every Wave tick",
            "required_inputs":    "Cluster assignments, asset returns",
            "expected_range":     "Asset-level log-return scale",
            "wave_permission_use":"Strong positive → early directional pressure on BTC/ETH.",
        },
        {
            "feature_name":       "wave.cluster_leader_id",
            "description":        "Symbol of the current cluster leader asset.",
            "formula":            "Asset with highest abs loading on PC1 within dominant cluster.",
            "update_cadence":     "Rolling window update",
            "required_inputs":    "K-means cluster assignments, PCA loadings",
            "expected_range":     "Categorical (symbol string)",
            "wave_permission_use":"Identifies which asset currently drives market structure.",
        },
        {
            "feature_name":       "wave.cluster_dispersion",
            "description":        "Average intra-cluster return variance; high value indicates fragmented market.",
            "formula":            "Mean(std(returns within each cluster)) across all clusters.",
            "update_cadence":     "Every Wave tick",
            "required_inputs":    "Cluster assignments, universe returns",
            "expected_range":     ">0; relative to asset return scale",
            "wave_permission_use":"High → fragmentation, favour mean-reversion; Low → cohesion, favour breakout.",
        },
        {
            "feature_name":       "wave.btc_residual_z",
            "description":        "Z-score of BTC return minus its factor-model-expected return.",
            "formula":            "z = (btc_actual - btc_expected) / rolling_std(btc_residual)",
            "update_cadence":     "Every Wave tick",
            "required_inputs":    "BTC return, PC/cluster factor returns, rolling OLS coefficients",
            "expected_range":     "Approximately N(0,1); extreme values ±2–3 are signal",
            "wave_permission_use":"|z| > 2 with positive sign → idiosyncratic BTC strength, amplify long permission.",
        },
        {
            "feature_name":       "wave.eth_residual_z",
            "description":        "Z-score of ETH return minus its factor-model-expected return.",
            "formula":            "z = (eth_actual - eth_expected) / rolling_std(eth_residual)",
            "update_cadence":     "Every Wave tick",
            "required_inputs":    "ETH return, PC/cluster factor returns, rolling OLS coefficients",
            "expected_range":     "Approximately N(0,1)",
            "wave_permission_use":"Same as btc_residual_z applied to ETH trading permissions.",
        },
        {
            "feature_name":       "wave.market_beta_pressure",
            "description":        "Normalised score indicating whether broader market structure implies "
                                  "upward or downward pressure on BTC/ETH.",
            "formula":            "tanh((btc_residual_z + eth_residual_z) / 4)",
            "update_cadence":     "Every Wave tick",
            "required_inputs":    "wave.btc_residual_z, wave.eth_residual_z",
            "expected_range":     "(-1, +1); +1 = strong upward beta pressure",
            "wave_permission_use":"Positive → upward macro structure, strengthen long permission weight.",
        },
        {
            "feature_name":       "wave.predictive_corr_breadth",
            "description":        "Number / share of universe assets with positive lead/lag correlation "
                                  "to future BTC/ETH returns.",
            "formula":            "Count of assets where lag_corr_1m_btc > threshold, normalised by universe size.",
            "update_cadence":     "Rolling window (recalculated every N bars)",
            "required_inputs":    "Universe returns, lag-correlation scores",
            "expected_range":     "[0, 1]",
            "wave_permission_use":"High breadth → many assets pointing in same direction, strengthen permission.",
        },
        {
            "feature_name":       "wave.risk_on_score",
            "description":        "Composite score capturing broad risk-appetite (equities up, gold down, "
                                  "high-beta crypto leading).",
            "formula":            "Weighted average: +corr(NAS100/SPX), +BTC beta, -XAU return, +high_beta_cluster_ret",
            "update_cadence":     "Every Wave tick",
            "required_inputs":    "Macro Oanda + crypto universe returns, cluster assignments",
            "expected_range":     "[0, 1] after normalisation",
            "wave_permission_use":"High → enable long breakout/momentum; Low → suppress or prefer short.",
        },
        {
            "feature_name":       "wave.risk_off_score",
            "description":        "Composite score capturing broad risk-aversion (equities down, gold up, "
                                  "defensive assets leading).",
            "formula":            "Weighted average: +XAU return, -NAS100 return, -BTC beta, +USD strength",
            "update_cadence":     "Every Wave tick",
            "required_inputs":    "Macro Oanda + crypto universe returns",
            "expected_range":     "[0, 1] after normalisation",
            "wave_permission_use":"High → suppress long breakout, enable short/breakdown or NEUTRAL/MEAN_REVERSION.",
        },
    ]
    return pd.DataFrame(rows).set_index("feature_name")


# ──────────────────────────────────────────────────────────────────────────────
# 7.  Regime interpretation mapping (research only)
# ──────────────────────────────────────────────────────────────────────────────

WAVE_REGIME_RULES: List[dict] = [
    {
        "label": "BREAKOUT / risk-on",
        "conditions": {
            "pc1_variance_explained": "> 0.45",
            "pc1_return":             "> 0 (positive)",
            "market_beta_pressure":   "> 0.3",
            "risk_on_score":          "> 0.6",
        },
        "notes": "Broad market leading BTC/ETH upward.  Strong directional coherence.",
    },
    {
        "label": "BREAKOUT / risk-off",
        "conditions": {
            "pc1_variance_explained": "> 0.45",
            "pc1_return":             "< 0 (negative)",
            "market_beta_pressure":   "< -0.3",
            "risk_off_score":         "> 0.6",
        },
        "notes": "Coordinated broad-market sell-off.  BTC/ETH downside breakout context.",
    },
    {
        "label": "FRAGMENTED / MEAN_REVERSION context",
        "conditions": {
            "pc1_variance_explained": "< 0.30",
            "cluster_dispersion":     "> high (>75th pct)",
            "market_cohesion":        "< 0.30",
        },
        "notes": "Market fragmented.  Assets not moving together.  Favour mean-reversion archetypes.",
    },
    {
        "label": "BREAKDOWN / correlation-collapse",
        "conditions": {
            "correlation_regime":     "== COLLAPSE",
            "cluster_dispersion":     "rising (trend > 0)",
            "market_beta_pressure":   "< -0.2",
        },
        "notes": "Correlation collapse + rising dispersion signals stress and possible sharp BTC/ETH move.",
    },
    {
        "label": "Early directional pressure",
        "conditions": {
            "market_beta_pressure":   "> 0.5 or < -0.5",
            "btc_residual_z":         "near 0 (BTC not yet moved)",
            "cluster_leader_return":  "significant in pressure direction",
        },
        "notes": "Broader assets have moved but BTC/ETH residual is flat — potential leading indicator.",
    },
    {
        "label": "NEUTRAL / no signal",
        "conditions": {
            "pc1_variance_explained": "0.30–0.45",
            "market_beta_pressure":   "-0.2 to +0.2",
            "risk_on_score":          "0.4–0.6",
            "risk_off_score":         "0.4–0.6",
        },
        "notes": "No dominant regime.  Default to single-symbol Wave signals.",
    },
]


def regime_rules_table() -> pd.DataFrame:
    """Return the regime interpretation rules as a formatted DataFrame."""
    rows = []
    for rule in WAVE_REGIME_RULES:
        cond_str = "; ".join(f"{k} {v}" for k, v in rule["conditions"].items())
        rows.append({
            "regime_label": rule["label"],
            "conditions":   cond_str,
            "notes":        rule["notes"],
        })
    return pd.DataFrame(rows).set_index("regime_label")
