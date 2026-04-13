#!/usr/bin/env python3
"""
Medifact — SaaS API v3.0
Multi-user · Stripe subscriptions · PostgreSQL · 8-axis scientific gate
"""

import asyncio, hashlib, json, os, re, uuid
from datetime import datetime, timezone, timedelta
from typing import Optional, Annotated

import httpx
import stripe
from anthropic import AsyncAnthropic
from fastapi import FastAPI, HTTPException, Depends, Header, Request
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import StreamingResponse, JSONResponse
import bcrypt as _bcrypt
from jose import JWTError, jwt
from pydantic import BaseModel, EmailStr
from sqlalchemy import Column, String, Integer, Boolean, DateTime, Text, select, update, text
from sqlalchemy import func
from sqlalchemy.ext.asyncio import AsyncSession, create_async_engine, async_sessionmaker
from sqlalchemy.orm import DeclarativeBase

# ── CONFIG ─────────────────────────────────────────────────────────────────────
ANTHROPIC_API_KEY = os.environ.get("ANTHROPIC_API_KEY", "")
DATABASE_URL      = os.environ.get("DATABASE_URL", "sqlite+aiosqlite:///./medifact.db").replace("postgres://", "postgresql+asyncpg://")
JWT_SECRET        = os.environ.get("JWT_SECRET", "change-this-to-a-random-secret-in-production")
JWT_ALGORITHM     = "HS256"
JWT_EXPIRE_DAYS   = 30
FRONTEND_URL      = os.environ.get("FRONTEND_URL", "http://localhost:8000")
MODEL             = os.environ.get("MEDIFACT_MODEL", "claude-haiku-4-5-20251001")
NCBI              = "https://eutils.ncbi.nlm.nih.gov/entrez/eutils"
CT_V2             = "https://clinicaltrials.gov/api/v2/studies"

stripe.api_key              = os.environ.get("STRIPE_SECRET_KEY", "")
STRIPE_WEBHOOK_SECRET       = os.environ.get("STRIPE_WEBHOOK_SECRET", "")
STRIPE_PRICE_STARTER        = os.environ.get("STRIPE_PRICE_STARTER", "")    # Set in Railway
STRIPE_PRICE_PROFESSIONAL   = os.environ.get("STRIPE_PRICE_PROFESSIONAL", "")
STRIPE_PRICE_ENTERPRISE     = os.environ.get("STRIPE_PRICE_ENTERPRISE", "")
STRIPE_PRICE_SOLO           = os.environ.get("STRIPE_PRICE_SOLO", "")
STRIPE_PRICE_TEAM           = os.environ.get("STRIPE_PRICE_TEAM", "")
STRIPE_PRICE_BUSINESS       = os.environ.get("STRIPE_PRICE_BUSINESS", "")

# ── SUBSCRIPTION TIERS ────────────────────────────────────────────────────────
TIERS = {
    "free":         {"name": "Gratis",       "analyses": 10,     "price": 0,   "seats": 1,      "stripe_price": None},
    "solo":         {"name": "Solo",         "analyses": 30,     "price": 79,  "seats": 1,      "stripe_price": STRIPE_PRICE_SOLO},
    "professional": {"name": "Professional", "analyses": 100,    "price": 199, "seats": 1,      "stripe_price": STRIPE_PRICE_PROFESSIONAL},
    "team":         {"name": "Team",         "analyses": 200,    "price": 399, "seats": 5,      "stripe_price": STRIPE_PRICE_TEAM},
    "business":     {"name": "Business",     "analyses": 600,    "price": 799, "seats": 15,     "stripe_price": STRIPE_PRICE_BUSINESS},
    "enterprise":   {"name": "Enterprise",   "analyses": 999999, "price": 0,   "seats": 999999, "stripe_price": STRIPE_PRICE_ENTERPRISE},
}

# ── DATABASE ───────────────────────────────────────────────────────────────────
engine         = create_async_engine(DATABASE_URL, echo=False)
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
    is_active           = Column(Boolean, default=True)
    created_at          = Column(DateTime(timezone=True), default=lambda: datetime.now(timezone.utc))

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

