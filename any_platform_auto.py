"""
job_agent.py - JD-aware auto-apply agent (Naukri + LinkedIn + Google Login)
"""
from __future__ import annotations

import datetime
import json
import logging
import os
import random
import re
import sqlite3
import sys
import time
from dataclasses import dataclass
from html import escape
from pathlib import Path
from typing import Optional, TypeVar
from urllib.parse import quote

import anthropic
import numpy as np
from dotenv import load_dotenv
from openai import OpenAI
from pydantic import BaseModel
from pypdf import PdfReader
from rich.logging import RichHandler
from tenacity import retry, stop_after_attempt, wait_exponential
from playwright.sync_api import sync_playwright, Page

BASE = Path(__file__).parent
env_file = BASE / ".env"
if not env_file.is_file():
    env_file = BASE / "s.env"
load_dotenv(env_file)

# ----------------------------------------------------------------- config
class CFG:
    resume_pdf = Path(os.getenv("RESUME_PDF", BASE / "resume.pdf"))
    profile_json = Path(os.getenv("PROFILE_JSON", BASE / "profile.json"))
    chrome_profile = Path(os.getenv("CHROME_PROFILE", BASE / "chrome_profile_pw"))
    out_dir = BASE / "tailored_resumes"
    db_path = BASE / "applications.db"
    report_path = BASE / "universal_report.html"
    extra_urls = BASE / "extra_jobs.txt"

    mode = os.getenv("MODE", "dry").strip().lower()
    provider = os.getenv("LLM_PROVIDER", "anthropic").lower()
    claude_model = os.getenv("CLAUDE_MODEL", "claude-sonnet-4-5-20250929")
    openai_model = os.getenv("OPENAI_MODEL", "gpt-4o-mini")

    min_score = int(os.getenv("MIN_MATCH_SCORE", "70"))
    max_per_role = int(os.getenv("MAX_JOBS_PER_ROLE", "10"))
    max_apply = int(os.getenv("MAX_APPLICATIONS_PER_RUN", "25"))
    city = os.getenv("CITY", "Hyderabad")
    linkedin_location = os.getenv("LINKEDIN_LOCATION", "India")

CFG.out_dir.mkdir(exist_ok=True)
CFG.chrome_profile.mkdir(exist_ok=True)
logging.basicConfig(
    level=logging.INFO,
    format="%(message)s",
    handlers=[RichHandler(show_path=False), logging.FileHandler(BASE / "universal_log.txt", encoding="utf-8")],
)
log = logging.getLogger("agent")

def pause(a: float = 1.5, b: float = 3.5):
    time.sleep(random.uniform(a, b))

def slug(s: str, n: int = 30) -> str:
    return re.sub(r"[^a-zA-Z0-9]+", "_", s or "x").strip("_")[:n]

# ----------------------------------------------------------------- schemas
class Bullet(BaseModel):
    text: str
    source_ids: list[str]

class ProjectOut(BaseModel):
    name: str
    tools: list[str]
    bullets: list[Bullet]

class ResumeOut(BaseModel):
    headline: str
    summary: str
    summary_source_ids: list[str]
    skills: list[str]
    projects: list[ProjectOut]

class Package(BaseModel):
    relevant: bool
    match_score: int
    company: str
    job_title: str
    matched_keywords: list[str]
    gaps: list[str]
    cover_note: str
    resume: Optional[ResumeOut]

ModelT = TypeVar("ModelT", bound=BaseModel)

@dataclass
class Job:
    platform: str
    url: str
    title: str = ""
    company: str = ""
    jd: str = ""
    @property
    def key(self) -> str:
        return self.url.split("?")[0]

# ----------------------------------------------------------------- fact bank + RAG
@dataclass
class Fact:
    id: str
    kind: str
    text: str

def read_pdf_text(path: Path) -> str:
    try:
        return "\n".join((p.extract_text() or "") for p in PdfReader(str(path)).pages)
    except Exception as e:
        log.warning(f"Could not read resume PDF ({e})")
        return ""

