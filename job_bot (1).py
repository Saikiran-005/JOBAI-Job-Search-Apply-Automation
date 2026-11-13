"""
Multi-site job apply assistant (LangChain edition)
Sites: Naukri, LinkedIn, Indeed, Foundit

Setup:
    pip install -r requirements.txt
    python "job_bot (1).py" --login  # log in once to every site (session is saved)
    python "job_bot (1).py"          # DRY_RUN=true by default: tailors resumes, does NOT apply
    DRY_RUN=false in .env or s.env   # only after you have reviewed the dry-run output
"""
from __future__ import annotations

import datetime
import logging
import os
import random
import re
import sqlite3
import sys
import time
from pathlib import Path
from typing import List
from xml.sax.saxutils import escape

from dotenv import load_dotenv
from pydantic import BaseModel, Field
from pypdf import PdfReader
from reportlab.lib.pagesizes import A4
from reportlab.lib.styles import getSampleStyleSheet
from reportlab.platypus import Paragraph, SimpleDocTemplate, Spacer
from selenium import webdriver
from selenium.webdriver.common.by import By
from selenium.webdriver.chrome.options import Options

from langchain_anthropic import ChatAnthropic
from langchain_community.cache import SQLiteCache
from langchain_core.globals import set_llm_cache
from langchain_core.prompts import ChatPromptTemplate
from langchain_openai import ChatOpenAI

BASE = Path(__file__).parent
ENV_FILE = BASE / ".env"
if not ENV_FILE.is_file():
    ENV_FILE = BASE / "s.env"
if ENV_FILE.is_file():
    load_dotenv(ENV_FILE)


# ============================ CONFIG (all from .env) ============================
def env_bool(key: str, default: bool) -> bool:
    return os.getenv(key, str(default)).strip().lower() == "true"


RESUME_PDF = Path(os.getenv("RESUME_PDF", BASE / "resume.pdf"))
PROFILE_PATH = os.getenv("CHROME_PROFILE", str(BASE / "chrome_profile"))
RESUME_DIR = BASE / "tailored_resumes"
RESUME_DIR.mkdir(exist_ok=True)

ANTHROPIC_KEY = os.getenv("ANTHROPIC_API_KEY")
OPENAI_KEY = os.getenv("OPENAI_API_KEY")
USE_CLAUDE = env_bool("USE_CLAUDE", True)
CLAUDE_MODEL = os.getenv("CLAUDE_MODEL", "claude-sonnet-4-6")
OPENAI_MODEL = os.getenv("OPENAI_MODEL", "gpt-4o-mini")

# Candidate details (answers to form fields come from here, NOT from the LLM)
FULL_NAME = os.getenv("FULL_NAME", "Your Name")
EMAIL = os.getenv("EMAIL", "")
PHONE = os.getenv("PHONE", "")
LOCATION = os.getenv("LOCATION", "Hyderabad")
EXPERIENCE = os.getenv("EXPERIENCE", "Fresher")
CTC = os.getenv("EXPECTED_CTC", "5 LPA")
NOTICE = os.getenv("NOTICE_PERIOD", "Immediate")

# Behaviour
DRY_RUN = env_bool("DRY_RUN", True)             # True = tailor + log only, never click Apply
MIN_SCORE = int(os.getenv("MIN_SCORE", "70"))    # skip jobs the LLM scores below this
MAX_PER_SITE = int(os.getenv("MAX_PER_SITE", "15"))
JOBS_PER_ROLE = int(os.getenv("JOBS_PER_ROLE", "5"))
ENABLED_SITES = [s.strip().lower() for s in os.getenv("SITES", "naukri,linkedin,indeed,foundit").split(",")]

SKILLS = ["qa", "quality assurance", "manual testing", "sdlc", "stlc", "selenium", "webdriver",
          "sql", "oracle", "pl/sql", "power bi", "dax", "power query", "tableau", "python",
          "pandas", "data validation", "data modeling", "etl"]
TARGET_ROLES = ["Data Analyst", "Power BI Developer", "BI Analyst", "Data Scientist",
                "QA Engineer", "QA Automation Engineer", "Manual Tester", "Test Engineer"]

logging.basicConfig(level=logging.INFO, format="[%(asctime)s] %(message)s", datefmt="%H:%M:%S",
                    handlers=[logging.StreamHandler(), logging.FileHandler(BASE / "bot_log.txt", encoding="utf-8")])