# ── APP SETUP ──────────────────────────────────────────────────────────────────
app = FastAPI(title="Medifact API", version="3.0.0")
app.add_middleware(
    CORSMiddleware, allow_origins=["*"],
    allow_credentials=True, allow_methods=["*"], allow_headers=["*"],
)
claude = AsyncAnthropic(api_key=ANTHROPIC_API_KEY)
@app.on_event("startup")
async def startup():
    try:
        async with engine.begin() as conn:
            await conn.run_sync(Base.metadata.create_all)
        print("✅ Database tables created/verified")
    except Exception as e:
        print(f"❌ DB startup error: {e}")

@app.get("/debug/db")
async def debug_db():
    try:
        async with engine.begin() as conn:
            result = await conn.execute(text("SELECT 1"))
            return {"db": "ok", "url": DATABASE_URL.split("@")[-1]}
    except Exception as e:
        return {"db": "error", "detail": str(e)}


# ── AUTH UTILITIES ─────────────────────────────────────────────────────────────
def hash_password(pw: str) -> str:
    return _bcrypt.hashpw(pw.encode(), _bcrypt.gensalt()).decode()

def verify_password(pw: str, hashed: str) -> bool:
    return _bcrypt.checkpw(pw.encode(), hashed.encode())

def create_token(user_id: str, email: str) -> str:
    payload = {
        "sub": user_id,
        "email": email,
        "exp": datetime.now(timezone.utc) + timedelta(days=JWT_EXPIRE_DAYS),
    }
    return jwt.encode(payload, JWT_SECRET, algorithm=JWT_ALGORITHM)

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
        payload = jwt.decode(token, JWT_SECRET, algorithms=[JWT_ALGORITHM])
        user_id: str = payload.get("sub")
    except JWTError:
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

async def increment_usage(user: User, db: AsyncSession):
    await db.execute(
        update(User).where(User.id == user.id).values(analyses_used=User.analyses_used + 1)
    )
    await db.commit()

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
async def pm_search(term: str, max_results: int = 10) -> tuple[int, list[str]]:
    async with httpx.AsyncClient() as c:
        r = await c.get(f"{NCBI}/esearch.fcgi",
                        params={"db": "pubmed", "term": term, "retmax": max_results,
                                "retmode": "json", "sort": "relevance"}, timeout=12)
        d = r.json()
    sr = d.get("esearchresult", {})
    return int(sr.get("count", 0)), sr.get("idlist", [])

async def pm_count(term: str) -> int:
    n, _ = await pm_search(term, 0)
    return n

async def pm_fetch(pmids: list[str]) -> list[dict]:
    if not pmids:
        return []
    async with httpx.AsyncClient() as c:
        r = await c.get(f"{NCBI}/efetch.fcgi",
                        params={"db": "pubmed", "id": ",".join(pmids),
                                "rettype": "abstract", "retmode": "xml"}, timeout=15)
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
        async with httpx.AsyncClient() as c:
            r = await c.get(CT_V2, params={"query.titles": query, "pageSize": 5, "format": "json"}, timeout=10)
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
async def register(req: RegisterRequest, db: AsyncSession = Depends(get_db)):
    try:
        result = await db.execute(select(User).where(User.email == req.email.lower()))
        if result.scalar_one_or_none():
            raise HTTPException(status_code=409, detail="E-mailadres al in gebruik")
        user = User(
            email=req.email.lower().strip(),
            name=req.name.strip(),
            password_hash=hash_password(req.password),
            tier="free",
        )
        db.add(user)
        await db.commit()
        await db.refresh(user)
        token = create_token(user.id, user.email)
        tier_info = TIERS["free"]
        return {
            "token": token,
            "user": {"id": user.id, "email": user.email, "name": user.name,
                     "tier": user.tier, "tier_name": tier_info["name"],
                     "analyses_used": user.analyses_used, "analyses_limit": tier_info["analyses"]},
        }
    except HTTPException:
        raise
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"Register error: {str(e)}")

