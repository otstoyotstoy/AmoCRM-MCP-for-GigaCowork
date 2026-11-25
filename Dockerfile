FROM python:3.12-slim

ENV PYTHONDONTWRITEBYTECODE=1 PYTHONUNBUFFERED=1 \
    MCP_TRANSPORT=streamable-http MCP_HOST=0.0.0.0 MCP_PORT=8000 EXPORT_DIR=/data/exports MCP_AUDIT_DIR=/data/audit

WORKDIR /app
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt
COPY amocrm_mcp ./amocrm_mcp
COPY server.py .

RUN useradd -r -u 10001 app && mkdir -p /data/exports /data/audit && chown -R app /data
USER app

EXPOSE 8000
HEALTHCHECK --interval=30s --timeout=5s --retries=3 \
  CMD python -c "import urllib.request,sys; sys.exit(0 if urllib.request.urlopen('http://127.0.0.1:8000/health').status==200 else 1)"

CMD ["python", "-m", "amocrm_mcp.server"]
