"""Market indicators in pure Python (standard library only, so they run in Lambda).

Conventions:
- Price series are lists of floats; aligned series may contain None (no price that day).
- Returns are simple daily returns unless a function says otherwise.
- Annualisation uses 252 trading days.
"""

from __future__ import annotations

import math
import statistics

TRADING_DAYS = 252


# ----- basic series helpers --------------------------------------------------
def returns(values: list) -> list[float]:
    """Simple returns between consecutive non-missing values."""
    out, prev = [], None
    for v in values:
        if v is None:
            continue
        if prev is not None and prev > 0:
            out.append(v / prev - 1)
        prev = v
    return out


def log_returns(values: list) -> list[float]:
    return [math.log1p(r) for r in returns(values) if r > -1]


def first_last(values: list) -> tuple[float | None, float | None, int | None]:
    """First and last non-missing value, and the index of the first one."""
    first_i = next((i for i, v in enumerate(values) if v is not None), None)
    if first_i is None:
        return None, None, None
    last = next(v for v in reversed(values) if v is not None)
    return values[first_i], last, first_i


def window(values: list, n: int) -> list:
    """The last n+1 points (n returns)."""
    return values[-(n + 1):] if n + 1 <= len(values) else list(values)


def covered(values: list, n: int, min_share: float = 0.9) -> bool:
    """True if the window starts with a price and at least min_share of its points exist."""
    w = window(values, n)
    if len(w) < n + 1 or w[0] is None:
        return False
    return sum(v is not None for v in w) >= min_share * len(w)


def total_return(values: list, n: int) -> float | None:
    w = window(values, n)
    if not covered(values, n):
        return None
    v0, v1, _ = first_last(w)
    return v1 / v0 - 1 if v0 else None


def cagr(v0: float | None, v1: float | None, years: float) -> float | None:
    if not v0 or not v1 or years <= 0:
        return None
    return (v1 / v0) ** (1 / years) - 1


def ann_vol(rets: list[float]) -> float | None:
    if len(rets) < 15:
        return None
    return statistics.stdev(rets) * math.sqrt(TRADING_DAYS)


def downside_dev(rets: list[float], mar_daily: float = 0.0) -> float | None:
    if len(rets) < 15:
        return None
    return math.sqrt(sum(min(r - mar_daily, 0.0) ** 2 for r in rets) / len(rets)) * math.sqrt(TRADING_DAYS)


def max_drawdown(values: list) -> tuple[float | None, dict | None]:
    """Largest peak-to-trough fall (<= 0) with the positions of peak and trough."""
    peak_v, peak_i, mdd, info = None, None, 0.0, None
    for i, v in enumerate(values):
        if v is None:
            continue
        if peak_v is None or v > peak_v:
            peak_v, peak_i = v, i
        dd = v / peak_v - 1
        if dd < mdd:
            mdd, info = dd, {"peak_index": peak_i, "trough_index": i}
    if peak_v is None:
        return None, None
    return mdd, info


def sharpe(rets: list[float], rf_daily: list[float]) -> float | None:
    if len(rets) < 60 or len(rf_daily) != len(rets):
        return None
    ex = [r - f for r, f in zip(rets, rf_daily)]
    sd = statistics.stdev(rets)
    return None if sd == 0 else statistics.fmean(ex) * TRADING_DAYS / (sd * math.sqrt(TRADING_DAYS))


def sortino(rets: list[float], rf_daily: list[float]) -> float | None:
    if len(rets) < 60 or len(rf_daily) != len(rets):
        return None
    ex = [r - f for r, f in zip(rets, rf_daily)]
    dd = math.sqrt(sum(min(e, 0.0) ** 2 for e in ex) / len(ex))
    return None if dd == 0 else statistics.fmean(ex) * TRADING_DAYS / (dd * math.sqrt(TRADING_DAYS))


def var_es(rets: list[float], level: float = 0.95) -> tuple[float | None, float | None]:
    """Historical one-day value at risk and expected shortfall (= CVaR), as positive losses."""
    if len(rets) < 100:
        return None, None
    s = sorted(rets)
    k = max(1, int(len(s) * (1 - level)))
    tail = s[:k]
    return -s[k - 1], -statistics.fmean(tail)


