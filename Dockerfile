FROM python:3.12-slim
ARG SOURCE_COMMIT=local
LABEL org.opencontainers.image.source="https://github.com/Prodigalgal/bybit-mcp" org.opencontainers.image.revision=$SOURCE_COMMIT
ENV SOURCE_COMMIT=$SOURCE_COMMIT
ENV PYTHONDONTWRITEBYTECODE=1 PYTHONUNBUFFERED=1 DATA_DIR=/data
WORKDIR /app
COPY requirements.txt ./
RUN pip install --no-cache-dir -r requirements.txt && useradd --uid 10001 --create-home app && mkdir /data && chown app:app /data
COPY bybit_mcp ./bybit_mcp
USER 10001:10001
EXPOSE 8000
VOLUME ["/data"]
HEALTHCHECK --interval=30s --timeout=5s --start-period=20s CMD python -c "import urllib.request; urllib.request.urlopen('http://127.0.0.1:8000/readyz',timeout=3)"
CMD ["python", "-m", "bybit_mcp.app"]