def build_facts(profile: dict, resume_text: str) -> list[Fact]:
    facts: list[Fact] = []
    def add(kind: str, text: str):
        if text.strip():
            facts.append(Fact(f"F{len(facts) + 1:03d}", kind, text.strip()))
    for cat, items in profile.get("skills", {}).items():
        add("skill", f"{cat}: {', '.join(items)}")
    for p in profile.get("projects", []):
        for b in p.get("bullets", []):
            add("project", f"{p['name']} [{', '.join(p.get('tools', []))}]: {b}")
    for e in profile.get("education", []): add("education", e)
    for e in profile.get("experience", []): add("experience", e)
    for c in profile.get("certifications", []): add("certification", c)
    for line in resume_text.splitlines():
        if len(line.strip()) > 30:
            add("resume_line", line)
    return facts

def _tok(s: str) -> set[str]:
    return set(re.findall(r"[a-z0-9+#.]{2,}", s.lower()))

class Retriever:
    def __init__(self, facts: list[Fact]):
        self.facts = facts
        self.model = None
        self.mat: np.ndarray | None = None
        try:
            from sentence_transformers import SentenceTransformer
            model = SentenceTransformer("all-MiniLM-L6-v2")
            mat = model.encode([f.text for f in facts], normalize_embeddings=True)
            self.model, self.mat = model, mat
        except Exception as e:
            log.warning(f"sentence-transformers unavailable ({e}); using keyword retrieval")

    def top(self, query: str, k: int = 16) -> list[Fact]:
        if self.model is not None and self.mat is not None:
            q = self.model.encode([query[:4000]], normalize_embeddings=True)[0]
            scores = self.mat @ q
        else:
            qt = _tok(query)
            scores = np.array([len(_tok(f.text) & qt) / (len(_tok(f.text)) ** 0.5 + 1) for f in self.facts])
        order = list(np.argsort(-scores)[:k])
        picked = {int(i) for i in order}
        for i, f in enumerate(self.facts):
            if f.kind in ("education", "experience", "certification"):
                picked.add(i)
        return [self.facts[i] for i in sorted(picked)]

# ----------------------------------------------------------------- LLM
class LLM:
    def __init__(self):
        self.anthropic_client = None
        self.openai_client = None
        if CFG.provider == "anthropic":
            self.anthropic_client = anthropic.Anthropic()
        else:
            self.openai_client = OpenAI()

    @retry(stop=stop_after_attempt(3), wait=wait_exponential(min=2, max=20), reraise=True)
    def structured(self, system: str, user: str, model_cls: type[ModelT]) -> ModelT:
        if CFG.provider == "anthropic":
            r = self.anthropic_client.messages.create(
                model=CFG.claude_model, max_tokens=4000, system=system,
                tools=[{"name": "submit", "description": "Return result.", "input_schema": model_cls.model_json_schema()}],
                tool_choice={"type": "tool", "name": "submit"},
                messages=[{"role": "user", "content": user}],
            )
            blk = next(b for b in r.content if b.type == "tool_use")
            return model_cls.model_validate(blk.input)
        r = self.openai_client.chat.completions.parse(
            model=CFG.openai_model, temperature=0.2, response_format=model_cls,
            messages=[{"role": "system", "content": system}, {"role": "user", "content": user}],
        )
        if r.choices[0].message.parsed is None:
            raise ValueError("No structured response")
        return r.choices[0].message.parsed

SYSTEM = """You are an expert ATS resume writer. Tailor candidate resume to ONE JD.
HARD RULES:
1. Use ONLY candidate facts. Every bullet and summary must cite fact ids.
2. Never invent employers, titles, years, metrics, tools, certs or projects.
3. You MAY rephrase using JD terminology where fact supports it.
4. Missing skills go into gaps. Do NOT add to resume.
5. Respect candidate level.
6. skills must be from allowed list. projects[].name must match profile exactly.
7. relevant=false if not a fit.
8. match_score 0-100 honest fit.
9. One page: summary <=45 words, max 3 projects, max 3 bullets each, max 18 skills."""

