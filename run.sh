#!/bin/zsh
# Start Top3: MongoDB + Redis (docker), Celery worker (new Terminal window), then the FastAPI app.

PROJECT_DIR="$(cd "$(dirname "$0")" && pwd)"
cd "$PROJECT_DIR" || exit 1

# Use the project virtualenv if present
if [[ -f "$PROJECT_DIR/.venv/bin/activate" ]]; then
  source "$PROJECT_DIR/.venv/bin/activate"
  ACTIVATE="source '$PROJECT_DIR/.venv/bin/activate' && "
else
  ACTIVATE=""
fi

# 1. Make sure Docker is running
if ! docker info >/dev/null 2>&1; then
  echo "Starting Docker Desktop..."
  open -a Docker
  until docker info >/dev/null 2>&1; do sleep 2; done
fi

# 2. Start MongoDB + Redis containers
echo "Starting MongoDB and Redis..."
docker compose up -d || { echo "docker compose failed"; exit 1; }

echo "Waiting for MongoDB to become healthy..."
for i in {1..30}; do
  [[ "$(docker inspect -f '{{.State.Health.Status}}' top3-mongo 2>/dev/null)" == "healthy" ]] && break
  sleep 2
done

# 3. Start Celery worker in a new Terminal window
echo "Launching Celery worker in a new Terminal window..."
osascript <<EOF
tell application "Terminal"
  do script "cd '$PROJECT_DIR' && ${ACTIVATE}celery -A src.tasks.celery_app worker --loglevel=info"
  activate
end tell
EOF

# 4. Open the browser once the app is up (in background)
( until curl -s -o /dev/null http://localhost:8000; do sleep 1; done; open http://localhost:8000 ) &

# 5. Start the FastAPI app in this terminal
echo "Starting Top3 app on http://localhost:8000 ..."
exec python3 app.py