log = logging.info


def pause(a: float = 2, b: float = 4) -> None:
    time.sleep(random.uniform(a, b))


# ============================ LANGCHAIN PIPELINE ============================
class Project(BaseModel):
    name: str
    bullets: List[str]


class TailoredResume(BaseModel):
    company: str = Field(default="Unknown", description="Company name from the job post")
    match_score: int = Field(ge=0, le=100, description="Honest fit score for this job")
    summary: str
    skills: List[str] = Field(description="Only skills that appear in the base resume")
    projects: List[Project] = Field(description="Only projects that appear in the base resume")
    education: List[str]
    cover_letter: str = Field(description="Two sentences, specific to this job")


PROMPT = ChatPromptTemplate.from_messages([
    ("system",
     "You tailor resumes for ATS systems.\n"
     "HARD RULES:\n"
     "1. Use ONLY facts present in the BASE RESUME. Never invent employers, projects, tools, "
     "certifications, metrics, or years of experience.\n"
     "2. Candidate experience level: {experience}. Do not claim professional work experience.\n"
     "3. You may reorder, reword, and emphasise, and mirror job keywords ONLY where the candidate "
     "genuinely has that skill.\n"
     "4. match_score must honestly reflect real fit, not be inflated.\n"
     "5. Keep it to one page."),
    ("human", "BASE RESUME:\n{base}\n\nJOB TITLE: {title}\n\nJOB DESCRIPTION:\n{jd}"),
])


def load_base_resume() -> str:
    pdf_error = None
    if RESUME_PDF.is_file():
        try:
            text = "".join(p.extract_text() or "" for p in PdfReader(str(RESUME_PDF)).pages)
            if len(text) >= 100:
                return text
            pdf_error = "PDF has fewer than 100 extractable characters"
        except Exception as e:
            pdf_error = str(e)
    else:
        pdf_error = "file does not exist"

    resume_text = BASE / "resume.txt"
    if resume_text.is_file():
        try:
            text = resume_text.read_text(encoding="utf-8-sig").strip()
        except OSError as e:
            sys.exit(f"Cannot read resume at {resume_text}: {e}")
        if len(text) >= 100:
            log(f"Using {resume_text.name}; {RESUME_PDF.name} is not a readable text-based PDF.")
            return text

    sys.exit(
        f"Cannot read a usable resume. PDF: {RESUME_PDF} ({pdf_error}); "
        f"text fallback: {resume_text} is missing or has fewer than 100 characters."
    )


def build_chain():
    set_llm_cache(SQLiteCache(database_path=str(BASE / ".llm_cache.db")))  # identical job = no repeat API cost
    models = []
    if ANTHROPIC_KEY and USE_CLAUDE:
        models.append(ChatAnthropic(model=CLAUDE_MODEL, temperature=0.2, max_tokens=3000, api_key=ANTHROPIC_KEY))
    if OPENAI_KEY:
        models.append(ChatOpenAI(model=OPENAI_MODEL, temperature=0.2, api_key=OPENAI_KEY))
    if not models:
        sys.exit(f"Set ANTHROPIC_API_KEY or OPENAI_API_KEY in {ENV_FILE.name}")
    structured = [m.with_structured_output(TailoredResume) for m in models]
    llm = structured[0].with_fallbacks(structured[1:]) if len(structured) > 1 else structured[0]
    return PROMPT | llm


BASE_RESUME = load_base_resume()
BASE_LOWER = BASE_RESUME.lower()


def relevant(title: str, jd: str) -> bool:
    """Cheap keyword gate so we don't spend an LLM call on irrelevant jobs."""
    text = f"{title} {jd}".lower()
    return sum(s in text for s in SKILLS) >= 2 or any(w in text for w in ("qa", "analyst", "power bi", "tester"))


def tailor(chain, title: str, jd: str) -> TailoredResume:
    out: TailoredResume = chain.invoke(
        {"base": BASE_RESUME[:6000], "title": title, "jd": jd[:4000], "experience": EXPERIENCE})
    # Guardrail: drop any skill the LLM added that is not in your real resume
    out.skills = [s for s in out.skills if s.lower() in BASE_LOWER]
    return out


