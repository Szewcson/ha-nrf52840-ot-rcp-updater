#!/usr/bin/with-contenv bashio
set -euo pipefail

export OT_RCP_OPTIONS=/data/options.json
export OT_RCP_STATE_DIR=/data
export OT_RCP_MQTT_HOST="$(bashio::services mqtt 'host')"
export OT_RCP_MQTT_PORT="$(bashio::services mqtt 'port')"
export OT_RCP_MQTT_USERNAME="$(bashio::services mqtt 'username')"
export OT_RCP_MQTT_PASSWORD="$(bashio::services mqtt 'password')"

exec python3 -m app.main

