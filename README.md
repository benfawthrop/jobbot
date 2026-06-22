# LinkedIn Job Bot

A semi-autonomous bot that scrapes LinkedIn for relevant job postings and fills out applications on your behalf. It pauses for human help when it hits CAPTCHAs, unknown fields, or anything it can't handle automatically.

---

## Project Structure

```
job_bot/
├── main.py              ← Entry point; CLI flags; orchestrates everything
├── scraper.py           ← Playwright-based LinkedIn scraper
├── filler.py            ← Form-filling agent (profile lookup + Claude AI)
├── notifier.py          ← Email (SMTP) notifications
├── state_manager.py     ← Tracks applied jobs; prevents re-application
├── test_filler.py       ← Debug script: run the filler on a single hardcoded job
├── profile.json         ← YOUR job application data (name, email, short answers, etc.)
├── config.json          ← Search settings, notification config (git-ignored)
├── config.example.json  ← Template to copy → config.json
├── resume.pdf           ← Your resume for file upload fields (git-ignored)
├── requirements.txt     ← Python dependencies
├── state/               ← Persistent bot state (applied job records, browser session)
├── logs/                ← Run logs and scraped job JSON files
└── screenshots/         ← Debug screenshots (created automatically in --debug mode)
```

---

## How Each Part Works

### `main.py` — The Orchestrator

This is the only file you run directly (for full runs). It:
1. Parses CLI flags (covered below).
2. Loads `config.json` and `profile.json`.
3. Runs Phase 1: calls `scraper.py` to find jobs.
4. Prints the found jobs.
5. If not `--scrape-only`, runs Phase 2: loops through jobs and calls `filler.py` to apply.
6. Sends a summary email notification at the end (if SMTP is configured).

**Why it's structured this way:** Separating the scrape phase from the apply phase means you can safely test each half independently. The orchestrator is intentionally simple — all the hard logic lives in `scraper.py` and `filler.py`.

---

### `scraper.py` — The LinkedIn Scraper

Uses **Playwright** (a browser automation library) to control a real Chromium browser. It does not use the LinkedIn API (which is heavily restricted and requires approval).

**How it scrapes:**
1. Builds a LinkedIn Jobs search URL with your keywords, location, and filters (experience level, date posted, remote/hybrid, etc.).
2. Opens the page and scrolls down to lazy-load more job cards.
3. Collects all job card elements: title, company, and the job URL.
4. For each card, navigates to the individual job page to get: full description, actual location, and the apply button URL.
5. Runs each job through `_passes_filters()` which checks your blocklists and required keyword rules.
6. Returns a clean list of job dicts.

**Session management (important):**
The first time you run the bot, it will open a **visible browser window** and ask you to log into LinkedIn manually. After you log in and press ENTER, it saves your session (cookies) to `state/browser_profile/`. Every run after that reuses this session — no login needed. If LinkedIn logs you out, delete `state/browser_profile/` and re-run to trigger the manual login again.

**Anti-detection measures:**
- Uses a persistent browser profile (looks like a real returning user).
- Randomizes delays between actions (1.5–3.5 seconds by default).
- Spoofs the User-Agent string to look like a regular Mac Chrome browser.
- Does NOT use LinkedIn's API or any scraping framework that LinkedIn actively blocks.

**Blocklists (configured in `config.json`):**

- `blocked_companies` — substring-matched against the company name. Includes 25+ defense contractors by default (Raytheon, Lockheed Martin, Northrop Grumman, BAE Systems, General Dynamics, Leidos, SAIC, Booz Allen, etc.). Matching is case-insensitive and partial — "raytheon" blocks "Raytheon Technologies".
- `description_blocklist` — if any of these phrases appear in the job description, the job is skipped. Defaults include experience gates ("3+ years", "5+ years"), security clearance terms ("secret clearance", "ts/sci", "dod clearance"), and contractor roles ("defense contractor", "government contractor").

> **Note:** LinkedIn actively fights scraping. The selectors in `_collect_job_cards()` and `_fetch_job_detail()` may break when LinkedIn updates their HTML. This is the part of the codebase most likely to need maintenance. If jobs stop being found, check and update the CSS selectors.

---

### `filler.py` — The Form Filler

This is the most complex file. It handles filling out application forms on any website.

**Three-tier answer strategy:**