def render_pdf(path: Path, r: TailoredResume) -> None:
    st = getSampleStyleSheet()
    P = lambda t, s="BodyText": Paragraph(escape(t), st[s])
    contact = " | ".join(x for x in (LOCATION, EMAIL, PHONE) if x)
    flow = [P(FULL_NAME, "Title"), P(contact), Spacer(1, 8),
            P("SUMMARY", "Heading3"), P(r.summary),
            P("SKILLS", "Heading3"), P(", ".join(r.skills)),
            P("PROJECTS", "Heading3")]
    for pr in r.projects:
        flow.append(P(pr.name, "Heading4"))
        flow += [P("\u2022 " + b) for b in pr.bullets]
    flow.append(P("EDUCATION", "Heading3"))
    flow += [P(e) for e in r.education]
    SimpleDocTemplate(str(path), pagesize=A4, leftMargin=40, rightMargin=40,
                      topMargin=36, bottomMargin=36).build(flow)


# ============================ TRACKING (never apply twice) ============================
DB = sqlite3.connect(BASE / "applied.db")
DB.execute("CREATE TABLE IF NOT EXISTS jobs(url TEXT PRIMARY KEY, site TEXT, title TEXT, score INT, status TEXT, ts TEXT)")


def seen(url: str) -> bool:
    return DB.execute("SELECT 1 FROM jobs WHERE url=?", (url,)).fetchone() is not None


def record(url, site, title, score, status) -> None:
    DB.execute("INSERT OR REPLACE INTO jobs VALUES(?,?,?,?,?,?)",
               (url, site, title, score, status, datetime.datetime.now().isoformat(timespec="seconds")))
    DB.commit()


# ============================ SELENIUM HELPERS ============================
def get_driver():
    o = Options()
    o.add_argument("--start-maximized")
    o.add_argument(f"--user-data-dir={PROFILE_PATH}")
    return webdriver.Chrome(options=o)


def scrape_jd(d) -> str:
    for sel in ["div.job-desc", "div.styles_JDC__dang-inner-html__h0K4t", "div#jobDescriptionText",
                "div.jobs-description__content", "div.description", "main", "article"]:
        try:
            for el in d.find_elements(By.CSS_SELECTOR, sel):
                if len(el.text) > 200:
                    return el.text[:4000]
        except Exception:
            continue
    try:
        return d.find_element(By.TAG_NAME, "body").text[:4000]
    except Exception:
        return ""


def find_first(d, xpaths):
    for xp in xpaths:
        try:
            for el in d.find_elements(By.XPATH, xp):
                if el.is_displayed():
                    return el
        except Exception:
            continue
    return None


def click(d, el) -> None:
    d.execute_script("arguments[0].scrollIntoView({block:'center'});", el)
    d.execute_script("arguments[0].click();", el)


def fill_form(d, r: TailoredResume, resume_pdf: Path) -> None:
    answers = {"experience": EXPERIENCE, "ctc": CTC, "expected": CTC, "notice": NOTICE,
               "location": LOCATION, "city": LOCATION, "cover": r.cover_letter}
    for inp in d.find_elements(By.XPATH, "//input[@type='text' or @type='number' or not(@type)] | //textarea")[:25]:
        try:
            if not inp.is_displayed() or inp.get_attribute("value"):
                continue  # skip hidden or already-filled fields
            label = " ".join(inp.get_attribute(a) or "" for a in ("placeholder", "name", "id", "aria-label")).lower()
            for key, val in answers.items():
                if key in label:
                    inp.send_keys(val)
                    break
        except Exception:
            continue
    try:
        for f in d.find_elements(By.XPATH, "//input[@type='file']"):
            f.send_keys(str(resume_pdf))
            break
    except Exception:
        pass


SUBMIT_XP = ["//button[contains(@aria-label,'Submit application')]",
             "//button[normalize-space()='Submit']", "//button[contains(.,'Submit')]"]
NEXT_XP = ["//button[contains(@aria-label,'Review')]",
           "//button[contains(@aria-label,'Next') or contains(@aria-label,'Continue')]",
           "//button[normalize-space()='Continue']", "//button[normalize-space()='Next']",
           "//button[normalize-space()='Save']"]
SUCCESS_RE = re.compile(r"applied successfully|application (has been )?(sent|submitted)|you have applied|"
                        r"application was sent|successfully applied", re.I)


