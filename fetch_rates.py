"""
Builds rates.json for Remindr!

Two sources, merged:
  1. ExchangeRate-API  -> full baseline of ~160 currencies, refreshed at most once
                          every 24h (keeps us well inside the 1,500 req/month free tier).
  2. Published Google   -> hourly GOOGLEFINANCE values for a subset of currencies.
     Sheet CSV             Overrides the baseline when the value is present and sane.

The sheet wins when it has a usable number, because it's fresher. The API baseline
fills every gap, so a currency that's N/A in the sheet still has a working rate.
"""

import csv
import io
import json
import os
import sys
from datetime import datetime, timedelta, timezone

import requests

API_KEY = os.environ["EXCHANGE_API_KEY"]
SHEET_CSV_URL = os.environ.get("SHEET_CSV_URL", "").strip()

BASE = "USD"
OUT_FILE = "rates.json"

# Re-pull the API baseline only if the cached one is older than this.
API_MAX_AGE = timedelta(hours=24)

# Reject a sheet value if it differs from the API baseline by more than this.
# GOOGLEFINANCE occasionally returns stale or malformed values; without this guard
# one bad cell would silently ship a wrong rate to every user.
SANITY_TOLERANCE = 0.25  # 25%

# Things GOOGLEFINANCE puts in a cell when it has nothing useful.
BAD_VALUES = {"", "#n/a", "n/a", "na", "#value!", "#ref!", "#error!", "loading...", "-"}


def load_existing():
    """Previous rates.json, if any. Used to avoid re-hitting the API every run."""
    try:
        with open(OUT_FILE) as f:
            return json.load(f)
    except (FileNotFoundError, json.JSONDecodeError):
        return None


def api_baseline_is_fresh(existing):
    if not existing:
        return False
    stamp = existing.get("api_updated")
    if not stamp:
        return False
    try:
        fetched = datetime.fromisoformat(stamp)
    except ValueError:
        return False
    if fetched.tzinfo is None:
        fetched = fetched.replace(tzinfo=timezone.utc)
    return datetime.now(timezone.utc) - fetched < API_MAX_AGE


def fetch_api_rates():
    url = f"https://v6.exchangerate-api.com/v6/{API_KEY}/latest/{BASE}"
    r = requests.get(url, timeout=30)
    r.raise_for_status()
    data = r.json()
    if data.get("result") != "success":
        raise RuntimeError(f"ExchangeRate-API error: {data.get('error-type')}")
    return {k.upper(): float(v) for k, v in data["conversion_rates"].items()}


def fetch_sheet_rates():
    """
    Parse the published sheet CSV.

    Actual layout (confirmed against the live sheet):
      - A few metadata rows first (last_updated, main_currency), then a blank
        row, then the real header: CURRENCY, CURRENCY_CODE, COUNTRY,
        COUNTRY_CODE, USD_VALUE, ... (USD_VALUE repeats in a later column).
      - USD_VALUE is "1 unit of this currency, in USD" - the INVERSE of what
        rates.json stores (which is "1 USD, in this currency"). Every sheet
        value is inverted (1 / value) before use.

    Columns are located by header name rather than fixed position, so this
    keeps working if the sheet ever gains/reorders a column.
    Anything unparseable is skipped rather than failing the run.
    """
    if not SHEET_CSV_URL:
        print("No SHEET_CSV_URL set - skipping sheet source.")
        return {}

    try:
        r = requests.get(SHEET_CSV_URL, timeout=30)
        r.raise_for_status()
    except requests.RequestException as e:
        # A sheet outage must never break the build; we just fall back to the API.
        print(f"WARNING: could not fetch sheet ({e}) - using API baseline only.")
        return {}

    rows = list(csv.reader(io.StringIO(r.text)))

    # Find the real header row (the one containing CURRENCY_CODE), skipping
    # the last_updated / main_currency / blank rows above it.
    header_idx = None
    for i, row in enumerate(rows):
        if any(cell.strip().upper() == "CURRENCY_CODE" for cell in row):
            header_idx = i
            break

    if header_idx is None:
        print("WARNING: couldn't find a CURRENCY_CODE header in the sheet - skipping sheet source.")
        return {}

    header = [cell.strip().upper() for cell in rows[header_idx]]
    try:
        code_col = header.index("CURRENCY_CODE")
        value_col = header.index("USD_VALUE")  # first occurrence, if duplicated
    except ValueError:
        print("WARNING: expected columns missing from sheet header - skipping sheet source.")
        return {}

    out = {}
    for row in rows[header_idx + 1:]:
        if len(row) <= max(code_col, value_col):
            continue

        code = row[code_col].strip().upper()
        raw = row[value_col].strip()

        if len(code) != 3 or not code.isalpha():
            continue
        if raw.lower() in BAD_VALUES:
            continue

        try:
            value_in_usd = float(raw.replace(",", ""))
        except ValueError:
            continue
        if value_in_usd <= 0:
            continue

        # Invert: sheet gives "1 CODE = X USD", we need "1 USD = X CODE".
        out[code] = 1.0 / value_in_usd

    return out


