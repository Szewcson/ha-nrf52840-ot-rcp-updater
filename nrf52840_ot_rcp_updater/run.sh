#!/usr/bin/with-contenv bashio
set -euo pipefail

# Remove superseded target and endpoint overrides in one schema-valid Supervisor
# update. These values are now fixed app invariants, not user choices.
retired_options=(hardware manifest_url otbr_addon_slug otbr_api_url dfu_vid_pid)
if current_options="$(bashio::addon.options)"; then
    has_retired_option=false
    for option in "${retired_options[@]}"; do
        if bashio::jq.exists "${current_options}" ".${option}"; then
            has_retired_option=true
            break
        fi
    done
    if "${has_retired_option}"; then
        if migrated_options="$(
            bashio::jq "${current_options}" \
                'del(.hardware, .manifest_url, .otbr_addon_slug, .otbr_api_url, .dfu_vid_pid)'
        )" && payload="$(bashio::var.json options "^${migrated_options}")"; then
            if bashio::api.supervisor POST "/addons/self/options" "${payload}"; then
                bashio::cache.flush_all
                bashio::log.info "Removed retired configuration options"
            else
                bashio::log.warning "Could not remove retired configuration options"
            fi
        else
            bashio::log.warning "Could not prepare retired configuration cleanup"
        fi
    fi
else
    bashio::log.warning "Could not inspect retired configuration options"
fi

export OT_RCP_OPTIONS=/data/options.json
export OT_RCP_STATE_DIR=/data
export OT_RCP_MQTT_HOST="$(bashio::services mqtt 'host')"
export OT_RCP_MQTT_PORT="$(bashio::services mqtt 'port')"
export OT_RCP_MQTT_USERNAME="$(bashio::services mqtt 'username')"
export OT_RCP_MQTT_PASSWORD="$(bashio::services mqtt 'password')"
export PYTHONPATH="/:${PYTHONPATH:-}"

cd /
exec /usr/bin/python3 -B -m app.main
