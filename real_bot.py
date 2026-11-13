"""Naukri bot with MANUAL login. Searches 3 roles in Hyderabad, scores the first 6 results each."""
import os
from jobai_common import BASE_DIR, get_driver, process_naukri, write_report, log

ROLES = ["Data Analyst", "Power BI Developer", "BI Analyst"]


def main():
    print("Step 1: opening Chrome...")
    driver = get_driver()
    driver.get("https://www.naukri.com")
    input("Step 2: log in to Naukri in the Chrome window, then press Enter here... ")
    rows = []
    print("Step 3: searching and scoring jobs...")
    handled = process_naukri(driver, ROLES, ["Hyderabad"], 6, rows, label="real_bot")
    log(f"[real_bot] Matches handled: {handled}")
    write_report(os.path.join(BASE_DIR, "final_real_report.html"), rows, "Naukri real bot report")
    print("Done. Chrome stays open so you can review/apply.")


if __name__ == "__main__":
    main()
