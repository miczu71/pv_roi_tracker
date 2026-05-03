#!/usr/bin/with-contenv bashio

export CSV_URL="$(bashio::config 'csv_url')"
export FORCE_REIMPORT="$(bashio::config 'force_reimport')"
export GROSS_INVESTMENT="$(bashio::config 'gross_investment')"
export SUBSIDY="$(bashio::config 'subsidy')"
export SYSTEM_KWP="$(bashio::config 'system_kwp')"
export POLL_INTERVAL_MINUTES="$(bashio::config 'poll_interval_minutes')"
export MQTT_HOST="$(bashio::config 'mqtt_host')"
export MQTT_PORT="$(bashio::config 'mqtt_port')"
export MQTT_USER="$(bashio::config 'mqtt_user')"
export MQTT_PASSWORD="$(bashio::config 'mqtt_password')"
export LOG_LEVEL="$(bashio::config 'log_level')"

export HISTORIC_PATH="/data/historic.json"
export RCEM_HISTORY_PATH="/data/rcem_history.json"

bashio::log.info "Starting PV ROI Tracker v$(bashio::addon.version)"

exec python3 -m pv_roi_tracker.main