def tailor(llm: LLM, retr: Retriever, profile: dict, job: Job) -> Optional[Package]:
    facts = retr.top(f"{job.title}\n{job.jd}")
    fact_block = "\n".join(f"[{f.id}] ({f.kind}) {f.text}" for f in facts)
    allowed = sorted({s for items in profile.get("skills", {}).values() for s in items})
    user = (
        f"TARGET JOB\nTitle: {job.title}\nCompany: {job.company or 'unknown'}\nJD:\n{job.jd[:6000]}\n\n"
        f"CANDIDATE LEVEL: {profile.get('level')}\nTARGET ROLES: {', '.join(profile.get('target_roles', []))}\n"
        f"PROJECT NAMES: {[p['name'] for p in profile.get('projects', [])]}\n"
        f"ALLOWED SKILLS: {allowed}\n\nCANDIDATE FACTS\n{fact_block}"
    )
    pkg: Package = llm.structured(SYSTEM, user, Package)
    if pkg.resume:
        pkg.resume = sanitize(pkg.resume, profile, {f.id for f in facts})
    return pkg

def sanitize(r: ResumeOut, profile: dict, valid_ids: set[str]) -> ResumeOut:
    canon = {s.lower(): s for items in profile.get("skills", {}).values() for s in items}
    proj_tools = {p["name"]: p.get("tools", []) for p in profile.get("projects", [])}
    if not r.summary_source_ids or not all(i in valid_ids for i in r.summary_source_ids):
        r.summary = profile.get("default_summary", r.summary)
    seen, skills = set(), []
    for s in r.skills:
        c = canon.get(s.lower())
        if c and c not in seen:
            seen.add(c); skills.append(c)
    r.skills = skills[:18]
    clean = []
    for p in r.projects:
        if p.name not in proj_tools: continue
        p.bullets = [b for b in p.bullets if b.source_ids and all(i in valid_ids for i in b.source_ids)][:3]
        p.tools = [t for t in p.tools if t in proj_tools[p.name]]
        if p.bullets: clean.append(p)
    r.projects = clean[:3]
    return r

# ----------------------------------------------------------------- PDF
def render_pdf(path: Path, profile: dict, r: ResumeOut):
    from reportlab.lib import colors
    from reportlab.lib.pagesizes import A4
    from reportlab.lib.styles import ParagraphStyle
    from reportlab.lib.units import mm
    from reportlab.platypus import HRFlowable, Paragraph, SimpleDocTemplate, Spacer
    e = escape
    h1 = ParagraphStyle("h1", fontName="Helvetica-Bold", fontSize=16, leading=19)
    sub = ParagraphStyle("sub", fontName="Helvetica", fontSize=9, leading=12, textColor=colors.HexColor("#333333"))
    sec = ParagraphStyle("sec", fontName="Helvetica-Bold", fontSize=10.5, leading=13, spaceBefore=7, spaceAfter=2)
    body = ParagraphStyle("body", fontName="Helvetica", fontSize=9.5, leading=12.5)
    bul = ParagraphStyle("bul", parent=body, leftIndent=11, bulletIndent=2)
    contact = " | ".join(x for x in [profile.get("location"), profile.get("email"), profile.get("phone"), *profile.get("links", {}).values()] if x)
    s = [Paragraph(e(profile["name"]), h1), Paragraph(e(r.headline), sub), Paragraph(e(contact), sub), HRFlowable(width="100%", thickness=0.6, color=colors.grey, spaceBefore=3)]
    def section(title):
        s.extend([Paragraph(title.upper(), sec), HRFlowable(width="100%", thickness=0.3, color=colors.lightgrey)])
    section("Summary"); s.append(Paragraph(e(r.summary), body))
    section("Skills"); s.append(Paragraph(e(" | ".join(r.skills)), body))
    if r.projects:
        section("Projects")
        for p in r.projects:
            tools = f" <font size=8.5 color='#555555'>({e(', '.join(p.tools))})</font>" if p.tools else ""
            s.append(Paragraph(f"<b>{e(p.name)}</b>{tools}", body))
            for b in p.bullets:
                s.append(Paragraph(e(b.text), bul, bulletText="•"))
            s.append(Spacer(1, 3))
    if profile.get("experience"):
        section("Experience")
        for x in profile["experience"]: s.append(Paragraph(e(x), bul, bulletText="•"))
    section("Education")
    for x in profile.get("education", []): s.append(Paragraph(e(x), body))
    if profile.get("certifications"):
        section("Certifications")
        for x in profile["certifications"]: s.append(Paragraph(e(x), body))
    SimpleDocTemplate(str(path), pagesize=A4, leftMargin=16*mm, rightMargin=16*mm, topMargin=13*mm, bottomMargin=12*mm, title=f"{profile['name']} - Resume").build(s)