def pearson(x: list[float], y: list[float]) -> float | None:
    if len(x) < 10 or len(x) != len(y):
        return None
    mx, my = statistics.fmean(x), statistics.fmean(y)
    sxy = sum((a - mx) * (b - my) for a, b in zip(x, y))
    sxx = sum((a - mx) ** 2 for a in x)
    syy = sum((b - my) ** 2 for b in y)
    return None if sxx == 0 or syy == 0 else sxy / math.sqrt(sxx * syy)


def beta(x: list[float], m: list[float]) -> float | None:
    if len(x) < 10 or len(x) != len(m):
        return None
    mx, mm = statistics.fmean(x), statistics.fmean(m)
    var = sum((b - mm) ** 2 for b in m)
    return None if var == 0 else sum((a - mx) * (b - mm) for a, b in zip(x, m)) / var


# ----- technical indicators (own trading rows, listing currency) ------------
def sma(values: list[float], n: int) -> float | None:
    return statistics.fmean(values[-n:]) if len(values) >= n else None


def ema_series(values: list[float], n: int) -> list[float]:
    if not values:
        return []
    k = 2 / (n + 1)
    out = [values[0]]
    for v in values[1:]:
        out.append(v * k + out[-1] * (1 - k))
    return out


def rsi_wilder(closes: list[float], n: int = 14) -> float | None:
    if len(closes) < n * 3:
        return None
    gains = [max(closes[i] - closes[i - 1], 0.0) for i in range(1, len(closes))]
    losses = [max(closes[i - 1] - closes[i], 0.0) for i in range(1, len(closes))]
    avg_g, avg_l = statistics.fmean(gains[:n]), statistics.fmean(losses[:n])
    for g, l in zip(gains[n:], losses[n:]):
        avg_g = (avg_g * (n - 1) + g) / n
        avg_l = (avg_l * (n - 1) + l) / n
    if avg_l == 0:
        return 100.0
    return 100 - 100 / (1 + avg_g / avg_l)


def macd(closes: list[float], fast: int = 12, slow: int = 26, signal: int = 9) -> dict | None:
    if len(closes) < slow + signal * 3:
        return None
    ef, es = ema_series(closes, fast), ema_series(closes, slow)
    line = [a - b for a, b in zip(ef, es)]
    sig = ema_series(line[slow:], signal)
    hist_now = line[-1] - sig[-1]
    hist_prev = line[-2] - sig[-2]
    return {"macd": line[-1], "signal": sig[-1], "hist": hist_now, "hist_prev": hist_prev}


def atr_wilder(highs: list[float], lows: list[float], closes: list[float], n: int = 14) -> float | None:
    if len(closes) < n * 3:
        return None
    trs = [highs[0] - lows[0]]
    for i in range(1, len(closes)):
        trs.append(max(highs[i] - lows[i], abs(highs[i] - closes[i - 1]), abs(lows[i] - closes[i - 1])))
    atr = statistics.fmean(trs[:n])
    for tr in trs[n:]:
        atr = (atr * (n - 1) + tr) / n
    return atr


# ----- regression -------------------------------------------------------------
def _solve(a: list[list[float]], b: list[float]) -> list[float] | None:
    """Gaussian elimination with partial pivoting."""
    n = len(b)
    m = [row[:] + [b[i]] for i, row in enumerate(a)]
    for col in range(n):
        piv = max(range(col, n), key=lambda r: abs(m[r][col]))
        if abs(m[piv][col]) < 1e-14:
            return None
        m[col], m[piv] = m[piv], m[col]
        for r in range(n):
            if r != col:
                f = m[r][col] / m[col][col]
                for c in range(col, n + 1):
                    m[r][c] -= f * m[col][c]
    return [m[i][n] / m[i][i] for i in range(n)]


def ols(y: list[float], xs: list[list[float]]) -> dict | None:
    """OLS of y on the columns in xs plus an intercept. Returns betas (no intercept) and R squared."""
    n, k = len(y), len(xs)
    if n < k + 10 or any(len(x) != n for x in xs):
        return None
    cols = [[1.0] * n] + xs
    xtx = [[sum(ci[t] * cj[t] for t in range(n)) for cj in cols] for ci in cols]
    xty = [sum(ci[t] * y[t] for t in range(n)) for ci in cols]
    coef = _solve(xtx, xty)
    if coef is None:
        return None
    fitted = [sum(coef[j] * cols[j][t] for j in range(k + 1)) for t in range(n)]
    my = statistics.fmean(y)
    sst = sum((v - my) ** 2 for v in y)
    sse = sum((v - f) ** 2 for v, f in zip(y, fitted))
    return {"intercept": coef[0], "betas": coef[1:], "r2": None if sst == 0 else 1 - sse / sst, "n": n}


