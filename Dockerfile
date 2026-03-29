FROM python:3.11-slim

LABEL name="incident-postmortem-writer"
LABEL version="1.0.0"
LABEL description="OpenEnv environment for incident post-mortem writing"

WORKDIR /app

RUN apt-get update && apt-get install -y curl && rm -rf /var/lib/apt/lists/*

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY . .

RUN useradd -m -u 1000 appuser && chown -R appuser:appuser /app
USER appuser

EXPOSE 7860

HEALTHCHECK --interval=30s --timeout=10s --start-period=15s --retries=3 \
    CMD curl -f http://localhost:7860/health || exit 1

ENV HOST=0.0.0.0
ENV PORT=7860
ENV WORKERS=2
ENV MAX_CONCURRENT_ENVS=100
ENV DIFFICULTY=easy

CMD ["sh", "-c", "uvicorn server.app:app --host $HOST --port $PORT --workers $WORKERS"]