# ----------------------------------------------------------------- store
class Store:
    def __init__(self, path: Path):
        self.db = sqlite3.connect(path)
        self.db.execute("""CREATE TABLE IF NOT EXISTS jobs(
            key TEXT PRIMARY KEY, platform TEXT, title TEXT, company TEXT, url TEXT,
            score INTEGER, status TEXT, resume TEXT, gaps TEXT, ts TEXT)""")
        self.db.commit()
    def seen(self, key: str) -> bool:
        return self.db.execute("SELECT 1 FROM jobs WHERE key=?", (key,)).fetchone() is not None
    def save(self, job: Job, status: str, score: int = 0, resume: str = "", gaps: str = ""):
        self.db.execute("INSERT OR REPLACE INTO jobs VALUES (?,?,?,?,?,?,?,?,?,?)",
                        (job.key, job.platform, job.title, job.company, job.url, score, status, resume, gaps, datetime.datetime.now().isoformat(timespec="seconds")))
        self.db.commit()
    def since(self, ts: str):
        return self.db.execute("SELECT platform,title,company,url,score,status,resume,gaps FROM jobs WHERE ts>=? ORDER BY ts", (ts,)).fetchall()

# ----------------------------------------------------------------- form helpers
def answer_for(label: str, answers: dict) -> Optional[str]:
    l = label.lower()
    for key, val in answers.items():
        if key.lower() in l: return str(val)
    return None

def field_label(root, el) -> str:
    a = el.get_attribute("aria-label")
    if a: return a
    i = el.get_attribute("id")
    if i:
        lab = root.locator(f"label[for='{i}']")
        if lab.count(): return lab.first.inner_text()
    return el.get_attribute("placeholder") or el.get_attribute("name") or ""

def first_text(page: Page, selectors: list[str], min_len: int = 0) -> str:
    for sel in selectors:
        try:
            loc = page.locator(sel)
            for i in range(min(loc.count(), 5)):
                t = loc.nth(i).inner_text().strip()
                if len(t) >= min_len and t: return t
        except Exception: continue
    return ""

# ----------------------------------------------------------------- platform adapters
class Naukri:
    name = "naukri"
    check_title = True
    def queries(self, profile): return profile.get("target_roles", [])
    def logged_in(self, page: Page) -> bool:
        page.goto("https://www.naukri.com/mnjuser/profile", wait_until="domcontentloaded")
        page.wait_for_timeout(4000)
        return "login" not in page.url.lower()
    def search(self, page: Page, role: str) -> list[Job]:
        s = re.sub(r"[^a-z0-9]+", "-", role.lower()).strip("-")
        page.goto(f"https://www.naukri.com/{s}-jobs-in-{CFG.city.lower()}?k={quote(role)}&l={quote(CFG.city)}", wait_until="domcontentloaded")
        page.wait_for_timeout(4500)
        links, jobs = page.locator("a.title"), []
        for i in range(min(links.count(), CFG.max_per_role)):
            href = links.nth(i).get_attribute("href")
            if href: jobs.append(Job("Naukri", href, links.nth(i).inner_text().strip()))
        return jobs
    def read(self, page: Page, job: Job):
        page.goto(job.url, wait_until="domcontentloaded")
        page.wait_for_timeout(3500)
        job.jd = first_text(page, ["div[class*='dang-inner-html']", "section[class*='job-desc']", "div.job-desc", "div[class*='JDC']", "main"], 200)
        job.company = first_text(page, ["a[class*='comp-name']", "div[class*='comp-name'] a"])
    def apply(self, page: Page, _job: Job, _profile: dict, _pdf: Path) -> str:
        if page.locator("#already-applied").count(): return "already_applied"
        btn = page.locator("#apply-button")
        if not btn.count(): return "skipped_external_apply"
        btn.first.click()
        page.wait_for_timeout(3500)
        if page.locator("[class*='chatbot' i]").count(): return "needs_review_questionnaire"
        body = page.inner_text("body").lower()
        if "applied successfully" in body or "successfully applied" in body or page.locator("#already-applied").count():
            return "applied"
        return "unconfirmed"

