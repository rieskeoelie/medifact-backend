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
| `R2_ACCOUNT_ID` | Cloudflare R2 account ID (DB-backups) | Nee (backup wordt overgeslagen indien leeg) |
| `R2_ACCESS_KEY_ID` | R2 API access key | Nee |
| `R2_SECRET_ACCESS_KEY` | R2 API secret key | Nee |
| `R2_BUCKET` | R2 bucketnaam | Nee (default: medifact-db-backups) |
| `BACKUP_RETENTION_DAYS` | Bewaartermijn dumps in dagen | Nee (default: 30) |

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
| `/cron/backup-db` | POST | Database `pg_dump` → Cloudflare R2 (dagelijks) |

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

## Database backups (off-site → Cloudflare R2)

De PostgreSQL data leeft op een Railway-volume (`postgres-volume-Vhzp`, gemount op
`/var/lib/postgresql/data`). Dat beschermt tegen redeploys/crashes, maar **niet** tegen
een Railway-account/region-storing of een per ongeluk gewist volume. Daarvoor maakt
`POST /cron/backup-db` dagelijks een logische dump die *buiten* Railway wordt bewaard.

**Flow:** `pg_dump --format=custom` (gecomprimeerd) → upload naar R2 als
`backups/medifact-YYYYMMDD-HHMMSS.dump` → dumps ouder dan `BACKUP_RETENTION_DAYS`
worden automatisch opgeruimd.

### Eenmalige setup
1. Maak een R2-bucket aan (bijv. `medifact-db-backups`) in het Cloudflare-dashboard.
2. Maak een R2 API-token (Object Read & Write) → noteer Account ID, Access Key ID, Secret.
3. Zet `R2_ACCOUNT_ID`, `R2_ACCESS_KEY_ID`, `R2_SECRET_ACCESS_KEY`, `R2_BUCKET` als
   env-vars op de Railway **backend**-service.
4. Voeg een dagelijkse job toe op **cron-job.org**:
   - **URL:** `POST https://powerful-creation-production.up.railway.app/cron/backup-db`
   - **Header:** `Authorization: Bearer <CRON_SECRET>`
   - **Schema:** bijv. `0 3 * * *` (dagelijks 03:00)

### Handmatig testen
```bash
curl -X POST https://powerful-creation-production.up.railway.app/cron/backup-db \
  -H "Authorization: Bearer $CRON_SECRET"
# → {"ok": true, "key": "backups/medifact-...dump", "bytes": 12345, "pruned": 0}
```

### Herstellen (restore)
```bash
# Download de gewenste dump uit R2, daarna:
pg_restore --clean --no-owner --no-privileges -d "$DATABASE_URL" medifact-YYYYMMDD-HHMMSS.dump
```

---

## Kosten per analyse

| Model | Kosten |
|-------|--------|
| claude-haiku-4-5 | ~€0.02 per analyse |
| claude-sonnet-4-x | ~€0.25 per analyse |

Bij haiku: 1000 analyses/maand ≈ €20.
