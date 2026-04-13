# Medifact API — Deploy & Configuratie

FastAPI backend voor het Medifact Evidence Intelligence Platform.

**Live:** `https://powerful-creation-production.up.railway.app`

---

## Stack

- **Framework:** FastAPI (Python 3.11)
- **Database:** PostgreSQL via async SQLAlchemy + asyncpg
- **AI:** Anthropic Claude (claude-haiku-4-5-20251001)
- **Email:** Resend API (via httpx)
- **Auth:** JWT (python-jose)
- **Server:** Uvicorn
- **Hosting:** Railway

---

## Omgevingsvariabelen (Railway)

| Variabele | Beschrijving | Verplicht |
|-----------|-------------|-----------|
| `ANTHROPIC_API_KEY` | Claude API key | Ja |
| `DATABASE_URL` | PostgreSQL connection string | Ja |
| `JWT_SECRET` | Geheime sleutel voor JWT tokens | Ja |
| `ADMIN_SECRET` | Wachtwoord voor admin endpoints | Ja |
| `RESEND_API_KEY` | Resend API key voor emails | Ja |
| `CRON_SECRET` | Bearer token voor cron endpoints | Ja |
| `FROM_EMAIL` | Afzenderadres (bijv. noreply@medifact.eu) | Nee (default: noreply@medifact.eu) |
| `FRONTEND_URL` | URL van de frontend | Nee (default: http://localhost:8000) |
| `PORT` | Poort voor uvicorn | Nee (default: 8000) |

---

## API Endpoints

### Authenticatie
| Endpoint | Methode | Beschrijving |
|----------|---------|--------------|
| `/auth/register` | POST | Nieuw account aanmaken |
| `/auth/login` | POST | Inloggen, ontvangt JWT token |
| `/auth/me` | GET | Huidig gebruikersprofiel ophalen |

### Analyses
| Endpoint | Methode | Beschrijving |
|----------|---------|--------------|
| `/api/analyze` | POST | Volledige analyse (wacht op alle 8 assen) |
| `/api/analyze/stream` | GET | SSE stream — resultaten per as real-time |
| `/analyses` | POST | Analyse opslaan + gebruik teller ophogen |
| `/analyses` | GET | Opgeslagen analyses ophalen (max 8) |

### Billing
| Endpoint | Methode | Beschrijving |
|----------|---------|--------------|
| `/billing/checkout` | POST | Stripe checkout sessie aanmaken |
| `/billing/webhook` | POST | Stripe webhook handler |

### Cron (beveiligd met CRON_SECRET)
| Endpoint | Methode | Beschrijving |
|----------|---------|--------------|
| `/cron/weekly-digest` | POST | Digest email versturen naar alle gebruikers |
| `/cron/test-email` | POST | Testmail versturen naar één adres |

### Admin (beveiligd met ADMIN_SECRET)
| Endpoint | Methode | Beschrijving |
|----------|---------|--------------|
| `/admin/users` | GET | Alle gebruikers ophalen |
| `/admin/users/:id` | PATCH | Gebruiker bijwerken (tier, limiet, etc.) |

### Systeem
| Endpoint | Methode | Beschrijving |
|----------|---------|--------------|
| `/` | GET | Service info |
| `/health` | GET | Health check + config status |
| `/docs` | GET | Swagger UI |

---

## Lokaal draaien

```bash
# Installeer dependencies
pip install -r requirements.txt

# Maak .env aan
cat > .env << EOF
ANTHROPIC_API_KEY=sk-ant-...
DATABASE_URL=sqlite+aiosqlite:///./medifact.db
JWT_SECRET=local-dev-secret
ADMIN_SECRET=local-admin
RESEND_API_KEY=re_...
CRON_SECRET=local-cron-secret
FROM_EMAIL=noreply@medifact.eu
FRONTEND_URL=http://localhost:3000
EOF

# Start server
uvicorn main:app --reload --port 8000
```

API draait op [http://localhost:8000](http://localhost:8000).
Swagger UI op [http://localhost:8000/docs](http://localhost:8000/docs).

---

## Deployment (Railway)

Automatisch via GitHub push naar `main`. Railway bouwt en deployt de FastAPI app.

**Start command** (ingesteld in `railway.toml`):
```
uvicorn main:app --host 0.0.0.0 --port $PORT
```

---

## Tiers & limieten

| Tier | Analyses/maand | Stripe Price ID |
|------|---------------|-----------------|
| `free` | 10 | — |
| `pro` | 100 | Configureerbaar |
| `enterprise` | 999999 (onbeperkt) | Configureerbaar |

---

## Wekelijkse digest (cron)

De digest wordt elke maandag om 08:00 verstuurd via **cron-job.org**:

- **URL:** `POST https://powerful-creation-production.up.railway.app/cron/weekly-digest`
- **Header:** `Authorization: Bearer <CRON_SECRET>`
- **Schema:** `0 8 * * 1` (Europe/Amsterdam)

De email bevat: gebruikersnaam, tier, analyses gebruikt/limiet, en een CTA naar het dashboard.
Het `medifact.eu` domein is geverifieerd in Resend.

---

## Kosten per analyse

| Model | Kosten |
|-------|--------|
| claude-haiku-4-5 | ~€0.02 per analyse |
| claude-sonnet-4-x | ~€0.25 per analyse |

Bij haiku: 1000 analyses/maand ≈ €20.