class LinkedIn:
    name = "linkedin"
    check_title = True
    def queries(self, profile): return profile.get("target_roles", [])
    def logged_in(self, page: Page) -> bool:
        page.goto("https://www.linkedin.com/feed/", wait_until="domcontentloaded")
        page.wait_for_timeout(4000)
        return not any(x in page.url for x in ("login", "checkpoint", "authwall"))
    def search(self, page: Page, role: str) -> list[Job]:
        page.goto(f"https://www.linkedin.com/jobs/search/?keywords={quote(role)}&location={quote(CFG.linkedin_location)}&f_AL=true&f_TPR=r604800", wait_until="domcontentloaded")
        page.wait_for_timeout(5000)
        cards, jobs = page.locator("li[data-occludable-job-id]"), []
        for i in range(min(cards.count(), CFG.max_per_role)):
            jid = cards.nth(i).get_attribute("data-occludable-job-id")
            try: title = cards.nth(i).locator("a.job-card-container__link").first.inner_text().split("\n")[0].strip()
            except Exception: title = ""
            if jid: jobs.append(Job("LinkedIn", f"https://www.linkedin.com/jobs/view/{jid}/", title))
        return jobs
    def read(self, page: Page, job: Job):
        page.goto(job.url, wait_until="domcontentloaded")
        page.wait_for_timeout(4000)
        try: page.locator("button:has-text('See more')").first.click(timeout=1500)
        except Exception: pass
        job.jd = first_text(page, ["div.jobs-description__content", "#job-details", "div.jobs-box__html-content"], 200)
        job.company = first_text(page, ["div.job-details-jobs-unified-top-card__company-name a", ".jobs-unified-top-card__company-name"])
    def _fill(self, modal, answers: dict, pdf: Path):
        for f in modal.locator("input[type='file']").all():
            try: f.set_input_files(str(pdf))
            except Exception: pass
        for el in modal.locator("input[type='text'], input[type='number'], input[type='tel'], input:not([type]), textarea").all():
            try:
                if el.input_value().strip(): continue
                ans = answer_for(field_label(modal, el), answers)
                if ans is not None: el.fill(ans)
            except Exception: continue
        for sel in modal.locator("select").all():
            try:
                ans = answer_for(field_label(modal, sel), answers)
                if ans: sel.select_option(label=ans)
            except Exception: continue
        for fs in modal.locator("fieldset").all():
            try:
                ans = answer_for(fs.locator("legend").first.inner_text(), answers)
                if ans: fs.locator(f"label:text-is('{ans}')").first.click()
            except Exception: continue
    def _dismiss(self, page: Page, discard: bool):
        try:
            page.locator("button[aria-label='Dismiss']").first.click(timeout=2000)
            if discard: page.locator("button:has-text('Discard')").first.click(timeout=2000)
        except Exception: pass
    def apply(self, page: Page, _job: Job, profile: dict, pdf: Path) -> str:
        btn = page.locator("button.jobs-apply-button:has-text('Easy Apply'), button[aria-label*='Easy Apply']").first
        if not btn.count(): return "skipped_no_easy_apply"
        btn.click(); page.wait_for_timeout(2500)
        modal = page.locator("div[role='dialog']").first
        for _ in range(10):
            self._fill(modal, profile.get("answers", {}), pdf)
            submit = modal.locator("button[aria-label='Submit application']")
            if submit.count():
                try: modal.locator("label[for*='follow-company']").first.click(timeout=800)
                except Exception: pass
                submit.first.click(); page.wait_for_timeout(3000)
                body = page.inner_text("body").lower()
                self._dismiss(page, discard=False)
                return "applied" if ("application was sent" in body or "application sent" in body) else "unconfirmed"
            nxt = modal.locator("button[aria-label='Continue to next step'], button[aria-label='Review your application']")
            if not nxt.count(): break
            nxt.first.click(); page.wait_for_timeout(1500)
            if modal.locator(".artdeco-inline-feedback--error").count():
                self._dismiss(page, discard=True); return "needs_review_unanswered_fields"
        self._dismiss(page, discard=True); return "needs_review_flow"

