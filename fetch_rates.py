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

    Returns the RAW USD_VALUE per currency code, un-inverted. Direction
    (whether a given row needs inverting to match rates.json's convention)
    is auto-detected per-currency in merge(), against the API baseline -
    see the note there for why a single fixed direction doesn't work here.
    """
    if not SHEET_CSV_URL:
        print("No SHEET_CSV_URL set - skipping sheet source.")
        return {}

    try:
        r = requests.get(
            SHEET_CSV_URL,
            timeout=30,
            headers={"User-Agent": "Mozilla/5.0 (compatible; RemindrRatesBot/1.0)"},
        )
        r.raise_for_status()
    except requests.RequestException as e:
        # A sheet outage must never break the build; we just fall back to the API.
        print(f"WARNING: could not fetch sheet ({e}) - using API baseline only.")
        return {}

    print(f"Sheet fetch: HTTP {r.status_code}, {len(r.text)} bytes, "
          f"content-type={r.headers.get('content-type')}")
    if "CURRENCY_CODE" not in r.text.upper():
        print("WARNING: response doesn't look like the expected CSV. First 200 chars:")
        print(repr(r.text[:200]))

    rows = list(csv.reader(io.StringIO(r.text)))

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
        value_col = header.index("USD_VALUE")
    except ValueError:
        print("WARNING: expected columns missing from sheet header - skipping sheet source.")
        return {}

    out = {}
    skipped_bad_code = []
    skipped_no_value = []
    skipped_unparseable = []
    total_data_rows = 0

    for row in rows[header_idx + 1:]:
        if len(row) <= max(code_col, value_col):
            continue

        code = row[code_col].strip().upper()
        raw = row[value_col].strip()

        if not code:
            continue  # trailing blank row at the end of the sheet, not real data

        total_data_rows += 1

        if len(code) != 3 or not code.isalpha():
            skipped_bad_code.append(code or "(blank)")
            continue
        if raw.lower() in BAD_VALUES:
            skipped_no_value.append(code)
            continue

        try:
            value = float(raw.replace(",", ""))
        except ValueError:
            skipped_unparseable.append(f"{code}={raw!r}")
            continue
        if value <= 0:
            skipped_unparseable.append(f"{code}={raw!r} (non-positive)")
            continue

        out[code] = value  # raw, NOT inverted - direction decided in merge()

    print(f"Sheet: {total_data_rows} data row(s), {len(out)} usable.")
    if skipped_bad_code:
        print(f"  {len(skipped_bad_code)} skipped (not a 3-letter code): {skipped_bad_code}")
    if skipped_no_value:
        print(f"  {len(skipped_no_value)} skipped (N/A or blank value): {skipped_no_value}")
    if skipped_unparseable:
        print(f"  {len(skipped_unparseable)} skipped (unparseable value): {skipped_unparseable}")

    return out


def merge(api_rates, sheet_rates_raw, prev_sources=None):
    """
    API/baseline rates, with sane sheet values layered on top.

    The sheet is NOT internally consistent about direction: some rows give
    "1 unit of currency, in USD" (needs inverting to match rates.json), and
    others already give "1 USD, in that currency" (matches as-is) - almost
    certainly because the underlying GOOGLEFINANCE formulas were set up in
    different directions for different rows. A single fixed rule (always
    invert, or never invert) is wrong for a large fraction of currencies
    either way.

    So for each currency, both interpretations (raw value, and its
    reciprocal) are checked against the API baseline, and whichever one
    actually lands close to a trusted reference is used. A currency is only
    rejected if NEITHER direction is plausible.
    """
    rates = dict(api_rates)
    sources = dict(prev_sources) if prev_sources else {}
    applied = rejected = 0

    for code, raw_value in sheet_rates_raw.items():
        baseline = api_rates.get(code)

        if baseline is None:
            # No baseline to check direction against. Most of the sheet's
            # rows turned out to already match rates.json's own convention
            # directly (see the run that surfaced this), so that's the
            # default here - but there's no way to be sure for a currency
            # the API doesn't cover at all.
            rates[code] = raw_value
            sources[code] = "sheet"
            applied += 1
            continue

        drift_as_is = abs(raw_value - baseline) / baseline
        drift_inverted = abs((1.0 / raw_value) - baseline) / baseline if raw_value else float("inf")

        if drift_as_is <= drift_inverted and drift_as_is <= SANITY_TOLERANCE:
            rates[code] = raw_value
            sources[code] = "sheet"
            applied += 1
        elif drift_inverted < drift_as_is and drift_inverted <= SANITY_TOLERANCE:
            rates[code] = 1.0 / raw_value
            sources[code] = "sheet"
            applied += 1
        else:
            best_drift = min(drift_as_is, drift_inverted)
            print(
                f"  rejected {code}: sheet={raw_value} (as-is drift {drift_as_is:.1%}, "
                f"inverted drift {drift_inverted:.1%}) vs baseline={baseline} "
                f"- neither within {SANITY_TOLERANCE:.0%} tolerance"
            )
            rejected += 1

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
    sheet_count = sum(1 for v in sources.values() if v == "sheet")
    api_count = sum(1 for v in sources.values() if v == "api")
    print(f"Source breakdown: {sheet_count} from sheet, {api_count} from api.")


if __name__ == "__main__":
    main()
