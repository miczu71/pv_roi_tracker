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

export HISTORIC_PATH="/data/historic.json"
export RCEM_HISTORY_PATH="/data/rcem_history.json"
export RCEM_CORRECTIONS_PATH="/data/rcem_corrections.json"

exec python3 -m pv_roi_tracker.main
