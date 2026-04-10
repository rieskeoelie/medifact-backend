#!/bin/bash
# ─────────────────────────────────────────────────────────────────────────────
# Medifact — LOCAL BACKEND LAUNCHER
# Dubbelklik dit bestand (of run: bash run.sh) om de backend lokaal te starten.
# ─────────────────────────────────────────────────────────────────────────────

set -e
cd "$(dirname "$0")"

echo ""
echo "╔════════════════════════════════════════════════════╗"
echo "║    Medifact API — Opstarten        ║"
echo "╚════════════════════════════════════════════════════╝"
echo ""

# ── 1. Check Python ──────────────────────────────────────────────────────────
if ! command -v python3 &>/dev/null; then
    echo "❌  Python 3 niet gevonden."
    echo "    Installeer Python via: https://www.python.org/downloads/"
    exit 1
fi
echo "✅  Python $(python3 --version | awk '{print $2}') gevonden"

# ── 2. Check ANTHROPIC_API_KEY ───────────────────────────────────────────────
if [ -f ".env" ]; then
    export $(grep -v '^#' .env | xargs)
fi

if [ -z "$ANTHROPIC_API_KEY" ]; then
    echo ""
    echo "⚠️   ANTHROPIC_API_KEY niet gevonden."
    echo "    Voer je API-sleutel in (of zet hem in een .env bestand):"
    read -rp "    ANTHROPIC_API_KEY=  " key
    if [ -z "$key" ]; then
        echo "❌  Geen API-sleutel ingevoerd. Stop."
        exit 1
    fi
    export ANTHROPIC_API_KEY="$key"
    echo "ANTHROPIC_API_KEY=$key" > .env
    echo "✅  API-sleutel opgeslagen in .env"
else
    echo "✅  ANTHROPIC_API_KEY gevonden"
fi

# ── 3. Installeer packages (alleen als nodig) ────────────────────────────────
echo ""
echo "📦  Controleer Python-pakketten..."
pip3 install -q -r requirements.txt 2>&1 | grep -E "^(Collecting|Successfully|ERROR)" || true
echo "✅  Pakketten klaar"

# ── 4. Start server ──────────────────────────────────────────────────────────
echo ""
echo "🚀  Medifact API start op http://localhost:8000"
echo "    Open medifact.html in je browser nadat de server klaar is."
echo "    Stop met: Ctrl+C"
echo ""
echo "────────────────────────────────────────────────────────"
python3 -m uvicorn main:app --host 0.0.0.0 --port 8000 --reload
