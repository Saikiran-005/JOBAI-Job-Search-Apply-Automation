"""JOBAI shared settings + helpers. Edit the SETTINGS section only."""
import os
import re
import sys
import json
import time
import html
import webbrowser
from datetime import datetime
from urllib.parse import quote

try:
    from selenium import webdriver
    from selenium.webdriver.chrome.options import Options
    from selenium.webdriver.common.by import By
except ImportError:  # job_bot.py (test bot) works without selenium
    webdriver = Options = By = None

# ======================= SETTINGS =======================
BASE_DIR = os.path.dirname(os.path.abspath(__file__))
# Chrome profile lives OUTSIDE OneDrive so your login sessions are not synced to the cloud.
PROFILE_DIR = os.path.join(os.environ.get("LOCALAPPDATA", BASE_DIR), "JOBAI", "chrome_profile")
LOG_FILE = os.path.join(BASE_DIR, "universal_log.txt")
APPLIED_FILE = os.path.join(BASE_DIR, "applied.json")
RESUME_TXT = os.path.join(BASE_DIR, "resume.txt")
RESUME_PATH = RESUME_TXT  # change to your PDF path if a site asks for an upload

MIN_SCORE = 70            # % needed to count as a match
MAX_YEARS = 1             # skip jobs asking for more than this many years
AUTO_SUBMIT = False       # False = find + open + report only. True = actually click Apply.
MAX_APPLIES_PER_RUN = 10  # safety cap per run
HEADLESS = False          # set True for background daily runs (Chrome hidden)

YOUR_SKILLS = [
    "sql", "pl/sql", "joins", "cte", "window function", "power bi", "dax",
    "power query", "tableau", "python", "pandas", "numpy", "excel",
    "kpi", "dashboard", "data cleaning", "eda", "machine learning",
]
TARGET_ROLES = ["Data Analyst", "BI Analyst", "Power BI Developer",
                "SQL Developer", "Junior Data Scientist"]
LOCATIONS = ["Hyderabad", "Bangalore", "Remote"]

# A job title must contain one of these to count as a target role
ROLE_KEYWORDS = ["data analyst", "bi analyst", "business intelligence", "power bi",
                 "sql developer", "data scientist", "reporting analyst", "analytics"]
# Titles containing these are skipped
EXCLUDE_WORDS = ["senior", "sr", "lead", "manager", "architect", "principal",
                 "java", "php", "sap", "devops", "tester", "qa", "dotnet", ".net"]
# ========================================================


def log(msg):
    line = f"[{datetime.now().strftime('%Y-%m-%d %H:%M:%S')}] {msg}"
    print(line)
    try:
        with open(LOG_FILE, "a", encoding="utf-8") as f:
            f.write(line + "\n")
    except Exception:
        pass


def _has_word(text, word):
    return re.search(r"(?<!\w)" + re.escape(word) + r"(?!\w)", text) is not None


def _years_required(text):
    """Highest 'years of experience' number found in the text (0 if none)."""
    nums = []
    for a, b in re.findall(r"(?<!\d)(\d{1,2})\s*(?:[-–]|to)?\s*(\d{1,2})?\s*\+?\s*(?:yrs?|years?)\b", text):
        nums.append(int(a))
        if b:
            nums.append(int(b))
    return max(nums) if nums else 0


def check_match(job_title, job_desc=""):
    """Return a 0-100 score. 70 = target-role title; up to +30 from your skills in the text."""
    title = job_title.lower()
    text = (job_title + " " + job_desc).lower()
    if any(_has_word(title, w) for w in EXCLUDE_WORDS):
        return 0
    if _years_required(text) > MAX_YEARS:
        return 0
    hits = sum(1 for s in YOUR_SKILLS if s in text)
    if any(k in title for k in ROLE_KEYWORDS):
        return min(70 + hits * 6, 100)
    return min(hits * 6, 60)  # not a target role -> can never reach 70


