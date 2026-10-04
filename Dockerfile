# Build from repository root: docker build -f services/ai-usage-api/Dockerfile .
# Snapshots are never baked into this image; mount sanitized state read-only.
FROM python:3.12-slim
ENV PYTHONDONTWRITEBYTECODE=1 PYTHONUNBUFFERED=1 PIP_NO_CACHE_DIR=1 AI_USAGE_API_HOST=0.0.0.0 AI_USAGE_API_ENV=production AI_USAGE_STATE_DIR=/app/state
WORKDIR /app
COPY packages/service-auth /app/packages/service-auth
COPY services/ai-usage-api/pyproject.toml services/ai-usage-api/server.py services/ai-usage-api/model.py services/ai-usage-api/collector.py /app/services/ai-usage-api/
RUN pip install /app/packages/service-auth /app/services/ai-usage-api
RUN useradd --system --uid 10001 app
USER app
EXPOSE 3520
HEALTHCHECK --interval=15s --timeout=3s --retries=3 CMD python -c "import os, urllib.request; port=os.environ.get('AI_USAGE_API_PORT', os.environ.get('PORT', '3520')); urllib.request.urlopen('http://127.0.0.1:'+port+'/ready', timeout=2)"
CMD ["python", "/app/services/ai-usage-api/server.py"]