**Tier 1 — Local profile lookup (free, instant):**
`FIELD_MAP` is a dictionary of regex patterns → functions that extract answers from `profile.json`. For example: any field with "email" in its label → `profile["personal"]["email"]`. This covers ~80% of fields with zero AI calls. You can extend `FIELD_MAP` to cover more fields without touching the AI logic at all.

**Tier 2 — AI cache (free, instant on repeat fields):**
Before calling Claude, the bot checks `profile["ai_cache"]` for a previously saved answer to the same field label (normalized to lowercase). If found, it uses the cached answer immediately. The cache is stored directly in `profile.json` and persists across runs. This means the second time you encounter "Why are you interested in this role?", Claude is never called.

**Tier 3 — Claude CLI (for new open-ended fields):**
If neither the profile map nor the cache has an answer, the bot calls `claude -p` as a subprocess and streams the response live to your terminal. The full `profile.json` (minus the cache) is passed as context so Claude can give accurate, personalized answers. No separate API key is needed — it uses your existing Claude Pro subscription. After each new Claude response, the answer is saved to the cache for future use.

When `--no-ai` is set, Tier 3 falls back to prompting you directly in the terminal instead of calling Claude.

**Field detection:**
The bot scans for `<input>`, `<textarea>`, and `<select>` elements. For each one, it tries to find a human-readable label via: `aria-label` → `<label for="...">` → `aria-labelledby` → ancestor label → Workday `formLabel` → `placeholder` → `name` attribute.

**Resume upload:**
Detects `<input type="file">` elements. If the field label contains "resume" or "cv", or if the field accepts PDFs, it uploads the file at `config.resume_path`.

**Autofill modal handling:**
Many ATS platforms (especially Workday) show a "How do you want to fill this?" prompt after clicking Apply, with options like "Autofill from Resume" and "Fill Manually". The bot detects this modal by checking for the autofill button first (to avoid false positives), then automatically clicks the manual option. This happens both when first clicking Apply and at the start of each subsequent form page.

**Login / account creation handling:**
When the bot encounters a login wall or "Create Account" prompt, it attempts to authenticate automatically:
- Fills email from `profile.json`
- Handles Workday's two-step flow (email → Next → password)
- Detects account creation forms (two password fields) and fills name, email, password, and T&C checkboxes
- Falls back to asking for human help only if the login form is still visible after submitting (wrong password, 2FA, etc.)

The shared password used for all site accounts is hardcoded in `filler.py` (`_handle_login_wall`). Update it there if needed.

**LinkedIn Easy Apply:**
Easy Apply is a modal dialog on LinkedIn itself. The bot clicks "Easy Apply", then loops through the modal's steps: filling fields on each step, clicking "Next", and finally clicking "Submit". Max 15 steps (configurable) before it gives up.

**External applications:**
If a job links to an external company ATS (Greenhouse, Lever, Workday, etc.), the bot navigates to that URL directly and applies the same field-filling logic. These vary wildly — the bot handles simple forms well and may need human help on complex multi-page flows.

**Ctrl+C interrupt (human takeover):**
At any point during a run, press **Ctrl+C** to interrupt the bot. Rather than crashing, it:
1. Finishes filling the current field.
2. Pauses and prints a "BOT PAUSED" message in the terminal.
3. Leaves the browser open so you can fix any issues manually.
4. Resumes when you press ENTER.

Pressing **Ctrl+C a second time while paused** exits the bot entirely.

**Human help requests:**
When the bot gets stuck (CAPTCHA, no submit button found, disabled Next button after 5 attempts), it automatically pauses and waits for you to fix the issue and press ENTER. A native desktop notification is also sent.

---

### `test_filler.py` — Single-Job Debug Script

Use this to test the filler on one specific job without running the scraper first. Edit the `JOB` dict at the top of the file to point to the application URL you want to test, then run:

```bash
python test_filler.py
python test_filler.py --dry-run
python test_filler.py --dry-run --debug
python test_filler.py --no-ai        # terminal prompts instead of Claude
```

This is the fastest way to debug form-filling issues on a specific ATS. It also checks `state/applied_jobs.json` and refuses to re-run on a job that's already been applied to (edit the `JOB` dict's `apply_url` to change the target).

---

### `notifier.py` — Email Notifications

Sends email via **SMTP** (Gmail recommended). Configure credentials in `config.json` under `notifications.smtp`. Uses a Gmail App Password — set one up at Google Account → Security → App Passwords.

