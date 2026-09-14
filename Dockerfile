FROM node:22.22.0-bookworm-slim AS hermes-dashboard-build

RUN apt-get update \
    && apt-get install --yes --no-install-recommends ca-certificates git \
    && rm -rf /var/lib/apt/lists/*

ARG HERMES_REVISION=5538bd1f933be2e94aca9755deca5cc59cccc553
RUN git init /opt/hermes-agent \
    && git -C /opt/hermes-agent remote add origin https://github.com/NousResearch/hermes-agent.git \
    && git -C /opt/hermes-agent fetch --depth 1 --filter=blob:none origin "$HERMES_REVISION" \
    && git -C /opt/hermes-agent checkout --detach FETCH_HEAD \
    && test "$(git -C /opt/hermes-agent rev-parse HEAD)" = "$HERMES_REVISION" \
    && rm -rf /opt/hermes-agent/.git \
    && cd /opt/hermes-agent \
    && npm ci --workspace ui-tui --workspace web --include-workspace-root \
    && npm run build --workspace web -- --base=/hermes/ \
    && npm run build --workspace ui-tui \
    && mkdir -p hermes_cli/tui_dist \
    && cp ui-tui/dist/entry.js hermes_cli/tui_dist/entry.js \
    && rm -rf node_modules ui-tui/node_modules web/node_modules

FROM python:3.12-slim

WORKDIR /app

ARG WEB_LISTENING_REVISION=ac2343f89bc7939736d85f049ebe2beac571034a

RUN apt-get update \
    && apt-get install --yes --no-install-recommends ca-certificates git \
    && rm -rf /var/lib/apt/lists/*

ENV CLIMATE_WEB_LISTENING_DATA_DIR=/opt/web-listening-data
RUN git init /opt/web-listening-source \
    && git -C /opt/web-listening-source remote add origin https://github.com/ferryhe/web_listening_new.git \
    && git -C /opt/web-listening-source fetch --depth 1 --filter=blob:none origin "$WEB_LISTENING_REVISION" \
    && git -C /opt/web-listening-source checkout --detach FETCH_HEAD \
    && test "$(git -C /opt/web-listening-source rev-parse HEAD)" = "$WEB_LISTENING_REVISION" \
    && rm -rf /opt/web-listening-source/.git \
    && python -m venv /opt/web-listening-data/browser-runtimes/playwright \
    && /opt/web-listening-data/browser-runtimes/playwright/bin/pip install --no-cache-dir playwright==1.62.0 \
    && PLAYWRIGHT_BROWSERS_PATH=/opt/web-listening-data/browser-runtimes/playwright/browsers \
       /opt/web-listening-data/browser-runtimes/playwright/bin/python -m playwright install --with-deps chromium --no-shell \
    && rm -rf /var/lib/apt/lists/*

COPY --from=hermes-dashboard-build /usr/local/bin/node /usr/local/bin/node
COPY --from=hermes-dashboard-build /opt/hermes-agent /opt/hermes-agent

COPY requirements.txt ./
RUN pip install --no-cache-dir -r requirements.txt
RUN PYTHONDONTWRITEBYTECODE=1 PYTHONPATH=/opt/web-listening-source/src \
    python -c 'import json; from pathlib import Path; from web_listening.tool_registry.runners.browser_acquisition import host_runtime; root=Path("/opt/web-listening-data"); lock=json.loads(Path("/opt/web-listening-source/tools/browser/runtime-lock.json").read_text()); host_runtime(root / "browser-runtimes/playwright", root, lock["tools"]["playwright"])'
RUN pip install --no-cache-dir --editable '/opt/hermes-agent[web,pty]'

ARG CLIMATE_REPOSITORY_COMMIT_SHA
RUN python -c 'import re, sys; value = sys.argv[1]; raise SystemExit(0 if re.fullmatch(r"[0-9a-f]{40}", value) else "repository commit SHA must be a 40-character lowercase hex digest")' "$CLIMATE_REPOSITORY_COMMIT_SHA"
ENV CLIMATE_REPOSITORY_COMMIT_SHA=$CLIMATE_REPOSITORY_COMMIT_SHA

COPY api_server.py ./
COPY agentic_wiki ./agentic_wiki
COPY climate_delivery ./climate_delivery
COPY climate_monitor ./climate_monitor
COPY climate_registry ./climate_registry
COPY management_ui ./management_ui
COPY monitoring/taxonomies ./monitoring/taxonomies
COPY monitoring/jobs ./monitoring/jobs
COPY monitoring/run_config.yaml ./monitoring/run_config.yaml
COPY monitoring/supranational_sources.yaml ./monitoring/supranational_sources.yaml
COPY monitoring/site_scopes.yaml ./monitoring/site_scopes.yaml
COPY scripts ./scripts
RUN chmod 0755 /app/scripts/docker_entrypoint.sh /app/scripts/qualify_web_listening_playwright.py
COPY showcase ./showcase
COPY wiki ./wiki
COPY sources ./sources
COPY article_metadata ./article_metadata

ENV PYTHONUNBUFFERED=1
EXPOSE 8501

ENTRYPOINT ["/app/scripts/docker_entrypoint.sh"]
# Caddy is the request-log sink and skips credential-bearing OAuth callbacks.
# Keep Uvicorn lifecycle/error logging, but do not duplicate raw request URIs in Docker logs.
CMD ["uvicorn", "api_server:app", "--host", "0.0.0.0", "--port", "8501", "--no-access-log"]
