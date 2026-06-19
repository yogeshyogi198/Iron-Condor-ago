"""Implied Volatility calculator using Black-Scholes + Newton-Raphson."""
import math
from datetime import datetime

DAYS_IN_YEAR = 365.0
RISK_FREE_RATE = 0.065


def norm_cdf(x):
    return 0.5 * (1 + math.erf(x / math.sqrt(2)))


def norm_pdf(x):
    return math.exp(-x * x / 2) / math.sqrt(2 * math.pi)


def black_scholes_price(S, K, T, r, sigma, is_call):
    if sigma <= 0 or T <= 0:
        return 0
    d1 = (math.log(S / K) + (r + 0.5 * sigma * sigma) * T) / (sigma * math.sqrt(T))
    d2 = d1 - sigma * math.sqrt(T)
    if is_call:
        return S * norm_cdf(d1) - K * math.exp(-r * T) * norm_cdf(d2)
    else:
        return K * math.exp(-r * T) * norm_cdf(-d2) - S * norm_cdf(-d1)


def black_scholes_vega(S, K, T, r, sigma):
    if sigma <= 0 or T <= 0:
        return 0
    d1 = (math.log(S / K) + (r + 0.5 * sigma * sigma) * T) / (sigma * math.sqrt(T))
    return S * norm_pdf(d1) * math.sqrt(T)


def calculate_iv(market_price, spot, strike, expiry_date, is_call, r=RISK_FREE_RATE):
    now = datetime.now()
    if isinstance(expiry_date, str):
        expiry_date = datetime.strptime(expiry_date, "%Y-%m-%d")
    T = max((expiry_date - now).total_seconds() / (DAYS_IN_YEAR * 86400), 1 / DAYS_IN_YEAR)
    if market_price <= 0 or spot <= 0 or strike <= 0:
        return None

    intrinsic = max(spot - strike, 0) if is_call else max(strike - spot, 0)
    if market_price <= intrinsic:
        return 0.001

    sigma = 0.3
    for _ in range(100):
        price = black_scholes_price(spot, strike, T, r, sigma, is_call)
        vega = black_scholes_vega(spot, strike, T, r, sigma)
        diff = price - market_price
        if abs(diff) < 1e-6:
            break
        if abs(vega) < 1e-12:
            sigma += 0.01
            continue
        sigma = sigma - diff / vega
        sigma = max(sigma, 0.001)
        sigma = min(sigma, 5.0)

    return round(sigma * 100, 2)
