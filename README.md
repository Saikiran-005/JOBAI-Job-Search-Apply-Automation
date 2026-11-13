# JOBAI – job finder / auto-apply helper (Windows)

Copy all files into:
`C:\Users\V. Saikiran Achari\OneDrive\Desktop\JOBAI`

## 1. Install (once)
```
pip install -r requirements.txt
```
Needs Python 3 and Google Chrome. `plyer` is optional and only enables desktop notifications.

## 2. Test with sample jobs (no login)
```
cd "C:\Users\V. Saikiran Achari\OneDrive\Desktop\JOBAI"
python job_bot.py
```
It scores 5 sample jobs, shows a popup, and opens `report.html`.

## 3. Passwords: none stored in files
Your passwords are NOT put in any file. Log in by hand once and Chrome keeps the session:
```
python auto.py --login
```
Log in to Naukri, LinkedIn and Indeed in the tabs that open, then press Enter in the terminal.
The Chrome profile is saved in `%LOCALAPPDATA%\JOBAI\chrome_profile` (outside OneDrive on purpose,
so your sessions are not uploaded to the cloud). If a session expires, run `--login` again.

## 4. Run
| Command | What it does |
|---|---|
| `python real_bot.py` | Naukri only, you log in manually each time |
| `python auto_apply_final.py` | Naukri only, uses the saved login |
| `python any_platform_auto.py` | Legacy apply-all script; do not use for review-first applications |
| `python auto.py` | Shared profile/settings; opens matching Naukri, LinkedIn, and Indeed listings for manual review; never submits |

`auto.py` is interactive: review each open listing and submit it yourself if appropriate. Run it in a
terminal, not with `pythonw` or as an unattended task. Use `python auto.py --login` once to save
platform sessions in the shared Chrome profile.

## 5. Settings (in `jobai_common.py`)
- `AUTO_SUBMIT = False` (default): finds matches, opens them, writes the report. **Nothing is submitted.**
  This setting affects the shared Naukri helpers; `auto.py` never clicks Apply, even if it is enabled.
- `MIN_SCORE = 70`, `MAX_YEARS = 1` (jobs asking for more than 1 year are skipped),
  `MAX_APPLIES_PER_RUN = 10`.
- `HEADLESS = True` hides Chrome; do not use it with the interactive `auto.py` review flow.
- `applied.json` remembers jobs already handled, so nothing is applied to twice.

## 6. Scheduling
`auto.py` waits for your review and confirmation, so it is not suitable for an unattended
`pythonw`/Task Scheduler run. Launch it interactively when you are available to review listings.

## 7. Check what happened
Open `universal_log.txt` (same folder) or `universal_report.html`:
```
notepad universal_log.txt
```

## Notes
- Naukri, LinkedIn and Indeed change their page layout often. If a step stops working (0 cards found),
  the selector in the code needs updating. The selectors have not been tested against the live sites.
- `any_platform_auto.py` is a legacy apply-all script and does not use the review-first flow. Do not use it
  for applications requiring confirmation.
- If Indeed shows a "verify you are human" page, the bot stops for that run. Do not try to bypass it.
