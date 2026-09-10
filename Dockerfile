FROM python:3.12-slim

WORKDIR /app

RUN apt-get update \
    && apt-get install --yes --no-install-recommends git \
    && rm -rf /var/lib/apt/lists/*

COPY requirements.txt ./
RUN pip install --no-cache-dir -r requirements.txt
RUN git clone --filter=blob:none https://github.com/NousResearch/hermes-agent.git /opt/hermes-agent \
    && git -C /opt/hermes-agent checkout 5538bd1f933be2e94aca9755deca5cc59cccc553 \
    && pip install --no-cache-dir --editable /opt/hermes-agent \
    && rm -rf /opt/hermes-agent/.git

COPY api_server.py ./
COPY agentic_wiki ./agentic_wiki
COPY climate_delivery ./climate_delivery
COPY climate_monitor ./climate_monitor
COPY climate_registry ./climate_registry
COPY management_ui ./management_ui
COPY monitoring/taxonomies ./monitoring/taxonomies
COPY monitoring/jobs ./monitoring/jobs
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
CMD ["uvicorn", "api_server:app", "--host", "0.0.0.0", "--port", "8501"]