def click_through(d, r, resume_pdf, steps: int = 6) -> bool:
    """Walk a multi-step apply form. True only if we reached a submit AND saw a success message."""
    for _ in range(steps):
        fill_form(d, r, resume_pdf)
        pause(1, 2)
        btn = find_first(d, SUBMIT_XP)
        if btn:
            click(d, btn)
            pause(2, 4)
            return bool(SUCCESS_RE.search(d.find_element(By.TAG_NAME, "body").text))
        btn = find_first(d, NEXT_XP)
        if not btn:
            return False
        click(d, btn)
        pause(1, 2)
    return False


# ============================ SITES ============================
# NOTE: CSS/XPath selectors on job sites change often. If a site stops working,
# open DevTools (F12) on a job page and update that site's selectors here.
class Site:
    name = ""
    check_url = ""          # page that redirects to login when logged out
    login_url = ""
    link_css = ""           # CSS selector for job links on a results page

    def search_url(self, role: str) -> str:
        raise NotImplementedError

    def logged_in(self, d) -> bool:
        d.get(self.check_url)
        pause(4, 6)
        return not any(w in d.current_url.lower() for w in ("login", "signin", "auth", "checkpoint"))

    def collect(self, d, role: str, n: int):
        d.get(self.search_url(role))
        pause(5, 8)
        d.execute_script("window.scrollTo(0, 1500)")
        pause(1, 2)
        out, found = [], set()
        for a in d.find_elements(By.CSS_SELECTOR, self.link_css):
            href, title = a.get_attribute("href"), a.text.strip()
            if href and title and href not in found:
                found.add(href)
                out.append((title, href))
            if len(out) >= n:
                break
        return out

    def apply(self, d, r: TailoredResume, resume_pdf: Path) -> bool:
        raise NotImplementedError


class Naukri(Site):
    name = "naukri"
    check_url = "https://www.naukri.com/mnjuser/profile"
    login_url = "https://www.naukri.com/nlogin/login"
    link_css = "a.title"

    def search_url(self, role):
        return f"https://www.naukri.com/{role.lower().replace(' ', '-')}-jobs-in-hyderabad"

    def apply(self, d, r, resume_pdf):
        btn = find_first(d, ["//button[@id='apply-button']", "//button[contains(.,'Apply') and not(contains(.,'company site'))]"])
        if not btn:
            return False  # external "apply on company site" jobs are skipped
        click(d, btn)
        pause(3, 5)
        # Naukri applies with the resume saved on your profile; chatbot questions are not handled
        return bool(SUCCESS_RE.search(d.find_element(By.TAG_NAME, "body").text)) or click_through(d, r, resume_pdf, 3)


class LinkedIn(Site):
    name = "linkedin"
    check_url = "https://www.linkedin.com/feed/"
    login_url = "https://www.linkedin.com/login"
    link_css = "a.job-card-container__link"

    def search_url(self, role):
        return f"https://www.linkedin.com/jobs/search/?keywords={role.replace(' ', '%20')}&location=India&f_AL=true&f_TPR=r604800"

    def apply(self, d, r, resume_pdf):
        easy = find_first(d, ["//button[contains(@aria-label,'Easy Apply')]", "//button[contains(.,'Easy Apply')]"])
        if not easy:
            return False
        click(d, easy)
        pause(2, 4)
        ok = click_through(d, r, resume_pdf, 8)
        if not ok:  # close the dialog and discard the half-filled application
            dismiss = find_first(d, ["//button[@aria-label='Dismiss']"])
            if dismiss:
                click(d, dismiss)
                pause(1, 2)
                discard = find_first(d, ["//button[contains(.,'Discard')]"])
                if discard:
                    click(d, discard)
        return ok


class Indeed(Site):
    name = "indeed"
    check_url = "https://secure.indeed.com/account/view"
    login_url = "https://secure.indeed.com/auth"
    link_css = "a.jcs-JobTitle"

    def search_url(self, role):
        return f"https://in.indeed.com/jobs?q={role.replace(' ', '+')}&l=Hyderabad&fromage=7"

    def apply(self, d, r, resume_pdf):
        btn = find_first(d, ["//button[@id='indeedApplyButton']", "//button[contains(@aria-label,'Apply now')]"])
        if not btn:
            return False  # external employer sites are skipped
        click(d, btn)
        pause(3, 5)
        if len(d.window_handles) > 1:
            d.switch_to.window(d.window_handles[-1])
        ok = click_through(d, r, resume_pdf, 8)
        while len(d.window_handles) > 1:
            d.close()
            d.switch_to.window(d.window_handles[0])
        return ok


