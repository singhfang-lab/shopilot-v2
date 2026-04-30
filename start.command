#!/bin/bash
# USB Assistant - Mac startup script

# Change to project directory
cd "$(dirname "$0")"
PROJECT_DIR="$(pwd)"

mkdir -p "$PROJECT_DIR/logs"

echo "============================================"
echo "  USB Assistant - Starting Services"
echo "============================================"
echo ""

# ---- Helper functions ----

port_in_use() {
    lsof -i ":$1" -sTCP:LISTEN -t >/dev/null 2>&1
}

wait_for_port() {
    local port=$1
    local name=$2
    local max=30
    local count=0
    while ! curl -s "http://localhost:$port" >/dev/null 2>&1; do
        if [ $count -ge $max ]; then
            echo "  [TIMEOUT] $name did not respond within ${max}s"
            return 1
        fi
        sleep 1
        count=$((count + 1))
    done
    return 0
}


# ---- 1. PostgreSQL (Docker) ----

printf "Starting PostgreSQL...     "
if port_in_use 5432; then
    echo "(already running) ✓"
else
    cd "$PROJECT_DIR" && docker compose up -d > "$PROJECT_DIR/logs/docker.log" 2>&1
    count=0
    while ! nc -z localhost 5432 2>/dev/null; do
        if [ $count -ge 15 ]; then
            echo "✗  (check logs/docker.log)"
            exit 1
        fi
        sleep 1
        count=$((count + 1))
    done
    echo "✓"
fi

# ---- 2. OpenClaw ----

printf "Starting OpenClaw...     "
if port_in_use 18789; then
    echo "(already running) ✓"
else
    OPENCLAW_BIN=""
    for candidate in \
        "$PROJECT_DIR/openclaw" \
        "$PROJECT_DIR/scripts/openclaw" \
        "$(which openclaw 2>/dev/null)"; do
        if [ -x "$candidate" ]; then
            OPENCLAW_BIN="$candidate"
            break
        fi
    done

    if [ -n "$OPENCLAW_BIN" ]; then
        "$OPENCLAW_BIN" > "$PROJECT_DIR/logs/openclaw.log" 2>&1 &
        if wait_for_port 18789 "OpenClaw"; then
            echo "✓"
        else
            echo "✗  (check logs/openclaw.log)"
            exit 1
        fi
    else
        echo "(skipped — binary not found)"
    fi
fi

# ---- 3. FastAPI backend (uvicorn) ----

printf "Starting Backend...      "
if port_in_use 8081; then
    echo "(already running) ✓"
else
    if [ -f "$PROJECT_DIR/venv/bin/activate" ]; then
        source "$PROJECT_DIR/venv/bin/activate"
    fi
    cd "$PROJECT_DIR"
    python -m uvicorn backend.main:app --host 0.0.0.0 --port 8081 \
        > "$PROJECT_DIR/logs/backend.log" 2>&1 &
    if wait_for_port 8081 "Backend"; then
        echo "✓"
    else
        echo "✗  (check logs/backend.log)"
        exit 1
    fi
fi

# ---- 4. Frontend (python http.server) ----

printf "Starting Frontend...     "
if port_in_use 3001; then
    echo "(already running) ✓"
else
    cd "$PROJECT_DIR/frontend"
    python3 -m http.server 3001 \
        > "$PROJECT_DIR/logs/frontend.log" 2>&1 &
    cd "$PROJECT_DIR"
    if wait_for_port 3001 "Frontend"; then
        echo "✓"
    else
        echo "✗  (check logs/frontend.log)"
        exit 1
    fi
fi

# ---- Done ----

echo ""
echo "============================================"
echo "  All services running."
echo "  Opening http://localhost:3001 ..."
echo "============================================"
sleep 1
open "http://localhost:3001"