class ExtraURLs:
    name = "urls"
    check_title = False
    def queries(self, _profile): return ["-"]
    def logged_in(self, _page): return True
    def search(self, _page: Page, _role: str) -> list[Job]:
        if not CFG.extra_urls.exists(): return []
        urls = [u.strip() for u in CFG.extra_urls.read_text().splitlines() if u.strip().startswith("http")]
        return [Job("Other", u) for u in urls]
    def read(self, page: Page, job: Job):
        page.goto(job.url, wait_until="domcontentloaded"); page.wait_for_timeout(4000)
        job.title = job.title or page.title(); job.jd = page.inner_text("body")[:8000]
    def apply(self, _page: Page, job: Job, _profile: dict, pdf: Path) -> str:
        log.info(f"Manual: {job.url} resume {pdf.name}"); return "tailored_manual_apply"

# ----------------------------------------------------------------- orchestration
class Runner:
    def __init__(self, profile, retr, llm, store):
        self.profile, self.retr, self.llm, self.store = profile, retr, llm, store
        self.applied = 0
    def title_ok(self, title: str) -> bool:
        t = title.lower(); return not t or any(k in t for k in self.profile.get("title_keywords", []))
    def handle(self, page: Page, ad, job: Job):
        if self.store.seen(job.key): return
        if ad.check_title and not self.title_ok(job.title):
            self.store.save(job, "skipped_title"); return
        try: ad.read(page, job)
        except Exception as e:
            log.warning(f"read error {job.url}: {e}"); self.store.save(job, "read_error"); return
        if len(job.jd) < 200: self.store.save(job, "no_jd"); return
        pkg = tailor(self.llm, self.retr, self.profile, job)
        if pkg and not job.company: job.company = pkg.company
        if not pkg or not pkg.relevant or not pkg.resume:
            log.info(f"- not relevant: {job.title}"); self.store.save(job, "not_relevant"); return
        gaps = ", ".join(pkg.gaps)
        if pkg.match_score < CFG.min_score:
            log.info(f"- low score {pkg.match_score}: {job.title}"); self.store.save(job, "low_score", pkg.match_score, gaps=gaps); return
        stem = f"{slug(job.company, 18)}_{slug(job.title, 28)}_{datetime.datetime.now():%m%d_%H%M%S}"
        pdf = CFG.out_dir / f"{stem}.pdf"
        render_pdf(pdf, self.profile, pkg.resume)
        (CFG.out_dir / f"{stem}.json").write_text(pkg.model_dump_json(indent=2), encoding="utf-8")
        log.info(f"[green]RESUME[/green] {job.title} @ {job.company} | score {pkg.match_score}", extra={"markup": True})
        if CFG.mode == "dry": status = "tailored_dry_run"
        else:
            if CFG.mode == "review":
                if input(f"Apply to '{job.title}' @ {job.company}? [y/N] ").lower() != "y":
                    self.store.save(job, "declined", pkg.match_score, str(pdf), gaps); return
            status = ad_apply(ad, page, job, self.profile, pdf)
            if status == "applied": self.applied += 1
        log.info(f"  -> {status}"); self.store.save(job, status, pkg.match_score, str(pdf), gaps); pause()
    def run(self, page: Page, ad):
        try: logged_in = ad.logged_in(page)
        except Exception as e: log.warning(f"[{ad.name}] login check failed: {e}"); return
        if not logged_in: log.warning(f"[{ad.name}] not logged in - run --login"); return
        for q in ad.queries(self.profile):
            try: jobs = ad.search(page, q)
            except Exception as e: log.warning(f"[{ad.name}] search {q}: {e}"); continue
            log.info(f"[{ad.name}] {q}: {len(jobs)} jobs")
            for job in jobs:
                if self.applied >= CFG.max_apply: log.info("Cap reached"); return
                try: self.handle(page, ad, job)
                except Exception as e: log.warning(f"job error {job.url}: {e}")

def ad_apply(ad, page, job, profile, pdf) -> str:
    try: return ad.apply(page, job, profile, pdf)
    except Exception as e: log.warning(f"apply error: {e}"); return "apply_error"