def notify(title, message):
    try:
        from plyer import notification
        notification.notify(title=title, message=message, timeout=8)
    except Exception as e:
        print(f"(notification skipped: {e})")


def write_report(path, rows, heading, open_browser=True):
    resume = ""
    if os.path.exists(RESUME_TXT):
        resume = open(RESUME_TXT, encoding="utf-8").read()
    out = [f"<html><head><meta charset='utf-8'><title>{html.escape(heading)}</title>",
           "<style>body{font-family:Arial;margin:24px}.job{border:1px solid #ccc;border-radius:8px;"
           "padding:12px;margin:10px 0}.ok{color:green}.no{color:#b00}pre{background:#f4f4f4;padding:8px;"
           "white-space:pre-wrap}</style></head><body>",
           f"<h2>{html.escape(heading)}</h2><p>Generated {datetime.now():%Y-%m-%d %H:%M}</p>"]
    for r in rows:
        ok = r.get("score", 0) >= MIN_SCORE
        mark = "✅ MATCH" if ok else "❌ SKIP"
        link = r.get("link", "")
        out.append(f"<div class='job'><b>{html.escape(r['title'])}</b> - {html.escape(r.get('company', ''))}"
                   f"<br><span class='{'ok' if ok else 'no'}'>{mark} ({r.get('score', 0)}%)</span>"
                   f" &nbsp; Status: {html.escape(r.get('status', ''))}")
        if link:
            out.append(f"<br><a href='{html.escape(link)}' target='_blank'>Open job</a>")
        if ok:
            prompt = (f"Write a short, honest cover letter (under 150 words) for the role "
                      f"'{r['title']}' at {r.get('company', 'the company')}. "
                      f"Use only these facts about me:\n{resume}")
            out.append(f"<p><b>Cover letter prompt (paste into Claude):</b></p><pre>{html.escape(prompt)}</pre>")
        out.append("</div>")
    out.append("</body></html>")
    with open(path, "w", encoding="utf-8") as f:
        f.write("\n".join(out))
    log(f"Report saved: {path}")
    if open_browser:
        webbrowser.open("file:///" + path.replace("\\", "/"))


# ---------- applied-jobs memory (never apply twice) ----------
def load_applied():
    try:
        with open(APPLIED_FILE, encoding="utf-8") as f:
            return json.load(f)
    except Exception:
        return {}


def save_applied(db):
    with open(APPLIED_FILE, "w", encoding="utf-8") as f:
        json.dump(db, f, indent=2)


# ---------- browser ----------
def get_driver(headless=None):
    if webdriver is None:
        raise RuntimeError("selenium not installed. Run: pip install selenium plyer")
    if headless is None:
        headless = HEADLESS
    os.makedirs(PROFILE_DIR, exist_ok=True)
    opts = Options()
    opts.add_argument("--start-maximized")
    opts.add_argument(f"--user-data-dir={PROFILE_DIR}")
    opts.add_experimental_option("detach", True)
    if headless:
        opts.add_argument("--headless=new")  # background mode (no visible window)
    print("Starting Chrome...")
    return webdriver.Chrome(options=opts)  # Selenium 4.6+ downloads the driver itself


LOGIN_URLS = ["https://www.naukri.com/nlogin/login",
              "https://www.linkedin.com/login",
              "https://secure.indeed.com/auth"]


def login_once():
    """Open the login pages. You log in by hand; the session is saved in the Chrome profile."""
    driver = get_driver(headless=False)
    driver.get(LOGIN_URLS[0])
    for url in LOGIN_URLS[1:]:
        driver.switch_to.new_window("tab")
        driver.get(url)
    input("Log in to each tab (Naukri, LinkedIn, Indeed), then press Enter here... ")
    driver.quit()
    print("Sessions saved. You will not need to log in again until they expire.")


