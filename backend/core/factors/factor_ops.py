"""Core factor operators — Layer 1 of the Laddu factor platform.

Clean-room implementation. Every factor formula in core/factors/zoo/**
is built exclusively from the primitives defined here (plus numpy/pandas/
scipy/stdlib). No operator here may read a global, touch the network, or
otherwise have a side effect: every function takes a panel (or two) and
returns a panel, full stop.

Panel convention: a pandas DataFrame indexed by date (ascending, one row
per trading day) with one column per symbol. All operators are causal —
row t may only depend on rows <= t of the input. This is enforced by the
lookahead-gate test in tests/test_factor_gates.py, not just by convention.
"""

from __future__ import annotations

import numpy as np
import pandas as pd


def _validate_window(n: int) -> None:
    if n < 1:
        raise ValueError(f"window must be >= 1, got {n}")


def rank(panel: pd.DataFrame) -> pd.DataFrame:
    """Cross-sectional percentile rank, per row, in [0, 1]."""
    return panel.rank(axis=1, pct=True)


def scale(panel: pd.DataFrame, target_sum: float = 1.0) -> pd.DataFrame:
    """Rescale each row so the sum of absolute values equals target_sum."""
    abs_sum = panel.abs().sum(axis=1)
    abs_sum = abs_sum.replace(0.0, np.nan)
    return panel.div(abs_sum, axis=0) * target_sum


def delta(panel: pd.DataFrame, d: int) -> pd.DataFrame:
    """panel[t] - panel[t-d]. d must be >= 1 (no forward differencing)."""
    if d < 1:
        raise ValueError(f"delta window d must be >= 1, got {d}")
    return panel.diff(d)


def ts_sum(panel: pd.DataFrame, n: int) -> pd.DataFrame:
    _validate_window(n)
    return panel.rolling(window=n, min_periods=n).sum()


def ts_mean(panel: pd.DataFrame, n: int) -> pd.DataFrame:
    _validate_window(n)
    return panel.rolling(window=n, min_periods=n).mean()


def ts_std(panel: pd.DataFrame, n: int) -> pd.DataFrame:
    _validate_window(n)
    return panel.rolling(window=n, min_periods=n).std()


def ts_max(panel: pd.DataFrame, n: int) -> pd.DataFrame:
    _validate_window(n)
    return panel.rolling(window=n, min_periods=n).max()


def ts_min(panel: pd.DataFrame, n: int) -> pd.DataFrame:
    _validate_window(n)
    return panel.rolling(window=n, min_periods=n).min()


def _last_rank(window: np.ndarray) -> float:
    # percentile rank of the final element within the trailing window
    n = len(window)
    if n == 0 or np.all(np.isnan(window)):
        return np.nan
    last = window[-1]
    valid = window[~np.isnan(window)]
    if len(valid) == 0:
        return np.nan
    return float((valid <= last).sum()) / float(len(valid))


def ts_rank(panel: pd.DataFrame, n: int) -> pd.DataFrame:
    """Percentile rank of today's value within its own trailing n-day window.

    Vectorized via numpy sliding_window_view (falls back to the slow
    pandas .rolling().apply() path for panels shorter than the window,
    where there's nothing to vectorize anyway). Same output as the
    original per-window implementation, just faster -- this was Layer 3's
    documented bottleneck-if-measured caveat; scanning multiple factor
    families live is that measured case.
    """
    _validate_window(n)
    from numpy.lib.stride_tricks import sliding_window_view

    arr = panel.to_numpy(dtype=np.float64)
    t, c = arr.shape
    if t < n:
        return panel.rolling(window=n, min_periods=n).apply(_last_rank, raw=True)

    windows = sliding_window_view(arr, window_shape=n, axis=0)  # (t-n+1, c, n)
    last_vals = windows[:, :, -1]
    nan_last = np.isnan(last_vals)
    nan_count = np.isnan(windows).sum(axis=2)
    valid_count = n - nan_count

    last_expanded = last_vals[:, :, np.newaxis]
    valid_mask = ~np.isnan(windows) & ~nan_last[:, :, np.newaxis]
    less_eq = np.sum(np.where(valid_mask, windows <= last_expanded, 0), axis=2)
    with np.errstate(divide="ignore", invalid="ignore"):
        pct = less_eq / valid_count
    pct[nan_last | (nan_count > 0)] = np.nan

    result = np.full((t, c), np.nan)
    result[n - 1:] = pct
    return pd.DataFrame(result, index=panel.index, columns=panel.columns)


def _last_argmax(window: np.ndarray) -> float:
    n = len(window)
    if n == 0 or np.all(np.isnan(window)):
        return np.nan
    # distance (in days) from the end of the window to the max, 0 = today
    idx = np.nanargmax(window)
    return float((n - 1) - idx)


