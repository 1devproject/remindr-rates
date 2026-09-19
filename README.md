# remindr-rates — sheet + API merge

## What changed

`rates.json` is now built from **two** sources instead of one:

| Source | Refresh | Role |
|---|---|---|
| ExchangeRate-API | once every 24h (its own data only changes this often) | Full baseline of ~160 currencies |
| Published Google Sheet (CSV) | hourly, checked at :20 past the hour | Overrides the baseline where it has a usable value |

The sheet wins when it has a valid number, because it's fresher. The API baseline
fills every gap, so a currency that's `#N/A` in the sheet still ships a working rate.

## Setup

Add a second repo secret alongside your existing `EXCHANGE_API_KEY`:

- **`SHEET_CSV_URL`** — your published sheet's **CSV** link.
  In Google Sheets: *File → Share → Publish to web → (select the sheet) → Comma-separated values (.csv)*.
  It looks like `https://docs.google.com/spreadsheets/d/e/2PACX-.../pub?gid=0&single=true&output=csv`

If `SHEET_CSV_URL` is unset, or the sheet is unreachable, the script logs a warning
and falls back to the API baseline alone — a sheet outage never breaks the build.

## Expected sheet layout

Confirmed against your live sheet. A few metadata rows first, then the real header:

```
last_updated,2026-09-19 11:00:25,...
main_currency,USD,...
,,,,,,,,
CURRENCY,CURRENCY_CODE,COUNTRY,COUNTRY_CODE,USD_VALUE,,,USD_VALUE,
Malaysian Ringgit,MYR,Malaysia,MY,0.24487597,,,0.24487597,TRUE
```

The parser locates the header by name (looks for `CURRENCY_CODE` and `USD_VALUE`),
not by fixed position — reordering or adding columns won't break it.

**Important: the sheet's `USD_VALUE` is inverted from what `rates.json` needs.**
Your sheet stores "1 unit of this currency, in USD" (e.g. MYR → `0.2449`). This
script stores "1 USD, in this currency" (e.g. MYR → `4.08`), matching
ExchangeRate-API's convention. Every sheet value is inverted (`1 / value`) before
it's used — this is already handled, just flagging it so it's not a mystery later
if you ever eyeball the numbers and they look "backwards" at a glance.

Rows are skipped (not fatal) when the code isn't 3 alpha characters, or the value is
blank, `N/A`, `Loading...`, non-numeric, or ≤ 0. The sheet's `TRUE` column is *not*
used as a validity signal — some rows are marked `TRUE` with an `N/A` value, so the
`USD_VALUE` cell itself is the only thing checked.

## Guards worth knowing about

- **Sanity check (`SANITY_TOLERANCE`, 25%):** a sheet value more than 25% away from the
  API baseline is rejected and logged. GOOGLEFINANCE occasionally returns stale or
  malformed numbers; without this, one bad cell would ship a wrong rate to every user.
  Loosen it if you track a genuinely volatile currency that trips it legitimately.
- **Minimum-rate floor:** if fewer than 100 rates resolve, the script exits non-zero
  rather than committing a gutted `rates.json`.
- **API call budget:** the workflow runs hourly, but the script only re-hits
  ExchangeRate-API when the cached baseline is older than 24h — roughly 30 calls/month
  against the 1,500/month free tier. Between real API fetches, the *entire* previous
  `rates.json` (not just the API-sourced part) is reused as this run's baseline. This
  matters for one specific case: if a currency comes from the sheet on one run, then
  temporarily shows `N/A` on a later run before the next real API fetch, it keeps its
  last known sheet value instead of disappearing from the output.

## Output shape

`rates` keeps the same key it always had, so no app-side change is required:

```json
{
  "base": "USD",
  "rates": { "EUR": 0.9162, "MYR": 4.7215, "...": 0 },
  "updated": "2026-09-19T05:20:00+00:00",
  "api_updated": "2026-09-19T00:20:00+00:00",
  "sheet_updated": "2026-09-19T05:20:00+00:00",
  "sources": { "EUR": "sheet", "MYR": "sheet", "JPY": "api" }
}
```

`sources` is additive and purely diagnostic — useful for spotting a sheet column that
has quietly stopped updating.

## Files

- `fetch_rates.py` — replaces your existing script
- `update-rates.yml` — replaces `.github/workflows/update-rates.yml`