def scroll_page(driver, times=3):
    for _ in range(times):
        driver.execute_script("window.scrollTo(0, document.body.scrollHeight);")
        time.sleep(1.5)


def get_desc(driver):
    for sel in ["div[class*='JDC']", "section[class*='job-desc']", "div.job-desc", "#job_description"]:
        els = driver.find_elements(By.CSS_SELECTOR, sel)
        if els and els[0].text.strip():
            return els[0].text[:5000]
    try:  # fallback: top of the page (includes the experience chip)
        return driver.find_element(By.TAG_NAME, "body").text[:1500]
    except Exception:
        return ""


# ---------- Naukri (shared by real_bot, auto_apply_final, any_platform_auto) ----------
def naukri_url(role, location):
    city = {"bangalore": "bengaluru"}.get(location.lower(), location.lower())
    return f"https://www.naukri.com/{role.lower().replace(' ', '-')}-jobs-in-{city}"


def naukri_logged_in(driver):
    driver.get("https://www.naukri.com")
    time.sleep(3)
    return len(driver.find_elements(By.LINK_TEXT, "Login")) == 0


def naukri_apply(driver):
    """Returns: applied / ready (AUTO_SUBMIT off) / external (company site) / not_found."""
    if driver.find_elements(By.ID, "company-site-button"):
        return "external"
    btns = driver.find_elements(By.ID, "apply-button")
    if not btns:
        return "not_found"
    if not AUTO_SUBMIT:
        return "ready"
    btns[0].click()
    time.sleep(3)
    return "applied"


def process_naukri(driver, roles, locations, per_role, rows, label="Naukri", scroll=False):
    applied_db = load_applied()
    main_tab = driver.current_window_handle
    handled = 0
    for loc in locations:
        if loc.lower() == "remote":
            continue  # Naukri city URLs only; remote is covered on LinkedIn/Indeed
        for role in roles:
            if handled >= MAX_APPLIES_PER_RUN:
                log(f"[{label}] Daily cap reached ({MAX_APPLIES_PER_RUN}).")
                return handled
            log(f"[{label}] Searching: {role} in {loc}")
            try:
                driver.get(naukri_url(role, loc))
                time.sleep(4)
                if scroll:
                    scroll_page(driver)
                jobs = [(a.text.strip(), a.get_attribute("href"))
                        for a in driver.find_elements(By.CSS_SELECTOR, "a.title")[:per_role]]
            except Exception as e:
                log(f"[{label}] Search failed for {role}: {e}")
                continue
            log(f"[{label}] Found {len(jobs)} cards")
            for title, href in jobs:
                if not title or not href or href in applied_db:
                    continue
                score = check_match(title)
                if score < MIN_SCORE:
                    log(f"[{label}] ❌ SKIP ({score}%) {title}")
                    rows.append({"title": title, "score": score, "link": href, "status": "skipped"})
                    continue
                try:
                    driver.switch_to.new_window("tab")
                    driver.get(href)
                    time.sleep(3)
                    score = check_match(title, get_desc(driver))  # re-check with the full description
                    if score < MIN_SCORE:
                        log(f"[{label}] ❌ SKIP after reading JD ({score}%) {title}")
                        rows.append({"title": title, "score": score, "link": href, "status": "skipped (JD)"})
                        continue
                    status = naukri_apply(driver)
                    log(f"[{label}] ✅ MATCH ({score}%) {title} -> {status}")
                    rows.append({"title": title, "score": score, "link": href, "status": status})
                    if status == "applied":
                        applied_db[href] = datetime.now().isoformat()
                        save_applied(applied_db)
                        notify("JOBAI applied", title)
                    handled += 1
                except Exception as e:
                    log(f"[{label}] Error on '{title}': {e}")
                finally:
                    try:
                        if driver.current_window_handle != main_tab:
                            driver.close()
                        driver.switch_to.window(main_tab)
                    except Exception:
                        pass
    return handled
