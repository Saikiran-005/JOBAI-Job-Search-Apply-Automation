# WHATSAPP AI RAG BOT V3 - Saikiran - Fixes "just clicking and leaving"
# Needs: pip install openai PyPDF2 selenium langchain

import time, re, os, json
from selenium import webdriver
from selenium.webdriver.chrome.options import Options
from selenium.webdriver.common.by import By
from PyPDF2 import PdfReader

# ============ CONFIG ============
RESUME_PDF = r"C:\Users\V. Saikiran Achari\OneDrive\Desktop\JOBAI\resume.pdf" # Put your resume PDF here
OPENAI_API_KEY = "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" # Get from platform.openai.com - 100% needed for RAG
SELECTED_GROUPS = ["My Job Finder (IT & HR)", "Grow With gargee"]

# ============ RAG - LOAD YOUR RESUME ============
def load_resume_rag():
    try:
        reader = PdfReader(RESUME_PDF)
        text = "".join([p.extract_text() for p in reader.pages])
        print(f"✅ RAG Loaded Resume: {len(text)} chars")
        return text
    except:
        # Fallback if PDF not found
        return """
        Name: V. Saikiran Achari
        Skills: QA Manual, Automation, Selenium, SQL, Power BI, Data Analyst, Data Science, Python, Testing, Data Verification
        Experience: QA Engineer, Data Analyst Projects, Power BI Dashboards
        Education: B.Tech
        Location: Hyderabad, Chanda Nagar
        Email: vsaikiranachari@gmail.com
        Target Companies: TCS, Accenture, Wipro, Infosys, Cognizant
        """

RESUME_TEXT = load_resume_rag()

# ============ AI RAG - GENERATE ANSWERS ============
def ai_rag_fill(job_description):
    """This is the RAG brain - uses your resume + JD to generate fill data"""
    try:
        from openai import OpenAI
        client = OpenAI(api_key=OPENAI_API_KEY)

        prompt = f"""
        You are Job AI for Saikiran. Resume: {RESUME_TEXT[:3000]}

        Job Description: {job_description[:3000]}

        Task: Generate JSON to fill job application form.
        Return ONLY valid JSON like:
        {{
          "is_match": true/false,
          "match_score": 0-100,
          "cover_letter": "Tailored 3 line cover letter for TCS/Wipro/Accenture if mentioned",
          "answers": {{
            "experience_years": "1-2 years / Fresher with projects",
            "current_ctc": "Fresher / As per company",
            "expected_ctc": "4-6 LPA",
            "notice_period": "Immediate",
            "location": "Hyderabad, Open to Bangalore",
            "why_fit": "2 line why fit",
            "skills_match": "QA, Selenium, SQL, Power BI"
          }}
        }}
        If not QA/Data Analyst/TCS/Wipro/Accenture type, set is_match=false.
        """

        resp = client.chat.completions.create(
            model="gpt-4o-mini",
            messages=[{"role":"user","content":prompt}],
            temperature=0.3
        )
        result = resp.choices[0].message.content
        # Extract JSON
        json_match = re.search(r'\{.*\}', result, re.DOTALL)
        if json_match:
            data = json.loads(json_match.group())
            print(f"🤖 AI RAG: Match={data.get('is_match')} Score={data.get('match_score')}")
            return data
    except Exception as e:
        print(f"AI RAG fallback (no key): {e}")
        # Fallback logic without AI - simple keyword RAG
        jd_lower = job_description.lower()
        score = sum(1 for s in ["qa","test","data analyst","power bi","selenium","tcs","wipro","accenture"] if s in jd_lower)
        return {
            "is_match": score >= 1,
            "match_score": score*20,
            "cover_letter": f"Dear Hiring Team, I am Saikiran with skills in QA, Selenium, Power BI, Data Analyst. I am interested in this role at your company and can join immediately in Hyderabad.",
            "answers": {
                "experience_years": "Fresher with QA & Data Analyst projects",
                "expected_ctc": "4-6 LPA",
                "notice_period": "Immediate Joiner",
                "location": "Hyderabad"
            }
        }
    return None

# ============ SCRAPE JOB DESCRIPTION ============
def scrape_job_description(driver):
    try:
        # Try common JD selectors
        for sel in ["div.job-desc", "div.description", "div.jd", "div#jobDescriptionText", "div.jobs-description__content", "div.jobs-box__html-content"]:
            try:
                elem = driver.find_element(By.CSS_SELECTOR, sel)
                if len(elem.text) > 100:
                    return elem.text
            except:
                continue
        return driver.find_element(By.TAG_NAME, "body").text[:4000]
    except:
        return "QA / Data Analyst Job"

