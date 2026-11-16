"""Investor profiles: validation, risk bands, caps, radar targets and persona presets.

All thresholds below are explicit team assumptions. They are returned to the UI
with every result so a user can see which numbers drove a conclusion.
"""

from __future__ import annotations

import copy
from typing import Any

RISK_LEVELS: dict[str, dict[str, Any]] = {
    # vol_band: acceptable annualised volatility for a single holding.
    # portfolio_vol_target / portfolio_vol_max: whole-portfolio volatility.
    # below_softness: how gently assets *below* the band are penalised.
    "low": {
        "label": "Low", "vol_band": (0.0, 0.08), "below_softness": 2.0,
        "portfolio_vol_target": 0.05, "portfolio_vol_max": 0.08,
    },
    "low_moderate": {
        "label": "Low to moderate", "vol_band": (0.05, 0.14), "below_softness": 1.5,
        "portfolio_vol_target": 0.08, "portfolio_vol_max": 0.11,
    },
    "moderate": {
        "label": "Moderate", "vol_band": (0.08, 0.18), "below_softness": 1.0,
        "portfolio_vol_target": 0.12, "portfolio_vol_max": 0.15,
    },
    "high": {
        "label": "High", "vol_band": (0.12, 0.60), "below_softness": 1.0,
        "portfolio_vol_target": 0.20, "portfolio_vol_max": 0.28,
    },
}

DEFAULT_CAPS: dict[str, dict[str, float]] = {
    "low":          {"max_single_security": 0.05, "max_single_fund": 0.60, "max_sector": 0.20, "max_crypto": 0.00, "min_defensive": 0.70},
    "low_moderate": {"max_single_security": 0.10, "max_single_fund": 0.60, "max_sector": 0.25, "max_crypto": 0.02, "min_defensive": 0.50},
    "moderate":     {"max_single_security": 0.10, "max_single_fund": 0.60, "max_sector": 0.30, "max_crypto": 0.05, "min_defensive": 0.30},
    "high":         {"max_single_security": 0.15, "max_single_fund": 0.60, "max_sector": 0.35, "max_crypto": 0.10, "min_defensive": 0.10},
}

# Radar targets (0-100 on each axis). None = no target for this profile.
RADAR_TARGETS: dict[str, dict[str, float | None]] = {
    "low":          {"diversification": 70, "risk": 12.5, "geography": 40, "currency": 25, "defensiveness": 75, "growth": 10, "income": 60, "event_exposure": 20},
    "low_moderate": {"diversification": 75, "risk": 20,   "geography": 50, "currency": 35, "defensiveness": 60, "growth": 15, "income": 60, "event_exposure": 20},
    "moderate":     {"diversification": 75, "risk": 30,   "geography": 60, "currency": 50, "defensiveness": 40, "growth": 30, "income": 35, "event_exposure": 20},
    "high":         {"diversification": 65, "risk": 50,   "geography": 60, "currency": 60, "defensiveness": 15, "growth": 45, "income": 10, "event_exposure": 20},
}

CRYPTO_PREFS = ("avoid", "limited", "open")
ESG_EXCLUSION_OPTIONS = ("fossil_fuels",)
INTEREST_TAGS = (
    "ai", "emerging_tech", "growth", "tech", "crypto", "esg", "broad_market", "established",
    "dividend", "defensive", "bonds", "gold", "energy", "bank", "healthcare", "consumer_staples", "utility",
)


def drawdown_tolerance(horizon_years: int) -> float:
    """Largest peak-to-trough fall we treat as tolerable for a horizon alone (assumption)."""
    if horizon_years <= 5:
        return 0.20
    if horizon_years <= 12:
        return 0.35
    return 0.60


# Largest fall tolerated by risk level. The profile's tolerance is the lower of this and the
# horizon value, so a long horizon never makes a low-risk profile accept deep falls.
DRAWDOWN_BY_RISK = {"low": 0.12, "low_moderate": 0.20, "moderate": 0.30, "high": 0.55}


def profile_drawdown_tolerance(profile: dict) -> tuple[float, str]:
    by_h = drawdown_tolerance(profile["horizon_years"])
    by_r = DRAWDOWN_BY_RISK[profile["risk_level"]]
    if by_r <= by_h:
        return by_r, f"risk level ({RISK_LEVELS[profile['risk_level']]['label'].lower()}: {by_r:.0%})"
    return by_h, f"horizon ({horizon_label(profile['horizon_years'])}: {by_h:.0%})"


