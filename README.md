# PV ROI Tracker — Home Assistant Add-on

Tracks the return-on-investment of a residential photovoltaic system (Polish net-billing market), publishes 30 sensors to Home Assistant via MQTT discovery and serves a mobile-friendly ingress dashboard with ROI history, payback forecast (fan chart with P10–P90 band), Tauron invoice reconciliation, prosumer-deposit tracking with an invoices-vs-inverter reconciliation table, tariff analysis, degradation tracking, and an RCEm-vs-hourly-RCE settlement simulation with an export×price heatmap.

## What it does

| Stage | What happens |
|---|---|
| **First start** | Fetches the historic Google Sheets CSV once, parses the Polish-format pivot table, and writes every month from 2023-06 into `/data/historic.json`. Never fetches the CSV again. |
| **Every 30 min** | Reads live HA sensors for the current month, runs the ROI calculation, publishes MQTT sensors, refreshes the tariff comparison and the RCE-hourly simulation, publishes the health sensor. |
| **11th–20th of each month, every 2 h 08–22 local** | Scrapes last month's RCEm feed-in price from the PSE website, back-fills `historic.json`, recomputes. |
| **1st of each month, 06:00 UTC** | RCEm correction scan (PSE may amend prices up to 12 months back). |
| **Last day of each month, 23:55 local** | Snapshots the current month into `historic.json` before utility meters reset; sends a Polish summary push via `notify.family` (optional, `monthly_notify`). |
| **16th of each month, 12:00 UTC** | Refreshes Polish CPI from GUS (inflation-adjusted ROI). |
| **Daily, 02:00 UTC** | Backs up all `/data` files to `/share/pv_roi_tracker`. |

## Web UI (ingress)

Tabs: **Historia miesięczna** · **Prognoza spłaty** (wachlarz spłaty P10–P90) · **Podsumowanie roczne** (z kolumnami r/r) · **Wykresy** (m.in. waterfall miesięczny, Sankey przepływu energii, oszczędności nominalne vs realne CPI, trend degradacji kWh/kWp, ranking produkcji miesięcznej z kolorem per rok i medalami top 3) · **Faktury** (upload PDF Tauron z trwałym przechowywaniem oryginałów, trening parsera, składniki kosztów faktur, depozyt prosumencki z prognozą przedawnienia, rekonsyliacja faktury vs falownik) · **Analiza taryf** (G12w vs dynamiczna) · **RCE vs RCEm** (symulacja rozliczenia godzinowego + heatmapa eksport × cena). Nagłówek pokazuje wersję add-onu. The layout is responsive — tabs scroll horizontally and tables keep a sticky first column on phones.

### Faktury — składniki kosztów i trwałe PDF-y

Parser wyciąga z faktury Tauron nie tylko stawki jednostkowe, ale też realne kwoty złotówkowe ("wartość netto") dla energii, składnika zmiennego sieciowego, jakościowej, OZE i kogeneracji, oraz opłatę przejściową/handlową i akcyzę, jeśli występują. Zakładka Faktury pokazuje **rozbicie kosztów** — tabelę sum per składnik (z % udziału) i wykres słupkowy skumulowany per miesiąc, z przełącznikiem **Netto / Brutto**. Faktury przetworzone przed wprowadzeniem realnych kwot (lub bez odnalezionej kolumny wartości) dolicza się ze stawki × kWh — UI sygnalizuje to notatką.

Oryginalne wgrane pliki PDF są **trwale przechowywane** w `/data/pdfs/`, obok `invoices.json`. Każdy wiersz faktury ma przyciski **PDF** (podgląd oryginału) i **↻ PDF** (przeliczenie faktury ponownie z zapisanego pliku — przydatne po poprawce parsera, bez ponownego wgrywania).

### RCE vs RCEm

Simulates what feed-in revenue would have been under hourly RCE settlement instead of monthly RCEm: hourly export energy (HA long-term statistics) × 15-min RCE prices. Today's prices come from the `rce_pse` HA integration (`prices` attribute); historic months are fetched once from the official PSE REST API (`api.raporty.pse.pl/api/rce-pln`) and cached in `/data/rce_hourly.json`. Settled months are frozen. Gross ×1.23 VAT applies from 2025-02 (same rule as RCEm). **Negative RCE prices are replaced with 0 zł** in the simulation (art. 4b ustawy o OZE — the statutory default for prosumers); per-month columns show export volume in negative-price hours and how much the zero-floor rule protects. A 24h×month heatmap shows when you export vs when prices are high or negative. Produces a ROZWAŻ RCE / ZOSTAŃ PRZY RCEm / NEUTRALNA recommendation after ≥3 settled months.

### Depozyt prosumencki (12-month expiry)

