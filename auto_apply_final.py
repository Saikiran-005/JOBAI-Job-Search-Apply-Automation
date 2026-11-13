"""Naukri bot using a SAVED login session (no password stored in any file).

First time only:   python auto_apply_final.py --login
Then run:          python auto_apply_final.py
To really click Apply, set AUTO_SUBMIT = True in jobai_common.py.
For a hidden background run, set HEADLESS = True in jobai_common.py.
"""
import os
import sys
import jobai_common as jc
from jobai_common import BASE_DIR, TARGET_ROLES, log, write_report, process_naukri, naukri_logged_in

YOUR_SKILLS = jc.YOUR_SKILLS
RESUME_PATH = jc.RESUME_PATH


def setup():
    return jc.get_driver()  # start-maximized + saved Chrome profile + detach (see jobai_common.py)


def main():
    if "--login" in sys.argv:
        jc.login_once()
        return
    print("Step 1: starting Chrome...")
    driver = setup()
    print("Step 2: checking Naukri login...")
    if not naukri_logged_in(driver):
        log("[auto_apply] Not logged in. Run: python auto_apply_final.py --login")
        driver.quit()
        return
    rows = []
    print("Step 3: searching roles (Hyderabad, 10 jobs each)...")
    total = process_naukri(driver, TARGET_ROLES, ["Hyderabad"], 10, rows, label="auto_apply", scroll=True)
    log(f"[auto_apply] TOTAL handled this run: {total}  (AUTO_SUBMIT={jc.AUTO_SUBMIT})")
    interactive = sys.stdin is not None
    write_report(os.path.join(BASE_DIR, "final_real_report.html"), rows, "Naukri auto report",
                 open_browser=interactive)


if __name__ == "__main__":
    main()
