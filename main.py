#!/usr/bin/env python3
"""
Medifact — SaaS API v3.0
Multi-user · Stripe subscriptions · PostgreSQL · 8-axis scientific gate
"""

import asyncio, hashlib, itertools, json, os, re, secrets, time, uuid
from datetime import datetime, timezone, timedelta
from typing import Optional, Annotated

import httpx
import redis.asyncio as aioredis
import stripe
from anthropic import AsyncAnthropic
from fastapi import FastAPI, HTTPException, Depends, Header, Request, BackgroundTasks
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import StreamingResponse, JSONResponse
from slowapi import Limiter, _rate_limit_exceeded_handler
from slowapi.util import get_remote_address
from slowapi.errors import RateLimitExceeded
import bcrypt as _bcrypt
import jwt as _jwt
from pydantic import BaseModel, EmailStr
from sqlalchemy import Column, String, Integer, Boolean, DateTime, Text, select, update, delete, text
from sqlalchemy import func
from sqlalchemy.ext.asyncio import AsyncSession, create_async_engine, async_sessionmaker
from sqlalchemy.orm import DeclarativeBase

# ── CONFIG ─────────────────────────────────────────────────────────────────────
ANTHROPIC_API_KEY = os.environ.get("ANTHROPIC_API_KEY", "")
DATABASE_URL      = os.environ.get("DATABASE_URL", "sqlite+aiosqlite:///./medifact.db").replace("postgres://", "postgresql+asyncpg://")
JWT_SECRET        = os.environ.get("JWT_SECRET", "")
if not JWT_SECRET:
    raise RuntimeError("JWT_SECRET environment variable is not set")
RESEND_API_KEY    = os.environ.get("RESEND_API_KEY", "")
CRON_SECRET       = os.environ.get("CRON_SECRET", "")
FROM_EMAIL        = os.environ.get("FROM_EMAIL", "noreply@medifact.eu")
JWT_ALGORITHM        = "HS256"
JWT_EXPIRE_MINUTES   = 15
REFRESH_EXPIRE_DAYS  = 30
FRONTEND_URL      = os.environ.get("FRONTEND_URL", "http://localhost:8000")
MODEL             = os.environ.get("MEDIFACT_MODEL", "claude-haiku-4-5-20251001")
NCBI              = "https://eutils.ncbi.nlm.nih.gov/entrez/eutils"
CT_V2             = "https://clinicaltrials.gov/api/v2/studies"
NCBI_API_KEY      = os.environ.get("NCBI_API_KEY", "")   # Register at: https://www.ncbi.nlm.nih.gov/account/
REDIS_URL         = os.environ.get("REDIS_URL", "")
ANALYSIS_CACHE_TTL = int(os.environ.get("ANALYSIS_CACHE_TTL", "86400"))  # 24h default

# NCBI key pool — add NCBI_API_KEY_2, NCBI_API_KEY_3 etc. for higher throughput
_ncbi_key_pool = [k for name in ["NCBI_API_KEY", "NCBI_API_KEY_2", "NCBI_API_KEY_3"]
                  if (k := os.environ.get(name, ""))]
_ncbi_key_cycle = itertools.cycle(_ncbi_key_pool) if _ncbi_key_pool else None

stripe.api_key              = os.environ.get("STRIPE_SECRET_KEY", "")
STRIPE_WEBHOOK_SECRET       = os.environ.get("STRIPE_WEBHOOK_SECRET", "")
STRIPE_PRICE_STARTER        = os.environ.get("STRIPE_PRICE_STARTER", "")    # Set in Railway
STRIPE_PRICE_PROFESSIONAL   = os.environ.get("STRIPE_PRICE_PROFESSIONAL", "")
STRIPE_PRICE_ENTERPRISE     = os.environ.get("STRIPE_PRICE_ENTERPRISE", "")
STRIPE_PRICE_SOLO           = os.environ.get("STRIPE_PRICE_SOLO", "")
STRIPE_PRICE_TEAM           = os.environ.get("STRIPE_PRICE_TEAM", "")
STRIPE_PRICE_BUSINESS       = os.environ.get("STRIPE_PRICE_BUSINESS", "")

# ── SUBSCRIPTION TIERS ────────────────────────────────────────────────────────
# Canonical tiers (April 2026): free / pro / team / enterprise
# Legacy tiers (solo / professional / business) blijven voor grandfathered bestaande klanten
TIERS = {
    "free":         {"name": "Gratis",       "analyses": 5,      "price": 0,   "seats": 1,      "stripe_price": None},
    "pro":          {"name": "Pro",          "analyses": 50,     "price": 99,  "seats": 1,      "stripe_price": None},
    "team":         {"name": "Team",         "analyses": 300,    "price": 349, "seats": 10,     "stripe_price": None},
    "enterprise":   {"name": "Enterprise",   "analyses": 999999, "price": 1500,"seats": 999999, "stripe_price": None},
    # ── Legacy — grandfathered bestaande klanten (niet zichtbaar op pricing page) ──
    "solo":         {"name": "Solo",         "analyses": 30,     "price": 79,  "seats": 1,      "stripe_price": STRIPE_PRICE_SOLO},
    "professional": {"name": "Professional", "analyses": 100,    "price": 199, "seats": 1,      "stripe_price": STRIPE_PRICE_PROFESSIONAL},
    "business":     {"name": "Business",     "analyses": 600,    "price": 799, "seats": 15,     "stripe_price": STRIPE_PRICE_BUSINESS},
}

# ── DATABASE ───────────────────────────────────────────────────────────────────
_pool_kwargs = {"pool_size": 10, "max_overflow": 20, "pool_pre_ping": True} if "postgresql" in DATABASE_URL else {}
engine         = create_async_engine(DATABASE_URL, echo=False, **_pool_kwargs)
AsyncSessionLocal = async_sessionmaker(engine, expire_on_commit=False)

class Base(DeclarativeBase):
    pass

class User(Base):
    __tablename__ = "users"
    id                  = Column(String, primary_key=True, default=lambda: str(uuid.uuid4()))
    email               = Column(String, unique=True, nullable=False, index=True)
    name                = Column(String, nullable=False)
    password_hash       = Column(String, nullable=False)
    tier                = Column(String, default="free")
    stripe_customer_id  = Column(String, nullable=True)
    stripe_sub_id       = Column(String, nullable=True)
    analyses_used       = Column(Integer, default=0)
    analyses_reset      = Column(DateTime(timezone=True), default=lambda: datetime.now(timezone.utc))
    is_active               = Column(Boolean, default=True)
    failed_login_attempts   = Column(Integer, default=0)
    locked_until            = Column(DateTime(timezone=True), nullable=True)
    email_verified          = Column(Boolean, default=False)
    email_verification_token = Column(String, nullable=True)
    created_at              = Column(DateTime(timezone=True), default=lambda: datetime.now(timezone.utc))

class Analysis(Base):
    __tablename__ = "analyses"
    id          = Column(String, primary_key=True, default=lambda: str(uuid.uuid4()))
    user_id     = Column(String, nullable=False, index=True)
    query       = Column(String, nullable=False)
    normalized  = Column(String, nullable=True)
    gate        = Column(String, nullable=False)   # open | closed
    score       = Column(String, nullable=False)   # e.g. "7/8"
    axes_json   = Column(Text, nullable=True)      # full JSON of axis results
    rid         = Column(String, nullable=True)
    created_at  = Column(DateTime(timezone=True), default=lambda: datetime.now(timezone.utc))

class ContactRequest(Base):
    __tablename__ = "contact_requests"
    id         = Column(String, primary_key=True, default=lambda: str(uuid.uuid4()))
    name       = Column(String, nullable=False)
    email      = Column(String, nullable=False)
    message    = Column(Text, nullable=True)
    plan       = Column(String, nullable=True)
    status     = Column(String, default="new")   # new | handled
    created_at = Column(DateTime(timezone=True), default=lambda: datetime.now(timezone.utc))

class SharedAnalysis(Base):
    __tablename__ = "shared_analyses"
    id                  = Column(String, primary_key=True, default=lambda: str(uuid.uuid4()))
    token               = Column(String, unique=True, nullable=False, index=True,
                                 default=lambda: secrets.token_urlsafe(16))
    user_id             = Column(String, nullable=False)
    sharer_name         = Column(String, nullable=True)
    query               = Column(String, nullable=False)
    results_json        = Column(Text, nullable=False)
    normalize_info_json = Column(Text, nullable=True)
    verdict             = Column(String, nullable=False)
    passed              = Column(Integer, nullable=False)
    created_at          = Column(DateTime(timezone=True), default=lambda: datetime.now(timezone.utc))
    expires_at          = Column(DateTime(timezone=True), nullable=False)
    view_count          = Column(Integer, default=0)

class PasswordResetToken(Base):
    __tablename__ = "password_reset_tokens"
    id         = Column(String, primary_key=True, default=lambda: str(uuid.uuid4()))
    user_id    = Column(String, nullable=False, index=True)
    token      = Column(String, unique=True, nullable=False, index=True,
                        default=lambda: secrets.token_urlsafe(32))
    expires_at = Column(DateTime(timezone=True), nullable=False)
    used       = Column(Boolean, default=False)
    created_at = Column(DateTime(timezone=True), default=lambda: datetime.now(timezone.utc))

class RefreshToken(Base):
    __tablename__ = "refresh_tokens"
    id         = Column(String, primary_key=True, default=lambda: str(uuid.uuid4()))
    user_id    = Column(String, nullable=False, index=True)
    token      = Column(String, unique=True, nullable=False, index=True)
    expires_at = Column(DateTime(timezone=True), nullable=False)
    revoked    = Column(Boolean, default=False)
    created_at = Column(DateTime(timezone=True), default=lambda: datetime.now(timezone.utc))

class EmailTemplate(Base):
    __tablename__ = "email_templates"
    key        = Column(String, primary_key=True)   # welcome | reminder | low_credits | password_reset | weekly_digest
    subject    = Column(String, nullable=False)
    html_body  = Column(Text, nullable=False)
    updated_at = Column(DateTime(timezone=True), default=lambda: datetime.now(timezone.utc))

class AnalysisJob(Base):
    __tablename__ = "analysis_jobs"
    id          = Column(String, primary_key=True, default=lambda: str(uuid.uuid4()))
    user_id     = Column(String, nullable=False, index=True)
    user_email  = Column(String, nullable=False)
    user_name   = Column(String, nullable=False)
    query       = Column(String, nullable=False)
    status      = Column(String, default="pending", index=True)  # pending | running | done | failed
    result_json = Column(Text, nullable=True)
    error       = Column(String, nullable=True)
    created_at  = Column(DateTime(timezone=True), default=lambda: datetime.now(timezone.utc))
    started_at  = Column(DateTime(timezone=True), nullable=True)
    finished_at = Column(DateTime(timezone=True), nullable=True)

# ── APP SETUP ──────────────────────────────────────────────────────────────────
app = FastAPI(title="Medifact API", version="3.0.0")

def _real_ip(request: Request) -> str:
    # Railway/Fastly forwards the real client IP in X-Forwarded-For
    xff = request.headers.get("X-Forwarded-For", "")
    if xff:
        return xff.split(",")[0].strip()
    return request.client.host if request.client else "127.0.0.1"

limiter = Limiter(key_func=_real_ip, storage_uri=REDIS_URL if REDIS_URL else "memory://")
app.state.limiter = limiter
app.add_exception_handler(RateLimitExceeded, _rate_limit_exceeded_handler)
app.add_middleware(
    CORSMiddleware,
    allow_origins=["https://medifact.eu", "https://www.medifact.eu"],
    allow_credentials=True,
    allow_methods=["GET", "POST", "PATCH", "DELETE", "OPTIONS"],
    allow_headers=["Authorization", "Content-Type", "X-Admin-Secret"],
)
claude = AsyncAnthropic(api_key=ANTHROPIC_API_KEY)

# Shared HTTP client — avoids TCP handshake per call
_http = httpx.AsyncClient(timeout=15)
# Max 4 concurrent NCBI requests — tuned to NCBI's 10 req/s limit (~400ms avg call = 4×2.5/s ≈ 10/s)
_ncbi_sem = asyncio.Semaphore(4)
# Max 5 concurrent Claude calls — prevents Anthropic TPM bursts under heavy load
_claude_sem = asyncio.Semaphore(5)
# Lazy Redis client — initialised on first use
_redis: aioredis.Redis | None = None

async def _get_redis() -> aioredis.Redis | None:
    global _redis
    if not REDIS_URL:
        return None
    if _redis is None:
        _redis = aioredis.from_url(REDIS_URL, decode_responses=True)
    return _redis

async def _cache_get(key: str) -> list | None:
    r = await _get_redis()
    if not r:
        return None
    try:
        data = await r.get(key)
        return json.loads(data) if data else None
    except Exception:
        return None

async def _cache_set(key: str, value: list, ttl: int = ANALYSIS_CACHE_TTL) -> None:
    r = await _get_redis()
    if not r:
        return
    try:
        await r.setex(key, ttl, json.dumps(value))
    except Exception:
        pass

# ── SLOT TRACKER (Redis sorted set) ────────────────────────────────────────────
# Each active analysis holds a slot: member=slot_id, score=expiry_timestamp.
# Expired slots are cleaned automatically on every acquire/count call.
# TTL of 300 s covers browser crashes that never release their slot.
_SLOT_KEY = "medifact:analysis_slots"
_SLOT_TTL = 300  # seconds

_SLOT_ACQUIRE_LUA = """
local key = KEYS[1]
local max_slots = tonumber(ARGV[1])
local slot_id = ARGV[2]
local expiry = tonumber(ARGV[3])
local now = tonumber(ARGV[4])
redis.call('ZREMRANGEBYSCORE', key, '-inf', now)
local count = redis.call('ZCARD', key)
if count >= max_slots then
    return 0
end
redis.call('ZADD', key, expiry, slot_id)
return 1
"""

async def _slot_acquire(slot_id: str) -> bool:
    r = await _get_redis()
    if not r:
        return True  # fail-open: no Redis → don't block
    max_slots = int(os.environ.get("MAX_CONCURRENT_ANALYSES", "3"))
    now = time.time()
    try:
        # Clean expired slots, count active, add new slot if room
        await r.zremrangebyscore(_SLOT_KEY, "-inf", now)
        count = await r.zcard(_SLOT_KEY)
        if count >= max_slots:
            return False
        await r.zadd(_SLOT_KEY, {slot_id: now + _SLOT_TTL})
        return True
    except Exception as e:
        print(f"[Slot] Redis error in acquire: {e}", flush=True)
        return True  # fail-open on Redis error

async def _slot_release(slot_id: str) -> None:
    r = await _get_redis()
    if not r:
        return
    try:
        await r.zrem(_SLOT_KEY, slot_id)
    except Exception:
        pass