A pure FIFO ledger (`deposit.py`) models the prosumer deposit: monthly accruals (= feed-in revenue), consumption (from invoices, or inverter estimate), the statutory **12-month expiry** of each month's accrual, and the refund cap on unused funds (`deposit_refund_pct`: 20% under RCEm, 30% under hourly RCE). The current-balance estimate anchors on the **post-invoice balance** of the latest invoice (`previous − used` — Tauron typically consumes the whole deposit on each invoice) plus inverter-computed accruals for export months the utility has not posted yet (posting lag auto-detected from the invoice chain, 1–3 months, default 2).

The Faktury tab additionally shows a **reconciliation table — invoices vs inverter**: implied utility accruals reconstructed from the invoice balance chain (`implied(M) = previous(M) − post-invoice balance(M−1)`), shifted by the detected posting lag and compared month-by-month with the model (export × RCEm ×1.23), with the difference in PLN and % — raw values, no calibration. KPI cards show both totals, the cumulative difference and the detected lag; two sensors expose the balance and the next-month expiry for automations.

## ROI formula

Matches the source spreadsheet exactly:

```
status = dofinansowanie + oszczędności
       = subsidy + (self_consumption_savings + feedin_revenue + battery_arbitrage)
ROI %  = status / gross_investment × 100
```

Payback remaining:
```
remaining = max(0, gross_investment − status)
months    = remaining / monthly_avg_savings
```

## Sensors published (MQTT discovery)

All sensors appear under one device **PV ROI Tracker** in HA Settings → Devices.

| Unique id | Friendly name | Unit | Notes |
|---|---|---|---|
| `pv_roi_tracker_roi_pct` | PV ROI | % | (subsidy + savings) / gross × 100 |
| `pv_roi_tracker_payback_years` | PV Payback Remaining | years | |
| `pv_roi_tracker_payback_date` | PV Payback Date | — | ISO date |
| `pv_roi_tracker_total_savings` | PV Total Savings | PLN | self-consumption + feed-in + arbitrage |
| `pv_roi_tracker_self_consumption_savings` | PV Self-Consumption Savings | PLN | |
| `pv_roi_tracker_feedin_revenue` | PV Feed-in Revenue | PLN | |
| `pv_roi_tracker_net_investment` | PV Net Investment | PLN | gross − subsidy |
| `pv_roi_tracker_monthly_avg_savings` | PV Monthly Avg Savings | PLN | |
| `pv_roi_tracker_total_produced_kwh` | PV Total Produced | kWh | |
| `pv_roi_tracker_total_exported_kwh` | PV Total Exported | kWh | |
| `pv_roi_tracker_specific_yield` | PV Specific Yield | kWh/kWp | lifetime |
| `pv_roi_tracker_battery_arbitrage_savings` | PV Battery Arbitrage Savings | PLN | grid-charged off-peak |
| `pv_roi_tracker_net_profit` | PV Net Profit | PLN | above gross investment |
| `pv_roi_tracker_current_month_savings` | PV Savings This Month | PLN | |
| `pv_roi_tracker_rcem_scrape_status` | RCEm Scrape Status | — | ok / pending / retrying / error |
| `pv_roi_tracker_projected_month_kwh` | PV Projected Month kWh | kWh | Solcast-based |
| `pv_roi_tracker_projected_month_savings` | PV Projected Month Savings | PLN | Solcast × historic zł/kWh |
| `pv_roi_tracker_real_total_savings` | PV Real Total Savings | PLN | CPI-deflated |
| `pv_roi_tracker_real_roi_pct` | PV Real ROI | % | CPI-deflated |
| `pv_roi_tracker_npv` | PV NPV | PLN | at `discount_rate_real` |
| `pv_roi_tracker_irr_pct` | PV IRR | % | |
| `pv_roi_tracker_vs_bond_delta` | PV vs Bond Delta | PLN | vs `comparison_yield_rate` |
| `pv_roi_tracker_cumulative_inflation` | PV Cumulative Inflation | % | GUS CPI |
| `pv_roi_tracker_self_consumption_rate` | PV Self-Consumption Rate | % | Σ self-consumed / Σ produced |
| `pv_roi_tracker_autarky` | PV Autarky | % | Σ self-consumed / Σ consumed |
| `pv_roi_tracker_co2_avoided` | PV CO2 Avoided | kg | Σ produced × `co2_factor_kg_kwh` (KOBiZE) |
| `pv_roi_tracker_yoy_yield_delta` | PV YoY Yield Delta | % | production year-over-year, paired months |
| `pv_roi_tracker_deposit_balance_est` | PV Deposit Balance Est | PLN | post-invoice balance + unposted inverter accruals |
| `pv_roi_tracker_deposit_expiring_30d` | PV Deposit Expiring 30d | PLN | deposit value hitting 12-month expiry next month |
| `pv_roi_tracker_health` | PV ROI Tracker Health | — | `ok`/`degraded`/`error`; JSON attributes per job + `solcast_available` |

## Live HA sensor mapping (current month)

