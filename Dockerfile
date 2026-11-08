FROM python:3.12-slim

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PORT=8000 \
    SLAY_DB_PATH=/var/lib/slay/stockguessr.sqlite3

WORKDIR /app

RUN groupadd --system --gid 10001 slay \
    && useradd --system --uid 10001 --gid slay --home-dir /nonexistent --shell /usr/sbin/nologin slay \
    && mkdir -p /var/lib/slay \
    && chown slay:slay /var/lib/slay

COPY --chown=slay:slay server.py llm_provider.py ./
COPY --chown=slay:slay web/ ./web/
COPY --chown=slay:slay data/rounds.json data/education.json ./data/

USER slay
EXPOSE 8000

HEALTHCHECK --interval=30s --timeout=5s --start-period=5s --retries=3 \
    CMD python -c "from urllib.request import urlopen; urlopen('http://127.0.0.1:8000/', timeout=3)" || exit 1

CMD ["python", "server.py"]