class Foundit(Site):
    name = "foundit"
    check_url = "https://www.foundit.in/seeker/profile"
    login_url = "https://www.foundit.in/rio/login"
    link_css = "a[href*='/job/']"

    def search_url(self, role):
        return f"https://www.foundit.in/srp/results?query={role.replace(' ', '%20')}&locations=Hyderabad"

    def apply(self, d, r, resume_pdf):
        btn = find_first(d, ["//button[normalize-space()='Apply' or normalize-space()='Apply Now']"])
        if not btn:
            return False
        click(d, btn)
        pause(3, 5)
        return bool(SUCCESS_RE.search(d.find_element(By.TAG_NAME, "body").text)) or click_through(d, r, resume_pdf, 4)


SITES = [Naukri(), LinkedIn(), Indeed(), Foundit()]


# ============================ MAIN LOOP ============================
def run_site(d, site: Site, chain, rows: list) -> int:
    log(f"[{site.name}] starting (DRY_RUN={DRY_RUN})")
    if not site.logged_in(d):
        log(f"[{site.name}] not logged in -> run: python job_bot.py --login")
        return 0
    done = 0
    for role in TARGET_ROLES:
        if done >= MAX_PER_SITE:
            break
        try:
            jobs = site.collect(d, role, JOBS_PER_ROLE)
        except Exception as e:
            log(f"[{site.name}] search failed for {role}: {e}")
            continue
        log(f"[{site.name}] {role}: {len(jobs)} jobs")
        for title, url in jobs:
            if done >= MAX_PER_SITE:
                break
            if seen(url):
                continue
            try:
                d.get(url)
                pause(3, 6)
                jd = scrape_jd(d)
                if not relevant(title, jd):
                    record(url, site.name, title, 0, "irrelevant")
                    continue
                r = tailor(chain, title, jd)                  # ONE LLM call per job, with the real JD
                if r.match_score < MIN_SCORE:
                    record(url, site.name, title, r.match_score, "low_score")
                    continue
                pdf = RESUME_DIR / f"{site.name}_{re.sub(r'[^a-zA-Z0-9]', '_', title)[:30]}_{abs(hash(url)) % 10**6}.pdf"
                render_pdf(pdf, r)
                if DRY_RUN:
                    status = "dry_run"
                else:
                    status = "applied" if site.apply(d, r, pdf) else "failed"
                record(url, site.name, title, r.match_score, status)
                done += status == "applied"
                rows.append(f"<tr><td>{site.name}</td><td>{escape(title)}</td><td>{escape(r.company)}</td>"
                            f"<td>{r.match_score}%</td><td>{status}</td><td><a href='{escape(url)}'>link</a></td>"
                            f"<td>{pdf.name}</td></tr>")
                log(f"[{site.name}] {status.upper()}: {title} ({r.match_score}%)")
                pause(4, 9)
            except Exception as e:
                log(f"[{site.name}] error on '{title}': {e}")
    return done


def main():
    if "--login" in sys.argv:
        d = get_driver()
        for s in SITES:
            d.get(s.login_url)
            input(f"Log in to {s.name}, then press ENTER...")
        d.quit()
        return

    chain = build_chain()
    d = get_driver()
    rows, totals = [], {}
    try:
        for s in SITES:
            if s.name in ENABLED_SITES:
                totals[s.name] = run_site(d, s, chain, rows)
    finally:
        d.quit()
    log(f"TOTAL applied: {totals}")
    (BASE / "report.html").write_text(
        f"<html><body><h2>Run {datetime.datetime.now():%Y-%m-%d %H:%M} (DRY_RUN={DRY_RUN})</h2>"
        "<table border=1 cellpadding=6><tr><th>Site</th><th>Title</th><th>Company</th><th>Score</th>"
        f"<th>Status</th><th>Job</th><th>Resume</th></tr>{''.join(rows)}</table></body></html>",
        encoding="utf-8")


if __name__ == "__main__":
    main()