DEFAULT_EMAIL_TEMPLATES = {
    "welcome": {
        "subject": "Welkom bij Medifact — je eerste claim analyseren?",
        "html_body": """<!DOCTYPE html><html lang="nl"><head><meta charset="UTF-8"/><meta name="viewport" content="width=device-width,initial-scale=1.0"/></head>
<body style="margin:0;padding:0;background:#F1F5F9;font-family:-apple-system,BlinkMacSystemFont,'Segoe UI',Roboto,sans-serif;">
<table width="100%" cellpadding="0" cellspacing="0" style="background:#F1F5F9;padding:40px 16px;"><tr><td align="center">
<table width="600" cellpadding="0" cellspacing="0" style="max-width:600px;width:100%;">
<tr><td style="background:#1E4DA1;border-radius:12px 12px 0 0;padding:28px 36px;text-align:center;">
  <div style="font-size:22px;font-weight:800;color:#fff;letter-spacing:-.3px;">medifact.eu</div>
  <div style="font-size:12px;color:#93C5FD;margin-top:4px;letter-spacing:.5px;text-transform:uppercase;">Evidence Intelligence Platform</div>
</td></tr>
<tr><td style="background:#ffffff;padding:36px;">
  <p style="font-size:16px;font-weight:700;color:#0F172A;margin:0 0 8px;">Hallo {{name}},</p>
  <p style="font-size:15px;color:#475569;line-height:1.7;margin:0 0 24px;">
    Je account is aangemaakt. Je hebt <strong style="color:#1E4DA1;">10 gratis analyses</strong> — gebruik ze om te zien wat Medifact voor jouw claims kan doen.
  </p>
  <div style="text-align:center;margin-bottom:28px;">
    <a href="https://medifact.eu/dashboard" style="display:inline-block;background:#1E4DA1;color:#ffffff;font-size:15px;font-weight:700;padding:14px 36px;border-radius:10px;text-decoration:none;">
      Analyseer je eerste claim →
    </a>
    <div style="font-size:12px;color:#94A3B8;margin-top:10px;">Geen creditcard nodig · Resultaat binnen 30 seconden</div>
  </div>
</td></tr>
<tr><td style="background:#F8FAFC;border-radius:0 0 12px 12px;padding:20px 36px;border-top:1px solid #E2E8F0;">
  <p style="font-size:11px;color:#94A3B8;margin:0;">Je ontvangt deze e-mail omdat je een account hebt aangemaakt op <a href="https://medifact.eu" style="color:#1E4DA1;">medifact.eu</a>.</p>
</td></tr>
</table></td></tr></table></body></html>""",
    },
    "reminder": {
        "subject": "Je hebt nog {{remaining}} gratis analyses — probeer er één",
        "html_body": """<!DOCTYPE html><html lang="nl"><head><meta charset="UTF-8"/></head>
<body style="margin:0;padding:0;background:#F1F5F9;font-family:-apple-system,BlinkMacSystemFont,'Segoe UI',Roboto,sans-serif;">
<table width="100%" cellpadding="0" cellspacing="0" style="background:#F1F5F9;padding:40px 16px;"><tr><td align="center">
<table width="600" cellpadding="0" cellspacing="0" style="max-width:600px;width:100%;">
<tr><td style="background:#1E4DA1;border-radius:12px 12px 0 0;padding:28px 36px;text-align:center;">
  <div style="font-size:22px;font-weight:800;color:#fff;">medifact.eu</div>
</td></tr>
<tr><td style="background:#ffffff;padding:36px;">
  <p style="font-size:16px;font-weight:700;color:#0F172A;margin:0 0 8px;">Hallo {{name}},</p>
  <p style="font-size:15px;color:#475569;line-height:1.7;margin:0 0 20px;">
    Je hebt je account aangemaakt maar nog weinig analyses gedaan. Je hebt nog <strong style="color:#1E4DA1;">{{remaining}} gratis analyses</strong> die op je wachten.
  </p>
  <div style="text-align:center;margin-bottom:20px;">
    <a href="https://medifact.eu/dashboard" style="display:inline-block;background:#1E4DA1;color:#ffffff;font-size:15px;font-weight:700;padding:14px 36px;border-radius:10px;text-decoration:none;">
      Start je analyse →
    </a>
  </div>
</td></tr>
<tr><td style="background:#F8FAFC;border-radius:0 0 12px 12px;padding:20px 36px;border-top:1px solid #E2E8F0;">
  <p style="font-size:11px;color:#94A3B8;margin:0;"><a href="https://medifact.eu" style="color:#1E4DA1;">medifact.eu</a></p>
</td></tr>
</table></td></tr></table></body></html>""",
    },
    "low_credits": {
        "subject": "Je hebt nog 2 gratis analyses over — upgrade of gebruik ze nu",
        "html_body": """<!DOCTYPE html><html lang="nl"><head><meta charset="UTF-8"/></head>
<body style="margin:0;padding:0;background:#F1F5F9;font-family:-apple-system,BlinkMacSystemFont,'Segoe UI',Roboto,sans-serif;">
<table width="100%" cellpadding="0" cellspacing="0" style="background:#F1F5F9;padding:40px 16px;"><tr><td align="center">
<table width="600" cellpadding="0" cellspacing="0" style="max-width:600px;width:100%;">
<tr><td style="background:#1E4DA1;border-radius:12px 12px 0 0;padding:28px 36px;text-align:center;">
  <div style="font-size:22px;font-weight:800;color:#fff;">medifact.eu</div>
  <div style="font-size:12px;color:#93C5FD;margin-top:4px;text-transform:uppercase;">Evidence Intelligence Platform</div>
</td></tr>
<tr><td style="background:#ffffff;padding:36px;">
  <p style="font-size:16px;font-weight:700;color:#0F172A;margin:0 0 8px;">Hallo {{name}},</p>
  <div style="background:#FFF7ED;border:1.5px solid #FED7AA;border-radius:10px;padding:16px 20px;margin-bottom:20px;">
    <div style="font-size:14px;font-weight:700;color:#C2410C;margin-bottom:4px;">⚠️ Je hebt nog {{remaining}} gratis analyse(s) over</div>
    <div style="font-size:13px;color:#92400E;line-height:1.6;">Je hebt {{analyses_used}} van je 10 gratis analyses gebruikt. Daarna heb je een abonnement nodig.</div>
  </div>
  <p style="font-size:15px;color:#475569;line-height:1.7;margin:0 0 24px;">
    Wil je onbeperkt claims blijven analyseren? Upgrade dan nu naar een betaald abonnement.
  </p>
  <div style="text-align:center;margin-bottom:16px;">
    <a href="https://medifact.eu/dashboard/subscription" style="display:inline-block;background:#1E4DA1;color:#ffffff;font-size:15px;font-weight:700;padding:14px 36px;border-radius:10px;text-decoration:none;">
      Bekijk abonnementen →
    </a>
  </div>
  <div style="text-align:center;">
    <a href="https://medifact.eu/dashboard" style="font-size:13px;color:#64748B;text-decoration:none;">
      Of gebruik eerst je resterende analyse(s) →
    </a>
  </div>
</td></tr>
<tr><td style="background:#F8FAFC;border-radius:0 0 12px 12px;padding:20px 36px;border-top:1px solid #E2E8F0;">
  <p style="font-size:11px;color:#94A3B8;margin:0;"><a href="https://medifact.eu" style="color:#1E4DA1;">medifact.eu</a> · <a href="mailto:hello@medifact.eu" style="color:#1E4DA1;">Uitschrijven</a></p>
</td></tr>
</table></td></tr></table></body></html>""",
    },
    "password_reset": {
        "subject": "Wachtwoord herstellen — Medifact",
        "html_body": """<!DOCTYPE html><html lang="nl"><head><meta charset="UTF-8"/></head>
<body style="margin:0;padding:0;background:#F1F5F9;font-family:-apple-system,BlinkMacSystemFont,'Segoe UI',Roboto,sans-serif;">
<table width="100%" cellpadding="0" cellspacing="0" style="background:#F1F5F9;padding:40px 16px;"><tr><td align="center">
<table width="600" cellpadding="0" cellspacing="0" style="max-width:600px;width:100%;">
<tr><td style="background:#1E4DA1;border-radius:12px 12px 0 0;padding:28px 36px;text-align:center;">
  <div style="font-size:22px;font-weight:800;color:#fff;">medifact.eu</div>
  <div style="font-size:12px;color:#93C5FD;margin-top:4px;text-transform:uppercase;">Evidence Intelligence Platform</div>
</td></tr>
<tr><td style="background:#ffffff;padding:36px;">
  <p style="font-size:16px;font-weight:700;color:#0F172A;margin:0 0 8px;">Hallo {{name}},</p>
  <p style="font-size:15px;color:#475569;line-height:1.7;margin:0 0 24px;">
    We hebben een verzoek ontvangen om het wachtwoord van je Medifact-account te herstellen. Klik op de knop hieronder om een nieuw wachtwoord in te stellen.
  </p>
  <div style="text-align:center;margin-bottom:24px;">
    <a href="{{reset_link}}" style="display:inline-block;background:#1E4DA1;color:#ffffff;font-size:15px;font-weight:700;padding:14px 36px;border-radius:10px;text-decoration:none;">
      Stel nieuw wachtwoord in →
    </a>
    <div style="font-size:12px;color:#94A3B8;margin-top:10px;">Deze link is 1 uur geldig</div>
  </div>
  <div style="background:#F8FAFC;border:1px solid #E2E8F0;border-radius:8px;padding:14px 16px;margin-bottom:16px;">
    <div style="font-size:11px;font-weight:700;color:#94A3B8;text-transform:uppercase;margin-bottom:6px;">Of kopieer deze link:</div>
    <div style="font-size:12px;color:#475569;word-break:break-all;">{{reset_link}}</div>
  </div>
  <p style="font-size:13px;color:#94A3B8;margin:0;">Als jij dit verzoek niet hebt gedaan, kun je deze e-mail veilig negeren. Je wachtwoord blijft ongewijzigd.</p>
</td></tr>
<tr><td style="background:#F8FAFC;border-radius:0 0 12px 12px;padding:20px 36px;border-top:1px solid #E2E8F0;">
  <p style="font-size:11px;color:#94A3B8;margin:0;"><a href="https://medifact.eu" style="color:#1E4DA1;">medifact.eu</a> · <a href="mailto:hello@medifact.eu" style="color:#1E4DA1;">Support</a></p>
</td></tr>
</table></td></tr></table></body></html>""",
    },
    "weekly_digest": {
        "subject": "📊 Jouw wekelijkse Medifact digest",
        "html_body": """<!DOCTYPE html><html lang="nl"><head><meta charset="UTF-8"/></head>
<body style="margin:0;padding:0;background:#F1F5F9;font-family:-apple-system,BlinkMacSystemFont,'Segoe UI',Roboto,sans-serif;">
<table width="100%" cellpadding="0" cellspacing="0" style="background:#F1F5F9;padding:40px 16px;"><tr><td align="center">
<table width="600" cellpadding="0" cellspacing="0" style="max-width:600px;width:100%;">
<tr><td style="background:#1E4DA1;border-radius:12px 12px 0 0;padding:28px 36px;text-align:center;">
  <div style="font-size:22px;font-weight:800;color:#fff;">medifact.eu</div>
  <div style="font-size:12px;color:#93C5FD;margin-top:4px;text-transform:uppercase;">Wekelijkse Digest</div>
</td></tr>
<tr><td style="background:#ffffff;padding:36px;">
  <p style="font-size:16px;font-weight:700;color:#0F172A;margin:0 0 8px;">Hallo {{name}},</p>
  <p style="font-size:15px;color:#475569;line-height:1.7;margin:0 0 20px;">
    Je hebt deze week <strong style="color:#1E4DA1;">{{analyses_used}} van {{limit}}</strong> analyses gebruikt.
  </p>
  <div style="text-align:center;margin-bottom:20px;">
    <a href="https://medifact.eu/dashboard" style="display:inline-block;background:#1E4DA1;color:#ffffff;font-size:15px;font-weight:700;padding:14px 36px;border-radius:10px;text-decoration:none;">
      Naar dashboard →
    </a>
  </div>
</td></tr>
<tr><td style="background:#F8FAFC;border-radius:0 0 12px 12px;padding:20px 36px;border-top:1px solid #E2E8F0;">
  <p style="font-size:11px;color:#94A3B8;margin:0;"><a href="https://medifact.eu" style="color:#1E4DA1;">medifact.eu</a> · <a href="mailto:hello@medifact.eu" style="color:#1E4DA1;">Uitschrijven</a></p>
</td></tr>
</table></td></tr></table></body></html>""",
    },
}