def _last_argmin(window: np.ndarray) -> float:
    n = len(window)
    if n == 0 or np.all(np.isnan(window)):
        return np.nan
    idx = np.nanargmin(window)
    return float((n - 1) - idx)


def ts_argmax(panel: pd.DataFrame, n: int) -> pd.DataFrame:
    """Days since the trailing-n-day max (0 = today is the max). Vectorized."""
    _validate_window(n)
    return _ts_arg_extreme(panel, n, mode="max")


def ts_argmin(panel: pd.DataFrame, n: int) -> pd.DataFrame:
    """Days since the trailing-n-day min (0 = today is the min). Vectorized."""
    _validate_window(n)
    return _ts_arg_extreme(panel, n, mode="min")


def _ts_arg_extreme(panel: pd.DataFrame, n: int, mode: str) -> pd.DataFrame:
    from numpy.lib.stride_tricks import sliding_window_view

    arr = panel.to_numpy(dtype=np.float64)
    t, c = arr.shape
    fallback_fn = _last_argmax if mode == "max" else _last_argmin
    if t < n:
        return panel.rolling(window=n, min_periods=n).apply(fallback_fn, raw=True)

    windows = sliding_window_view(arr, window_shape=n, axis=0)  # (t-n+1, c, n)
    all_nan = np.isnan(windows).all(axis=2)
    fill = -np.inf if mode == "max" else np.inf
    filled = np.where(np.isnan(windows), fill, windows)
    idx = np.argmax(filled, axis=2) if mode == "max" else np.argmin(filled, axis=2)
    days_since = (n - 1) - idx  # distance from window end; 0 = today
    days_since = days_since.astype(np.float64)
    days_since[all_nan] = np.nan

    result = np.full((t, c), np.nan)
    result[n - 1:] = days_since
    return pd.DataFrame(result, index=panel.index, columns=panel.columns)


def decay_linear(panel: pd.DataFrame, n: int) -> pd.DataFrame:
    """Linearly-weighted moving average over the trailing n days
    (weight n for today, 1 for n-1 days ago; causal, no lookahead).
    Vectorized via sliding_window_view + einsum."""
    _validate_window(n)
    from numpy.lib.stride_tricks import sliding_window_view

    weights = np.arange(1, n + 1, dtype=np.float64)  # oldest->newest = 1..n
    weights_sum = weights.sum()

    arr = panel.to_numpy(dtype=np.float64)
    t, c = arr.shape
    if t < n:
        def _weighted(window: np.ndarray) -> float:
            if np.any(np.isnan(window)):
                return np.nan
            return float(np.dot(window, weights) / weights_sum)
        return panel.rolling(window=n, min_periods=n).apply(_weighted, raw=True)

    windows = sliding_window_view(arr, window_shape=n, axis=0)  # (t-n+1, c, n)
    nan_mask = np.isnan(windows).any(axis=2)
    safe_windows = np.where(nan_mask[..., np.newaxis], 0.0, windows)
    dot = np.einsum("ijk,k->ij", safe_windows, weights) / weights_sum
    dot[nan_mask] = np.nan

    result = np.full((t, c), np.nan)
    result[n - 1:] = dot
    return pd.DataFrame(result, index=panel.index, columns=panel.columns)


def safe_div(numerator: pd.DataFrame, denominator: pd.DataFrame) -> pd.DataFrame:
    """Elementwise division; zero/NaN denominator -> NaN, never inf or a crash."""
    denom = denominator.replace(0.0, np.nan)
    return numerator.div(denom)


def sign(panel: pd.DataFrame) -> pd.DataFrame:
    return np.sign(panel)


def log(panel: pd.DataFrame) -> pd.DataFrame:
    """Natural log; non-positive values -> NaN rather than raising/complex."""
    safe = panel.where(panel > 0)
    return np.log(safe)


def ts_correlation(a: pd.DataFrame, b: pd.DataFrame, n: int) -> pd.DataFrame:
    """Rolling n-day Pearson correlation between two panels, column-wise.

    pandas' rolling().corr() can emit +/-inf (not NaN) when a window is
    numerically near-constant -- force those to NaN so downstream never
    silently treats an infinite correlation as a real signal.
    """
    _validate_window(n)
    if list(a.columns) != list(b.columns):
        raise ValueError("ts_correlation requires identical columns on both panels")
    out = pd.DataFrame(index=a.index, columns=a.columns, dtype=float)
    for col in a.columns:
        out[col] = a[col].rolling(window=n, min_periods=n).corr(b[col])
    return out.replace([np.inf, -np.inf], np.nan)


