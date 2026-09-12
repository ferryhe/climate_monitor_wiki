FROM node:22.22.0-bookworm-slim AS hermes-dashboard-build

RUN apt-get update \
    && apt-get install --yes --no-install-recommends ca-certificates git \
    && rm -rf /var/lib/apt/lists/*
RUN git clone --filter=blob:none https://github.com/NousResearch/hermes-agent.git /opt/hermes-agent \
    && git -C /opt/hermes-agent checkout 5538bd1f933be2e94aca9755deca5cc59cccc553 \
    && rm -rf /opt/hermes-agent/.git \
    && cd /opt/hermes-agent \
    && npm ci --workspace ui-tui --workspace web --include-workspace-root \
    && npm run build --workspace web \
    && npm run build --workspace ui-tui \
    && mkdir -p hermes_cli/tui_dist \
    && cp ui-tui/dist/entry.js hermes_cli/tui_dist/entry.js \
    && rm -rf node_modules ui-tui/node_modules web/node_modules

FROM python:3.12-slim

WORKDIR /app

ARG CLIMATE_REPOSITORY_COMMIT_SHA
RUN python -c 'import re, sys; value = sys.argv[1]; raise SystemExit(0 if re.fullmatch(r"[0-9a-f]{40}", value) else "repository commit SHA must be a 40-character lowercase hex digest")' "$CLIMATE_REPOSITORY_COMMIT_SHA"
ENV CLIMATE_REPOSITORY_COMMIT_SHA=$CLIMATE_REPOSITORY_COMMIT_SHA

RUN apt-get update \
    && apt-get install --yes --no-install-recommends ca-certificates git \
    && rm -rf /var/lib/apt/lists/*

COPY --from=hermes-dashboard-build /usr/local/bin/node /usr/local/bin/node
COPY --from=hermes-dashboard-build /opt/hermes-agent /opt/hermes-agent

COPY requirements.txt ./
RUN pip install --no-cache-dir -r requirements.txt
RUN pip install --no-cache-dir --editable '/opt/hermes-agent[web,pty]'

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
RUN chmod 0755 /app/scripts/docker_entrypoint.sh
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