# ============ AI FORM FILLER ============
def ai_fill_form(driver, rag_data):
    """Finds all inputs and fills using RAG answers"""
    try:
        answers = rag_data.get("answers", {})
        # Map labels to values
        fill_map = {
            "experience": answers.get("experience_years", "1 Year"),
            "ctc": answers.get("expected_ctc", "4.5 LPA"),
            "expected": answers.get("expected_ctc", "5 LPA"),
            "notice": answers.get("notice_period", "Immediate"),
            "location": answers.get("location", "Hyderabad"),
            "current location": "Hyderabad",
            "cover letter": rag_data.get("cover_letter", ""),
            "why": rag_data.get("answers", {}).get("why_fit", ""),
            "skill": answers.get("skills_match", "QA, Selenium, SQL, Power BI")
        }

        inputs = driver.find_elements(By.XPATH, "//input[@type='text' or @type='number' or not(@type)] | //textarea")
        print(f"🤖 Found {len(inputs)} form fields to fill")
        for inp in inputs:
            try:
                label = (inp.get_attribute("placeholder") or inp.get_attribute("name") or inp.get_attribute("id") or "").lower()
                for key, val in fill_map.items():
                    if key in label or label in key:
                        driver.execute_script("arguments[0].scrollIntoView();", inp)
                        time.sleep(0.3)
                        inp.clear()
                        inp.send_keys(val)
                        print(f"Filled {label} -> {val[:30]}")
                        break
            except:
                continue

        # Select dropdowns
        try:
            # Example: Notice period immediate
            for dd in driver.find_elements(By.XPATH, "//select"):
                try:
                    opts = dd.find_elements(By.TAG_NAME, "option")
                    for o in opts:
                        if "immediate" in o.text.lower() or "0" in o.text:
                            o.click()
                            break
                except:
                    pass
        except:
            pass

        return True
    except Exception as e:
        print(f"Fill error: {e}")
        return False

# ============ MAIN WHATSAPP MONITOR WITH RAG ============
PROFILE_DIR = r"C:\Temp\chrome_job_profile"
opts = Options()
opts.add_argument(f"--user-data-dir={PROFILE_DIR}")
opts.add_argument("--profile-directory=Default")
opts.add_experimental_option("detach", True)

driver = webdriver.Chrome(options=opts)
driver.get("https://web.whatsapp.com")
print(">>> WhatsApp loading... 25 sec")
time.sleep(25)

seen = set()
print(f">>> RAG BOT READY for {SELECTED_GROUPS}")

while True:
    for group_name in SELECTED_GROUPS:
        try:
            grp = driver.find_element(By.XPATH, f'//span[@title="{group_name}"]')
            driver.execute_script("arguments[0].click();", grp)
            time.sleep(4)

            msgs = driver.find_elements(By.XPATH, '//div[contains(@class,"copyable-text")]//span[contains(@class,"selectable-text")]')[-10:]
            for m in msgs:
                txt = m.text.strip()
                if not txt or txt in seen: continue
                seen.add(txt)

                links = re.findall(r'https?://[^\s]+', txt)
                if not links: continue

                for link in links:
                    if not any(x in link.lower() for x in ["naukri","linkedin","indeed","careers","tcs","wipro","accenture"]):
                        continue

                    print(f"\n🔗 New Job Link in {group_name}: {link}")
                    driver.execute_script(f"window.open('{link}', '_blank');")
                    driver.switch_to.window(driver.window_handles[-1])
                    time.sleep(8)

                    jd = scrape_job_description(driver)
                    print(f"📄 JD: {jd[:150]}...")

                    rag_data = ai_rag_fill(jd)
                    if not rag_data or not rag_data.get("is_match"):
                        print("❌ RAG says NOT matching your resume - skipping")
                        driver.close()
                        driver.switch_to.window(driver.window_handles[0])
                        continue

                    print(f"✅ RAG MATCH {rag_data['match_score']}% - Auto Filling form...")
                    # Click Apply first
                    try:
                        btn = driver.find_element(By.XPATH, "//button[contains(text(),'Apply') or contains(text(),'Easy Apply')]")
                        btn.click()
                        time.sleep(4)
                    except:
                        pass

                    # NOW AI FILLS
                    ai_fill_form(driver, rag_data)

                    # Don't auto-submit yet, show you
                    print(f"📝 Filled Cover: {rag_data.get('cover_letter')[:100]}")
                    print(">>> Check form, then manual Submit OR bot will Submit in 10 sec")
                    time.sleep(10)

                    # Auto Submit
                    try:
                        sub = driver.find_element(By.XPATH, "//button[contains(text(),'Submit') or contains(@aria-label,'Submit')]")
                        sub.click()
                        print("✅✅ RAG APPLIED SUCCESSFULLY")
                    except:
                        print("⚠️ Fill done, please Submit manually")

                    time.sleep(3)
                    driver.close()
                    driver.switch_to.window(driver.window_handles[0])

        except Exception as e:
            print(f"Group error {group_name}: {e}")
            time.sleep(2)

    time.sleep(20)