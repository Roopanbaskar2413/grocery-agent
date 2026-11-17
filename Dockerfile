FROM python:3.12-slim

WORKDIR /app
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY agent/ agent/
COPY run_dev.py .

# SQLite db and the encrypted Swiggy token file live here. Mount a persistent volume
# at this path, or every redeploy/restart starts from empty state.
ENV AGENT_DB=/data/agent.db
ENV SWIGGY_TOKEN_PATH=/data/.swiggy_token
RUN mkdir -p /data

EXPOSE 8000
CMD ["sh", "-c", "uvicorn run_dev:app --host 0.0.0.0 --port ${PORT:-8000}"]
