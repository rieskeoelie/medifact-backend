#!/bin/bash
# ─────────────────────────────────────────────────────────────────────────────
# Medifact — RAILWAY DEPLOY SCRIPT
# Eenmalig uitvoeren: bash deploy-railway.sh
# Vereist: Railway account op railway.app (gratis tier volstaat)
# ─────────────────────────────────────────────────────────────────────────────

set -e
cd "$(dirname "$0")"

echo ""
echo "╔════════════════════════════════════════════════════╗"
echo "║    Medifact API — Deploy naar Railway.app               ║"
echo "╚════════════════════════════════════════════════════╝"
echo ""

# ── 1. Check ANTHROPIC_API_KEY ───────────────────────────────────────────────
if [ -f ".env" ]; then
    export $(grep -v '^#' .env | xargs)
fi
if [ -z "$ANTHROPIC_API_KEY" ]; then
    echo "⚠️   ANTHROPIC_API_KEY niet gevonden."
    read -rp "    Voer je Anthropic API-sleutel in: " key
    export ANTHROPIC_API_KEY="$key"
    echo "ANTHROPIC_API_KEY=$key" > .env
fi
echo "✅  ANTHROPIC_API_KEY klaar"

# ── 2. Installeer Railway CLI (als niet aanwezig) ────────────────────────────
if ! command -v railway &>/dev/null; then
    echo ""
    echo "📦  Railway CLI installeren..."
    curl -fsSL https://railway.app/install.sh | sh
    export PATH="$HOME/.railway/bin:$PATH"
fi
echo "✅  Railway CLI $(railway --version 2>/dev/null || echo 'klaar')"

# ── 3. Login (opent browser) ─────────────────────────────────────────────────
echo ""
echo "🔑  Je browser opent voor Railway-login (eenmalig)..."
echo "    Log in met je Railway account en kom terug naar dit venster."
echo ""
railway login

# ── 4. Init project (als nog niet gelinkt) ──────────────────────────────────
if [ ! -f ".railway/config.json" ]; then
    echo ""
    echo "🚂  Nieuw Railway project aanmaken..."
    railway init --name "bck-medical-api"
fi
echo "✅  Railway project klaar"

# ── 5. Zet environment variable ──────────────────────────────────────────────
echo ""
echo "🔐  ANTHROPIC_API_KEY instellen in Railway..."
railway variables --set "ANTHROPIC_API_KEY=$ANTHROPIC_API_KEY"
echo "✅  Environment variable ingesteld"

# ── 6. Deploy ────────────────────────────────────────────────────────────────
echo ""
echo "🚀  Deploying naar Railway..."
railway up --detach

# ── 7. Haal de publieke URL op ───────────────────────────────────────────────
echo ""
echo "🌐  Publieke URL ophalen..."
sleep 5
RAILWAY_URL=$(railway domain 2>/dev/null || echo "")

echo ""
echo "╔════════════════════════════════════════════════════╗"
echo "║    ✅  Deploy geslaagd!                            ║"
echo "╚════════════════════════════════════════════════════╝"
echo ""
if [ -n "$RAILWAY_URL" ]; then
    echo "🔗  Je API is live op: https://$RAILWAY_URL"
    echo ""
    echo "    Zet deze URL in medifact.html als backendUrl:"
    echo "    const backendUrl = 'https://$RAILWAY_URL';"
else
    echo "    Ga naar railway.app → jouw project → Settings → Domains"
    echo "    Kopieer de URL en zet die in medifact.html als backendUrl."
fi
echo ""
echo "    Testen: curl https://$RAILWAY_URL/health"
echo ""