def write_report(store: Store, since: str):
    rows = store.since(since)
    tr = "".join(f"<tr><td>{escape(p)}</td><td><a href='{escape(u)}'>{escape(t)}</a></td><td>{escape(c)}</td><td>{s}</td><td>{escape(st)}</td><td>{escape(Path(r).name if r else '')}</td><td>{escape(g)}</td></tr>" for p, t, c, u, s, st, r, g in rows)
    CFG.report_path.write_text(f"<html><body style='font-family:sans-serif'><h2>Report {CFG.mode} - {datetime.datetime.now():%Y-%m-%d %H:%M}</h2><table border=1 cellpadding=6 style='border-collapse:collapse'><tr><th>Platform</th><th>Title</th><th>Company</th><th>Score</th><th>Status</th><th>Resume</th><th>Gaps</th></tr>{tr}</table><p>Folder: {CFG.out_dir}</p></body></html>", encoding="utf-8")
    log.info(f"Report: {CFG.report_path}")

def launch(p):
    # FIXED: no channel="chrome" - works on any system
    return p.chromium.launch_persistent_context(
        str(CFG.chrome_profile),
        headless=False,
        no_viewport=True,
        args=["--start-maximized", "--disable-blink-features=AutomationControlled", "--no-sandbox"]
    )

def validate_config():
    if CFG.mode not in {"dry", "review", "auto"}: raise SystemExit(f"Invalid MODE {CFG.mode}")
    if CFG.provider not in {"anthropic", "openai"}: raise SystemExit(f"Invalid PROVIDER {CFG.provider}")
    if not 0 <= CFG.min_score <= 100: raise SystemExit("MIN_MATCH_SCORE 0-100")

def main():
    if "--login" in sys.argv:
        with sync_playwright() as p:
            ctx = launch(p)
            pg = ctx.pages[0] if ctx.pages else ctx.new_page()

            print("\n=== STEP 1: GOOGLE LOGIN (Your Google Account) ===")
            pg.goto("https://accounts.google.com/", wait_until="domcontentloaded")
            input(">> Login to your GOOGLE account fully here, then press ENTER...")

            print("\n=== STEP 2: Naukri via Google ===")
            pg.goto("https://www.naukri.com/nlogin/login", wait_until="domcontentloaded")
            pg.wait_for_timeout(3000)
            # Auto-try to click Google button if present
            for sel in ["text=Google", "text=Continue with Google", "[class*='google']"]:
                try:
                    if pg.locator(sel).first.count() > 0:
                        pg.locator(sel).first.click()
                        pg.wait_for_timeout(4000)
                        break
                except: pass
            input(">> Finish Naukri login with Google, then press ENTER...")

            print("\n=== STEP 3: LinkedIn via Google ===")
            pg.goto("https://www.linkedin.com/login", wait_until="domcontentloaded")
            pg.wait_for_timeout(3000)
            for sel in ["text=Continue with Google", "text=Sign in with Google", "text=Google"]:
                try:
                    if pg.locator(sel).first.count() > 0:
                        pg.locator(sel).first.click()
                        pg.wait_for_timeout(4000)
                        break
                except: pass
            input(">> Finish LinkedIn login with Google, then press ENTER...")

            ctx.close()
            print(f"\n✅ Done! Login saved to {CFG.chrome_profile}")
        return

    validate_config()
    only = None
    if "--only" in sys.argv:
        idx = sys.argv.index("--only")
        if idx + 1 >= len(sys.argv): raise SystemExit("--only needs naukri|linkedin|urls")
        only = sys.argv[idx+1].lower()

    if not CFG.profile_json.is_file():
        raise SystemExit(f"Profile not found: {CFG.profile_json}")
    profile = json.loads(CFG.profile_json.read_text(encoding="utf-8"))
    facts = build_facts(profile, read_pdf_text(CFG.resume_pdf) if CFG.resume_pdf.exists() else "")
    log.info(f"{len(facts)} facts | mode={CFG.mode} | provider={CFG.provider}")
    runner = Runner(profile, Retriever(facts), LLM(), Store(CFG.db_path))
    adapters = [Naukri(), LinkedIn(), ExtraURLs()]
    if only: adapters = [a for a in adapters if a.name == only]

    start = datetime.datetime.now().isoformat(timespec="seconds")
    with sync_playwright() as p:
        ctx = launch(p)
        page = ctx.pages[0] if ctx.pages else ctx.new_page()
        try:
            for ad in adapters: runner.run(page, ad)
        finally:
            ctx.close()
            write_report(runner.store, start)
    log.info(f"Done. Applied: {runner.applied}")

if __name__ == "__main__":
    main()