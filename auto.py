"""Review-first job search using the shared JOBAI settings and helpers."""

from __future__ import annotations

import html
import sys
import time
from datetime import datetime
from pathlib import Path
from urllib.parse import urlencode, urlparse

import jobai_common as common
from selenium.webdriver.common.by import By


REPORT_PATH = Path(common.BASE_DIR) / "universal_report.html"
MAX_JOBS_PER_SEARCH = 8
PAGE_WAIT_SECONDS = 4

PLATFORMS = {
    "Naukri": ("naukri.com", "a.title"),
    "LinkedIn": ("linkedin.com", "a.job-card-list__title"),
    "Indeed": ("indeed.com", "h2.jobTitle a"),
}


def build_search_url(platform: str, role: str, location: str) -> str | None:
    if platform == "Naukri":
        if location.casefold() == "remote":
            return None
        return common.naukri_url(role, location)
    if platform == "LinkedIn":
        query = urlencode({"keywords": role, "location": location, "f_AL": "true"})
        return f"https://www.linkedin.com/jobs/search/?{query}"
    if platform == "Indeed":
        query = urlencode({"q": role, "l": location})
        return f"https://in.indeed.com/jobs?{query}"
    raise ValueError(f"Unsupported platform: {platform}")


def is_platform_job_url(platform: str, url: str) -> bool:
    host = urlparse(url).hostname or ""
    expected_host = PLATFORMS[platform][0]
    return host == expected_host or host.endswith(f".{expected_host}")


def collect_jobs(driver, platform: str, search_url: str) -> list[tuple[str, str]]:
    driver.get(search_url)
    time.sleep(PAGE_WAIT_SECONDS)

    found = []
    seen_urls = set()
    for element in driver.find_elements(By.CSS_SELECTOR, PLATFORMS[platform][1]):
        title = element.text.strip()
        url = element.get_attribute("href")
        if not title or not url or not is_platform_job_url(platform, url) or url in seen_urls:
            continue
        seen_urls.add(url)
        found.append((title, url))
        if len(found) >= MAX_JOBS_PER_SEARCH:
            break
    return found


def review_job(driver, platform: str, title: str, url: str) -> dict[str, object]:
    original_window = driver.current_window_handle
    driver.switch_to.new_window("tab")
    review_window = driver.current_window_handle
    try:
        driver.get(url)
        time.sleep(PAGE_WAIT_SECONDS)
        description = common.get_desc(driver)
        score = common.check_match(title, description)
        status = "below threshold"

        if score >= common.MIN_SCORE:
            common.log(
                f"[{platform}] Match ({score}%): {title}. "
                "Review the open page and submit manually if appropriate."
            )
            while True:
                answer = input("Did you submit this application? [y/n]: ").strip().casefold()
                if answer in {"y", "yes"}:
                    status = "submitted (user-confirmed)"
                    break
                if answer in {"n", "no"}:
                    status = "reviewed, not submitted"
                    break
                print("Please enter y or n.")
        else:
            common.log(f"[{platform}] Below threshold ({score}%): {title}")

        return {"title": title, "score": score, "link": url, "status": status}
    finally:
        if review_window in driver.window_handles:
            driver.switch_to.window(review_window)
            driver.close()
        if original_window in driver.window_handles:
            driver.switch_to.window(original_window)


def run_platform(driver, platform: str, applied_db: dict, remaining: int) -> tuple[list[dict], int]:
    rows = []
    seen_urls = set()
    reviewed = 0

    for location in common.LOCATIONS:
        for role in common.TARGET_ROLES:
            if reviewed >= remaining:
                return rows, reviewed

            search_url = build_search_url(platform, role, location)
            if search_url is None:
                common.log(f"[{platform}] Skipping unsupported search location: {location}")
                continue

            common.log(f"[{platform}] Searching {role} in {location}")
            try:
                jobs = collect_jobs(driver, platform, search_url)
            except Exception as error:
                common.log(f"[{platform}] Search failed: {error}")
                continue

            for title, url in jobs:
                if reviewed >= remaining:
                    return rows, reviewed
                if url in seen_urls or url in applied_db:
                    continue
                seen_urls.add(url)

                try:
                    result = review_job(driver, platform, title, url)
                except Exception as error:
                    common.log(f"[{platform}] Could not review '{title}': {error}")
                    result = {
                        "title": title,
                        "score": 0,
                        "link": url,
                        "status": "review failed",
                    }
                rows.append(result)
                reviewed += 1

                if result["status"] == "submitted (user-confirmed)":
                    applied_db[url] = datetime.now().isoformat()
                    common.save_applied(applied_db)

    return rows, reviewed


def write_report(rows: list[dict]) -> None:
    content = [
        "<!doctype html><html lang='en'><meta charset='utf-8'>",
        "<title>JOBAI job search report</title>",
        "<style>body{font:16px Arial,sans-serif;margin:2rem}table{border-collapse:collapse}",
        "th,td{border:1px solid #bbb;padding:.6rem;text-align:left}</style>",
        "<h1>JOBAI job search report</h1>",
        "<p>Only applications you confirmed are marked as submitted. No application is submitted by the script.</p>",
        "<table><thead><tr><th>Platform</th><th>Job</th><th>Match</th><th>Status</th></tr></thead><tbody>",
    ]
    for row in rows:
        link = html.escape(str(row["link"]), quote=True)
        title = html.escape(str(row["title"]))
        content.append(
            "<tr>"
            f"<td>{html.escape(str(row.get('platform', '')))}</td>"
            f"<td><a href='{link}'>{title}</a></td>"
            f"<td>{row['score']}%</td>"
            f"<td>{html.escape(str(row['status']))}</td>"
            "</tr>"
        )
    if not rows:
        content.append("<tr><td colspan='4'>No job listings were reviewed.</td></tr>")
    content.append("</tbody></table></html>")
    REPORT_PATH.write_text("\n".join(content), encoding="utf-8")
    common.log(f"Report saved: {REPORT_PATH}")


def main() -> None:
    if "--login" in sys.argv:
        common.login_once()
        return

    driver = common.get_driver(headless=False)
    applied_db = common.load_applied()
    rows = []
    reviewed = 0
    try:
        print(
            "Using job roles, locations, match rules, and saved Chrome profile from "
            "jobai_common.py."
        )
        print("No password is read or stored. Review each match and submit it manually.")
        if common.AUTO_SUBMIT:
            common.log(
                "AUTO_SUBMIT is enabled in shared settings, but auto.py will not click Apply."
            )

        for platform in PLATFORMS:
            remaining = common.MAX_APPLIES_PER_RUN - reviewed
            if remaining <= 0:
                common.log(f"Per-run review cap reached ({common.MAX_APPLIES_PER_RUN}).")
                break
            platform_rows, platform_reviewed = run_platform(
                driver, platform, applied_db, remaining
            )
            for row in platform_rows:
                row["platform"] = platform
            rows.extend(platform_rows)
            reviewed += platform_reviewed
    finally:
        driver.quit()

    write_report(rows)
    confirmed = sum(row["status"] == "submitted (user-confirmed)" for row in rows)
    common.log(f"Run finished: reviewed={reviewed}, user-confirmed submitted={confirmed}")
    print(f"Done. Report: {REPORT_PATH}")


if __name__ == "__main__":
    main()