| Field | HA entity |
|---|---|
| `produced_kwh` | `sensor.inverter_yield_monthly` |
| `exported_kwh` | `sensor.power_meter_exported_energy_monthly` (also hourly statistics for the RCE simulation) |
| `purchased_kwh` | `sensor.monthly_energy_peak` + `sensor.monthly_energy_offpeak` |
| `consumed_kwh` | `sensor.house_consumption_energy_monthly` |
| `buy_price_pln_kwh` | blended from tariff options; fallback `sensor.srednia_cena_energii_w_miesiacu` |
| `feedin_price_pln_kwh` | scraped from PSE (RCEm) |
| battery arbitrage | `sensor.battery_grid_charge_off_peak_monthly` (kWh) × (peak × efficiency − offpeak); fallback `sensor.battery_arbitrage_savings_monthly` |
| Solcast projection | `sensor.solcast_pv_forecast_*` (7 days) |
| RCE today | `sensor.rce_pse_price` (attribute `prices`, integracja `rce_pse`) |

Derived fields (`self_consumed_kwh`, `self_consumed_savings_pln`, `purchase_cost_pln`, `feedin_revenue_pln`, `specific_yield`) are calculated inside the add-on.

## Installation

### Option A — local add-on (recommended)

1. In HA go to **Settings → Add-ons → Add-on store** → ⋮ → **Add local add-on repository**.
2. Enter the path to the `addons/` directory that contains this folder (e.g. `/config/addons`).
3. Find **PV ROI Tracker** in the store and click **Install**.
4. Configure options (see below), then click **Start**.

### Option B — from this GitHub repo

Add `https://github.com/miczu71/pv_roi_tracker` in **Settings → Add-ons → Add-on store → ⋮ → Repositories**.

## Configuration options

| Option | Default | Description |
|---|---|---|
| `gross_investment` | `51900.0` | Total project cost before subsidy (zł) |
| `subsidy` | `28714.0` | One-time government grant received (zł) |
| `system_kwp` | `6.72` | Installed peak power for specific-yield calculation |
| `poll_interval_minutes` | `30` | How often to recompute and publish |
| `mqtt_host` | `core-mosquitto` | MQTT broker hostname |
| `mqtt_port` | `1883` | MQTT broker port |
| `mqtt_user` / `mqtt_password` | `` | MQTT credentials (blank = no auth) |
| `log_level` | `info` | `debug` / `info` / `warning` / `error` |
| `backup_share` | `/share/pv_roi_tracker` | Daily backup target |
| `discount_rate_real` | `0.04` | Real discount rate for NPV |
| `inflation_rate_assumption` | `0.05` | Fallback inflation when GUS CPI unavailable |
| `comparison_yield_rate` | `0.055` | Alternative-investment yield (bond comparison) |
| `tariff_peak_price` | `1.23` | G12w peak rate (PLN/kWh, gross) |
| `tariff_offpeak_price` | `0.63` | G12w off-peak rate (PLN/kWh, gross) |
| `battery_roundtrip_efficiency` | `0.92` | Battery round-trip efficiency for arbitrage savings |
| `monthly_notify` | `true` | Push a Polish month-close summary via `notify.family` |
| `co2_factor_kg_kwh` | `0.597` | Grid CO₂ emission factor for the avoided-emissions sensor (KOBiZE, end-user electricity) |
| `deposit_refund_pct` | `0.20` | Refund cap on expired deposit: `0.20` under RCEm, `0.30` under hourly RCE |

## Data files

| File | Description |
|---|---|
| `/data/historic.json` (+ `.bak`) | Frozen monthly records |
| `/data/rcem_history.json` | RCEm prices keyed `YYYY-MM` (PLN/kWh gross), last 60 months |
| `/data/rcem_corrections.json` | History of PSE price corrections |
| `/data/rce_hourly.json` | Hourly RCE price cache + frozen monthly RCE-vs-RCEm results |
| `/data/invoices.json` | Parsed Tauron invoices |
| `/data/invoice_layouts.json` | Learned invoice parser patterns |
| `/data/cpi_history.json` | GUS CPI chain index |

All files are backed up daily to `/share/pv_roi_tracker`.

## Architecture

```
First start
  CSV (Google Sheets) → parser → historic.json

Every 30 min
  historic.json   ┐
  live HA reader  ├→ concat → ROI engine → MQTT publisher → HA sensors
  rcem_history    ┘            ├→ tariff_analysis (HA statistics)
                               └→ rce_hourly (PSE API + rce_pse + HA statistics)

23:55 last day of month
  live HA reader → historic.json (month-close) → notify.family summary

Days 11–20 / 1st of month
  PSE website → rcem_history.json → backfill historic.json → recompute
```

## Requirements

- Home Assistant with the **Mosquitto broker** add-on (or any MQTT broker) and the MQTT integration
- The live sensor entities listed above; `rce_pse` integration for the RCE-hourly tab
- Architecture: `aarch64`, `amd64`, or `armv7`

## Development

```bash
cd pv_roi_tracker          # the add-on directory inside this repo
pip install -r requirements.txt pytest
python -m pytest -q
```

165 tests covering the CSV parser, ROI engine, historic store, concatenator, invoice parser/layouts and the RCE-hourly comparison.