def horizon_label(horizon_years: int) -> str:
    if horizon_years <= 5:
        return "short (5 years or less)"
    if horizon_years <= 12:
        return "medium (6 to 12 years)"
    return "long (more than 12 years)"


def effective_caps(profile: dict) -> dict[str, float]:
    caps = dict(DEFAULT_CAPS[profile["risk_level"]])
    if profile.get("crypto") == "avoid":
        caps["max_crypto"] = 0.0
    caps.update({k: float(v) for k, v in (profile.get("caps_override") or {}).items() if k in caps})
    return caps


def applicable_cap(asset: dict, caps: dict[str, float]) -> tuple[str, float]:
    """Which cap limits a single position in this asset."""
    if asset["asset_group"] == "crypto":
        return "max_crypto", caps["max_crypto"]
    if asset.get("is_broad_fund"):
        return "max_single_fund", caps["max_single_fund"]
    return "max_single_security", caps["max_single_security"]


def radar_targets(profile: dict) -> dict[str, float | None]:
    t = dict(RADAR_TARGETS[profile["risk_level"]])
    if {"ai", "emerging_tech"} & set(profile.get("interests", [])):
        t["growth"] = min(100, (t["growth"] or 0) + 10)
    t["esg"] = 100 if profile.get("esg_exclusions") else None
    return t


def profile_assumptions(profile: dict) -> list[str]:
    rl = RISK_LEVELS[profile["risk_level"]]
    lo, hi = rl["vol_band"]
    caps = effective_caps(profile)
    return [
        f"Single-holding volatility band for a {rl['label'].lower()} profile: {lo:.0%} to {hi:.0%} a year.",
        f"Portfolio volatility target {rl['portfolio_vol_target']:.0%}, hard limit {rl['portfolio_vol_max']:.0%}.",
        f"Loss tolerance {profile_drawdown_tolerance(profile)[0]:.0%} peak to trough: the lower of the risk-level limit "
        f"and the limit for a {horizon_label(profile['horizon_years'])} horizon.",
        f"Caps: single security {caps['max_single_security']:.0%}, single fund {caps['max_single_fund']:.0%}, "
        f"sector {caps['max_sector']:.0%}, crypto {caps['max_crypto']:.0%}, minimum defensive share {caps['min_defensive']:.0%}.",
        "These thresholds are team assumptions for a simulation, not regulatory suitability rules.",
    ]


class ProfileError(ValueError):
    pass


def validate_profile(raw: dict, known_tickers: set[str]) -> dict:
    """Return a clean profile dict or raise ProfileError with a readable message."""
    if not isinstance(raw, dict):
        raise ProfileError("profile must be a JSON object")
    p: dict[str, Any] = {}
    p["display_name"] = str(raw.get("display_name") or "Investor")[:60]
    p["preset_id"] = raw.get("preset_id") if raw.get("preset_id") in PRESETS else None
    age = raw.get("age")
    p["age"] = int(age) if isinstance(age, (int, float)) and 16 <= age <= 110 else None
    if raw.get("risk_level") not in RISK_LEVELS:
        raise ProfileError(f"risk_level must be one of {list(RISK_LEVELS)}")
    p["risk_level"] = raw["risk_level"]
    try:
        p["horizon_years"] = int(raw.get("horizon_years"))
    except (TypeError, ValueError):
        raise ProfileError("horizon_years must be a whole number of years") from None
    if not 1 <= p["horizon_years"] <= 50:
        raise ProfileError("horizon_years must be between 1 and 50")
    p["objective"] = str(raw.get("objective") or "")[:300]
    p["interests"] = [t for t in raw.get("interests", []) if t in INTEREST_TAGS]
    p["avoid_sectors"] = [str(s)[:60] for s in raw.get("avoid_sectors", [])][:20]
    p["esg_exclusions"] = [e for e in raw.get("esg_exclusions", []) if e in ESG_EXCLUSION_OPTIONS]
    p["crypto"] = raw.get("crypto") if raw.get("crypto") in CRYPTO_PREFS else "limited"
    holdings = []
    for h in raw.get("holdings", []):
        ticker = str(h.get("ticker", "")).upper()
        if ticker not in known_tickers:
            raise ProfileError(f"unknown ticker in holdings: {ticker!r}")
        amount = float(h.get("amount", 0))
        if amount < 0:
            raise ProfileError("holding amounts must be positive")
        if amount > 0:
            holdings.append({"ticker": ticker, "amount": round(amount, 2)})
    p["holdings"] = holdings
    caps_override = raw.get("caps_override") or {}
    p["caps_override"] = {
        k: float(v) for k, v in caps_override.items()
        if k in DEFAULT_CAPS["moderate"] and isinstance(v, (int, float)) and 0 <= v <= 1
    }
    settings = raw.get("settings") or {}
    threshold = settings.get("alert_threshold", 4)
    p["settings"] = {
        "alert_threshold": int(threshold) if isinstance(threshold, (int, float)) and 1 <= threshold <= 5 else 4,
        "email_alerts": bool(settings.get("email_alerts", False)),
        "email": str(settings.get("email") or "")[:200] or None,
    }
    return p


