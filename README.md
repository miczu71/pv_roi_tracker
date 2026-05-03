# PV ROI Tracker — Home Assistant Add-on

Tracks the return-on-investment of a residential photovoltaic system and publishes 12 sensors to Home Assistant via MQTT discovery.

## What it does

| Stage | What happens |
|---|---|
| **First start** | Fetches the historic Google Sheets CSV once, parses the Polish-format pivot table, and writes every month from 2023-06 to the previous month into `/data/historic.json`. Never fetches the CSV again. |
| **Every 30 min** | Reads six live HA sensors for the current month, runs the ROI calculation, publishes 12 MQTT sensors. |
| **11th–20th of each month, 20:00 UTC** | Scrapes last month's RCEm feed-in price from the PSE website, back-fills `feedin_price_pln_kwh` and `feedin_revenue_pln` in `historic.json`, triggers an immediate recompute. |
| **Last day of each month, 23:55 local** | Snapshots the current month into `historic.json` before utility meters reset at midnight. |

## ROI formula

Matches the source spreadsheet exactly:

```
status  = dofinansowanie + oszczędności
       = subsidy + (self_consumption_savings + feedin_revenue)
ROI %  = status / gross_investment × 100
```

With the default constants (gross = 51 900 zł, subsidy = 28 714 zł) the result matches the spreadsheet's **80.94 %** for data through 2026-04.

Payback remaining:
```
remaining = max(0, gross_investment − status)
months    = remaining / monthly_avg_savings
```

## Sensors published (MQTT discovery)

All 12 sensors appear under one device **PV ROI Tracker** in HA Settings → Devices.

| Entity | Friendly name | Unit | Notes |
|---|---|---|---|
| `sensor.pv_roi_pct` | PV ROI | % | (subsidy + savings) / gross × 100 |
| `sensor.pv_payback_years` | PV Payback Remaining | years | |
| `sensor.pv_payback_date` | PV Payback Date | — | ISO date |
| `sensor.pv_total_savings` | PV Total Savings | PLN | self-consumption + feed-in |
| `sensor.pv_self_consumption_savings` | PV Self-Consumption Savings | PLN | |
| `sensor.pv_feedin_revenue` | PV Feed-in Revenue | PLN | |
| `sensor.pv_net_investment` | PV Net Investment | PLN | gross − subsidy |
| `sensor.pv_monthly_avg_savings` | PV Monthly Avg Savings | PLN | |
| `sensor.pv_total_produced_kwh` | PV Total Produced | kWh | |
| `sensor.pv_total_exported_kwh` | PV Total Exported | kWh | |
| `sensor.pv_specific_yield` | PV Specific Yield | kWh/kWp | lifetime |
| `sensor.pv_rcem_current_month` | RCEm Current Month | PLN/kWh | scraped from PSE |

## Live HA sensor mapping (current month)

| Field | HA entity |
|---|---|
| `produced_kwh` | `sensor.inverter_yield_monthly` |
| `exported_kwh` | `sensor.power_meter_exported_energy_monthly` |
| `purchased_kwh` | `sensor.monthly_energy_peak` + `sensor.monthly_energy_offpeak` |
| `consumed_kwh` | `sensor.house_consumption_energy_monthly` |
| `buy_price_pln_kwh` | `sensor.srednia_cena_energii_w_miesiacu` |
| `feedin_price_pln_kwh` | scraped from PSE (RCEm) |

Derived fields (`self_consumed_kwh`, `self_consumed_savings_pln`, `purchase_cost_pln`, `feedin_revenue_pln`, `specific_yield`) are calculated inside the add-on.

## Installation

### Option A — local add-on (recommended)

1. In HA go to **Settings → Add-ons → Add-on store** → ⋮ → **Add local add-on repository**.
2. Enter the path to the `addons/` directory that contains this folder (e.g. `/config/addons`).
3. Find **PV ROI Tracker** in the store and click **Install**.
4. Configure options (see below), then click **Start**.
5. Watch the log — the first start imports the CSV. Within 60 s all 12 sensors should appear in **Developer Tools → States**.

### Option B — from this GitHub repo

Add `https://github.com/miczu71/pv_roi_tracker` as a local add-on store URL (HA does not enforce HACS structure for local stores).

## Configuration options

| Option | Default | Description |
|---|---|---|
| `csv_url` | Google Sheets URL | Published CSV of the historic pivot table |
| `force_reimport` | `false` | Set `true` once to re-fetch and overwrite `historic.json` |
| `gross_investment` | `51900.0` | Total project cost before subsidy (zł) |
| `subsidy` | `28714.0` | One-time government grant received (zł) |
| `system_kwp` | `6.72` | Installed peak power for specific-yield calculation |
| `poll_interval_minutes` | `30` | How often to recompute and publish |
| `mqtt_host` | `core-mosquitto` | MQTT broker hostname |
| `mqtt_port` | `1883` | MQTT broker port |
| `mqtt_user` | `` | MQTT username (leave blank if no auth) |
| `mqtt_password` | `` | MQTT password |
| `log_level` | `info` | `debug` / `info` / `warning` / `error` |

## Data files

| File | Description |
|---|---|
| `/data/historic.json` | Frozen monthly records (written once from CSV, appended monthly by month-close job) |
| `/data/rcem_history.json` | RCEm prices keyed `YYYY-MM` in PLN/kWh, last 36 months |
| `/data/historic.json.bak` | Automatic backup before any overwrite |

## Architecture

```
First start
  CSV (Google Sheets) → parser → historic.json

Every 30 min
  historic.json  ┐
                 ├→ concat → ROI engine → MQTT publisher → HA sensors
  live HA reader ┘

23:55 last day of month
  live HA reader → historic.json (month-close snapshot)

11th–20th at 20:00 UTC
  PSE website → rcem_history.json → backfill historic.json → recompute
```

## Requirements

- Home Assistant with the **Mosquitto broker** add-on (or any MQTT broker)
- MQTT integration enabled in HA
- The six live sensor entities listed above must exist and be non-zero during the current month
- Architecture: `aarch64`, `amd64`, or `armv7`

## Development

```bash
cd addons/pv_roi_tracker
pip install -r requirements.txt pytest
python -m pytest tests/ -v
```

44 tests covering the CSV parser, ROI engine, historic store, and concatenator.
