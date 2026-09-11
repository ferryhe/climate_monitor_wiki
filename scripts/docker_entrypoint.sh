#!/bin/sh
set -eu

# The shipped TLS Compose deployment exposes the management console, so it
# must never start with disabled credentials, an ephemeral signing secret, or
# cookies that can cross plaintext connections. Direct image users retain the
# API's optional-console behavior unless they opt into this deployment gate.
if [ "${CLIMATE_REQUIRE_CONSOLE_AUTH:-}" = "1" ]; then
    : "${CLIMATE_CONSOLE_USERNAME:?console username is required}"
    : "${CLIMATE_CONSOLE_PASSWORD_HASH:?console Argon2 password hash is required}"
    : "${CLIMATE_CONSOLE_SESSION_SECRET:?console session secret is required}"
    case "$CLIMATE_CONSOLE_PASSWORD_HASH" in
        '$argon2'*) ;;
        *) echo "CLIMATE_CONSOLE_PASSWORD_HASH must be an Argon2 hash" >&2; exit 78 ;;
    esac
    if [ "${#CLIMATE_CONSOLE_SESSION_SECRET}" -lt 32 ]; then
        echo "CLIMATE_CONSOLE_SESSION_SECRET must contain at least 32 characters" >&2
        exit 78
    fi
    case "${CLIMATE_CONSOLE_SECURE_COOKIE:-}" in
        1|true|TRUE|yes|YES) ;;
        *)
            echo "CLIMATE_CONSOLE_SECURE_COOKIE must be true for the Compose deployment" >&2
            exit 78
            ;;
    esac
fi

# Seed mutable runtime configuration exactly once. Existing operator state wins.
if [ -n "${CLIMATE_TASK_CONFIG:-}" ] && [ ! -e "$CLIMATE_TASK_CONFIG" ]; then
    mkdir -p "$(dirname "$CLIMATE_TASK_CONFIG")"
    cp /app/monitoring/jobs/weekly-climate-monitor-08h/task-definition.json "$CLIMATE_TASK_CONFIG"
fi
if [ -n "${CLIMATE_TASK_VERSION_DIR:-}" ]; then
    mkdir -p "$CLIMATE_TASK_VERSION_DIR"
fi
if [ -n "${CLIMATE_ACQUISITION_RUN_DIR:-}" ]; then
    mkdir -p "$CLIMATE_ACQUISITION_RUN_DIR"
fi

if [ "${HERMES_DASHBOARD_ENABLED:-}" = "1" ]; then
    : "${CLIMATE_PUBLIC_ORIGIN:?trusted public HTTPS origin is required for Hermes OAuth callbacks}"
    python -c 'from climate_monitor.hermes_dashboard_server import trusted_public_origin; trusted_public_origin()'
    export HERMES_HOME="${HERMES_HOME:-/app/output/hermes}"
    if [ -z "${HERMES_DASHBOARD_SESSION_TOKEN:-}" ]; then
        HERMES_DASHBOARD_SESSION_TOKEN="$(python -c 'import secrets; print(secrets.token_urlsafe(32))')"
    fi
    export HERMES_DASHBOARD_SESSION_TOKEN
    mkdir -p "$HERMES_HOME"
    python -m climate_monitor.hermes_dashboard_server &
    hermes_pid=$!
    "$@" &
    app_pid=$!
    trap 'kill "$app_pid" "$hermes_pid" 2>/dev/null || true' INT TERM EXIT
    status=0
    wait "$app_pid" || status=$?
    kill "$hermes_pid" 2>/dev/null || true
    wait "$hermes_pid" 2>/dev/null || true
    exit "$status"
fi

exec "$@"