async def _process_next_job() -> None:
    """Pick up one pending job, run it, email the user."""
    async with AsyncSessionLocal() as db:
        result = await db.execute(
            select(AnalysisJob)
            .where(AnalysisJob.status == "pending")
            .order_by(AnalysisJob.created_at)
            .limit(1)
            .with_for_update(skip_locked=True)
        )
        job = result.scalar_one_or_none()
        if not job:
            return
        job.status = "running"
        job.started_at = datetime.now(timezone.utc)
        await db.commit()
        job_id, user_id = job.id, job.user_id
        user_email, user_name, query = job.user_email, job.user_name, job.query

    try:
        cache_key = "medifact:axes:" + hashlib.sha256(query.lower().strip().encode()).hexdigest()[:32]
        cached = await _cache_get(cache_key)
        if cached:
            results = [AxisResult(**r) for r in cached]
        else:
            pr = PROFILE
            axis_fns = [run_a1, run_a2, run_a3, run_a4, run_a5, run_a6, run_a7, run_a8]
            results = list(await asyncio.gather(*[fn(query, pr) for fn in axis_fns]))
            await _cache_set(cache_key, [r.dict() for r in results])

        failed = [r for r in results if r.status == "fail"]
        passed = [r for r in results if r.status == "pass"]
        meta_s, meta_d = meta_check(list(results))
        is_open = len(failed) == 0 and meta_s != "FAIL"
        rid = "MF-" + uuid.uuid4().hex[:8]
        ts  = datetime.now(timezone.utc).isoformat()
        result_payload = {
            "query": query, "profile": "medical", "profile_label": "Medical",
            "gate": "open" if is_open else "closed",
            "score": f"{len(passed)}/{len(results)}",
            "results": [r.dict() for r in results],
            "meta": {"status": meta_s, "detail": meta_d},
            "rid": rid, "timestamp": ts,
            "regulatory": PROFILE.get("reg", []),
        }

        async with AsyncSessionLocal() as db:
            db.add(Analysis(
                user_id=user_id, query=query,
                gate="open" if is_open else "closed",
                score=f"{len(passed)}/{len(results)}",
                axes_json=json.dumps([r.dict() for r in results]),
                rid=rid,
            ))
            await db.execute(update(User).where(User.id == user_id).values(
                analyses_used=User.analyses_used + 1
            ))
            await db.execute(update(AnalysisJob).where(AnalysisJob.id == job_id).values(
                status="done",
                result_json=json.dumps(result_payload),
                finished_at=datetime.now(timezone.utc),
            ))
            await db.commit()

        first_name  = user_name.split()[0] if user_name else "daar"
        passed_count = len(passed)
        total        = len(results)

        # Verdict copy + colours — matches frontend logic (8=bewezen, 5-7=matig, 0-4=afgewezen)
        if is_open:
            verdict_nl     = "Wetenschappelijk ondersteund"
            verdict_bg     = "#F0FDF4"; verdict_border = "#BBF7D0"
            verdict_color  = "#15803D"; verdict_sub    = "#166534"
        elif passed_count >= 5:
            verdict_nl     = "Beperkte wetenschappelijke ondersteuning"
            verdict_bg     = "#FFFBEB"; verdict_border = "#FDE68A"
            verdict_color  = "#B45309"; verdict_sub    = "#92400E"
        else:
            verdict_nl     = "Niet wetenschappelijk ondersteund"
            verdict_bg     = "#FEF2F2"; verdict_border = "#FECACA"
            verdict_color  = "#DC2626"; verdict_sub    = "#991B1B"

        html = f"""<!DOCTYPE html>
<html lang="nl">
<head>
  <meta charset="UTF-8"/>
  <meta name="viewport" content="width=device-width,initial-scale=1.0"/>
</head>
<body style="margin:0;padding:0;background:#F1F5F9;font-family:-apple-system,BlinkMacSystemFont,'Segoe UI',Roboto,sans-serif;">
<table width="100%" cellpadding="0" cellspacing="0" style="background:#F1F5F9;padding:40px 16px;">
<tr><td align="center">
<table width="600" cellpadding="0" cellspacing="0" style="max-width:600px;width:100%;">

  <!-- Header -->
  <tr><td style="background:#1E4DA1;border-radius:16px 16px 0 0;padding:28px 36px;text-align:center;">
    <div style="font-size:24px;font-weight:800;color:#fff;letter-spacing:-0.5px;">medifact.eu</div>
    <div style="font-size:11px;color:#93C5FD;margin-top:4px;letter-spacing:1px;text-transform:uppercase;">Evidence Intelligence Platform</div>
  </td></tr>

  <!-- Body -->
  <tr><td style="background:#ffffff;padding:36px 36px 28px;">
    <p style="font-size:18px;font-weight:700;color:#0F172A;margin:0 0 6px;">Hallo {first_name},</p>
    <p style="font-size:15px;color:#475569;line-height:1.7;margin:0 0 28px;">
      Je analyse is voltooid. Hieronder vind je een samenvatting van het resultaat.
    </p>

    <!-- Claim -->
    <div style="background:#F8FAFC;border:1px solid #E2E8F0;border-radius:12px;padding:18px 22px;margin-bottom:16px;">
      <div style="font-size:11px;font-weight:600;color:#94A3B8;text-transform:uppercase;letter-spacing:0.6px;margin-bottom:8px;">Geanalyseerde claim</div>
      <div style="font-size:15px;font-weight:600;color:#0F172A;line-height:1.55;">&ldquo;{query[:200]}&rdquo;</div>
    </div>

    <!-- Verdict -->
    <div style="background:{verdict_bg};border:1px solid {verdict_border};border-radius:12px;padding:20px 22px;margin-bottom:28px;">
      <div style="font-size:11px;font-weight:600;color:{verdict_sub};text-transform:uppercase;letter-spacing:0.6px;margin-bottom:6px;">Uitkomst</div>
      <div style="font-size:20px;font-weight:800;color:{verdict_color};margin-bottom:4px;">{verdict_nl}</div>
      <div style="font-size:13px;color:{verdict_sub};">{passed_count} van {total} wetenschappelijke criteria geslaagd</div>
    </div>

    <!-- CTA -->
    <table width="100%" cellpadding="0" cellspacing="0">
      <tr><td align="center" style="padding-bottom:16px;">
        <a href="{FRONTEND_URL}/dashboard"
           style="display:inline-block;background:#1E4DA1;color:#ffffff;font-size:15px;font-weight:700;padding:16px 48px;border-radius:12px;text-decoration:none;letter-spacing:0.01em;">
          Bekijk volledig rapport &rarr;
        </a>
      </td></tr>
    </table>
    <p style="font-size:13px;color:#94A3B8;text-align:center;margin:0;">
      Je vindt het rapport ook terug onder <strong style="color:#64748B;">Geschiedenis</strong> in je dashboard.
    </p>
  </td></tr>

  <!-- Footer -->
  <tr><td style="background:#F8FAFC;border-radius:0 0 16px 16px;padding:18px 36px;border-top:1px solid #E2E8F0;">
    <p style="font-size:11px;color:#94A3B8;margin:0;text-align:center;">
      Je ontvangt dit bericht omdat je een analyse hebt aangevraagd via
      <a href="{FRONTEND_URL}" style="color:#1E4DA1;text-decoration:none;">medifact.eu</a>
    </p>
  </td></tr>

</table>
</td></tr>
</table>
</body>
</html>"""
        await _send_resend_email(user_email, f"Medifact analyse klaar: {verdict_nl}", html)
        print(f"[Worker] Job {job_id} done — emailed {user_email}")

    except Exception as e:
        print(f"[Worker] Job {job_id} failed: {e}")
        try:
            async with AsyncSessionLocal() as db:
                await db.execute(update(AnalysisJob).where(AnalysisJob.id == job_id).values(
                    status="failed",
                    error=str(e)[:500],
                    finished_at=datetime.now(timezone.utc),
                ))
                await db.commit()
        except Exception:
            pass


async def _job_worker() -> None:
    """Long-running background task: polls for pending jobs every 5 seconds."""
    await asyncio.sleep(10)  # Let startup finish first
    while True:
        try:
            await _process_next_job()
        except Exception as e:
            print(f"[Worker] Unexpected error: {e}")
        await asyncio.sleep(5)


@app.on_event("startup")
async def startup():
    try:
        # Step 1: create new tables in own transaction
        async with engine.begin() as conn:
            await conn.run_sync(Base.metadata.create_all)
        print("✅ Database tables created/verified")

        # Step 2: column migrations — each in own transaction so one failure can't abort others
        for col_sql in [
            "ALTER TABLE users ADD COLUMN failed_login_attempts INTEGER DEFAULT 0",
            "ALTER TABLE users ADD COLUMN locked_until TIMESTAMP WITH TIME ZONE",
            "ALTER TABLE users ADD COLUMN email_verified BOOLEAN DEFAULT TRUE",
            "ALTER TABLE users ADD COLUMN email_verification_token VARCHAR",
        ]:
            try:
                async with engine.begin() as conn:
                    await conn.execute(text(col_sql))
            except Exception:
                pass  # Column already exists

        # Step 3: mark pre-existing accounts as verified
        try:
            async with engine.begin() as conn:
                await conn.execute(text(
                    "UPDATE users SET email_verified = TRUE WHERE email_verification_token IS NULL"
                ))
        except Exception:
            pass
        # Seed default email templates (skip if already exist)
        async with AsyncSessionLocal() as session:
            for key, tpl in DEFAULT_EMAIL_TEMPLATES.items():
                existing = await session.execute(select(EmailTemplate).where(EmailTemplate.key == key))
                if not existing.scalar_one_or_none():
                    session.add(EmailTemplate(key=key, subject=tpl["subject"], html_body=tpl["html_body"]))
            await session.commit()
        print("✅ Email templates seeded")
    except Exception as e:
        print(f"❌ DB startup error: {e}")

    # Start 3 parallel background job workers for queued analyses
    for _ in range(3):
        asyncio.create_task(_job_worker())
    print("✅ Analysis job workers started (3 parallel)")



# ── AUTH UTILITIES ─────────────────────────────────────────────────────────────
async def hash_password(pw: str) -> str:
    loop = asyncio.get_event_loop()
    return await loop.run_in_executor(None, lambda: _bcrypt.hashpw(pw.encode(), _bcrypt.gensalt()).decode())

async def verify_password(pw: str, hashed: str) -> bool:
    loop = asyncio.get_event_loop()
    return await loop.run_in_executor(None, lambda: _bcrypt.checkpw(pw.encode(), hashed.encode()))

def log_security(event: str, **kwargs):
    import json as _json
    print(_json.dumps({"sec": event, "ts": datetime.now(timezone.utc).isoformat(), **kwargs}), flush=True)

def create_token(user_id: str, email: str) -> str:
    payload = {
        "sub": user_id,
        "email": email,
        "exp": datetime.now(timezone.utc) + timedelta(minutes=JWT_EXPIRE_MINUTES),
    }
    return _jwt.encode(payload, JWT_SECRET, algorithm=JWT_ALGORITHM)

async def create_refresh_token(user_id: str, db: AsyncSession) -> str:
    token = secrets.token_urlsafe(48)
    rt = RefreshToken(
        user_id=user_id,
        token=token,
        expires_at=datetime.now(timezone.utc) + timedelta(days=REFRESH_EXPIRE_DAYS),
    )
    db.add(rt)
    await db.commit()
    return token

async def get_db():
    async with AsyncSessionLocal() as session:
        yield session

async def get_current_user(
    authorization: Annotated[Optional[str], Header()] = None,
    db: AsyncSession = Depends(get_db),
) -> User:
    if not authorization or not authorization.startswith("Bearer "):
        raise HTTPException(status_code=401, detail="Niet ingelogd")
    token = authorization.split(" ", 1)[1]
    try:
        payload = _jwt.decode(token, JWT_SECRET, algorithms=[JWT_ALGORITHM])
        user_id: str = payload.get("sub")
    except _jwt.PyJWTError:
        raise HTTPException(status_code=401, detail="Ongeldige sessie — log opnieuw in")
    result = await db.execute(select(User).where(User.id == user_id))
    user = result.scalar_one_or_none()
    if not user or not user.is_active:
        raise HTTPException(status_code=401, detail="Gebruiker niet gevonden")
    return user

async def check_usage(user: User, db: AsyncSession):
    """Reset monthly counter if needed, then check limit."""
    now = datetime.now(timezone.utc)
    reset = user.analyses_reset
    if reset.tzinfo is None:
        reset = reset.replace(tzinfo=timezone.utc)
    if (now - reset).days >= 30:
        await db.execute(
            update(User).where(User.id == user.id).values(analyses_used=0, analyses_reset=now)
        )
        await db.commit()
        user.analyses_used = 0
    limit = TIERS.get(user.tier, TIERS["free"])["analyses"]
    if user.analyses_used >= limit:
        raise HTTPException(
            status_code=429,
            detail=f"Maandlimiet bereikt ({limit} analyses voor {TIERS[user.tier]['name']}). Upgrade je abonnement."
        )

def _render_template(html: str, variables: dict) -> str:
    """Replace {{var}} placeholders in template HTML."""
    for k, v in variables.items():
        html = html.replace(f"{{{{{k}}}}}", str(v))
    return html

async def _send_email_bg(key: str, to: str, variables: dict) -> None:
    """Background-safe email sender: opens its own DB session so it works after request ends."""
    try:
        async with AsyncSessionLocal() as db:
            result = await db.execute(select(EmailTemplate).where(EmailTemplate.key == key))
            tpl = result.scalar_one_or_none()
            if tpl:
                subject_tpl, html_tpl = tpl.subject, tpl.html_body
            else:
                fallback = DEFAULT_EMAIL_TEMPLATES.get(key, {"subject": key, "html_body": "<p>{{name}}</p>"})
                subject_tpl, html_tpl = fallback["subject"], fallback["html_body"]
        subject = _render_template(subject_tpl, variables)
        html    = _render_template(html_tpl, variables)
        await _send_resend_email(to, subject, html)
    except Exception as e:
        print(f"[Email BG] Failed to send '{key}' to {to}: {e}")

async def _send_templated_email(db: AsyncSession, key: str, to: str, variables: dict) -> tuple[bool, str]:
    """Inline (in-request) send: uses the provided session. For admin test endpoints."""
    result = await db.execute(select(EmailTemplate).where(EmailTemplate.key == key))
    tpl = result.scalar_one_or_none()
    if tpl:
        subject_tpl, html_tpl = tpl.subject, tpl.html_body
    else:
        fallback = DEFAULT_EMAIL_TEMPLATES.get(key, {"subject": key, "html_body": "<p>{{name}}</p>"})
        subject_tpl, html_tpl = fallback["subject"], fallback["html_body"]
    subject = _render_template(subject_tpl, variables)
    html    = _render_template(html_tpl, variables)
    return await _send_resend_email(to, subject, html)

async def increment_usage(user: User, db: AsyncSession):
    new_count = user.analyses_used + 1
    await db.execute(
        update(User).where(User.id == user.id).values(analyses_used=new_count)
    )
    await db.commit()
    # Low-credits warning: send when free user hits 8 out of 10
    if user.tier == "free" and new_count == 8:
        remaining = 10 - new_count
        asyncio.create_task(_send_email_bg(
            key="low_credits", to=user.email,
            variables={"name": user.name.split()[0], "remaining": remaining, "analyses_used": new_count},
        ))

# ── PYDANTIC SCHEMAS ───────────────────────────────────────────────────────────
class RegisterRequest(BaseModel):
    email: str
    name: str
    password: str

class LoginRequest(BaseModel):
    email: str
    password: str

class NormalizeRequest(BaseModel):
    query: str
    profile: str = "medical"

class AnalyzeRequest(BaseModel):
    query: str
    profile: str = "medical"

class CheckoutRequest(BaseModel):
    tier: str   # starter | professional | enterprise

class SaveAnalysisRequest(BaseModel):
    query: str
    score: str
    gate: str
    axes_json: Optional[str] = None

class ForgotPasswordRequest(BaseModel):
    email: str

class ResetPasswordRequest(BaseModel):
    token: str
    password: str

class EmailTemplatePatch(BaseModel):
    subject: Optional[str] = None
    html_body: Optional[str] = None

class Paper(BaseModel):
    pmid: str = ""; title: str = ""; authors: str = ""
    journal: str = ""; year: str = ""; doi: Optional[str] = None

class Source(BaseModel):
    label: str; url: str; type: str = "live"

class AxisResult(BaseModel):
    axis: str; name: str; status: str; score: str
    finding: str; detail: str; papers: list[Paper] = []
    sources: list[Source] = []; confidence: str = "MEDIUM"
    live: bool = True; quantValues: list[dict] = []; ctStudies: list[dict] = []

# ── MEDIFACT GUARDRAILS ─────────────────────────────────────────────────────────────
GUARDRAILS = """
MASTER MEDIFACT GUARDRAILS — STRICTLY ENFORCED:
1. ONLY use evidence from the provided abstracts. Never use training-data knowledge as evidence.
2. Every factual claim needs ≥2 abstracts as sources. Single-source values → flag [SINGLE_SOURCE].
3. If data is insufficient, set confidence="LOW" and note it.
4. ALL contradictions MUST be reported.
5. Confidence: HIGH (≥3 consistent sources), MEDIUM (2), LOW (<2 or conflicting).
6. Use exact quotes and exact numbers from abstracts. Cite [P1], [P2], etc.
7. NEVER invent values, studies, authors, or conclusions not present in the abstracts.
8. Output ONLY valid JSON. No preamble, no explanation outside JSON.
"""

