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

exec "$@"
