"""Pay conversion (spec §2.1): nominal exchange rates for remote-paid-from-abroad roles, PPP for relocation.

Factors come from the World Bank (PA.NUS.PPP: local currency per international $; PA.NUS.FCRF: local
currency per US$, annual average), refreshed yearly. Built-in values cover the gap until the first
refresh succeeds — approximate, labelled as such.
"""

import logging
from datetime import datetime, timezone

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.core.http import PoliteClient
from app.models.assessment import ExchangeFactor

log = logging.getLogger(__name__)

CURRENCY_COUNTRY = {"INR": "IND", "USD": "USA", "GBP": "GBR", "EUR": "DEU", "SGD": "SGP", "AED": "ARE",
                    "CAD": "CAN", "AUD": "AUS"}
# (PPP LCU per intl $, FX LCU per US$) — approximate recent values, used only until a World Bank refresh.
BUILTIN = {"INR": (20.4, 85.0), "USD": (1.0, 1.0), "GBP": (0.67, 0.78), "EUR": (0.72, 0.92),
           "SGD": (0.83, 1.34), "AED": (2.24, 3.6725), "CAD": (1.18, 1.37), "AUD": (1.42, 1.53)}
PERIOD_TO_YEAR = {"year": 1, "month": 12, "hour": 2080}

WB_URL = "https://api.worldbank.org/v2/country/{countries}/indicator/{indicator}"


def factors(session: Session) -> dict[str, tuple[float, float, str]]:
    """currency → (ppp, fx, source)."""
    stored = {f.currency: (f.ppp_lcu_per_intl, f.fx_lcu_per_usd, f"{f.source} {f.year}")
              for f in session.scalars(select(ExchangeFactor))}
    return {cur: stored.get(cur, (*BUILTIN[cur], "builtin (approximate)")) for cur in BUILTIN}


def to_inr(amount: float, currency: str, *, basis: str, table: dict) -> float | None:
    """Annual amount in `currency` → INR, either 'nominal' (exchange rate) or 'ppp' (purchasing power)."""
    if currency == "INR":
        return amount
    if currency not in table or "INR" not in table:
        return None
    ppp, fx, _ = table[currency]
    inr_ppp, inr_fx, _ = table["INR"]
    if basis == "ppp":
        return amount / ppp * inr_ppp
    return amount / fx * inr_fx


def refresh_from_world_bank(session: Session, http: PoliteClient) -> dict:
    """Fetch the latest PPP and exchange-rate values per currency; keep the previous ones on failure."""
    countries = ";".join(CURRENCY_COUNTRY.values())
    latest: dict[str, dict[str, tuple[int, float]]] = {}
    for indicator in ("PA.NUS.PPP", "PA.NUS.FCRF"):
        resp = http.get(WB_URL.format(countries=countries, indicator=indicator),
                        params={"format": "json", "mrnev": 1, "per_page": 100})
        resp.raise_for_status()
        body = resp.json()
        for row in (body[1] if isinstance(body, list) and len(body) > 1 and body[1] else []):
            iso3, value = row.get("countryiso3code"), row.get("value")
            if iso3 and value:
                latest.setdefault(iso3, {})[indicator] = (int(row.get("date") or 0), float(value))

    updated = []
    now = datetime.now(timezone.utc)
    for currency, iso3 in CURRENCY_COUNTRY.items():
        got = latest.get(iso3, {})
        if "PA.NUS.PPP" not in got:
            continue
        ppp_year, ppp = got["PA.NUS.PPP"]
        # Euro-area members report no national exchange rate; the US is 1 by definition.
        fx = got.get("PA.NUS.FCRF", (ppp_year, BUILTIN[currency][1]))[1] if currency != "USD" else 1.0
        row = session.get(ExchangeFactor, currency) or ExchangeFactor(currency=currency)
        row.country_iso3, row.ppp_lcu_per_intl, row.fx_lcu_per_usd = iso3, ppp, fx
        row.year, row.source, row.fetched_at = ppp_year, "worldbank", now
        session.add(row)
        updated.append(currency)
    return {"updated": updated}