NORMALIZE_SYSTEM_PROMPT = """You are a PubMed MeSH terminology expert.
Convert any user input (any language, any phrasing) to the most precise PubMed MeSH search query in English.

Rules:
1. Medical/health/pharma input → convert to ≤5 MeSH terms, combined with AND/OR. Set is_medical=true.
2. Non-medical input (finance, sports, tech, etc.) → is_medical=false, normalized="", mesh_terms=[], explain in reason.
3. Any language → translate AND normalize to English MeSH terms.
4. Consumer language (e.g. "blood thinners") → precise MeSH ("Anticoagulants").
5. Headlines/full sentences → extract core medical claim, convert to search query.
6. Abbreviations → expand to full MeSH term (e.g. "MI" → "Myocardial Infarction").
7. Return ONLY valid JSON, no preamble.

Output schema (ALWAYS exactly this):
{"normalized": "<MeSH query or empty>", "is_medical": true|false, "mesh_terms": ["term1",...], "reason": "<1 sentence>"}"""

# ── Medifact DOMAIN PROFILE ─────────────────────────────────────────────────────────
PROFILE = {"v": 15, "r": 70, "c": 15, "s": 2, "q": 8, "i": 5, "a": 90, "p": 2,
           "label": "Medical",
           "reg": ["GRADE evidence framework", "ICH E6(R2) GCP", "FDA 21 CFR Part 312", "EMA Scientific Guidelines"]}

# ── PUBMED + CT HELPERS ────────────────────────────────────────────────────────
async def _ncbi_get(url: str, params: dict) -> httpx.Response:
    """Semaphore-gated NCBI request with key rotation and exponential backoff on 429."""
    if _ncbi_key_cycle:
        params = {**params, "api_key": next(_ncbi_key_cycle)}
    for attempt in range(4):
        async with _ncbi_sem:
            r = await _http.get(url, params=params, timeout=15)
        if r.status_code != 429:
            return r
        await asyncio.sleep(1.5 ** attempt)
    return r

async def pm_search(term: str, max_results: int = 10) -> tuple[int, list[str]]:
    r = await _ncbi_get(f"{NCBI}/esearch.fcgi",
                        {"db": "pubmed", "term": term, "retmax": max_results,
                         "retmode": "json", "sort": "relevance"})
    sr = r.json().get("esearchresult", {})
    return int(sr.get("count", 0)), sr.get("idlist", [])

async def pm_count(term: str) -> int:
    n, _ = await pm_search(term, 0)
    return n

async def pm_fetch(pmids: list[str]) -> list[dict]:
    if not pmids:
        return []
    r = await _ncbi_get(f"{NCBI}/efetch.fcgi",
                        {"db": "pubmed", "id": ",".join(pmids),
                         "rettype": "abstract", "retmode": "xml"})
    xml = r.text
    papers = []
    for m in re.finditer(r"<PubmedArticle>(.*?)</PubmedArticle>", xml, re.DOTALL):
        art = m.group(1)
        def ex(pat, flags=0):
            mm = re.search(pat, art, re.DOTALL | flags)
            return mm.group(1) if mm else ""
        pmid    = ex(r"<PMID[^>]*>(\d+)</PMID>")
        title   = re.sub(r"<[^>]+>", "", ex(r"<ArticleTitle>(.*?)</ArticleTitle>"))
        abs_raw = re.findall(r"<AbstractText[^>]*>(.*?)</AbstractText>", art, re.DOTALL)
        abstract = " ".join(re.sub(r"<[^>]+>", "", p) for p in abs_raw)
        authors = ", ".join(re.findall(r"<LastName>([^<]+)</LastName>", art)[:3])
        year    = ex(r"<PubDate>.*?<Year>(\d{4})</Year>")
        journal = ex(r"<Journal>.*?<ISOAbbreviation>([^<]+)</ISOAbbreviation>")
        doi_m   = re.search(r'<ArticleId IdType="doi">([^<]+)</ArticleId>', art)
        doi     = doi_m.group(1) if doi_m else None
        aff_all = re.findall(r"<Affiliation>([^<]+)</Affiliation>", art)
        insts   = list({a.split(",")[0].strip() for a in aff_all if 4 < len(a.split(",")[0].strip()) < 90})
        if pmid:
            papers.append({"pmid": pmid, "title": title, "abstract": abstract,
                           "authors": authors, "year": year, "journal": journal,
                           "doi": doi, "institutions": insts})
    return papers

async def ct_search(query: str) -> tuple[int, list[dict]]:
    try:
        r = await _http.get(CT_V2, params={"query.titles": query, "pageSize": 5, "format": "json"}, timeout=10)
        d = r.json()
        studies = []
        for s in d.get("studies", []):
            ps = s.get("protocolSection", {})
            studies.append({"nctid": ps.get("identificationModule", {}).get("nctId", ""),
                            "title": ps.get("identificationModule", {}).get("briefTitle", ""),
                            "status": ps.get("statusModule", {}).get("overallStatus", "")})
        return d.get("totalCount", 0), studies
    except Exception:
        return 0, []

def build_paper_objects(raw: list[dict]) -> list[Paper]:
    return [Paper(pmid=p.get("pmid",""), title=p.get("title","")[:120],
                  authors=p.get("authors",""), journal=p.get("journal",""),
                  year=p.get("year",""), doi=p.get("doi")) for p in raw]

def paper_url(p: dict) -> str:
    if p.get("doi"): return f"https://doi.org/{p['doi']}"
    return f"https://pubmed.ncbi.nlm.nih.gov/{p.get('pmid','')}"

def format_abstracts(papers: list[dict], max_chars: int = 400) -> str:
    lines = []
    for i, p in enumerate(papers, 1):
        abstract = p.get("abstract", "")[:max_chars]
        lines.append(f"[P{i}] {p.get('title','')} — {p.get('authors','')} ({p.get('year','')}), {p.get('journal','')}\nAbstract: {abstract}{'…' if len(p.get('abstract',''))>max_chars else ''}")
    return "\n\n".join(lines)

async def call_claude(system: str, user: str, max_tokens: int = 900) -> dict:
    try:
        async with _claude_sem:
            response = await claude.messages.create(
                model=MODEL, max_tokens=max_tokens, system=system,
                messages=[{"role": "user", "content": user}]
            )
        text = response.content[0].text.strip()
        text = re.sub(r"^```(?:json)?\s*", "", text)
        text = re.sub(r"\s*```$", "", text)
        return json.loads(text)
    except Exception as e:
        return {"_error": str(e)}

# ── AXIS FUNCTIONS (A1–A8) ────────────────────────────────────────────────────
async def run_a1(query: str, pr: dict) -> AxisResult:
    count, ids = await pm_search(query, 5)
    papers_raw = await pm_fetch(ids)
    papers = build_paper_objects(papers_raw)
    fmt = lambda n: f"{round(n/1000,1)}k+" if n >= 10000 else f"{round(n/1000,1)}k" if n >= 1000 else str(n)
    s = "pass" if count >= pr["v"] else "border" if count >= round(pr["v"] * 0.7) else "fail"
    return AxisResult(
        axis="A1", name="Volume", status=s, score=fmt(count), live=True,
        papers=papers, confidence="HIGH" if count > 100 else "MEDIUM" if count > 10 else "LOW",
        finding=f"{count:,} papers op PubMed · Drempel: ≥{pr['v']}",
        detail=f"PubMed-zoekopdracht naar \"{query}\" geeft {count:,} peer-reviewed resultaten. Drempel: ≥{pr['v']} papers.{' Volumedrempel ruim behaald.' if s=='pass' else ' Onvoldoende volume.' if s=='fail' else ' Volume borderline.'}",
        sources=[Source(label=f"PubMed: {count:,} resultaten", url=f"https://pubmed.ncbi.nlm.nih.gov/?term={query}", type="live")]
        + [Source(label=f"{p.authors.split(',')[0] or '?'} {p.year}, {p.journal}",
                  url=paper_url({"doi": p.doi, "pmid": p.pmid}), type="live") for p in papers[:3]]
    )

async def run_a2(query: str, pr: dict) -> AxisResult:
    from datetime import date
    yr = date.today().year - 5
    total, recent = await asyncio.gather(
        pm_count(query),
        pm_count(f'{query} AND "{yr}/01/01"[PDAT]:"3000"[PDAT]')
    )
    if total == 0:
        return AxisResult(axis="A2", name="Recency", status="fail", score="—", live=True,
                          finding="Geen papers — recency niet berekbaar", detail="Geen papers gevonden.", sources=[], confidence="LOW")
    ratio = round(recent / total * 100)
    s = "pass" if ratio >= pr["r"] else "border" if ratio >= pr["r"] - 10 else "fail"
    return AxisResult(
        axis="A2", name="Recency", status=s, score=f"{ratio}%", live=True,
        confidence="HIGH" if ratio > pr["r"] + 10 else "MEDIUM",
        finding=f"{recent:,} van {total:,} papers uit {yr}–nu · {ratio}% · Drempel: ≥{pr['r']}%",
        detail=f"{recent:,} van {total:,} papers gepubliceerd {yr}–nu → recency {ratio}%.",
        sources=[
            Source(label=f"PubMed {yr}–nu: {recent:,}", url=f"https://pubmed.ncbi.nlm.nih.gov/?term={query}&filter=dates.{yr}-3000", type="live"),
            Source(label=f"PubMed totaal: {total:,}", url=f"https://pubmed.ncbi.nlm.nih.gov/?term={query}", type="live"),
        ]
    )

async def run_a3(query: str, pr: dict) -> AxisResult:
    total, retracted_n, sysrev_n = await asyncio.gather(
        pm_count(query),
        pm_count(f"{query} AND retracted publication[pt]"),
        pm_count(f"{query} AND (systematic review[pt] OR meta-analysis[pt])")
    )
    _, ids = await pm_search(query, 12)
    papers_raw = await pm_fetch(ids)
    if not papers_raw:
        return AxisResult(axis="A3", name="Consensus", status="fail", score="—", live=True,
                          finding="Geen papers — consensus niet meetbaar", detail="Geen abstracts beschikbaar.", sources=[], confidence="LOW")
    schema = '{"contradiction_rate": <0-100>, "contradictions": [{"summary": "...", "paper_indices": [1,2]}], "consensus_summary": "...", "finding": "...", "detail": "...", "confidence": "HIGH|MEDIUM|LOW"}'
    result = await call_claude(
        system=GUARDRAILS + f"\nYou are Medifact Agent A3 — Scientific Consensus Evaluator.\nTask: Analyze abstracts about \"{query}\". Identify what % of papers contradict the majority finding.\nAdditional context: {retracted_n} retracted papers; {sysrev_n} systematic reviews.\nReturn ONLY this JSON: {schema}",
        user=f"Query: {query}\nContradiction threshold: <{pr['c']}%\n\nAbstracts:\n{format_abstracts(papers_raw)}"
    )
    rate = float(result.get("contradiction_rate", 0)) if "_error" not in result else round(((retracted_n * 3) / max(total, 1)) * 100, 1)
    s = "pass" if rate < pr["c"] else "border" if rate < pr["c"] * 1.4 else "fail"
    return AxisResult(
        axis="A3", name="Consensus", status=s, score=f"{rate:.0f}%", live=True,
        confidence=result.get("confidence", "MEDIUM"),
        finding=result.get("finding") or f"Contradictie: {rate:.0f}% · {sysrev_n} syst. reviews · {retracted_n} intrekkingen",
        detail=result.get("detail") or f"Analyse van {len(papers_raw)} abstracts.",
        papers=build_paper_objects(papers_raw[:3]),
        sources=[
            Source(label=f"PubMed syst.reviews: {sysrev_n}", url=f"https://pubmed.ncbi.nlm.nih.gov/?term={query}+AND+systematic+review%5Bpt%5D", type="live"),
            Source(label=f"PubMed retracted: {retracted_n}", url=f"https://pubmed.ncbi.nlm.nih.gov/?term={query}+AND+retracted+publication%5Bpt%5D", type="live"),
        ]
    )

async def run_a4(query: str, pr: dict) -> AxisResult:
    guide_n, cons_n, prot_n = await asyncio.gather(
        pm_count(f"{query} AND (guideline[pt] OR practice guideline[pt])"),
        pm_count(f"{query} AND (consensus[tiab] OR recommendation[tiab])"),
        pm_count(f"{query} AND (standard[tiab] OR norm[tiab] OR protocol[pt])")
    )
    _, gids = await pm_search(f"{query} AND (guideline[pt] OR practice guideline[pt])", 4)
    guide_papers = await pm_fetch(gids)
    schema = '{"standards_found": ["name + org + year"], "norm_count": <int>, "finding": "...", "detail": "...", "confidence": "HIGH|MEDIUM|LOW"}'
    result = await call_claude(
        system=GUARDRAILS + f"\nYou are Medifact Agent A4 — Standards & Norms Analyzer.\nTask: Identify official standards, guidelines, norms in abstracts about \"{query}\".\nReturn ONLY this JSON: {schema}",
        user=f"Query: {query}\nNorm threshold: ≥{pr['s']}\n\nGuideline abstracts:\n{format_abstracts(guide_papers)}\n\nCounts: {guide_n} guidelines, {cons_n} consensus, {prot_n} protocol/standard papers."
    )
    found_types = (1 if guide_n > 0 else 0) + (1 if cons_n > 0 else 0) + (1 if prot_n > 0 else 0)
    norm_count = result.get("norm_count", found_types) if "_error" not in result else found_types
    s = "pass" if norm_count >= pr["s"] else "border" if norm_count > 0 else "fail"
    stds = result.get("standards_found", [])
    return AxisResult(
        axis="A4", name="Standaarden", status=s, score=f"{norm_count} normen", live=True,
        confidence=result.get("confidence", "MEDIUM"),
        finding=result.get("finding") or f"{guide_n} richtlijnen · {cons_n} consensus · {prot_n} protocollen",
        detail=result.get("detail") or f"{guide_n} richtlijnen, {cons_n} consensusdocumenten, {prot_n} standaarden. Geïdentificeerd: {'; '.join(stds[:3]) or 'zie bronnen'}.",
        papers=build_paper_objects(guide_papers[:3]),
        sources=[
            Source(label=f"PubMed richtlijnen: {guide_n}", url=f"https://pubmed.ncbi.nlm.nih.gov/?term={query}+AND+guideline%5Bpt%5D", type="live"),
            Source(label=f"PubMed consensus: {cons_n}", url=f"https://pubmed.ncbi.nlm.nih.gov/?term={query}+AND+consensus%5Btiab%5D", type="live"),
        ] + [Source(label=f"{p.authors.split(',')[0] or '?'} {p.year}, {p.journal}", url=paper_url({"doi": p.doi, "pmid": p.pmid}), type="live") for p in build_paper_objects(guide_papers[:2])]
    )

