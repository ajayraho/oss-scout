#!/bin/bash
set -e

echo "==> Starting Streamlit on port 8501 at /tool ..."
streamlit run app.py \
  --server.port 8501 \
  --server.headless true \
  --server.baseUrlPath /tool &

echo "==> Waiting for Streamlit to be ready..."
for i in $(seq 1 30); do
  if curl -sf http://localhost:8501/tool/_stcore/health > /dev/null 2>&1; then
    echo "==> Streamlit is up!"
    break
  fi
  sleep 2
done

PORT=${PORT:-8000}
echo "==> Starting landing server on port $PORT ..."
exec uvicorn landing_server:app --host 0.0.0.0 --port "$PORT"
