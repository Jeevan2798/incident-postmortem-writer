FROM python:3.11-slim

LABEL name="incident-postmortem-writer"
LABEL version="1.0.0"

WORKDIR /app

RUN apt-get update && apt-get install -y curl && rm -rf /var/lib/apt/lists/*

# Create user first
RUN useradd -m -u 1000 appuser

# Install dependencies
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

# Copy files with correct ownership in one step
COPY --chown=appuser:appuser . .

USER appuser

EXPOSE 7860

ENV HOST=0.0.0.0
ENV PORT=7860
ENV WORKERS=2
ENV MAX_CONCURRENT_ENVS=100
ENV DIFFICULTY=easy

HEALTHCHECK --interval=30s --timeout=10s --start-period=20s --retries=3 \
    CMD curl -f http://localhost:7860/health || exit 1

CMD ["sh", "-c", "uvicorn server.app:app --host $HOST --port $PORT --workers $WORKERS"]