async def run_a5(query: str, pr: dict) -> AxisResult:
    rct_n, ct_n = await asyncio.gather(
        pm_count(f"{query} AND randomized controlled trial[pt]"),
        pm_count(f"{query} AND clinical trial[pt]")
    )
    _, rct_ids = await pm_search(f"{query} AND randomized controlled trial[pt]", 5)
    _, ct_ids  = await pm_search(f"{query} AND clinical trial[pt]", 3)
    all_ids = list(dict.fromkeys(rct_ids + ct_ids))[:8]
    papers_raw = await pm_fetch(all_ids)
    schema = '{"values": [{"value": "exact text", "meaning": "what it represents", "source_index": 1}], "total_count": <int>, "finding": "...", "detail": "...", "confidence": "HIGH|MEDIUM|LOW"}'
    result = await call_claude(
        system=GUARDRAILS + f"\nYou are Medifact Agent A5 — Quantitative Value Extractor.\nTask: Extract ALL numerical values from abstracts about \"{query}\".\nReturn ONLY this JSON: {schema}",
        user=f"Query: {query}\nThreshold: ≥{pr['q']} quantitative values\n\nAbstracts:\n{format_abstracts(papers_raw, max_chars=500)}"
    )
    quant_vals = result.get("values", []) if "_error" not in result else []
    total_q = len(quant_vals) or (rct_n + ct_n)
    s = "pass" if total_q >= pr["q"] else "border" if total_q >= round(pr["q"] * 0.6) else "fail"
    return AxisResult(
        axis="A5", name="Kwantitatief", status=s,
        score=f"{len(quant_vals)} waarden" if quant_vals else f"{rct_n}+{ct_n} studies",
        live=True, confidence=result.get("confidence", "MEDIUM"), quantValues=quant_vals[:20],
        finding=result.get("finding") or f"{rct_n} RCTs + {ct_n} trials · {len(quant_vals)} waarden",
        detail=result.get("detail") or f"{rct_n} RCTs + {ct_n} klinische trials. {len(quant_vals)} kwantitatieve waarden geëxtraheerd.",
        papers=build_paper_objects(papers_raw[:4]),
        sources=[
            Source(label=f"PubMed RCTs: {rct_n}", url=f"https://pubmed.ncbi.nlm.nih.gov/?term={query}+AND+randomized+controlled+trial%5Bpt%5D", type="live"),
            Source(label=f"PubMed clinical trials: {ct_n}", url=f"https://pubmed.ncbi.nlm.nih.gov/?term={query}+AND+clinical+trial%5Bpt%5D", type="live"),
        ] + [Source(label=f"{p.authors.split(',')[0] or '?'} {p.year}, {p.journal}", url=paper_url({"doi": p.doi, "pmid": p.pmid}), type="live") for p in build_paper_objects(papers_raw[:2])]
    )

async def run_a6(query: str, pr: dict) -> AxisResult:
    multi_n, (_, ids) = await asyncio.gather(
        pm_count(f"{query} AND multicenter study[pt]"),
        pm_search(query, 8)
    )
    papers_raw = await pm_fetch(ids[:6])
    all_insts_raw = []
    for p in papers_raw:
        all_insts_raw.extend(p.get("institutions", []))
    unique_insts = list(dict.fromkeys(all_insts_raw))[:20]
    schema = '{"institutions": ["institution name"], "inst_count": <int>, "finding": "...", "detail": "...", "confidence": "HIGH|MEDIUM|LOW"}'
    result = await call_claude(
        system=GUARDRAILS + f"\nYou are Medifact Agent A6 — Independence & Institution Analyzer.\nIdentify unique independent research institutions for \"{query}\".\nReturn ONLY this JSON: {schema}",
        user=f"Query: {query}\nThreshold: ≥{pr['i']} institutions\n\nInstitutions:\n{chr(10).join(f'- {i}' for i in unique_insts)}\n\nMulticenter studies: {multi_n}\n\nPapers:\n{format_abstracts(papers_raw[:4], max_chars=200)}"
    )
    inst_list  = result.get("institutions", unique_insts[:8]) if "_error" not in result else unique_insts[:8]
    inst_count = result.get("inst_count", max(len(unique_insts), min(multi_n, 20)))
    s = "pass" if inst_count >= pr["i"] else "border" if inst_count >= round(pr["i"] * 0.7) else "fail"
    return AxisResult(
        axis="A6", name="Onafhankelijkheid", status=s, score=f"{inst_count}+ inst.", live=True,
        confidence=result.get("confidence", "MEDIUM"),
        finding=result.get("finding") or f"{inst_count}+ instellingen · {multi_n} multicenterstudies",
        detail=result.get("detail") or f"Instellingen: {', '.join(inst_list[:5])}. {multi_n} multicenterstudies.",
        papers=build_paper_objects(papers_raw[:3]),
        sources=[Source(label=f"PubMed multicentre: {multi_n}", url=f"https://pubmed.ncbi.nlm.nih.gov/?term={query}+AND+multicenter+study%5Bpt%5D", type="live")]
        + [Source(label=f"{p.authors.split(',')[0] or '?'} {p.year}, {p.journal}", url=paper_url({"doi": p.doi, "pmid": p.pmid}), type="live") for p in build_paper_objects(papers_raw[:3])]
    )

async def run_a7(query: str, pr: dict) -> AxisResult:
    total, applied, humans = await asyncio.gather(
        pm_count(query),
        pm_count(f"{query} AND (clinical trial[pt] OR randomized controlled trial[pt] OR cohort study[mh])"),
        pm_count(f"{query} AND humans[mh]")
    )
    _, ids = await pm_search(query, 8)
    papers_raw = await pm_fetch(ids)
    if total == 0:
        return AxisResult(axis="A7", name="Toepasbaarheid", status="fail", score="—", live=True,
                          finding="Geen papers", detail="Geen papers gevonden.", sources=[], confidence="LOW")
    schema = '{"applicability_ratio": <0-100>, "trl_estimate": <1-9>, "applied_studies": <int>, "theoretical_studies": <int>, "finding": "...", "detail": "...", "confidence": "HIGH|MEDIUM|LOW"}'
    result = await call_claude(
        system=GUARDRAILS + f"\nYou are Medifact Agent A7 — Applicability & TRL Assessor.\nAssess applicability for \"{query}\". Gate requires ≥{pr['a']}% applied papers and TRL ≥ 6.\nContext: {applied} clinical/cohort studies, {humans} human-subject papers, {total} total.\nReturn ONLY this JSON: {schema}",
        user=f"Query: {query}\nThreshold: ≥{pr['a']}%\n\nAbstracts:\n{format_abstracts(papers_raw, max_chars=350)}"
    )
    app_ratio = float(result.get("applicability_ratio", round(max(applied, humans) / total * 100))) if "_error" not in result else round(max(applied, humans) / total * 100)
    s = "pass" if app_ratio >= pr["a"] else "border" if app_ratio >= round(pr["a"] * 0.8) else "fail"
    trl = result.get("trl_estimate", "N/A")
    return AxisResult(
        axis="A7", name="Toepasbaarheid", status=s, score=f"~{app_ratio:.0f}%", live=True,
        confidence=result.get("confidence", "MEDIUM"),
        finding=result.get("finding") or f"~{app_ratio:.0f}% toegepast · TRL ~{trl} · {applied} klinische studies",
        detail=result.get("detail") or f"{applied} klinische studies, {humans} met humane proefpersonen van {total:,} totaal. TRL: {trl}.",
        papers=build_paper_objects(papers_raw[:3]),
        sources=[
            Source(label=f"PubMed klinisch: {applied}", url=f"https://pubmed.ncbi.nlm.nih.gov/?term={query}+AND+clinical+trial%5Bpt%5D", type="live"),
            Source(label=f"PubMed humans: {humans}", url=f"https://pubmed.ncbi.nlm.nih.gov/?term={query}+AND+humans%5Bmh%5D", type="live"),
        ]
    )

async def run_a8(query: str, pr: dict) -> AxisResult:
    prot_n, reg_n, (ct_count, ct_studies) = await asyncio.gather(
        pm_count(f"{query} AND protocol[pt]"),
        pm_count(f"{query} AND (registered[tiab] OR clinicaltrials.gov[tiab])"),
        ct_search(query)
    )
    min_p = pr["p"]
    total_verif = (1 if prot_n > 0 else 0) + (1 if ct_count > 0 else 0) + (1 if reg_n > 0 else 0)
    s = "pass" if total_verif >= min_p else "border" if total_verif > 0 else "fail"
    ct_src = [Source(label=f"ClinicalTrials.gov: {st['nctid']} — {st['title'][:60]}",
                     url=f"https://clinicaltrials.gov/study/{st['nctid']}", type="live") for st in ct_studies[:3]]
    return AxisResult(
        axis="A8", name="Verifieerbaarheid", status=s, score=f"{total_verif} types", live=True,
        confidence="HIGH" if total_verif >= 2 else "MEDIUM" if total_verif == 1 else "LOW",
        ctStudies=[{"nctid": st["nctid"], "title": st["title"], "status": st.get("status","")} for st in ct_studies],
        finding=f"{prot_n} protocollen · {ct_count} ClinicalTrials.gov · Drempel: ≥{min_p}",
        detail=f"{prot_n} gepubliceerde protocollen. {ct_count} geregistreerde trials. {reg_n} registratiereferenties. Score: {total_verif}/{min_p}.",
        sources=[
            Source(label=f"ClinicalTrials.gov: {ct_count}", url=f"https://clinicaltrials.gov/search?term={query}", type="live"),
            Source(label=f"PubMed protocollen: {prot_n}", url=f"https://pubmed.ncbi.nlm.nih.gov/?term={query}+AND+protocol%5Bpt%5D", type="live"),
        ] + ct_src
    )

def meta_check(results: list[AxisResult]) -> tuple[str, str]:
    issues = []
    r = {a.axis: a for a in results}
    if "A1" in r and "A3" in r:
        if r["A1"].status == "fail" and r["A3"].status == "pass":
            issues.append("A1/A3 incoherentie: volume onvoldoende maar consensus 'pass'")
    if "A2" in r and "A7" in r:
        if r["A2"].status == "fail" and r["A7"].status == "pass":
            issues.append("A2/A7 incoherentie: recency onvoldoende maar toepasbaarheid hoog")
    a1, a6 = r.get("A1"), r.get("A6")
    if a1 and a6:
        try:
            vol = float(re.sub(r"[^\d.]", "", a1.score.replace("k+","000").replace("k","00")))
            if vol > 500 and a6.status == "fail":
                issues.append("A1/A6 signaal: groot volume maar onafhankelijkheid onvoldoende")
        except Exception:
            pass
    if "A4" in r and "A8" in r:
        if r["A4"].status == "fail" and r["A8"].status == "fail":
            issues.append("A4+A8 beide FAIL: geen normen én geen protocollen")
    status = "FAIL" if len(issues) >= 2 else "WARN" if issues else "PASS"
    detail = f"{6 - len(issues)}/6 coherentiechecks geslaagd. " + (" | ".join(issues) if issues else "Geen inconsistenties.")
    return status, detail

# ══════════════════════════════════════════════════════════════════════════════
# AUTH ENDPOINTS
# ══════════════════════════════════════════════════════════════════════════════
@app.post("/auth/register")
@limiter.limit("3/minute")
async def register(request: Request, req: RegisterRequest, background_tasks: BackgroundTasks, db: AsyncSession = Depends(get_db)):
    try:
        if len(req.password) < 8:
            raise HTTPException(status_code=400, detail="Wachtwoord moet minimaal 8 tekens zijn")
        result = await db.execute(select(User).where(User.email == req.email.lower()))
        if result.scalar_one_or_none():
            raise HTTPException(status_code=409, detail="E-mailadres al in gebruik")
        verification_token = secrets.token_urlsafe(32)
        user = User(
            email=req.email.lower().strip(),
            name=req.name.strip(),
            password_hash=await hash_password(req.password),
            tier="free",
            email_verified=False,
            email_verification_token=verification_token,
        )
        db.add(user)
        await db.commit()
        await db.refresh(user)
        # Send verification email (not welcome — that comes after verify)
        verify_url = f"{FRONTEND_URL}/verify-email?token={verification_token}"
        first_name = user.name.split()[0]
        background_tasks.add_task(
            _send_resend_email,
            to=user.email,
            subject="Bevestig je e-mailadres — Medifact",
            html=f"""<div style="font-family:sans-serif;max-width:520px;margin:0 auto;padding:32px 24px">
<img src="https://medifact.eu/logo.png" alt="Medifact" style="height:40px;margin-bottom:24px">
<h2 style="font-size:22px;font-weight:900;color:#1e293b;margin:0 0 12px">Hoi {first_name}, bevestig je e-mailadres</h2>
<p style="color:#475569;font-size:15px;line-height:1.6;margin:0 0 24px">
  Klik op de knop hieronder om je e-mailadres te bevestigen en je account te activeren.
</p>
<a href="{verify_url}" style="display:inline-block;background:#316BBA;color:#fff;font-weight:700;font-size:15px;padding:14px 28px;border-radius:10px;text-decoration:none">
  E-mailadres bevestigen →
</a>
<p style="color:#94a3b8;font-size:12px;margin-top:24px">
  Link geldig voor 48 uur. Niet aangevraagd? Negeer dan deze mail.
</p>
</div>""",
        )
        log_security("registration", email=user.email)
        return {"message": "Registratie succesvol. Controleer je inbox om je e-mailadres te bevestigen."}
    except HTTPException:
        raise
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"Register error: {str(e)}")

@app.post("/auth/login")
@limiter.limit("5/minute")
async def login(request: Request, req: LoginRequest, db: AsyncSession = Depends(get_db)):
    result = await db.execute(select(User).where(User.email == req.email.lower()))
    user = result.scalar_one_or_none()
    # Check lockout before verifying password (prevents timing side-channel)
    if user and user.locked_until and user.locked_until > datetime.now(timezone.utc):
        raise HTTPException(status_code=429, detail="Account tijdelijk geblokkeerd. Probeer over 15 minuten opnieuw.")
    if not user or not await verify_password(req.password, user.password_hash):
        if user:
            attempts = (user.failed_login_attempts or 0) + 1
            if attempts >= 10:
                user.locked_until = datetime.now(timezone.utc) + timedelta(minutes=15)
                user.failed_login_attempts = 0
                log_security("account_locked", email=req.email.lower())
            else:
                user.failed_login_attempts = attempts
                log_security("login_failed", email=req.email.lower(), attempt=attempts)
            await db.commit()
        else:
            log_security("login_unknown_email", email=req.email.lower())
        raise HTTPException(status_code=401, detail="E-mailadres of wachtwoord onjuist")
    if not user.is_active:
        raise HTTPException(status_code=403, detail="Account geblokkeerd")
    if not user.email_verified:
        raise HTTPException(status_code=403, detail="Verifieer eerst je e-mailadres. Controleer je inbox.")
    # Reset lockout on successful login
    user.failed_login_attempts = 0
    user.locked_until = None
    await db.commit()
    access_token = create_token(user.id, user.email)
    refresh_token = await create_refresh_token(user.id, db)
    tier_info = TIERS.get(user.tier, TIERS["free"])
    log_security("login_success", user_id=user.id, email=user.email, tier=user.tier)
    return {
        "token": access_token,
        "refresh_token": refresh_token,
        "expires_in": JWT_EXPIRE_MINUTES * 60,
        "user": {"id": user.id, "email": user.email, "name": user.name,
                 "tier": user.tier, "tier_name": tier_info["name"],
                 "analyses_used": user.analyses_used, "analyses_limit": tier_info["analyses"]},
    }