Twilio SMS and SendGrid are present in the codebase but currently commented out. If you want to re-enable them, uncomment the relevant blocks in `notifier.py._dispatch()`.

---

### `state_manager.py` — State Persistence

Reads and writes `state/applied_jobs.json`. This file records every job you've applied to. Before each application, the bot checks this list and skips already-seen jobs.

**ATS URL deduplication:** The bot tracks the actual ATS apply URL (e.g. the Workday or Greenhouse URL), not just the LinkedIn job ID. Query parameters are stripped during comparison so tracking parameters don't cause missed deduplication. This means if the same job is posted twice on LinkedIn with different IDs, the bot will still recognize it as already applied if the underlying ATS URL is the same.

State is written immediately after each successful application, so even if the bot crashes mid-run, you won't lose track of completed applications.

---

## Setup

### Prerequisites

- Python 3.11 or higher
- **Claude CLI** installed and authenticated (`claude` must be available in your PATH). Requires an active Claude Pro subscription. Install: [claude.ai/code](https://claude.ai/code)
- (Optional) Gmail App Password for email notifications: Google Account → Security → App Passwords

### 1. Install

```bash
# Create a virtual environment using the standalone Python installer (NOT Microsoft Store Python,
# which uses symlinks that break on updates)
"C:\Users\<you>\AppData\Local\Programs\Python\Python313\python.exe" -m venv .venv
.venv\Scripts\activate          # Windows

# Install dependencies
pip install -r requirements.txt

# Install Playwright's browser binaries
playwright install chromium
```

### 2. Add your files

```bash
# Copy the example config
cp config.example.json config.json

# Put your resume in the project root
cp ~/Downloads/your_resume.pdf resume.pdf
```

`profile.json` should already be in the project root with your personal info.

### 3. Edit `config.json`

Open `config.json` and fill in:
- `resume_path` — path to your resume PDF (default: `"resume.pdf"`)
- `search.keywords` — what to search for (e.g. `"software engineer entry level"`)
- `search.location` — where to search (array of locations)
- `search.filters` — adjust experience level, remote/hybrid, blocklists, etc.
- `notifications.smtp` — Gmail credentials if you want email alerts (optional)

### 4. First run (manual LinkedIn login)

```bash
python main.py --scrape-only --limit 5
```

A browser window will open. Log into LinkedIn normally, then come back to the terminal and press ENTER. Your session will be saved for all future runs.

---

## CLI Flags Reference

| Flag | Description |
|---|---|
| `--scrape-only` | Only scrape; print found jobs to terminal and save to `logs/scraped_*.json`. No applications. |
| `--dry-run` | Fill out every form completely but never click Submit. Pauses so you can inspect the filled form. |
| `--review` | Fill and pause before each Submit. Asks "Submit? [y/N]" so you approve each one manually. |
| `--limit N` | Stop after N applications (or N scraped jobs in `--scrape-only` mode). Default: 5. Use `--limit 0` for unlimited. |
| `--no-ai` | Skip Claude entirely. Prompts you in the terminal for any field not covered by `FIELD_MAP` or the AI cache. |
| `--fill-only URL` | Skip the scraper and run the filler directly on this external apply URL. Useful for re-testing a specific application. |
| `--job-title "..."` | Job title label to use with `--fill-only` (default: `"Test Job"`). |
| `--job-company "..."` | Company label to use with `--fill-only` (default: `"Test Company"`). |
| `--no-notify` | Skip all email notifications for this run. |
| `--debug` | Enable verbose logging + take a screenshot at every major step. Saved to `screenshots/`. Also keeps the browser visible (non-headless). |
| `--resume-from FILE` | Load state from a specific JSON file instead of the default `state/applied_jobs.json`. |
| `--keywords "..."` | Override the search keywords from `config.json` for this run only. |
| `--location "..."` | Override the search location from `config.json` for this run only. |

---

## Usage Examples

```bash
# Test the scraper — see what jobs are being found
python main.py --scrape-only --limit 10

# Test the scraper in a specific city
python main.py --scrape-only --keywords "software engineer" --location "New York, NY"

# Full dry run — fill one application but don't submit. Great for first-time testing.
python main.py --dry-run --limit 1 --debug

# Review mode — apply to 3 jobs but you approve each submit
python main.py --review --limit 3

# Fully autonomous — apply to up to 10 jobs without stopping
python main.py --limit 10

# Test filler on a specific job URL directly (no scrape)
python main.py --fill-only "https://careers.example.com/apply/12345" --job-title "Software Engineer" --job-company "Example Corp" --dry-run

# Same thing but with the dedicated debug script (URL hardcoded to avoid shell quoting issues)
python test_filler.py --dry-run --debug

# Run without Claude (you answer every unknown field manually)
python main.py --dry-run --limit 1 --no-ai

# Check how many jobs you've applied to
python -c "import json; d=json.load(open('state/applied_jobs.json')); print(len(d.get('applied_jobs',[])), 'applications')"
```

---

## Workflow Recommendation (First Time)

1. **Run `--scrape-only --limit 20`** — Check `logs/scraped_*.json` to see if the jobs found are relevant. Adjust `config.json` keywords, blocklists, and filters if not.

2. **Run `--dry-run --limit 1 --debug`** — Watch the bot fill out one full application without submitting. Check the screenshots in `screenshots/` to make sure it's filling fields correctly.

3. **Run `--review --limit 3`** — Let it apply to 3 jobs but pause before each submit for you to approve.

4. **Run `--limit 10`** — Once you're confident it's working, let it go fully autonomous.

---

## Extending the Field Map

The most impactful way to improve fill accuracy (without AI calls) is to extend `FIELD_MAP` in `filler.py`. It's a dictionary of:

```python
r"regex pattern matching field label": lambda profile: "answer"
```

For example, to add a "years of experience" field:
```python
r"years.?of.?experience": lambda p: "0-2",
```

Every pattern you add is one fewer Claude call per application. The AI cache also helps with this — after the first time a field is answered by Claude, it's never called again for that label.

---

## AI Response Cache

After Claude answers a field it hasn't seen before, the answer is saved to `profile.json` under the `ai_cache` key:

```json
{
  "ai_cache": {
    "why are you interested in this role": "I am looking for an environment...",
    "select:highest level of education": "Bachelor's Degree"
  }
}
```

You can view, edit, or delete entries here directly. If Claude gave a bad answer for a field, delete its entry from `ai_cache` and the bot will ask Claude again next time. Select-field entries (prefixed with `select:`) are additionally validated against the current dropdown options before being used, so a cached value that doesn't match the current site's options is automatically ignored.

---

## Maintaining the Scraper Selectors

LinkedIn frequently updates their HTML structure. If the scraper stops finding jobs, the CSS selectors in `scraper.py` need updating. Here's how to debug:

1. Open LinkedIn Jobs in your browser.
2. Right-click a job card → Inspect Element.
3. Find the `<li>` element containing the job.
4. Note the class names and update `_collect_job_cards()` in `scraper.py`.

The selectors are in `_collect_job_cards()` and `_fetch_job_detail()`. Each one has multiple comma-separated fallbacks to handle LinkedIn's A/B testing.

---

## Troubleshooting

**"No new jobs found"**
- Your keywords may be too specific. Try broader terms.
- Your `description_blocklist` or `blocked_companies` may be filtering too aggressively.
- Run `--scrape-only --debug` and check the logs.

**"LinkedIn session expired"**
- Delete `state/browser_profile/` and re-run to trigger the manual login again.

**Bot keeps getting stuck on CAPTCHAs**
- This is common if you run the bot too frequently. LinkedIn rate-limits aggressive traffic.
- Wait a few hours and try again.

**Fields not being filled correctly**
- Add more patterns to `FIELD_MAP` in `filler.py`.
- Run with `--debug` to see exactly which fields are being detected and what labels they have.
- Check `logs/` for the full fill log.
- If the AI gave a bad answer for a specific field, delete it from `ai_cache` in `profile.json`.

**Bot gets stuck on a disabled "Next" button (Workday loops)**
- Press **Ctrl+C** to interrupt. The bot will pause, leave the browser open, and wait for you to fix the issue and press ENTER to continue.

**`claude` not found in PATH**
- Make sure the Claude CLI is installed and you're running inside the activated `.venv`.
- Run `claude --version` to verify. If missing, install from [claude.ai/code](https://claude.ai/code).

**Microsoft Store Python / venv symlink errors**
- Use the standalone Python installer from python.org, not the Microsoft Store version.
- The Store version uses symlinks that break whenever Python auto-updates.
- Recreate the venv: `"C:\Users\<you>\AppData\Local\Programs\Python\Python313\python.exe" -m venv .venv`