# ----- GARCH(1,1) ---------------------------------------------------------------
def _garch_ll(r: list[float], var: float, a: float, b: float) -> tuple[float, float]:
    omega = var * (1 - a - b)
    s2, ll = var, 0.0
    for x in r:
        ll -= 0.5 * (math.log(s2) + x * x / s2)
        s2 = omega + a * x * x + b * s2
    return ll, s2  # s2 is the one-step-ahead variance after the last observation


def garch11(rets: list[float], horizon: int = 21) -> dict | None:
    """GARCH(1,1) fitted by maximum likelihood with variance targeting (grid search, then a finer grid).

    Returns the average annualised volatility expected over the next `horizon` trading days,
    the long-run volatility and the persistence (alpha + beta).
    """
    if len(rets) < 250:
        return None
    mu = statistics.fmean(rets)
    r = [x - mu for x in rets]
    var = statistics.fmean([x * x for x in r])
    if var <= 0:
        return None
    best = None
    grid_a = [0.01 + 0.02 * i for i in range(15)]
    grid_b = [0.60 + 0.02 * i for i in range(20)]
    for a in grid_a:
        for b in grid_b:
            if a + b >= 0.995:
                continue
            ll, nxt = _garch_ll(r, var, a, b)
            if best is None or ll > best[0]:
                best = (ll, a, b, nxt)
    _, a0, b0, _ = best
    for a in [a0 + 0.005 * i for i in range(-4, 5)]:
        for b in [b0 + 0.005 * i for i in range(-4, 5)]:
            if a <= 0 or b <= 0 or a + b >= 0.995:
                continue
            ll, nxt = _garch_ll(r, var, a, b)
            if ll > best[0]:
                best = (ll, a, b, nxt)
    ll, a, b, next_var = best
    persistence = a + b
    path = [var + persistence ** (h - 1) * (next_var - var) for h in range(1, horizon + 1)]
    return {
        "alpha": round(a, 4), "beta": round(b, 4), "persistence": round(persistence, 4),
        "vol_forecast": math.sqrt(statistics.fmean(path) * TRADING_DAYS),
        "vol_long_run": math.sqrt(var * TRADING_DAYS),
        "vol_next_day": math.sqrt(next_var * TRADING_DAYS),
        "half_life_days": None if persistence <= 0 else round(math.log(0.5) / math.log(persistence), 1),
        "log_likelihood": round(ll, 2), "observations": len(r),
    }


# ----- two-state volatility regime (Gaussian hidden Markov model) ---------------------------
def _norm_pdf(x: float, mu: float, sd: float) -> float:
    z = (x - mu) / sd
    return math.exp(-0.5 * z * z) / (sd * math.sqrt(2 * math.pi))