@app.get("/auth/verify-email")
@limiter.limit("10/minute")
async def verify_email(request: Request, token: str, db: AsyncSession = Depends(get_db)):
    result = await db.execute(select(User).where(User.email_verification_token == token))
    user = result.scalar_one_or_none()
    if not user:
        raise HTTPException(status_code=400, detail="Ongeldige of verlopen verificatielink.")
    user.email_verified = True
    user.email_verification_token = None
    await db.commit()
    log_security("email_verified", user_id=user.id, email=user.email)
    return {"ok": True}

@app.post("/auth/refresh")
@limiter.limit("20/minute")
async def refresh_token(request: Request, db: AsyncSession = Depends(get_db)):
    body = await request.json()
    raw = body.get("refresh_token", "")
    if not raw:
        raise HTTPException(status_code=401, detail="Refresh token ontbreekt.")
    result = await db.execute(
        select(RefreshToken).where(RefreshToken.token == raw, RefreshToken.revoked == False)
    )
    rt = result.scalar_one_or_none()
    if not rt or rt.expires_at < datetime.now(timezone.utc):
        log_security("refresh_invalid", token_prefix=raw[:8])
        raise HTTPException(status_code=401, detail="Refresh token ongeldig of verlopen. Log opnieuw in.")
    # Rotate: revoke old, issue new
    rt.revoked = True
    user_result = await db.execute(select(User).where(User.id == rt.user_id))
    user = user_result.scalar_one_or_none()
    if not user or not user.is_active:
        await db.commit()
        raise HTTPException(status_code=401, detail="Gebruiker niet gevonden.")
    new_access = create_token(user.id, user.email)
    new_refresh = await create_refresh_token(user.id, db)
    tier_info = TIERS.get(user.tier, TIERS["free"])
    log_security("token_refreshed", user_id=user.id)
    return {
        "token": new_access,
        "refresh_token": new_refresh,
        "expires_in": JWT_EXPIRE_MINUTES * 60,
        "user": {"id": user.id, "email": user.email, "name": user.name,
                 "tier": user.tier, "tier_name": tier_info["name"],
                 "analyses_used": user.analyses_used, "analyses_limit": tier_info["analyses"]},
    }

@app.post("/auth/logout")
@limiter.limit("20/minute")
async def logout(request: Request, db: AsyncSession = Depends(get_db)):
    body = await request.json()
    raw = body.get("refresh_token", "")
    if raw:
        await db.execute(
            update(RefreshToken).where(RefreshToken.token == raw).values(revoked=True)
        )
        await db.commit()
    return {"ok": True}

@app.post("/auth/resend-verification")
@limiter.limit("3/minute")
async def resend_verification(request: Request, db: AsyncSession = Depends(get_db)):
    body = await request.json()
    email = body.get("email", "").lower().strip()
    result = await db.execute(select(User).where(User.email == email))
    user = result.scalar_one_or_none()
    if user and not user.email_verified:
        token = secrets.token_urlsafe(32)
        user.email_verification_token = token
        await db.commit()
        verify_url = f"{FRONTEND_URL}/verify-email?token={token}"
        first_name = user.name.split()[0]
        await _send_resend_email(
            to=user.email,
            subject="Bevestig je e-mailadres — Medifact",
            html=f"""<div style="font-family:sans-serif;max-width:520px;margin:0 auto;padding:32px 24px">
<img src="https://medifact.eu/logo.png" alt="Medifact" style="height:40px;margin-bottom:24px">
<h2 style="font-size:22px;font-weight:900;color:#1e293b;margin:0 0 12px">Nieuwe verificatielink, {first_name}</h2>
<a href="{verify_url}" style="display:inline-block;background:#316BBA;color:#fff;font-weight:700;font-size:15px;padding:14px 28px;border-radius:10px;text-decoration:none">
  E-mailadres bevestigen →
</a>
<p style="color:#94a3b8;font-size:12px;margin-top:24px">Niet aangevraagd? Negeer dan deze mail.</p>
</div>""",
        )
    # Always return 200 to prevent email enumeration
    return {"ok": True, "message": "Als dit e-mailadres bekend is, ontvang je een nieuwe verificatielink."}

@app.get("/auth/me")
async def me(current_user: User = Depends(get_current_user)):
    tier_info = TIERS.get(current_user.tier, TIERS["free"])
    return {
        "id": current_user.id, "email": current_user.email, "name": current_user.name,
        "tier": current_user.tier, "tier_name": tier_info["name"],
        "tier_price": tier_info["price"],
        "analyses_used": current_user.analyses_used, "analyses_limit": tier_info["analyses"],
        "created_at": current_user.created_at.isoformat() if current_user.created_at else None,
    }

@app.post("/auth/forgot-password")
@limiter.limit("3/minute")
async def forgot_password(
    request: Request,
    req: ForgotPasswordRequest,
    background_tasks: BackgroundTasks,
    db: AsyncSession = Depends(get_db),
):
    """Generate reset token and send email. Always returns 200 to prevent email enumeration."""
    result = await db.execute(select(User).where(User.email == req.email.lower().strip()))
    user = result.scalar_one_or_none()
    if user and user.is_active:
        # Expire old tokens
        await db.execute(
            update(PasswordResetToken)
            .where(PasswordResetToken.user_id == user.id, PasswordResetToken.used == False)
            .values(used=True)
        )
        reset_token = PasswordResetToken(
            user_id=user.id,
            expires_at=datetime.now(timezone.utc) + timedelta(hours=1),
        )
        db.add(reset_token)
        await db.commit()
        await db.refresh(reset_token)
        reset_link = f"{FRONTEND_URL}/reset-password?token={reset_token.token}"
        background_tasks.add_task(
            _send_email_bg,
            key="password_reset",
            to=user.email,
            variables={"name": user.name.split()[0], "reset_link": reset_link},
        )
    return {"ok": True, "message": "Als dit e-mailadres bekend is, ontvang je een herstelmail."}

@app.post("/auth/reset-password")
@limiter.limit("5/minute")
async def reset_password(request: Request, req: ResetPasswordRequest, db: AsyncSession = Depends(get_db)):
    """Validate token and set new password."""
    if len(req.password) < 8:
        raise HTTPException(status_code=400, detail="Wachtwoord moet minimaal 8 tekens zijn")
    result = await db.execute(
        select(PasswordResetToken).where(
            PasswordResetToken.token == req.token,
            PasswordResetToken.used == False,
        )
    )
    token_obj = result.scalar_one_or_none()
    if not token_obj:
        raise HTTPException(status_code=400, detail="Ongeldige of verlopen hersteltoken")
    now = datetime.now(timezone.utc)
    expires = token_obj.expires_at
    if expires.tzinfo is None:
        expires = expires.replace(tzinfo=timezone.utc)
    if now > expires:
        raise HTTPException(status_code=400, detail="Hersteltoken is verlopen. Vraag een nieuwe aan.")
    await db.execute(
        update(User).where(User.id == token_obj.user_id).values(password_hash=await hash_password(req.password))
    )
    await db.execute(
        update(PasswordResetToken).where(PasswordResetToken.id == token_obj.id).values(used=True)
    )
    await db.commit()
    log_security("password_reset_complete", user_id=user.id)
    return {"ok": True, "message": "Wachtwoord succesvol gewijzigd."}

# ══════════════════════════════════════════════════════════════════════════════
# BILLING ENDPOINTS (STRIPE)
# ══════════════════════════════════════════════════════════════════════════════
@app.post("/billing/checkout")
async def create_checkout(req: CheckoutRequest,
                          current_user: User = Depends(get_current_user),
                          db: AsyncSession = Depends(get_db)):
    if not stripe.api_key:
        raise HTTPException(status_code=500, detail="Stripe niet geconfigureerd")
    tier = TIERS.get(req.tier)
    if not tier or not tier["stripe_price"]:
        raise HTTPException(status_code=400, detail=f"Ongeldig abonnement: {req.tier}")

    # Create or reuse Stripe customer
    customer_id = current_user.stripe_customer_id
    if not customer_id:
        customer = stripe.Customer.create(email=current_user.email, name=current_user.name,
                                          metadata={"user_id": current_user.id})
        customer_id = customer.id
        await db.execute(update(User).where(User.id == current_user.id).values(stripe_customer_id=customer_id))
        await db.commit()

    session = stripe.checkout.Session.create(
        customer=customer_id,
        mode="subscription",
        line_items=[{"price": tier["stripe_price"], "quantity": 1}],
        success_url=f"{FRONTEND_URL}?billing=success&tier={req.tier}",
        cancel_url=f"{FRONTEND_URL}?billing=cancelled",
        metadata={"user_id": current_user.id, "tier": req.tier},
        subscription_data={"metadata": {"user_id": current_user.id, "tier": req.tier}},
    )
    return {"url": session.url}

@app.post("/billing/portal")
async def billing_portal(current_user: User = Depends(get_current_user)):
    if not stripe.api_key:
        raise HTTPException(status_code=500, detail="Stripe niet geconfigureerd")
    if not current_user.stripe_customer_id:
        raise HTTPException(status_code=400, detail="Geen actief abonnement gevonden")
    session = stripe.billing_portal.Session.create(
        customer=current_user.stripe_customer_id,
        return_url=FRONTEND_URL,
    )
    return {"url": session.url}

@app.post("/billing/webhook")
async def stripe_webhook(request: Request, db: AsyncSession = Depends(get_db)):
    payload  = await request.body()
    sig      = request.headers.get("stripe-signature", "")
    try:
        event = stripe.Webhook.construct_event(payload, sig, STRIPE_WEBHOOK_SECRET)
    except Exception as e:
        raise HTTPException(status_code=400, detail=str(e))

    obj  = event["data"]["object"]
    meta = obj.get("metadata", {})
    user_id = meta.get("user_id")
    tier    = meta.get("tier", "free")

    if event["type"] in ("checkout.session.completed", "customer.subscription.updated"):
        if event["type"] == "checkout.session.completed":
            sub_id = obj.get("subscription")
        else:
            sub_id = obj.get("id")
        if user_id:
            await db.execute(
                update(User).where(User.id == user_id).values(tier=tier, stripe_sub_id=sub_id)
            )
            await db.commit()

    elif event["type"] in ("customer.subscription.deleted", "invoice.payment_failed"):
        if user_id:
            await db.execute(update(User).where(User.id == user_id).values(tier="free"))
            await db.commit()

    return {"received": True}

# ══════════════════════════════════════════════════════════════════════════════
# CORE API ENDPOINTS (PROTECTED)
# ══════════════════════════════════════════════════════════════════════════════
@app.post("/api/normalize")
async def normalize_query(req: NormalizeRequest, current_user: User = Depends(get_current_user)):
    if not ANTHROPIC_API_KEY:
        raise HTTPException(status_code=500, detail="ANTHROPIC_API_KEY not configured")
    result = await call_claude(
        system=NORMALIZE_SYSTEM_PROMPT,
        user=f"Convert this input to PubMed MeSH terms:\n\n{req.query}",
        max_tokens=256,
    )
    if "_error" in result:
        return {"normalized": req.query, "is_medical": True, "mesh_terms": [],
                "reason": f"Normalization unavailable; using raw query."}
    return {
        "normalized": result.get("normalized", req.query),
        "is_medical": bool(result.get("is_medical", True)),
        "mesh_terms": result.get("mesh_terms", []),
        "reason": result.get("reason", ""),
    }

@app.post("/api/analyze")
@limiter.limit("3/minute")
async def analyze(request: Request, req: AnalyzeRequest,
                  current_user: User = Depends(get_current_user),
                  db: AsyncSession = Depends(get_db)):
    if not ANTHROPIC_API_KEY:
        raise HTTPException(status_code=500, detail="ANTHROPIC_API_KEY not configured")
    await check_usage(current_user, db)

    pr = PROFILE
    cache_key = "medifact:axes:" + hashlib.sha256(req.query.lower().strip().encode()).hexdigest()[:32]
    cached_axes = await _cache_get(cache_key)

    if cached_axes:
        results = [AxisResult(**r) for r in cached_axes]
    else:
        axis_fns = [run_a1, run_a2, run_a3, run_a4, run_a5, run_a6, run_a7, run_a8]
        results = await asyncio.gather(*[fn(req.query, pr) for fn in axis_fns], return_exceptions=False)
        await _cache_set(cache_key, [r.dict() for r in results])

    failed  = [r for r in results if r.status == "fail"]
    passed  = [r for r in results if r.status == "pass"]
    meta_s, meta_d = meta_check(list(results))
    is_open = len(failed) == 0 and meta_s != "FAIL"
    rid = "MF-" + uuid.uuid4().hex[:8]
    ts  = datetime.now(timezone.utc).isoformat()
    payload_str = json.dumps({"query": req.query, "results": [r.dict() for r in results], "rid": rid}, sort_keys=True)
    sha = hashlib.sha256(payload_str.encode()).hexdigest()[:32]

    # Save to DB
    analysis = Analysis(
        user_id=current_user.id, query=req.query,
        gate="open" if is_open else "closed",
        score=f"{len(passed)}/{len(results)}",
        axes_json=json.dumps([r.dict() for r in results]),
        rid=rid,
    )
    db.add(analysis)
    await increment_usage(current_user, db)

    return {
        "query": req.query, "profile": "medical", "profile_label": "Medical",
        "gate": "open" if is_open else "closed",
        "score": f"{len(passed)}/{len(results)}",
        "results": [r.dict() for r in results],
        "meta": {"status": meta_s, "detail": meta_d},
        "rid": rid, "timestamp": ts, "sha256": sha,
        "regulatory": pr.get("reg", []),
        "cached": cached_axes is not None,
    }