def ts_covariance(a: pd.DataFrame, b: pd.DataFrame, n: int) -> pd.DataFrame:
    """Rolling n-day covariance between two panels, column-wise."""
    _validate_window(n)
    if list(a.columns) != list(b.columns):
        raise ValueError("ts_covariance requires identical columns on both panels")
    out = pd.DataFrame(index=a.index, columns=a.columns, dtype=float)
    for col in a.columns:
        out[col] = a[col].rolling(window=n, min_periods=n).cov(b[col])
    return out.replace([np.inf, -np.inf], np.nan)


def amount_value(panel: dict | pd.DataFrame) -> pd.DataFrame:
    """Return a causal turnover/amount panel from market data.

    Vibe-Trading's GTJA191 formulas were written for China data, where
    ``amount`` is often supplied separately. Project Laddu's India path is
    intentionally OHLCV-first, so missing ``amount`` is reconstructed as
    typical-price × volume × 100. The 100x lot-style scale preserves the
    old ``amount / (volume * 100)`` VWAP formulas as a typical-price proxy,
    while pure turnover factors still receive a stable liquidity magnitude.
    """
    if "amount" in panel:
        return panel["amount"]
    if "vwap" in panel:
        ref_price = panel["vwap"]
    else:
        required = ("open", "high", "low", "close", "volume")
        missing = [k for k in required if k not in panel]
        if missing:
            raise KeyError(f"amount_value requires amount or OHLCV keys; missing {missing}")
        ref_price = (panel["open"] + panel["high"] + panel["low"] + panel["close"]) / 4.0
    return ref_price * panel["volume"] * 100.0


def vwap(close: pd.DataFrame | dict, volume: pd.DataFrame | str | None = None, n: int | None = None) -> pd.DataFrame:
    """VWAP helper.

    Backward-compatible forms:
    - ``vwap(close, volume, n)``: trailing n-bar volume-weighted average.
    - ``vwap(panel, market)``: Vibe-style reference price used by ported
      gtja191 factors; prefers panel['vwap'], otherwise OHLC typical price.
    """
    if isinstance(close, (dict, pd.DataFrame)) and isinstance(volume, str) and n is None:
        panel = close
        if "vwap" in panel:
            return panel["vwap"]
        required = ("open", "high", "low", "close")
        missing = [k for k in required if k not in panel]
        if missing:
            raise KeyError(f"vwap(panel, market) requires {required}; missing {missing}")
        return (panel["open"] + panel["high"] + panel["low"] + panel["close"]) / 4.0

    if volume is None or n is None:
        raise TypeError("vwap expects either vwap(close, volume, n) or vwap(panel, market)")
    _validate_window(n)
    pv = close * volume
    return safe_div(ts_sum(pv, n), ts_sum(volume, n))


def zscore(panel: pd.DataFrame) -> pd.DataFrame:
    """Cross-sectional z-score per row (mean 0, std 1 across symbols that day)."""
    row_mean = panel.mean(axis=1)
    row_std = panel.std(axis=1).replace(0.0, np.nan)
    return panel.sub(row_mean, axis=0).div(row_std, axis=0)


def returns(close: pd.DataFrame, n: int = 1) -> pd.DataFrame:
    """Simple n-day percentage return, causal (uses close[t] and close[t-n] only)."""
    if n < 1:
        raise ValueError(f"returns window n must be >= 1, got {n}")
    return safe_div(close - close.shift(n), close.shift(n))


def ts_skew(panel: pd.DataFrame, n: int) -> pd.DataFrame:
    """Rolling n-day skewness."""
    _validate_window(n)
    return panel.rolling(window=n, min_periods=n).skew()


def signed_power(panel: pd.DataFrame, p: float) -> pd.DataFrame:
    """sign(x) * |x|**p -- preserves sign, never produces complex output
    (unlike a naive x**p on negative x with fractional p)."""
    arr = panel.to_numpy(dtype=np.float64)
    out = np.sign(arr) * np.power(np.abs(arr), p)
    return pd.DataFrame(out, index=panel.index, columns=panel.columns)


# Aliases matching the upstream Vibe-Trading naming convention, so
# ported alpha101/qlib158/gtja191 formulas need zero call-site edits
# beyond the import line itself.
ts_corr = ts_correlation
ts_cov = ts_covariance


def missing_output(panel: pd.DataFrame) -> pd.DataFrame:
    """Return a shape-correct all-NaN factor panel for unavailable inputs.

    Factor implementations declare required columns in ``__alpha_meta__``.
    Research orchestration should normally exclude a factor when those inputs
    are absent, but a direct registry-wide audit must still degrade safely
    rather than raise a KeyError. The output intentionally contains no signal.
    """
    if isinstance(panel.columns, pd.MultiIndex) and panel.columns.nlevels >= 2:
        symbols = list(dict.fromkeys(panel.columns.get_level_values(-1)))
    else:
        symbols = list(panel.columns)
    return pd.DataFrame(np.nan, index=panel.index, columns=symbols, dtype=float)
