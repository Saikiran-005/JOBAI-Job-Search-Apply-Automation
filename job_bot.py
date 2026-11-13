"""Test bot: scores sample jobs against your skills. No browser login needed."""
import os
from jobai_common import BASE_DIR, MIN_SCORE, check_match, notify, write_report

SAMPLE_JOBS = [
    {"company": "TCS", "title": "Data Analyst",
     "desc": "SQL, Power BI, Excel, Python Pandas. Freshers 2026 batch welcome."},
    {"company": "Deloitte", "title": "Power BI Developer",
     "desc": "Power BI, DAX, Power Query, SQL, KPI dashboards. 0-1 years."},
    {"company": "Infosys", "title": "BI Analyst",
     "desc": "Tableau, SQL, Excel, data cleaning, KPI dashboards. Freshers."},
    {"company": "Wipro", "title": "Java Developer",
     "desc": "Java, Spring Boot, microservices. 0-1 years."},
    {"company": "Tech Mahindra", "title": "Junior Data Scientist",
     "desc": "Python, machine learning, Pandas, NumPy, SQL, EDA. Freshers can apply."},
]


def main():
    print("Step 1: scoring sample jobs...")
    rows = []
    for job in SAMPLE_JOBS:
        score = check_match(job["title"], job["desc"])
        mark = "✅ MATCH" if score >= MIN_SCORE else "❌ SKIP"
        print(f"{mark} ({score}%)  {job['company']} - {job['title']}")
        rows.append({"title": job["title"], "company": job["company"], "score": score,
                     "status": "sample job"})
    matched = [r for r in rows if r["score"] >= MIN_SCORE]
    print(f"Step 2: {len(matched)} matches. Sending desktop notification...")
    notify("JOBAI test", f"{len(matched)} of {len(rows)} sample jobs matched")
    print("Step 3: creating report.html and opening it...")
    write_report(os.path.join(BASE_DIR, "report.html"), rows, "JOBAI test report")


if __name__ == "__main__":
    main()