@app.get("/api/analyze/stream")
@limiter.limit("3/minute")
async def analyze_stream(request: Request, query: str, profile: str = "medical",
                         token: Optional[str] = None,
                         authorization: Optional[str] = Header(None),
                         db: AsyncSession = Depends(get_db)):
    if not ANTHROPIC_API_KEY:
        raise HTTPException(status_code=500, detail="ANTHROPIC_API_KEY not configured")

    # Accept token both as query param (EventSource) and Authorization header
    auth_header = authorization or (f"Bearer {token}" if token else None)
    current_user = await get_current_user(authorization=auth_header, db=db)
    await check_usage(current_user, db)

    pr = PROFILE
    cache_key = "medifact:axes:" + hashlib.sha256(query.lower().strip().encode()).hexdigest()[:32]
    cached_axes = await _cache_get(cache_key)

    async def _finish_and_stream(results: list[AxisResult], from_cache: bool):
        """Common tail: compute verdict, save to DB, yield done event."""
        failed  = [r for r in results if r.status == "fail"]
        passed  = [r for r in results if r.status == "pass"]
        meta_s, meta_d = meta_check(results)
        is_open = len(failed) == 0 and meta_s != "FAIL"
        rid = "MF-" + uuid.uuid4().hex[:8]
        ts  = datetime.now(timezone.utc).isoformat()
        payload_str = json.dumps({"query": query, "results": [r.dict() for r in results], "rid": rid}, sort_keys=True)
        sha = hashlib.sha256(payload_str.encode()).hexdigest()[:32]

        analysis = Analysis(
            user_id=current_user.id, query=query,
            gate="open" if is_open else "closed",
            score=f"{len(passed)}/{len(results)}",
            axes_json=json.dumps([r.dict() for r in results]),
            rid=rid,
        )
        async with AsyncSessionLocal() as save_db:
            save_db.add(analysis)
            await save_db.execute(update(User).where(User.id == current_user.id).values(analyses_used=User.analyses_used + 1))
            await save_db.commit()

        summary = {"type": "done", "gate": "open" if is_open else "closed",
                   "score": f"{len(passed)}/{len(results)}", "rid": rid, "timestamp": ts,
                   "sha256": sha, "meta": {"status": meta_s, "detail": meta_d},
                   "regulatory": pr.get("reg", []), "profile_label": "Medical",
                   "cached": from_cache}
        yield f"data: {json.dumps(summary)}\n\n"

    async def event_stream_cached(axes: list[dict]):
        """Stream cached axis results instantly, then finish."""
        for i, ax in enumerate(axes):
            d = {**ax, "axis_index": i, "type": "axis_result"}
            yield f"data: {json.dumps(d)}\n\n"
        results = [AxisResult(**ax) for ax in axes]
        async for chunk in _finish_and_stream(results, from_cache=True):
            yield chunk

    async def event_stream_live():
        """Run all 8 axes concurrently, stream results as they complete."""
        axis_fns = [run_a1, run_a2, run_a3, run_a4, run_a5, run_a6, run_a7, run_a8]
        queue: asyncio.Queue = asyncio.Queue()

        async def run_one(fn, idx):
            try:
                res = await fn(query, pr)
                d = res.dict(); d["axis_index"] = idx; d["type"] = "axis_result"
            except Exception as e:
                d = {"type": "axis_result", "axis_index": idx, "axis": f"A{idx+1}",
                     "status": "fail", "score": "err", "finding": str(e), "live": False}
            await queue.put(d)

        tasks = [asyncio.create_task(run_one(fn, i)) for i, fn in enumerate(axis_fns)]
        completed = []
        for _ in range(8):
            item = await queue.get()
            completed.append(item)
            yield f"data: {json.dumps(item)}\n\n"

        results = [AxisResult(**{k: v for k, v in c.items() if k not in ("axis_index","type")})
                   for c in sorted(completed, key=lambda x: x["axis_index"]) if "status" in c]
        await _cache_set(cache_key, [r.dict() for r in results])
        async for chunk in _finish_and_stream(results, from_cache=False):
            yield chunk
        for t in tasks:
            if not t.done(): t.cancel()

    generator = event_stream_cached(cached_axes) if cached_axes else event_stream_live()
    return StreamingResponse(
        generator, media_type="text/event-stream",
        headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"}
    )

@app.get("/api/history")
async def get_history(limit: int = 50,
                      current_user: User = Depends(get_current_user),
                      db: AsyncSession = Depends(get_db)):
    result = await db.execute(
        select(Analysis).where(Analysis.user_id == current_user.id)
        .order_by(Analysis.created_at.desc()).limit(limit)
    )
    analyses = result.scalars().all()
    return [{"id": a.id, "query": a.query, "gate": a.gate, "score": a.score,
             "rid": a.rid, "created_at": a.created_at.isoformat() if a.created_at else None}
            for a in analyses]

# ── ASYNC JOB QUEUE ────────────────────────────────────────────────────────────
@app.get("/api/analyze/capacity")
async def analyze_capacity():
    """Lightweight check: is the platform at capacity? No auth required."""
    max_concurrent = int(os.environ.get("MAX_CONCURRENT_ANALYSES", "3"))
    r = await _get_redis()
    if r:
        try:
            await r.zremrangebyscore(_SLOT_KEY, "-inf", time.time())
            active = await r.zcard(_SLOT_KEY)
        except Exception:
            active = 0
    else:
        active = 0
    return {"busy": active >= max_concurrent, "active": int(active), "max": max_concurrent}


@app.post("/api/analyze/slot")
@limiter.limit("20/minute")
async def acquire_analysis_slot(request: Request, current_user: User = Depends(get_current_user)):
    """Atomically reserve a concurrency slot before starting a client-side analysis."""
    slot_id = str(uuid.uuid4())
    acquired = await _slot_acquire(slot_id)
    if not acquired:
        raise HTTPException(
            status_code=503,
            detail={"busy": True, "message": "Platform is bezet. Je analyse wordt automatisch ingepland."},
        )
    return {"slot_id": slot_id}


@app.delete("/api/analyze/slot/{slot_id}")
async def release_analysis_slot(slot_id: str, current_user: User = Depends(get_current_user)):
    """Release a concurrency slot after analysis completes or errors."""
    await _slot_release(slot_id)
    return {"ok": True}


