# Medifact API — Deploy Gids

## Wat heb je nodig?
- **Anthropic API-sleutel** → [console.anthropic.com](https://console.anthropic.com)
- **Python 3.10+** (alleen voor lokaal) → [python.org/downloads](https://www.python.org/downloads/)
- **Railway account** (gratis, alleen voor online) → [railway.app](https://railway.app)

---

## ⚡ Optie A — Lokaal draaien (snelste, geen account nodig)

### Mac / Linux — dubbelklik of run:
```bash
bash run.sh
```

### Windows — dubbelklik:
```
run.bat
```

Het script vraagt eenmalig om je Anthropic API-sleutel en slaat die op in `.env`.
De API draait daarna op **http://localhost:8000**.

Zet daarna in `medifact.html`:
```javascript
const backendUrl = 'http://localhost:8000';
```

---

## 🚀 Optie B — Online via Railway (bereikbaar overal, gratis tier)

### Volledig automatisch — run eenmalig:
```bash
bash deploy-railway.sh
```
Het script installeert Railway CLI, logt je in (browser), maakt het project aan,
zet de API-sleutel als geheime variabele en deployt. Je krijgt aan het eind een URL.

Zet die URL daarna in `medifact.html`:
```javascript
const backendUrl = 'https://medifact-api.up.railway.app';
```

**Updates deployen** (na wijzigingen):
```bash
railway up --detach
```

**Kosten:** Railway free tier = $5 credit/maand — ruim genoeg voor persoonlijk gebruik.

---

## Render (alternatief, ook gratis)

1. Maak account op https://render.com
2. "New" → "Web Service" → koppel repo
3. Build Command: `pip install -r requirements.txt`
4. Start Command: `uvicorn main:app --host 0.0.0.0 --port $PORT`
5. Voeg environment variable toe: `ANTHROPIC_API_KEY`
6. Deploy

---

## Frontend koppelen

In `medifact.html`, ga naar de **API & Integraties** view en vul in:

```
Backend URL: https://jouw-url.railway.app
```

De frontend gebruikt dan automatisch de SSE stream (`/api/analyze/stream`) voor real-time resultaten. Zonder backend URL valt het terug op directe PubMed-calls vanuit de browser.

---

## API Endpoints

| Endpoint | Methode | Beschrijving |
|----------|---------|--------------|
| `/` | GET | Status |
| `/health` | GET | Health check + API key status |
| `/docs` | GET | Swagger UI |
| `/api/analyze` | POST | Volledige analyse (wacht op alle 8 assen) |
| `/api/analyze/stream` | GET | SSE stream — resultaten per as zodra ze klaar zijn |

### POST /api/analyze
```json
{
  "query": "COVID-19 mRNA vaccins",
  "profile": "medical"
}
```

### GET /api/analyze/stream
```
GET /api/analyze/stream?query=COVID-19+mRNA+vaccins&profile=medical
Content-Type: text/event-stream

data: {"type": "axis_result", "axis": "A1", "status": "pass", "score": "21k+", ...}
data: {"type": "axis_result", "axis": "A3", "status": "pass", "score": "4%", ...}
...
data: {"type": "done", "gate": "open", "score": "8/8", "rid": "Medifact-4f3a8c2e", ...}
```

---

## Kosten per analyse

| Model | A1/A2/A8 | A3/A4/A5/A6/A7 | Totaal |
|-------|----------|-----------------|--------|
| claude-haiku-4-5 | gratis (PubMed) | ~$0.004 per as | ~$0.02 |
| claude-sonnet-4-6 | gratis (PubMed) | ~$0.05 per as | ~$0.25 |

Bij haiku: 1000 analyses/maand ≈ $20.

---

## Model kiezen

In `.env` of Railway environment:
- `MEDIFACT_MODEL=claude-haiku-4-5-20251001` — snel (5-8 sec totaal), goedkoop, goed voor volume
- `MEDIFACT_MODEL=claude-sonnet-4-6` — precisie voor A3 consensusanalyse, aanbevolen voor enterprise

---

## CORS & beveiliging

In productie kun je CORS beperken tot jouw frontend-domein:

```python
# In main.py, vervang allow_origins=["*"] met:
allow_origins=["https://jouw-frontend.vercel.app"]
```

Voeg eventueel een API key middleware toe voor authenticatie.