def merge(api_rates, sheet_rates, prev_sources=None):
    """
    API/baseline rates, with sane sheet values layered on top.
    Entries not touched by this run's sheet keep whatever source label they
    already had (so the output honestly reflects "sheet, 3 runs ago" style
    provenance rather than being relabeled "api" just because it was reused).
    """
    rates = dict(api_rates)
    sources = dict(prev_sources) if prev_sources else {}
    applied = rejected = 0

    for code, sheet_value in sheet_rates.items():
        baseline = api_rates.get(code)

        if baseline is None:
            # Currency the baseline doesn't cover at all - nothing to sanity check
            # against, so take the sheet value as-is.
            rates[code] = sheet_value
            sources[code] = "sheet"
            applied += 1
            continue

        drift = abs(sheet_value - baseline) / baseline
        if drift > SANITY_TOLERANCE:
            print(
                f"  rejected {code}: sheet={sheet_value} vs baseline={baseline} "
                f"({drift:.1%} drift, over {SANITY_TOLERANCE:.0%} tolerance)"
            )
            rejected += 1
            continue

        rates[code] = sheet_value
        sources[code] = "sheet"
        applied += 1

    for code in rates:
        sources.setdefault(code, "api")

    print(f"Sheet values applied: {applied}, rejected: {rejected}")
    return rates, sources


def main():
    now = datetime.now(timezone.utc)
    existing = load_existing()

    if api_baseline_is_fresh(existing):
        # Reuse the full previous result as this run's baseline - not just the
        # currencies that came from the API last time. Treating it as "last
        # known good" (whatever its source) means a currency that goes
        # temporarily N/A in the sheet falls back to its last real value
        # instead of disappearing from the output entirely.
        print("API baseline still fresh (<24h) - reusing last known values.")
        baseline = {k.upper(): float(v) for k, v in existing["rates"].items()}
        prev_sources = existing.get("sources", {})
        api_updated = existing["api_updated"]
    else:
        print("Fetching fresh API baseline.")
        baseline = fetch_api_rates()
        prev_sources = {k: "api" for k in baseline}
        api_updated = now.isoformat()

    sheet_rates = fetch_sheet_rates()
    print(f"Sheet returned {len(sheet_rates)} usable value(s).")

    rates, sources = merge(baseline, sheet_rates, prev_sources)

    if len(rates) < 100:
        # Something went badly wrong upstream; don't commit a gutted file.
        print(f"ERROR: only {len(rates)} rates resolved - refusing to write.")
        sys.exit(1)

    payload = {
        "base": BASE,
        "rates": dict(sorted(rates.items())),
        "updated": now.isoformat(),
        "api_updated": api_updated,
        "sheet_updated": now.isoformat() if sheet_rates else None,
        "sources": dict(sorted(sources.items())),
    }

    with open(OUT_FILE, "w") as f:
        json.dump(payload, f, indent=2)
        f.write("\n")

    print(f"Wrote {len(rates)} rates to {OUT_FILE}.")


if __name__ == "__main__":
    main()