@app.post("/api/analyze/queue")
@limiter.limit("5/minute")
async def queue_analysis(
    request: Request,
    req: AnalyzeRequest,
    current_user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    """Submit an analysis to the background queue. Returns job_id immediately; result arrives by email."""
    await check_usage(current_user, db)
    job = AnalysisJob(
        user_id=current_user.id,
        user_email=current_user.email,
        user_name=current_user.name,
        query=req.query,
    )
    db.add(job)
    await db.commit()
    return {
        "job_id": job.id,
        "status": "pending",
        "message": "Analyse ingepland. Je ontvangt een e-mail op zodra het klaar is.",
    }


@app.get("/api/analyze/queue/{job_id}")
async def get_job_status(
    job_id: str,
    current_user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    """Poll the status of a queued analysis. Returns full result when done."""
    result = await db.execute(
        select(AnalysisJob).where(
            AnalysisJob.id == job_id,
            AnalysisJob.user_id == current_user.id,   # users can only see their own jobs
        )
    )
    job = result.scalar_one_or_none()
    if not job:
        raise HTTPException(status_code=404, detail="Job niet gevonden")
    resp: dict = {
        "job_id": job.id,
        "status": job.status,
        "query": job.query,
        "created_at": job.created_at.isoformat(),
    }
    if job.status == "done" and job.result_json:
        resp["result"] = json.loads(job.result_json)
    if job.status == "failed":
        resp["error"] = job.error
    return resp


# ── PUBLIC ENDPOINTS ───────────────────────────────────────────────────────────
@app.get("/")
def root():
    return {"service": "Medifact API", "version": "3.0.0", "status": "live", "docs": "/docs"}

@app.get("/health")
def health():
    return {"status": "ok"}

@app.get("/api/tiers")
def get_tiers():
    """Public endpoint — show available subscription tiers."""
    return [{"id": k, "name": v["name"], "analyses": v["analyses"], "price": v["price"]}
            for k, v in TIERS.items()]


# ── ADMIN ──────────────────────────────────────────────────────────────────────
ADMIN_SECRET = os.environ.get("ADMIN_SECRET", "")

def verify_admin(x_admin_secret: Annotated[str, Header()] = ""):
    if not ADMIN_SECRET:
        raise HTTPException(status_code=500, detail="ADMIN_SECRET not configured")
    if x_admin_secret != ADMIN_SECRET:
        raise HTTPException(status_code=401, detail="Unauthorized")

def user_to_dict(u: User) -> dict:
    return {
        "id":              u.id,
        "email":           u.email,
        "name":            u.name,
        "tier":            u.tier,
        "tier_name":       TIERS.get(u.tier, {}).get("name", u.tier),
        "analyses_used":   u.analyses_used,
        "analyses_limit":  TIERS.get(u.tier, {}).get("analyses", 10),
        "is_active":       u.is_active,
        "stripe_sub_id":   u.stripe_sub_id,
        "created_at":      u.created_at.isoformat() if u.created_at else None,
    }

class UserPatch(BaseModel):
    tier:          Optional[str]  = None
    analyses_used: Optional[int]  = None
    is_active:     Optional[bool] = None

@app.get("/admin/users")
@limiter.limit("30/minute")
async def admin_list_users(
    request: Request,
    registered_hours_ago: Optional[int] = None,
    _: str = Depends(verify_admin),
    db: AsyncSession = Depends(get_db),
):
    """
    List all users.
    ?registered_hours_ago=24  →  only users registered ~24h ago (±1h window)
    """
    stmt = select(User).order_by(User.created_at.desc())
    if registered_hours_ago is not None:
        window_end   = datetime.now(timezone.utc) - timedelta(hours=registered_hours_ago - 1)
        window_start = datetime.now(timezone.utc) - timedelta(hours=registered_hours_ago + 1)
        stmt = stmt.where(User.created_at >= window_start, User.created_at <= window_end)
    result = await db.execute(stmt)
    return [user_to_dict(u) for u in result.scalars().all()]

@app.patch("/admin/users/{user_id}")
@limiter.limit("30/minute")
async def admin_update_user(
    request: Request,
    user_id: str,
    patch: UserPatch,
    _: str = Depends(verify_admin),
    db: AsyncSession = Depends(get_db),
):
    """Update tier, analyses_used or is_active for a user."""
    result = await db.execute(select(User).where(User.id == user_id))
    user = result.scalar_one_or_none()
    if not user:
        raise HTTPException(status_code=404, detail="User not found")
    if patch.tier is not None:
        if patch.tier not in TIERS:
            raise HTTPException(status_code=400, detail=f"Unknown tier: {patch.tier}")
        user.tier = patch.tier
    if patch.analyses_used is not None:
        user.analyses_used = max(0, patch.analyses_used)
    if patch.is_active is not None:
        user.is_active = patch.is_active
    await db.commit()
    await db.refresh(user)
    return user_to_dict(user)

@app.delete("/admin/users/{user_id}")
@limiter.limit("30/minute")
async def admin_delete_user(
    request: Request,
    user_id: str,
    _: str = Depends(verify_admin),
    db: AsyncSession = Depends(get_db),
):
    """Permanently delete a user and all their data."""
    result = await db.execute(select(User).where(User.id == user_id))
    user = result.scalar_one_or_none()
    if not user:
        raise HTTPException(status_code=404, detail="User not found")
    # Also delete analyses
    await db.execute(delete(Analysis).where(Analysis.user_id == user_id))
    await db.delete(user)
    await db.commit()
    return {"ok": True, "deleted": user_id}

@app.get("/admin/stats")
@limiter.limit("30/minute")
async def admin_stats(
    request: Request,
    _: str = Depends(verify_admin),
    db: AsyncSession = Depends(get_db),
):
    """Aggregate stats for the superadmin dashboard."""
    users_result = await db.execute(select(User))
    users = users_result.scalars().all()
    now = datetime.now(timezone.utc)
    return {
        "total_users":      len(users),
        "active_users":     sum(1 for u in users if u.analyses_used > 0),
        "paid_users":       sum(1 for u in users if u.tier != "free"),
        "total_analyses":   sum(u.analyses_used for u in users),
        "mrr":              sum(TIERS.get(u.tier, {}).get("price", 0) for u in users),
        "new_today":        sum(1 for u in users if (now - u.created_at).total_seconds() < 86400),
        "new_week":         sum(1 for u in users if (now - u.created_at).total_seconds() < 604800),
        "new_month":        sum(1 for u in users if (now - u.created_at).total_seconds() < 2592000),
        "tier_counts":      {tier: sum(1 for u in users if u.tier == tier) for tier in TIERS},
    }

# ══════════════════════════════════════════════════════════════════════════════
# CONTACT REQUESTS
# ══════════════════════════════════════════════════════════════════════════════
class ContactRequestBody(BaseModel):
    name: str
    email: str
    message: Optional[str] = None
    plan: Optional[str] = None

@app.post("/contact")
async def submit_contact(req: ContactRequestBody, db: AsyncSession = Depends(get_db)):
    """Save contact request and notify admin via email."""
    cr = ContactRequest(name=req.name, email=req.email, message=req.message, plan=req.plan)
    db.add(cr)
    await db.commit()
    # Notify admin
    html = f"""<h2>Nieuwe aanvraag: {req.plan or 'onbekend plan'}</h2>
<p><strong>Naam:</strong> {req.name}<br>
<strong>Email:</strong> {req.email}<br>
<strong>Plan:</strong> {req.plan or '—'}<br>
<strong>Bericht:</strong> {req.message or '—'}</p>
<p><a href="https://medifact.eu/admin">Bekijk in admin panel →</a></p>"""
    await _send_resend_email(
        to="richard@medifact.eu",
        subject=f"📩 Nieuwe aanvraag: {req.plan or 'contact'} — {req.name}",
        html=html,
    )
    return {"ok": True}

@app.get("/admin/contacts")
@limiter.limit("30/minute")
async def admin_contacts(request: Request, _: str = Depends(verify_admin), db: AsyncSession = Depends(get_db)):
    result = await db.execute(select(ContactRequest).order_by(ContactRequest.created_at.desc()))
    contacts = result.scalars().all()
    return [{"id": c.id, "name": c.name, "email": c.email, "message": c.message,
             "plan": c.plan, "status": c.status, "created_at": c.created_at.isoformat()} for c in contacts]

@app.patch("/admin/contacts/{contact_id}")
@limiter.limit("30/minute")
async def admin_update_contact(request: Request, contact_id: str, data: dict, _: str = Depends(verify_admin), db: AsyncSession = Depends(get_db)):
    result = await db.execute(select(ContactRequest).where(ContactRequest.id == contact_id))
    c = result.scalar_one_or_none()
    if not c:
        raise HTTPException(status_code=404, detail="Not found")
    if "status" in data:
        c.status = data["status"]
    await db.commit()
    return {"ok": True}

# ══════════════════════════════════════════════════════════════════════════════
# SHARE ENDPOINTS (#15)
# ══════════════════════════════════════════════════════════════════════════════
class ShareRequest(BaseModel):
    query: str
    results: list
    normalize_info: Optional[dict] = None
    verdict: str
    passed: int

@app.post("/share")
async def create_share(
    data: ShareRequest,
    user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    """Create a 30-day read-only share link for an analysis."""
    share = SharedAnalysis(
        user_id=user.id,
        sharer_name=user.name,
        query=data.query,
        results_json=json.dumps(data.results),
        normalize_info_json=json.dumps(data.normalize_info) if data.normalize_info else None,
        verdict=data.verdict,
        passed=data.passed,
        expires_at=datetime.now(timezone.utc) + timedelta(days=30),
    )
    db.add(share)
    await db.commit()
    await db.refresh(share)
    return {
        "token": share.token,
        "expires_at": share.expires_at.isoformat(),
    }

@app.get("/share/{token}")
async def get_share(token: str, db: AsyncSession = Depends(get_db)):
    """Public endpoint — no auth required."""
    result = await db.execute(select(SharedAnalysis).where(SharedAnalysis.token == token))
    share = result.scalar_one_or_none()
    if not share:
        raise HTTPException(status_code=404, detail="Link niet gevonden of verlopen")
    now = datetime.now(timezone.utc)
    exp = share.expires_at
    if exp.tzinfo is None:
        exp = exp.replace(tzinfo=timezone.utc)
    if now > exp:
        raise HTTPException(status_code=410, detail="Deze deellink is verlopen (30 dagen)")
    # Increment view count
    share.view_count += 1
    await db.commit()
    return {
        "query": share.query,
        "results": json.loads(share.results_json),
        "normalize_info": json.loads(share.normalize_info_json) if share.normalize_info_json else None,
        "verdict": share.verdict,
        "passed": share.passed,
        "sharer_name": share.sharer_name,
        "created_at": share.created_at.isoformat(),
        "expires_at": share.expires_at.isoformat(),
        "view_count": share.view_count,
    }

# ══════════════════════════════════════════════════════════════════════════════
# ACTIVITY FEED (#17)
# ══════════════════════════════════════════════════════════════════════════════
@app.post("/analyses")
async def save_client_analysis(
    req: SaveAnalysisRequest,
    user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    """Save a client-side analysis result and increment usage counter."""
    await check_usage(user, db)
    analysis = Analysis(
        user_id=user.id,
        query=req.query,
        gate=req.gate,
        score=req.score,
        axes_json=req.axes_json,
        rid="MF-" + uuid.uuid4().hex[:8],
    )
    db.add(analysis)
    await increment_usage(user, db)
    await db.refresh(user)
    return {
        "id": analysis.id,
        "query": analysis.query,
        "score": analysis.score,
        "gate": analysis.gate,
        "created_at": analysis.created_at.isoformat() if analysis.created_at else None,
        "analyses_used": user.analyses_used,
    }

@app.get("/analyses")
async def get_recent_analyses(
    limit: int = 10,
    user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    """Return recent analyses for the activity feed."""
    result = await db.execute(
        select(Analysis)
        .where(Analysis.user_id == user.id)
        .order_by(Analysis.created_at.desc())
        .limit(limit)
    )
    analyses = result.scalars().all()
    return [
        {
            "id": a.id,
            "query": a.query,
            "score": a.score,
            "gate": a.gate,
            "created_at": a.created_at.isoformat(),
        }
        for a in analyses
    ]


# ══════════════════════════════════════════════════════════════════════════════
# WEEKLY DIGEST EMAIL (#18)
# ══════════════════════════════════════════════════════════════════════════════
def _digest_html(user_name: str, tier_name: str, used: int, limit: int) -> str:
    rem = max(0, limit - used) if limit != 999999 else None
    pct = min(100, round((used / limit) * 100)) if limit != 999999 else 0
    bar_color = "#ef4444" if pct >= 90 else "#f59e0b" if pct >= 70 else "#10b981"
    rem_text = f"{rem} analyses resterend" if rem is not None else "Onbeperkt"
    limit_text = "∞" if limit == 999999 else str(limit)
    bar_width = min(100, pct) if limit != 999999 else 0

    upgrade_block = ""
    if pct >= 80 and limit != 999999:
        upgrade_block = f"""
        <tr><td style="padding:0 40px 24px;">
          <div style="background:#fef3c7;border:1px solid #fbbf24;border-radius:8px;padding:16px 20px;font-size:14px;color:#92400e;">
            ⚠️ Je zit op {pct}% van je maandlimiet.
            <a href="https://medifact.eu/dashboard/billing" style="color:#d97706;font-weight:700;text-decoration:none;">Upgrade nu →</a>
          </div>
        </td></tr>"""

    return f"""<!DOCTYPE html>
<html lang="nl">
<head><meta charset="UTF-8"><meta name="viewport" content="width=device-width,initial-scale=1.0">
<title>Jouw wekelijkse Medifact digest</title></head>
<body style="margin:0;padding:0;background:#f8fafc;font-family:-apple-system,BlinkMacSystemFont,'Segoe UI',sans-serif;">
<table width="100%" cellpadding="0" cellspacing="0" style="background:#f8fafc;padding:40px 0;">
<tr><td align="center">
<table width="600" cellpadding="0" cellspacing="0" style="background:#ffffff;border-radius:12px;overflow:hidden;box-shadow:0 1px 3px rgba(0,0,0,.1);">

  <!-- Header -->
  <tr><td style="background:#0f172a;padding:28px 40px;text-align:center;">
    <span style="font-size:22px;font-weight:900;color:#ffffff;letter-spacing:-.5px;">Medifact</span>
    <span style="font-size:11px;color:#94a3b8;display:block;margin-top:2px;letter-spacing:.08em;text-transform:uppercase;">Evidence Intelligence</span>
  </td></tr>

  <!-- Greeting -->
  <tr><td style="padding:32px 40px 8px;">
    <p style="font-size:18px;font-weight:700;color:#0f172a;margin:0 0 8px;">Goedemorgen, {user_name} 👋</p>
    <p style="font-size:14px;color:#64748b;margin:0;">Hier is jouw wekelijkse overzicht van jouw Medifact-gebruik.</p>
  </td></tr>

  <!-- Usage card -->
  <tr><td style="padding:20px 40px;">
    <div style="background:#f1f5f9;border-radius:10px;padding:20px 24px;">
      <div style="display:flex;justify-content:space-between;margin-bottom:10px;">
        <span style="font-size:12px;font-weight:700;color:#475569;text-transform:uppercase;letter-spacing:.06em;">Analyses deze maand</span>
        <span style="font-size:20px;font-weight:900;color:#0f172a;">{used} / {limit_text}</span>
      </div>
      <!-- progress bar -->
      <div style="height:8px;background:#e2e8f0;border-radius:4px;overflow:hidden;margin-bottom:8px;">
        <div style="height:100%;width:{bar_width}%;background:{bar_color};border-radius:4px;"></div>
      </div>
      <p style="font-size:13px;color:#64748b;margin:0;">{rem_text} · <strong>{tier_name}</strong></p>
    </div>
  </td></tr>

  {upgrade_block}

  <!-- CTA -->
  <tr><td style="padding:8px 40px 32px;text-align:center;">
    <a href="https://medifact.eu/dashboard" style="display:inline-block;background:#316BBA;color:#ffffff;font-size:14px;font-weight:700;padding:13px 32px;border-radius:8px;text-decoration:none;">Open Medifact →</a>
  </td></tr>

  <!-- Footer -->
  <tr><td style="background:#f8fafc;padding:20px 40px;text-align:center;border-top:1px solid #e2e8f0;">
    <p style="font-size:12px;color:#94a3b8;margin:0;">
      Medifact · medifact.eu<br>
      <a href="https://medifact.eu/dashboard/settings" style="color:#94a3b8;">Afmelden van digest</a>
    </p>
  </td></tr>

</table>
</td></tr>
</table>
</body>
</html>"""


async def _send_resend_email(to: str, subject: str, html: str) -> tuple[bool, str]:
    """Send email via Resend API. Returns (success, error_message)."""
    if not RESEND_API_KEY:
        msg = f"RESEND_API_KEY not set"
        print(f"[Digest] {msg} — skipping email to {to}")
        return False, msg
    try:
        async with httpx.AsyncClient(timeout=15) as client:
            resp = await client.post(
                "https://api.resend.com/emails",
                headers={"Authorization": f"Bearer {RESEND_API_KEY}", "Content-Type": "application/json"},
                json={"from": f"Medifact <{FROM_EMAIL}>", "to": [to], "subject": subject, "html": html},
            )
            if resp.status_code not in (200, 201):
                msg = f"Resend {resp.status_code}: {resp.text}"
                print(f"[Digest] Error for {to}: {msg}")
                return False, msg
            return True, ""
    except Exception as e:
        msg = str(e)
        print(f"[Digest] Email send failed for {to}: {msg}")
        return False, msg


@app.post("/cron/weekly-digest")
async def weekly_digest(
    request: Request,
    db: AsyncSession = Depends(get_db),
):
    """
    Send weekly digest emails to all active users.
    Protected by CRON_SECRET header: Authorization: Bearer <CRON_SECRET>
    Call from Railway cron: POST /cron/weekly-digest weekly.
    """
    auth = request.headers.get("authorization", "")
    if not CRON_SECRET:
        raise HTTPException(status_code=500, detail="CRON_SECRET not configured")
    if auth != f"Bearer {CRON_SECRET}":
        raise HTTPException(status_code=401, detail="Unauthorized")

    result = await db.execute(select(User).where(User.is_active == True))
    users = result.scalars().all()

    sent = 0
    failed = 0
    errors: list[str] = []
    for u in users:
        tier_info = TIERS.get(u.tier, TIERS["free"])
        limit = tier_info["analyses"]
        html = _digest_html(
            user_name=u.name or u.email.split("@")[0],
            tier_name=tier_info["name"],
            used=u.analyses_used,
            limit=limit,
        )
        ok, err = await _send_resend_email(
            to=u.email,
            subject="📊 Jouw wekelijkse Medifact digest",
            html=html,
        )
        if ok:
            sent += 1
        else:
            failed += 1
            if err and err not in errors:
                errors.append(err)

    return {"sent": sent, "failed": failed, "total": len(users), "errors": errors}


@app.post("/cron/test-email")
async def test_email(
    request: Request,
):
    """Send a single test email to verify Resend config. Protected by CRON_SECRET."""
    auth = request.headers.get("authorization", "")
    if not CRON_SECRET:
        raise HTTPException(status_code=500, detail="CRON_SECRET not configured")
    if auth != f"Bearer {CRON_SECRET}":
        raise HTTPException(status_code=401, detail="Unauthorized")
    body = await request.json()
    to = body.get("to", "")
    if not to:
        raise HTTPException(status_code=400, detail="to field required")
    ok, err = await _send_resend_email(
        to=to,
        subject="✉️ Medifact email test",
        html="<p>Test email van Medifact. Als je dit ziet werkt Resend correct!</p>",
    )
    return {
        "success": ok,
        "error": err,
        "resend_api_key_set": bool(RESEND_API_KEY),
        "from_email": FROM_EMAIL,
    }


# ══════════════════════════════════════════════════════════════════════════════
# ADMIN — EMAIL TEMPLATES
# ══════════════════════════════════════════════════════════════════════════════
TEMPLATE_META = {
    "welcome":        {"label": "Welkomstmail",       "vars": ["name"]},
    "reminder":       {"label": "Herinnering (24u)",  "vars": ["name", "remaining", "analyses_used"]},
    "low_credits":    {"label": "Bijna door credits", "vars": ["name", "remaining", "analyses_used"]},
    "password_reset": {"label": "Wachtwoord reset",   "vars": ["name", "reset_link"]},
    "weekly_digest":  {"label": "Wekelijkse digest",  "vars": ["name", "analyses_used", "limit"]},
}

@app.get("/admin/email-templates")
@limiter.limit("30/minute")
async def admin_get_templates(
    request: Request,
    _: str = Depends(verify_admin),
    db: AsyncSession = Depends(get_db),
):
    result = await db.execute(select(EmailTemplate).order_by(EmailTemplate.key))
    templates = result.scalars().all()
    out = []
    for t in templates:
        meta = TEMPLATE_META.get(t.key, {})
        out.append({
            "key": t.key,
            "label": meta.get("label", t.key),
            "subject": t.subject,
            "html_body": t.html_body,
            "variables": meta.get("vars", []),
            "updated_at": t.updated_at.isoformat() if t.updated_at else None,
        })
    return out

@app.put("/admin/email-templates/{key}")
async def admin_update_template(
    key: str,
    patch: EmailTemplatePatch,
    _: str = Depends(verify_admin),
    db: AsyncSession = Depends(get_db),
):
    result = await db.execute(select(EmailTemplate).where(EmailTemplate.key == key))
    tpl = result.scalar_one_or_none()
    if not tpl:
        raise HTTPException(status_code=404, detail=f"Template '{key}' niet gevonden")
    if patch.subject is not None:
        tpl.subject = patch.subject
    if patch.html_body is not None:
        tpl.html_body = patch.html_body
    tpl.updated_at = datetime.now(timezone.utc)
    await db.commit()
    await db.refresh(tpl)
    return {"ok": True, "key": tpl.key, "subject": tpl.subject}

@app.post("/admin/email-templates/test/{key}")
@limiter.limit("30/minute")
async def admin_test_template(
    key: str,
    request: Request,
    _: str = Depends(verify_admin),
    db: AsyncSession = Depends(get_db),
):
    """Send a test email for the given template to the provided email address."""
    body = await request.json()
    to = body.get("to", "")
    if not to:
        raise HTTPException(status_code=400, detail="'to' field is vereist")
    test_vars = {
        "name": "Test Gebruiker",
        "remaining": 2,
        "analyses_used": 8,
        "reset_link": f"{FRONTEND_URL}/reset-password?token=TEST_TOKEN_EXAMPLE",
        "limit": 10,
    }
    ok, err = await _send_templated_email(db=db, key=key, to=to, variables=test_vars)
    return {"ok": ok, "error": err, "template": key, "to": to}

@app.post("/admin/email-templates/test-all")
@limiter.limit("10/minute")
async def admin_test_all_templates(
    request: Request,
    _: str = Depends(verify_admin),
    db: AsyncSession = Depends(get_db),
):
    """Send test emails for ALL templates to the provided address."""
    body = await request.json()
    to = body.get("to", "")
    if not to:
        raise HTTPException(status_code=400, detail="'to' field is vereist")
    results = {}
    for key in DEFAULT_EMAIL_TEMPLATES.keys():
        test_vars = {
            "name": "Test Gebruiker",
            "remaining": 2,
            "analyses_used": 8,
            "reset_link": f"{FRONTEND_URL}/reset-password?token=TEST_TOKEN_EXAMPLE",
            "limit": 10,
        }
        ok, err = await _send_templated_email(db=db, key=key, to=to, variables=test_vars)
        results[key] = {"ok": ok, "error": err}
        await asyncio.sleep(0.3)   # slight delay to avoid Resend rate limit
    return {"results": results, "to": to}
