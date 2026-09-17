#!/bin/sh
PORT="${PORT:-7860}"
echo "Starting JobDoc on port $PORT"

# Aggressive cleanup
pkill -9 -f gunicorn 2>/dev/null || true
fuser -k "${PORT}/tcp" 2>/dev/null || true
sleep 4

exec gunicorn \
  --bind "0.0.0.0:${PORT}" \
  --workers 1 \
  --threads 4 \
  --timeout 1800 \
  --reuse-port \
  --preload \
  --access-logfile - \
  app:app
