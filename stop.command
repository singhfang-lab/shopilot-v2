#!/bin/bash
# USB Assistant - Mac stop script

echo "============================================"
echo "  USB Assistant - Stopping Services"
echo "============================================"
echo ""

stop_port() {
    local port=$1
    local name=$2
    local pids
    pids=$(lsof -i ":$port" -sTCP:LISTEN -t 2>/dev/null)
    if [ -n "$pids" ]; then
        echo "$pids" | xargs kill 2>/dev/null
        printf "Stopped %-20s ✓\n" "$name"
    else
        printf "%-24s (not running)\n" "$name"
    fi
}

# Kill by port
stop_port 11434 "Ollama"
stop_port 18789 "OpenClaw"
stop_port 8081  "Backend (uvicorn)"
stop_port 3001  "Frontend (http.server)"

# Also kill any stray ollama processes not yet listening
pkill -f "ollama serve" 2>/dev/null

echo ""
echo "Done."