def with_risk_override(profile: dict, risk_level: str | None) -> dict:
    """Copy of the profile with a what-if risk level (sensitivity toggle)."""
    if not risk_level or risk_level == profile["risk_level"]:
        return profile
    if risk_level not in RISK_LEVELS:
        raise ProfileError(f"risk_level must be one of {list(RISK_LEVELS)}")
    p = copy.deepcopy(profile)
    p["risk_level"] = risk_level
    return p


# ----- Persona presets (fictional, from the challenge brief) --------------
PRESETS: dict[str, dict[str, Any]] = {
    "amira": {
        "display_name": "Amira",
        "age": 26,
        "risk_level": "high",
        "horizon_years": 15,
        "objective": "Grow capital over the long term; interested in AI, emerging tech and crypto, but wants some diversification.",
        "interests": ["ai", "emerging_tech", "growth", "crypto"],
        "avoid_sectors": [],
        "esg_exclusions": [],
        "crypto": "open",
        # Fictional starting portfolio, deliberately concentrated so the radar shows gaps.
        "holdings": [
            {"ticker": "NVDA", "amount": 12000}, {"ticker": "QQQ", "amount": 10000},
            {"ticker": "TSLA", "amount": 6000}, {"ticker": "BTC-USD", "amount": 8000},
            {"ticker": "SHOP.TO", "amount": 4000},
        ],
        "brief_test": "High risk does not mean anything goes: keep diversification and explain choices.",
    },
    "mateo": {
        "display_name": "Mateo",
        "age": 41,
        "risk_level": "moderate",
        "horizon_years": 10,
        "objective": "Balanced growth over the medium to long term; strong ESG preferences; little interest in speculation.",
        "interests": ["esg", "broad_market", "dividend"],
        "avoid_sectors": [],
        "esg_exclusions": ["fossil_fuels"],
        "crypto": "avoid",
        "holdings": [
            {"ticker": "SPY", "amount": 45000}, {"ticker": "XIU.TO", "amount": 30000},
            {"ticker": "ENB.TO", "amount": 20000}, {"ticker": "XOM", "amount": 20000},
            {"ticker": "MSFT", "amount": 15000}, {"ticker": "XBB.TO", "amount": 20000},
        ],
        "brief_test": "Integrate non-financial criteria (ESG), not only return and risk.",
    },
    "robert": {
        "display_name": "Robert",
        "age": 59,
        "risk_level": "low_moderate",
        "horizon_years": 4,
        "objective": "Preserve capital with some growth before retirement; prefers established, understandable companies.",
        "interests": ["established", "dividend", "defensive"],
        "avoid_sectors": [],
        "esg_exclusions": [],
        "crypto": "limited",
        "holdings": [
            {"ticker": "XBB.TO", "amount": 180000}, {"ticker": "XIU.TO", "amount": 150000},
            {"ticker": "RY.TO", "amount": 60000}, {"ticker": "FTS.TO", "amount": 60000},
            {"ticker": "ZST.TO", "amount": 90000}, {"ticker": "JNJ", "amount": 60000},
        ],
        "brief_test": "Challenge a request that conflicts with the profile (e.g. a large position in a highly publicized stock).",
        "demo_question": "Should I put 30% of my portfolio in Tesla?",
    },
}


def preset_profile(preset_id: str, known_tickers: set[str]) -> dict:
    if preset_id not in PRESETS:
        raise ProfileError(f"unknown preset {preset_id!r}")
    raw = copy.deepcopy(PRESETS[preset_id])
    raw["preset_id"] = preset_id
    return validate_profile(raw, known_tickers)
