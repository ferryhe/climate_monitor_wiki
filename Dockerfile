FROM python:3.11-slim

WORKDIR /app

COPY requirements.txt ./
RUN pip install --no-cache-dir -r requirements.txt

COPY api_server.py ./
COPY agentic_wiki ./agentic_wiki
COPY climate_registry ./climate_registry
COPY scripts ./scripts
COPY showcase ./showcase
COPY wiki ./wiki
COPY sources ./sources

ENV PYTHONUNBUFFERED=1
EXPOSE 8501

CMD ["uvicorn", "api_server:app", "--host", "0.0.0.0", "--port", "8501"]