def hmm2(rets: list[float], iterations: int = 60) -> dict | None:
    """Two-state Gaussian HMM fitted with Baum-Welch (scaled forward-backward).

    State 0 is the calm (low-volatility) regime and state 1 the turbulent one. Returns the
    filtered probability of the turbulent regime on the last day, each regime's annualised
    volatility, the share of days spent in it and its expected duration.
    """
    n = len(rets)
    if n < 250:
        return None
    sd_all = statistics.pstdev(rets)
    if sd_all == 0:
        return None
    mu = [statistics.fmean(rets)] * 2
    sd = [0.7 * sd_all, 1.6 * sd_all]
    A = [[0.98, 0.02], [0.05, 0.95]]
    pi = [0.5, 0.5]
    floor = sd_all * 0.05
    prev_ll = None
    alpha = []
    for _ in range(iterations):
        em = [(_norm_pdf(x, mu[0], sd[0]), _norm_pdf(x, mu[1], sd[1])) for x in rets]
        # forward
        alpha, scale = [], []
        a0 = (pi[0] * em[0][0], pi[1] * em[0][1])
        c = (a0[0] + a0[1]) or 1e-300
        alpha.append((a0[0] / c, a0[1] / c)); scale.append(c)
        for t in range(1, n):
            p0, p1 = alpha[t - 1]
            x0 = em[t][0] * (p0 * A[0][0] + p1 * A[1][0])
            x1 = em[t][1] * (p0 * A[0][1] + p1 * A[1][1])
            c = (x0 + x1) or 1e-300
            alpha.append((x0 / c, x1 / c)); scale.append(c)
        ll = sum(math.log(c) for c in scale)
        # backward
        beta_ = [(1.0, 1.0)] * n
        for t in range(n - 2, -1, -1):
            e0, e1 = em[t + 1]
            b0, b1 = beta_[t + 1]
            s = scale[t + 1]
            beta_[t] = ((A[0][0] * e0 * b0 + A[0][1] * e1 * b1) / s, (A[1][0] * e0 * b0 + A[1][1] * e1 * b1) / s)
        gamma = []
        for t in range(n):
            g0, g1 = alpha[t][0] * beta_[t][0], alpha[t][1] * beta_[t][1]
            tot = (g0 + g1) or 1e-300
            gamma.append((g0 / tot, g1 / tot))
        xi_sum = [[0.0, 0.0], [0.0, 0.0]]
        for t in range(n - 1):
            e0, e1 = em[t + 1]
            b0, b1 = beta_[t + 1]
            a_0, a_1 = alpha[t]
            t00, t01 = a_0 * A[0][0] * e0 * b0, a_0 * A[0][1] * e1 * b1
            t10, t11 = a_1 * A[1][0] * e0 * b0, a_1 * A[1][1] * e1 * b1
            denom = (t00 + t01 + t10 + t11) or 1e-300
            xi_sum[0][0] += t00 / denom
            xi_sum[0][1] += t01 / denom
            xi_sum[1][0] += t10 / denom
            xi_sum[1][1] += t11 / denom
        # M-step
        pi = list(gamma[0])
        for i in (0, 1):
            row = sum(xi_sum[i]) or 1e-300
            A[i] = [xi_sum[i][j] / row for j in (0, 1)]
        for s in (0, 1):
            w = sum(g[s] for g in gamma) or 1e-300
            mu[s] = sum(g[s] * x for g, x in zip(gamma, rets)) / w
            sd[s] = max(math.sqrt(sum(g[s] * (x - mu[s]) ** 2 for g, x in zip(gamma, rets)) / w), floor)
        if prev_ll is not None and abs(ll - prev_ll) < 1e-6:
            break
        prev_ll = ll
    hi = 1 if sd[1] >= sd[0] else 0
    lo = 1 - hi
    distinct = sd[hi] / sd[lo] >= 1.25
    stay = A[hi][hi]
    return {
        "p_turbulent": alpha[-1][hi],
        "vol_calm": sd[lo] * math.sqrt(TRADING_DAYS),
        "vol_turbulent": sd[hi] * math.sqrt(TRADING_DAYS),
        "share_turbulent": statistics.fmean([g[hi] for g in gamma]),
        "expected_turbulent_days": None if stay >= 1 else round(1 / (1 - stay), 1),
        "distinct": distinct,
        "observations": n,
    }


# ----- Hurst exponent ---------------------------------------------------------
def hurst_rs(rets: list[float]) -> float | None:
    """Rescaled-range (R/S) Hurst exponent of a return series.

    About 0.5 for a random walk, above 0.5 for persistent (trending) returns, below for
    mean-reverting ones. The classic R/S estimate is biased upward on short samples.
    """
    n_total = len(rets)
    if n_total < 256:
        return None
    sizes = [s for s in (8, 16, 32, 64, 128, 256) if s <= n_total // 2]
    xs, ys = [], []
    for size in sizes:
        rs_vals = []
        for k in range(n_total // size):
            chunk = rets[k * size:(k + 1) * size]
            m = statistics.fmean(chunk)
            cum, lo, hi = 0.0, 0.0, 0.0
            for x in chunk:
                cum += x - m
                lo, hi = min(lo, cum), max(hi, cum)
            s = statistics.pstdev(chunk)
            if s > 0:
                rs_vals.append((hi - lo) / s)
        if rs_vals:
            xs.append(math.log(size))
            ys.append(math.log(statistics.fmean(rs_vals)))
    if len(xs) < 3:
        return None
    mx, my = statistics.fmean(xs), statistics.fmean(ys)
    return sum((a - mx) * (b - my) for a, b in zip(xs, ys)) / sum((a - mx) ** 2 for a in xs)