@app.post("/auth/login")
async def login(req: LoginRequest, db: AsyncSession = Depends(get_db)):
    result = await db.execute(select(User).where(User.email == req.email.lower()))
    user = result.scalar_one_or_none()
    if not user or not verify_password(req.password, user.password_hash):
        raise HTTPException(status_code=401, detail="E-mailadres of wachtwoord onjuist")
    if not user.is_active:
        raise HTTPException(status_code=403, detail="Account geblokkeerd")
    token = create_token(user.id, user.email)
    tier_info = TIERS.get(user.tier, TIERS["free"])
    return {
        "token": token,
        "user": {"id": user.id, "email": user.email, "name": user.name,
                 "tier": user.tier, "tier_name": tier_info["name"],
                 "analyses_used": user.analyses_used, "analyses_limit": tier_info["analyses"]},
    }

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
async def analyze(req: AnalyzeRequest,
                  current_user: User = Depends(get_current_user),
                  db: AsyncSession = Depends(get_db)):
    if not ANTHROPIC_API_KEY:
        raise HTTPException(status_code=500, detail="ANTHROPIC_API_KEY not configured")
    await check_usage(current_user, db)

    pr = PROFILE
    axis_fns = [run_a1, run_a2, run_a3, run_a4, run_a5, run_a6, run_a7, run_a8]
    results = await asyncio.gather(*[fn(req.query, pr) for fn in axis_fns], return_exceptions=False)

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
    }

@app.get("/api/analyze/stream")
async def analyze_stream(query: str, profile: str = "medical",
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

    async def event_stream():
        tasks = [asyncio.create_task(run_one(fn, i)) for i, fn in enumerate(axis_fns)]
        completed = []
        for _ in range(8):
            item = await queue.get()
            completed.append(item)
            yield f"data: {json.dumps(item)}\n\n"

        results = [AxisResult(**{k: v for k, v in c.items() if k not in ("axis_index","type")})
                   for c in sorted(completed, key=lambda x: x["axis_index"]) if "status" in c]
        failed  = [r for r in results if r.status == "fail"]
        passed  = [r for r in results if r.status == "pass"]
        meta_s, meta_d = meta_check(results)
        is_open = len(failed) == 0 and meta_s != "FAIL"
        rid = "MF-" + uuid.uuid4().hex[:8]
        ts  = datetime.now(timezone.utc).isoformat()
        payload_str = json.dumps({"query": query, "results": [r.dict() for r in results], "rid": rid}, sort_keys=True)
        sha = hashlib.sha256(payload_str.encode()).hexdigest()[:32]

        # Save + count
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
                   "regulatory": pr.get("reg", []), "profile_label": "Medical"}
        yield f"data: {json.dumps(summary)}\n\n"
        for t in tasks:
            if not t.done(): t.cancel()

    return StreamingResponse(
        event_stream(), media_type="text/event-stream",
        headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no", "Access-Control-Allow-Origin": "*"}
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

# ── PUBLIC ENDPOINTS ───────────────────────────────────────────────────────────
@app.get("/")
def root():
    return {"service": "Medifact API", "version": "3.0.0", "status": "live", "docs": "/docs"}

@app.get("/health")
def health():
    return {"status": "ok", "model": MODEL,
            "anthropic_key": "set" if ANTHROPIC_API_KEY else "MISSING",
            "stripe": "configured" if stripe.api_key else "not configured",
            "database": DATABASE_URL.split("@")[-1] if "@" in DATABASE_URL else "local sqlite"}

@app.get("/api/tiers")
def get_tiers():
    """Public endpoint — show available subscription tiers."""
    return [{"id": k, "name": v["name"], "analyses": v["analyses"], "price": v["price"]}
            for k, v in TIERS.items()]


# ── ADMIN ──────────────────────────────────────────────────────────────────────
ADMIN_SECRET = os.environ.get("ADMIN_SECRET", "")

def verify_admin(x_admin_secret: Annotated[str, Header()] = ""):
    if not ADMIN_SECRET or x_admin_secret != ADMIN_SECRET:
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
async def admin_list_users(
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
async def admin_update_user(
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
async def admin_delete_user(
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
    await db.execute(text(f"DELETE FROM analyses WHERE user_id = '{user_id}'"))
    await db.delete(user)
    await db.commit()
    return {"ok": True, "deleted": user_id}

@app.get("/admin/stats")
async def admin_stats(
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
