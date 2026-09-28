#!/bin/sh
set -e

CONFIG="/data/options.json"

export GROSS_INVESTMENT=$(jq -r '.gross_investment' "$CONFIG")
export SUBSIDY=$(jq -r '.subsidy' "$CONFIG")
export SYSTEM_KWP=$(jq -r '.system_kwp' "$CONFIG")
export POLL_INTERVAL_MINUTES=$(jq -r '.poll_interval_minutes' "$CONFIG")
export MQTT_HOST=$(jq -r '.mqtt_host' "$CONFIG")
export MQTT_PORT=$(jq -r '.mqtt_port' "$CONFIG")
export MQTT_USER=$(jq -r '.mqtt_user' "$CONFIG")
export MQTT_PASSWORD=$(jq -r '.mqtt_password' "$CONFIG")
export LOG_LEVEL=$(jq -r '.log_level' "$CONFIG")
export BACKUP_SHARE=$(jq -r '.backup_share // "/share/pv_roi_tracker"' "$CONFIG")
export DISCOUNT_RATE_REAL=$(jq -r '.discount_rate_real // "0.04"' "$CONFIG")
export INFLATION_RATE=$(jq -r '.inflation_rate_assumption // "0.05"' "$CONFIG")
export COMPARISON_YIELD_RATE=$(jq -r '.comparison_yield_rate // "0.055"' "$CONFIG")
export ASSET_LIFETIME_YEARS=$(jq -r '.asset_lifetime_years // "25.0"' "$CONFIG")
export PANEL_DEGRADATION_PCT_YEAR=$(jq -r '.panel_degradation_pct_year // "0.5"' "$CONFIG")
export BATTERY_ROUNDTRIP_EFFICIENCY=$(jq -r '.battery_roundtrip_efficiency // "0.92"' "$CONFIG")
export MONTHLY_NOTIFY=$(jq -r '.monthly_notify // "true"' "$CONFIG")
export CO2_FACTOR_KG_KWH=$(jq -r '.co2_factor_kg_kwh // "0.597"' "$CONFIG")
export DEPOSIT_REFUND_PCT=$(jq -r '.deposit_refund_pct // "0.20"' "$CONFIG")
export TZ=$(jq -r '.timezone // "Europe/Warsaw"' "$CONFIG")
export HEATPUMP_ENERGY_ENTITY=$(jq -r '.heatpump_energy_entity // ""' "$CONFIG")
export HEATPUMP_HEATING_HOURS_ENTITY=$(jq -r '.heatpump_heating_hours_entity // ""' "$CONFIG")
export HEATPUMP_DHW_HOURS_ENTITY=$(jq -r '.heatpump_dhw_hours_entity // ""' "$CONFIG")
export HEATPUMP_OUTDOOR_TEMP_ENTITY=$(jq -r '.heatpump_outdoor_temp_entity // ""' "$CONFIG")
export HEATPUMP_HDD_BASE_TEMP=$(jq -r '.heatpump_hdd_base_temp // "15.0"' "$CONFIG")
export EBOK_USERNAME=$(jq -r '.ebok_username // ""' "$CONFIG")
export EBOK_PASSWORD=$(jq -r '.ebok_password // ""' "$CONFIG")
export EBOK_PAYER_ID=$(jq -r '.ebok_payer_id // ""' "$CONFIG")

export HISTORIC_PATH="/data/historic.json"
export RCEM_HISTORY_PATH="/data/rcem_history.json"
export RCEM_CORRECTIONS_PATH="/data/rcem_corrections.json"
export INVOICE_LAYOUTS_PATH="/data/invoice_layouts.json"
export HEATPUMP_HOURS_PATH="/data/heatpump_hours.json"

exec python3 -m pv_roi_tracker.main
