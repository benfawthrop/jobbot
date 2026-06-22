"""
filler.py — Application Form Filler
=====================================
Uses Playwright to navigate to each job's apply page, then uses
Google Gemini Flash (free tier) to intelligently fill in form fields.

HOW IT WORKS:
  1. Opens the application URL in a browser.
  2. Detects form fields by scanning the DOM for inputs, textareas, selects.
  3. For each field:
     a. Tries to fill it from profile.json via exact/fuzzy key matching (FREE — no API call).
     b. If the answer is not obvious, calls Gemini Flash to generate a response.
     c. Types the answer into the field with human-like keystroke delay.
  4. Handles file upload fields (resume) by uploading the configured PDF path.
  5. On encountering CAPTCHAs or unrecognized multi-step flows, sends a system
     notification and waits for you to intervene manually.
  6. Pauses before submit if --dry-run or --review is set.

TOKEN OPTIMIZATION:
  - profile.json lookups happen locally first; Gemini is only called for
    short-answer fields where the answer can't be inferred from your profile.
  - We pass only the relevant portion of profile.json + a short prompt to Gemini
    (no full conversation history), keeping each call to ~500–800 tokens.
  - Gemini Flash 1.5 has a 1M token free-tier context window and generous
    free RPM; for most applications you won't spend a cent.
"""

import asyncio
import json
import logging
import os
import platform
import random
import re
import signal
import subprocess
from pathlib import Path
from typing import Optional

from playwright.async_api import async_playwright, Page, ElementHandle

logger = logging.getLogger(__name__)


# ── Claude CLI helper ─────────────────────────────────────────────────────────

class ClaudeCLI:
    """Calls `claude -p` to answer form fields using the user's existing subscription."""

    async def complete(self, prompt: str, field_label: str = "") -> str:
        try:
            proc = await asyncio.create_subprocess_exec(
                "claude", "-p", prompt,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
            )
            header = f"Claude answering: '{field_label}'" if field_label else "Claude:"
            print(f"\n  ┌─ {header}")
            print(  "  │ ", end="", flush=True)
            chunks = []
            col = 0
            async for chunk in proc.stdout:
                text = chunk.decode("utf-8", errors="replace")
                for ch in text:
                    if ch == "\n":
                        print(f"\n  │ ", end="", flush=True)
                        col = 0
                    else:
                        print(ch, end="", flush=True)
                        col += 1
                chunks.append(text)
            await proc.wait()
            print(f"\n  └{'─' * 40}")
            if proc.returncode != 0:
                err = (await proc.stderr.read()).decode("utf-8", errors="replace")
                logger.warning(f"claude -p failed (exit {proc.returncode}): {err[:120]}")
                return ""
            return "".join(chunks).strip()
        except FileNotFoundError:
            logger.warning("'claude' not found in PATH — falling back to human prompt")
            return ""
        except Exception as e:
            logger.warning(f"claude -p error: {e} — falling back to human prompt")
            return ""


# ── Profile field matcher (no API calls) ─────────────────────────────────────

# Mapping of common field label patterns → profile.json keys
# Extend this list to cover more fields without spending tokens.
FIELD_MAP = {
    # Personal info
    r"first.?name":         lambda p: p["personal"]["name"].split()[0],
    r"middle.?name":        lambda p: p["personal"].get("middle_name", ""),
    r"last.?name":          lambda p: p["personal"]["name"].split()[-1],
    r"full.?name|your name": lambda p: p["personal"]["name"],
    r"email":               lambda p: p["personal"]["email"],
    # Phone extension / ext must come before generic phone to win (leave blank)
    r"phone.*ext|ext(?:ension)?": lambda p: "",
    # Phone device type must come before generic "phone|mobile" to win
    r"phone.*device|device.*type": lambda p: "Mobile",
    r"phone|mobile":        lambda p: p["personal"]["phone"],
    # Address — components are separate so each field gets the right piece
    r"address.?line.?1|^address$|street.?address|mailing.?address":
                            lambda p: p["personal"]["address_line1"],
    r"address.?line.?2|apt\.?|suite|unit":
                            lambda p: p["personal"].get("address_line2", ""),
    r"\bcity\b":            lambda p: p["personal"]["city"],
    r"\bstate\b|province":  lambda p: p["personal"]["state"],
    r"zip|postal":          lambda p: p["personal"]["zip"],
    # "country phone code" must come before plain "country" to win
    r"country.*phone.*code|country.*code|phone.*code|calling.*code|country.*dial":
                            lambda p: p["personal"]["country"],
    r"country":             lambda p: p["personal"]["country"],
    r"location":            lambda p: p["personal"]["location"],
    r"linkedin":            lambda p: p["personal"]["linkedin"],
    r"github":              lambda p: p["personal"]["github"],
    r"portfolio|website":   lambda p: p["personal"]["portfolio"],
    # Work auth
    r"work auth|authorized|legally|visa":
                            lambda p: "Yes, I am a U.S. Citizen.",
    r"sponsorship|require sponsor":
                            lambda p: "No, I do not require sponsorship.",
    r"citizen|citizenship":
                            lambda p: "United States Citizen",
    # Education
    r"university|college|school":
                            lambda p: p["education"][0]["school"],
    r"degree|major":        lambda p: p["education"][0]["degree"],
    r"graduation|grad year": lambda p: str(p["education"][0]["graduation_year"]),
    r"gpa":                 lambda p: "3.5",  # Update if you have your GPA
    # Job-specific
    r"salary|compensation|pay":
                            lambda p: "${:,.0f}".format(p.get("salary_expectation_usd", 100000)),
    r"relocat":             lambda p: "Yes, I am willing to relocate.",
    r"start date|when can you":
                            lambda p: "I am available to start within 2-4 weeks.",
    # Common short answers
    r"why.*software|why.*engineer|why.*career":
                            lambda p: p["short_answers"]["why_software_engineering"],
    r"strength":            lambda p: p["short_answers"]["greatest_strength"],
    r"weakness":            lambda p: p["short_answers"]["greatest_weakness"],
    r"tell me about yourself|about you":
                            lambda p: p["short_answers"]["tell_me_about_yourself"],
    r"why.*company|why.*us|why.*interested|why.*role|why.*position|why.*join|why.*here":
                            lambda p: p["short_answers"]["why_this_company"],
    # NOTE: "Why Sonos?", "Why Google?" etc. are intentionally NOT mapped here —
    # they fall through to _ask_ai so Claude generates a company-specific answer
    # that is then cached per-company in ai_cache.
    # Optional name fields
    r"preferred.?name|has.*preferred|prefer.*name":
                            lambda p: "No",
    # Common HR yes/no questions (short answers work for select dropdowns)
    r"current.*employ|currently\s+employ|currently\s+work\s+for":
                            lambda p: "No",
    r"previous.*employ|past.*employ|employ.*past|been.*employ|worked.*before|employ.*before":
                            lambda p: "No",
    r"background.*check|drug.*test":
                            lambda p: "Yes",
    r"by selecting.*consent|selecting.*i\s+consent|i\s+consent\b":
                            lambda p: "I consent",
    r"highest.*education|level.*education|degree.*obtain|highest.*degree|education.*level|education.*obtain":
                            lambda p: (
                                "Bachelor's Degree"
                                if re.search(r"b\.?s\.|b\.?a\.|bachelor", p["education"][0]["degree"].lower())
                                else "Master's Degree"
                                if re.search(r"m\.?s\.|m\.?a\.|master", p["education"][0]["degree"].lower())
                                else "Doctoral Degree"
                                if re.search(r"ph\.?d\.|doctor", p["education"][0]["degree"].lower())
                                else p["education"][0]["degree"]
                            ),
    r"family.*member.*work|close.*family.*work|family.*employ":
                            lambda p: "No",
    r"deloitte":            lambda p: "No",
    # Workday tag/typeahead fields — leave blank (can't reliably populate)
    r"type.*to.*add|add.*skill|tag.*skill":
                            lambda p: "",
}


FIELD_FAILURE_THRESHOLD = 3  # Pause for human help when this many fields can't be auto-filled


def _value_matches(current: str, answer: str) -> bool:
    """
    Loose match: return True when the field already contains an acceptable value
    so we don't needlessly clear and retype it.
    Handles: exact match, abbreviation vs full name (MA/Massachusetts),
    profile long-form vs dropdown short-form (Yes/Yes, I am willing...).
    """
    c = current.lower().strip()
    a = answer.lower().strip()
    if not c:
        return False
    if c == a:
        return True
    # One is a meaningful prefix of the other (length guard avoids "No" matching "North Dakota")
    if len(c) >= 3 and a.startswith(c):
        return True
    if len(a) >= 3 and c.startswith(a):
        return True
    # One is contained in the other (handles "United States" ⊆ "United States of America (+1)")
    if len(c) >= 5 and (c in a or a in c):
        return True
    return False


def _extract_salary_from_description(text: str) -> Optional[int]:
    """
    Parse a salary from a job description. Returns the midpoint of a range,
    or the single value, as an integer. Returns None if nothing found.
    """
    if not text:
        return None

    # Range pattern: two numbers
    range_re = re.compile(
        r'\$\s*([\d,]+)\s*(?:k)?\s*[-–—to]+\s*\$?\s*([\d,]+)\s*(k)?',
        re.IGNORECASE
    )
    m = range_re.search(text)
    if m:
        lo_str, hi_str, hi_k = m.group(1), m.group(2), m.group(3)
        lo = float(lo_str.replace(",", ""))
        hi = float(hi_str.replace(",", ""))
        # Detect shorthand like "$80k – $120k" or "$80,000 – $120k"
        if lo < 1000:
            lo *= 1000
        if hi < 1000:
            hi *= 1000
        return int((lo + hi) / 2)

    # Single value: $90,000 or $90k
    single_re = re.compile(r'\$\s*([\d,]+)\s*(k)?(?:\s*/\s*(?:yr|year|annual))?', re.IGNORECASE)
    m = single_re.search(text)
    if m:
        val = float(m.group(1).replace(",", ""))
        if m.group(2):  # "k" suffix
            val *= 1000
        if val >= 10000:  # sanity check — ignore "$5" etc.
            return int(val)

    return None


def lookup_from_profile(label: str, profile: dict, job: dict = None) -> Optional[str]:
    """
    Attempt to answer a field from profile.json using regex pattern matching.
    Returns None if no match found (will fall through to Gemini).
    When job is provided, salary fields use the posting's stated salary (midpoint
    of a range) and fall back to profile.salary_expectation_usd.
    """
    # Strip trailing required indicators (* and similar) so patterns like
    # r"^why\s+\w+\??\s*$" still match labels like "Why Sonos?*".
    label_lower = label.lower().strip().rstrip("*").strip()
    for pattern, extractor in FIELD_MAP.items():
        if re.search(pattern, label_lower):
            try:
                value = extractor(profile)
            except (KeyError, IndexError, TypeError):
                return None
            # For salary fields, prefer the number from the job posting
            if re.search(r"salary|compensation|pay", pattern) and job:
                desc = job.get("description", "")
                extracted = _extract_salary_from_description(desc)
                if extracted:
                    floored = max(extracted, 80000)
                    if floored != extracted:
                        logger.debug(f"    Salary from job posting (${extracted:,}) below floor — using $80,000")
                    else:
                        logger.debug(f"    Salary from job posting: ${extracted:,}")
                    return str(floored)
            # Apply floor to profile fallback too
            if re.search(r"salary|compensation|pay", pattern):
                try:
                    raw = int(float(re.sub(r'[^\d.]', '', value or "0") or "0"))
                    if raw and raw < 80000:
                        logger.debug(f"    Profile salary (${raw:,}) below floor — using $80,000")
                        return "${:,.0f}".format(80000)
                except (ValueError, TypeError):
                    pass
            return value
    return None


# ── System notification helper ────────────────────────────────────────────────

def send_system_notification(title: str, message: str):
    """
    Send a native desktop notification so you know when the bot needs help.
    Works on macOS (osascript), Linux (notify-send), Windows (PowerShell).
    """
    system = platform.system()
    try:
        if system == "Darwin":  # macOS
            subprocess.run([
                "osascript", "-e",
                f'display notification "{message}" with title "{title}" sound name "Basso"'
            ])
        elif system == "Linux":
            subprocess.run(["notify-send", "-u", "critical", title, message])
        elif system == "Windows":
            subprocess.run([
                "powershell", "-Command",
                f'[System.Windows.Forms.MessageBox]::Show("{message}", "{title}")'
            ], capture_output=True)
    except Exception as e:
        logger.warning(f"Desktop notification failed: {e}")


# ── Main Filler class ─────────────────────────────────────────────────────────

class ApplicationFiller:
    """
    Async context manager that fills out job application forms.

    Usage:
        async with ApplicationFiller(config, profile) as filler:
            result = await filler.apply(job, dry_run=False, review=False)
    """

    def __init__(self, config: dict, profile: dict, debug: bool = False,
                 use_ai: bool = True, profile_path: str = "profile.json"):
        self.config = config
        self.profile = profile
        self.debug = debug
        self.use_ai = use_ai
        self.resume_path = Path(config.get("resume_path", "resume.pdf"))
        self._profile_path = Path(profile_path)
        self.claude = ClaudeCLI()
        self._playwright = None
        self._browser = None
        self._context = None
        self.profile_dir = Path("state/browser_profile")
        self._interrupt_flag = False
        self._orig_sigint = None
        self._resume_uploaded = False

    def _on_sigint(self, signum, frame):
        """Custom Ctrl+C handler — sets a flag instead of crashing."""
        self._interrupt_flag = True
        print("\n  [Ctrl+C] Interrupt received — finishing current action then pausing...",
              flush=True)

    # ── AI response cache ────────────────────────────────────────────────────

    @staticmethod
    def _cache_key(label: str) -> str:
        """Normalize a field label for use as a cache dictionary key."""
        return re.sub(r'\s+', ' ', label.lower().strip().rstrip('*:?'))

    def _cache_ai_response(self, key: str, answer: str):
        """Persist an AI response in profile.json's ai_cache section."""
        if "ai_cache" not in self.profile:
            self.profile["ai_cache"] = {}
        self.profile["ai_cache"][key] = answer
        try:
            with open(self._profile_path, "w", encoding="utf-8") as f:
                json.dump(self.profile, f, indent=2, ensure_ascii=False)
            logger.debug(f"    AI cache saved for '{key}'")
        except Exception as e:
            logger.warning(f"    Could not save AI cache to {self._profile_path}: {e}")

    async def __aenter__(self):
        self._playwright = await async_playwright().start()
        self._context = await self._playwright.chromium.launch_persistent_context(
            str(self.profile_dir),
            headless=not self.debug,
            viewport={"width": 1280, "height": 800},
            user_agent=(
                "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
                "AppleWebKit/537.36 (KHTML, like Gecko) "
                "Chrome/122.0.0.0 Safari/537.36"
            ),
            args=["--disable-blink-features=AutomationControlled", "--no-sandbox"],
        )
        # Replace the default SIGINT handler so Ctrl+C sets a flag instead of
        # raising KeyboardInterrupt (which Python 3.11+ asyncio converts to
        # CancelledError before our except blocks can see it).
        try:
            self._orig_sigint = signal.signal(signal.SIGINT, self._on_sigint)
        except (OSError, ValueError):
            pass  # non-main thread — can't install signal handler
        return self

    async def __aexit__(self, *args):
        if self._orig_sigint is not None:
            try:
                signal.signal(signal.SIGINT, self._orig_sigint)
            except (OSError, ValueError):
                pass
        if self._context:
            await self._context.close()
        if self._playwright:
            await self._playwright.stop()

    # ── Entry point ──────────────────────────────────────────────────────────

    async def apply(self, job: dict, dry_run: bool = False, review: bool = False) -> dict:
        """
        Fill out and (optionally) submit a job application.

        Returns:
            dict with 'status': one of 'applied' | 'dry_run' | 'skipped' | 'error'
        """
        page = await self._context.new_page()
        # Dismiss any unexpected browser dialogs so they don't crash the driver
        # when input() is blocking the main thread.
        page.on("dialog", lambda d: asyncio.ensure_future(d.dismiss()))
        try:
            if job.get("easy_apply"):
                result = await self._handle_easy_apply(page, job, dry_run, review)
            else:
                result = await self._handle_external_apply(page, job, dry_run, review)
            return result
        except Exception as e:
            logger.exception(f"Error applying to {job['title']}: {e}")
            if self.debug:
                try:
                    await page.screenshot(path=f"screenshots/filler_error_{job['id']}.png")
                except Exception:
                    pass
            return {"status": "error", "error": str(e)}
        finally:
            try:
                await page.close()
            except Exception:
                pass

    # ── LinkedIn Easy Apply ──────────────────────────────────────────────────

    async def _handle_easy_apply(self, page: Page, job: dict, dry_run: bool, review: bool) -> dict:
        """Handle LinkedIn's built-in 'Easy Apply' modal flow."""
        logger.info("  Method: LinkedIn Easy Apply")
        await page.goto(job["url"], wait_until="domcontentloaded")
        await asyncio.sleep(random.uniform(1.5, 2.5))

        # Click the Easy Apply button
        easy_btn = page.locator(
            "button[aria-label*='Easy Apply'], .jobs-apply-button--top-card"
        ).first
        if not await easy_btn.is_visible():
            return {"status": "error", "error": "Easy Apply button not found"}

        await easy_btn.click()
        await asyncio.sleep(2)

        # Handle multi-step modal
        max_steps = 15  # Safety cap to avoid infinite loops
        for step in range(max_steps):
            logger.info(f"  Easy Apply step {step + 1}...")

            if self.debug:
                await page.screenshot(path=f"screenshots/easyapply_{job['id']}_step{step}.png")

            # Check if we hit a CAPTCHA or unusual page
            captcha_detected = await page.query_selector(
                ".captcha, #captcha, [data-test='captcha'], iframe[src*='recaptcha']"
            )
            if captcha_detected:
                await self._request_human_help(page, job, "CAPTCHA detected")

            # Fill visible fields on this step
            await self._fill_all_fields(page, job)

            # Check for resume upload field
            await self._handle_resume_upload(page)

            # Try to advance to next step
            next_btn = page.locator(
                "button[aria-label='Continue to next step'], "
                "button[aria-label='Review your application'], "
                "footer button.artdeco-button--primary"
            ).last

            review_btn = page.locator("button[aria-label='Review your application']")
            submit_btn = page.locator(
                "button[aria-label='Submit application'], "
                "button[data-control-name='submit_unify']"
            )

            if await submit_btn.is_visible():
                return await self._do_submit(page, job, dry_run, review, "submit_btn")

            if await review_btn.is_visible():
                if review:
                    logger.info("  [REVIEW] Application ready. Review the form, then press ENTER.")
                    send_system_notification(
                        "Job Bot: Review Required",
                        f"Review application for {job['title']} @ {job['company']}"
                    )
                    await asyncio.to_thread(input, "  Press ENTER to continue to submission... (or Ctrl+C to skip): ")
                await review_btn.click()
                await asyncio.sleep(1.5)
                continue

            if await next_btn.is_visible():
                await next_btn.click()
                await asyncio.sleep(1.5)
                continue

            # No recognized button — may need human help
            logger.warning("  No recognizable next/submit button found. Waiting for help...")
            await self._request_human_help(page, job, "No navigation button found on this step")
            break

        return {"status": "error", "error": "Exceeded max steps in Easy Apply modal"}

    # ── External application ─────────────────────────────────────────────────

    async def _handle_external_apply(self, page: Page, job: dict, dry_run: bool, review: bool) -> dict:
        """Handle external company application pages (non-LinkedIn)."""
        self._resume_uploaded = False  # reset per-application
        logger.info(f"  Method: External application -> {job['apply_url']}")
        await page.goto(job["apply_url"], wait_until="domcontentloaded")

        # ATS platforms render entirely in JS — wait for real content before acting.
        try:
            await page.wait_for_selector(
                "h1, button, input, [role='main'], [role='button']",
                timeout=20000,
            )
        except Exception:
            pass
        await asyncio.sleep(random.uniform(2, 3))

        if self.debug:
            await page.screenshot(path=f"screenshots/external_{job['id']}_start.png")

        # Check for CAPTCHA
        captcha = await page.query_selector("iframe[src*='recaptcha'], .g-recaptcha, [id*='captcha']")
        if captcha:
            await self._request_human_help(page, job, "CAPTCHA on external apply page")

        # ── Land on the application form ──────────────────────────────────────
        # The URL from LinkedIn is almost always the job *description* page, not
        # the application form.  We must click Apply to enter the form.
        #
        # We do NOT use "number of fields on the page" as a gate — career pages
        # often have extra inputs (search bars, job-alert email forms, etc.) that
        # would fool that check and cause the bot to fill the wrong fields.
        apply_clicked = False
        for label in ("Apply Now", "Apply for Job", "Apply for this Job",
                      "Apply for Role", "Apply"):
            candidates = page.locator(
                f"button:has-text('{label}'), a:has-text('{label}')"
            )
            for i in range(await candidates.count()):
                btn = candidates.nth(i)
                try:
                    if not await btn.is_visible(timeout=1000):
                        continue
                    # Require the button text to START WITH our target label so
                    # we skip "Apply Filters", "Apply Promo Code", etc.
                    btn_text = (await btn.inner_text()).strip()
                    if not btn_text.lower().startswith(label.lower()):
                        continue
                    logger.info(f"  Clicking '{btn_text}' to open application form...")
                    await btn.click()
                    apply_clicked = True
                    try:
                        await page.wait_for_selector(
                            "input:not([type='hidden']), textarea, select",
                            timeout=15000,
                        )
                    except Exception:
                        pass
                    await asyncio.sleep(random.uniform(2, 3))
                    # Dismiss autofill-method modal immediately after clicking Apply
                    await self._handle_autofill_modal(page)
                    if self.debug:
                        await page.screenshot(
                            path=f"screenshots/external_{job['id']}_form.png"
                        )
                    break
                except Exception:
                    continue
            if apply_clicked:
                break

        if not apply_clicked:
            logger.debug("  No Apply button found — assuming already on application form.")

        # ── Multi-page application loop ───────────────────────────────────────
        MAX_PAGES = 25
        NEXT_SELECTOR = (
            "[data-automation-id='bottom-navigation-next-btn'], "
            "button:has-text('Next'), "
            "button:has-text('Save and Continue'), "
            "button:has-text('Continue'), "
            "button:has-text('Next Step'), "
            "input[value='Next'], "
            "a:has-text('Next')"
        )
        SUBMIT_SELECTOR = (
            "button:has-text('Submit Application'), "
            "button:has-text('Submit My Application'), "
            "button:has-text('Submit'), "
            "button:has-text('Send Application'), "
            "input[type='submit']"
        )

        for page_num in range(MAX_PAGES):
            logger.info(f"  Filling form page {page_num + 1}...")

            # Autofill-method modals can appear on any step (e.g. Workday re-prompts)
            await self._handle_autofill_modal(page)

            # Auto-handle login / create-account walls; falls back to human only on failure
            await self._handle_login_wall(page, job)

            # ── Fill phase ────────────────────────────────────────────────────
            # _fill_field checks _interrupt_flag before each field, so pressing
            # Ctrl+C drains the remaining fields quickly and surfaces here.
            if self._interrupt_flag:
                self._interrupt_flag = False
                logger.warning(f"  [Ctrl+C] Pausing on page {page_num + 1} for manual help.")
                await self._request_human_help(
                    page, job,
                    f"Page {page_num + 1}: Ctrl+C — fix any fields, press ENTER to continue"
                )

            fill_failures = await self._fill_all_fields(page, job)
            await self._handle_add_buttons(page, job)
            await self._handle_resume_upload(page)

            if self.debug:
                await page.screenshot(
                    path=f"screenshots/external_{job['id']}_p{page_num}_filled.png"
                )

            # Pause if the bot couldn't fill enough fields, OR if Ctrl+C was
            # pressed mid-fill (flag gets set between fields by _fill_field).
            interrupted = self._interrupt_flag
            if interrupted or fill_failures >= FIELD_FAILURE_THRESHOLD:
                self._interrupt_flag = False
                reason = (
                    f"Page {page_num + 1}: Ctrl+C — fix any fields, press ENTER to continue"
                    if interrupted else
                    f"Page {page_num + 1}: {fill_failures} field(s) couldn't be filled — "
                    "complete them manually, then press ENTER to continue"
                )
                await self._request_human_help(page, job, reason)

            # ── Button-check inner loop ───────────────────────────────────────
            # Retries up to 5× WITHOUT re-filling the page so human fixes aren't
            # overwritten when Next is disabled and we pause for assistance.
            page_advanced = False
            for _attempt in range(5):
                # Ctrl+C during the navigation loop breaks out immediately
                if self._interrupt_flag:
                    self._interrupt_flag = False
                    logger.warning(f"  [Ctrl+C] Pausing navigation on page {page_num + 1}.")
                    await self._request_human_help(
                        page, job,
                        f"Page {page_num + 1}: Ctrl+C — manually advance the form if "
                        "needed, then press ENTER to continue"
                    )
                    page_advanced = True
                    break

                next_btn = page.locator(NEXT_SELECTOR).first

                if await next_btn.is_visible():
                    btn_text = (await next_btn.inner_text()).strip()

                    # Workday changes nav button text to "Submit" on the final page
                    if any(w in btn_text.lower() for w in ("submit", "send application")):
                        logger.info(f"  Submit button found on page {page_num + 1} ('{btn_text}').")
                        return await self._do_submit(page, job, dry_run, review, next_btn)

                    if await next_btn.is_disabled():
                        logger.warning(
                            f"  'Next' button is disabled on page {page_num + 1} "
                            "(likely a validation error)."
                        )
                        await self._request_human_help(
                            page, job,
                            f"Page {page_num + 1}: Next is disabled — fix any errors, "
                            "then press ENTER to continue"
                        )
                        continue  # Re-check button state without re-filling the page

                    logger.info(f"  Clicking '{btn_text}' to advance to page {page_num + 2}...")
                    await next_btn.click()
                    try:
                        await page.wait_for_load_state("networkidle", timeout=8000)
                    except Exception:
                        pass
                    try:
                        await page.wait_for_selector(
                            "input:not([type='hidden']), textarea, select, "
                            "[role='radio'], [data-automation-id='textAreaField']",
                            timeout=8000,
                        )
                    except Exception:
                        pass
                    await asyncio.sleep(random.uniform(1.5, 2.5))

                    # Detect rejected submissions (required fields still empty).
                    # The ATS re-enables the button and shows errors without
                    # changing the URL, so without this check the bot loops forever.
                    if await self._has_form_errors(page):
                        logger.warning(
                            f"  Page {page_num + 1}: submission rejected — "
                            "validation errors visible"
                        )
                        await self._request_human_help(
                            page, job,
                            f"Page {page_num + 1}: form has validation errors — "
                            "fix the highlighted fields, then press ENTER to retry"
                        )
                        continue  # re-check the button after human fixes errors

                    page_advanced = True
                    break

                # No Next/Continue — check for a standalone Submit button.
                submit_btn = page.locator(SUBMIT_SELECTOR).first
                if await submit_btn.is_visible():
                    logger.info(f"  Submit button found on page {page_num + 1}.")
                    return await self._do_submit(page, job, dry_run, review, submit_btn)

                # No navigation button at all — ask human to advance manually
                logger.warning(
                    f"  No submit or next button visible on page {page_num + 1}."
                )
                await self._request_human_help(
                    page, job,
                    f"Page {page_num + 1}: no next/submit button found — "
                    "manually advance the form, then press ENTER to continue filling"
                )
                page_advanced = True  # Human has advanced; re-fill the new page
                break

            if not page_advanced:
                return {
                    "status": "error",
                    "error": (
                        f"Page {page_num + 1}: Next button remained disabled "
                        "after 5 attempts"
                    ),
                }

        return {"status": "error", "error": "Exceeded max pages without finding submit button"}

    async def _has_form_errors(self, page: Page) -> bool:
        """
        Return True if the page is showing visible form validation errors,
        meaning the submission was rejected and the form didn't advance.
        Covers Workday error banners, aria-invalid fields, and generic ATS
        'Errors Found' headings (iCIMS, Greenhouse, etc.).
        """
        return await page.evaluate("""() => {
            // Workday explicit error automation IDs
            for (const sel of [
                '[data-automation-id="validationError"]',
                '[data-automation-id="errorBanner"]',
                '[data-automation-id="formError"]',
            ]) {
                for (const el of document.querySelectorAll(sel)) {
                    if (el.offsetParent !== null) return true;
                }
            }
            // aria-invalid fields (standard HTML5 / most ATS)
            for (const el of document.querySelectorAll('[aria-invalid="true"]')) {
                if (el.offsetParent !== null) return true;
            }
            // Generic "Errors Found" heading text (iCIMS, Taleo, etc.)
            const xp = document.evaluate(
                '//*[normalize-space(text())="Errors Found"]',
                document, null, XPathResult.FIRST_ORDERED_NODE_TYPE, null
            ).singleNodeValue;
            if (xp && xp.offsetParent !== null) return true;
            return false;
        }""")

    # ── Login / autofill modal automation ───────────────────────────────────

    async def _handle_autofill_modal(self, page: Page) -> bool:
        """
        Detect and dismiss "How do you want to fill this?" autofill-method
        prompts (Workday, Greenhouse, iCIMS, etc.) by always choosing the
        manual-fill option.  Returns True if a prompt was found.

        Strategy: first confirm we're actually on an autofill modal by checking
        for an "Autofill from Resume" type button.  Only then look for the manual
        option.  This prevents false positives from accessibility "Skip to content"
        links and other generic page elements.
        """
        # ── Step 1: confirm the autofill modal is actually present ───────
        AUTOFILL_SIGNALS = [
            "Autofill with Résumé",
            "Autofill from Resume",
            "Autofill from résumé",
            "Autofill with Resume",
            "Auto-fill from résumé",
            "Import from Resume",
            "Autofill",
        ]
        modal_visible = False
        for signal_text in AUTOFILL_SIGNALS:
            try:
                sig_btn = page.locator(
                    f"button:has-text('{signal_text}'), a:has-text('{signal_text}')"
                )
                if await sig_btn.is_visible(timeout=600):
                    modal_visible = True
                    break
            except Exception:
                continue

        if not modal_visible:
            return False

        # ── Step 2: click the manual-fill option ─────────────────────────
        MANUAL_LABELS = [
            "Fill out Manually",
            "Fill Out Manually",
            "Fill Manually",
            "Fill in Manually",
            "Enter Manually",
            "Fill form manually",
            "Manual Entry",
            "Continue without autofilling",
            "Apply Manually",
            "Manual",
        ]
        for text in MANUAL_LABELS:
            try:
                btn = page.locator(
                    f"button:has-text('{text}'), a:has-text('{text}')"
                )
                if await btn.is_visible(timeout=800):
                    logger.info(f"  Autofill modal: selecting '{text}'")
                    await btn.click()
                    await asyncio.sleep(1)
                    return True
            except Exception:
                continue

        logger.warning("  Autofill modal detected but no 'Fill Manually' button found — continuing")
        return False

    async def _handle_google_login(self, page: Page, job: dict) -> bool:
        """
        Detect and click a 'Sign in with Google' button, then handle the
        resulting OAuth popup (account picker → Allow/Continue prompts).
        Since Chromium is already signed into Google via the persistent profile,
        this usually resolves with one or zero extra clicks.

        Returns True if a Google login button was found (regardless of outcome).
        Falls back to _request_human_help only if the popup stalls.
        """
        email = self.profile["personal"]["email"]

        GOOGLE_SELECTORS = [
            "button:has-text('Sign in with Google')",
            "button:has-text('Continue with Google')",
            "button:has-text('Login with Google')",
            "button:has-text('Sign up with Google')",
            "a:has-text('Sign in with Google')",
            "a:has-text('Continue with Google')",
            "[data-provider='google']",
            "[data-testid*='google']",
            # Workday / iCIMS social login buttons
            "[aria-label*='Google']",
        ]

        google_btn = None
        for sel in GOOGLE_SELECTORS:
            try:
                loc = page.locator(sel)
                count = await loc.count()
                # Iterate last-to-first: when a sign-in modal is open, the modal's
                # copy of the button is added later in the DOM (higher index) and is
                # the one visually on top.  Using elementFromPoint verifies we pick
                # the button that actually receives pointer events, not a copy hidden
                # behind the modal.
                for i in range(count - 1, -1, -1):
                    el = loc.nth(i)
                    try:
                        if not await el.is_visible(timeout=400):
                            continue
                        on_top = await el.evaluate("""el => {
                            const r = el.getBoundingClientRect();
                            if (!r.width || !r.height) return false;
                            const hit = document.elementFromPoint(
                                r.left + r.width / 2, r.top + r.height / 2
                            );
                            return hit != null && (hit === el || el.contains(hit));
                        }""")
                        if on_top:
                            google_btn = el
                            break
                    except Exception:
                        continue
                if google_btn:
                    break
            except Exception:
                continue

        if not google_btn:
            return False

        logger.info("  Google login button found — starting OAuth flow...")
        pre_click_url = page.url

        # ── Try popup first ───────────────────────────────────────────────
        # Use a longer timeout (10 s) so Playwright's actionability checks
        # (scroll-into-view, stability wait) on the button don't eat the
        # entire window before the popup has a chance to open.
        popup = None
        try:
            async with page.expect_popup(timeout=10000) as popup_info:
                await google_btn.click()
            popup = await popup_info.value
            await popup.wait_for_load_state("domcontentloaded", timeout=15000)
            logger.info(f"  Google OAuth popup: {popup.url[:60]}")
        except Exception:
            popup = None

        # ── Handle popup flow ─────────────────────────────────────────────
        if popup:
            try:
                try:
                    await popup.wait_for_selector(
                        f"[data-email='{email}'], [data-identifier='{email}']",
                        timeout=4000,
                    )
                    acct = popup.locator(
                        f"[data-email='{email}'], [data-identifier='{email}']"
                    ).first
                    if await acct.is_visible(timeout=1000):
                        logger.info(f"  Google OAuth: selecting account '{email}'")
                        await acct.click()
                        await asyncio.sleep(1.5)
                except Exception:
                    pass

                for btn_text in ("Continue", "Allow", "Yes", "Confirm"):
                    try:
                        allow_btn = popup.locator(f"button:has-text('{btn_text}')").first
                        if await allow_btn.is_visible(timeout=2000):
                            logger.info(f"  Google OAuth: clicking '{btn_text}'")
                            await allow_btn.click()
                            await asyncio.sleep(1)
                            break
                    except Exception:
                        continue

                try:
                    await popup.wait_for_close(timeout=15000)
                    logger.info("  Google OAuth completed — popup closed")
                except Exception:
                    logger.warning("  Google OAuth popup stalled — asking for human help")
                    await self._request_human_help(
                        page, job,
                        "Google sign-in popup stalled — complete it, then press ENTER"
                    )
            except Exception as e:
                logger.warning(f"  Google OAuth popup error: {e}")
                await self._request_human_help(
                    page, job,
                    "Google sign-in popup — complete it, then press ENTER"
                )
            try:
                await page.wait_for_load_state("networkidle", timeout=12000)
            except Exception:
                pass
            await asyncio.sleep(2)
            return True

        # ── No popup — check for same-tab OAuth redirect ──────────────────
        await asyncio.sleep(2)

        if "accounts.google.com" in page.url or "/o/oauth2" in page.url:
            logger.info("  Google OAuth: same-tab redirect to Google — handling account picker")
            try:
                # Click our account if the picker is shown
                acct = page.locator(
                    f"[data-email='{email}'], [data-identifier='{email}']"
                ).first
                try:
                    if await acct.is_visible(timeout=5000):
                        logger.info(f"  Google OAuth: selecting account '{email}'")
                        await acct.click()
                        await asyncio.sleep(1.5)
                except Exception:
                    pass

                # Wait for redirect back to the original domain
                m = re.search(r"https?://([^/]+)", pre_click_url)
                if m:
                    domain = m.group(1)
                    await page.wait_for_url(
                        lambda url: domain in url,
                        timeout=20000,
                    )
            except Exception as e:
                logger.warning(f"  Google OAuth same-tab error: {e}")
            try:
                await page.wait_for_load_state("networkidle", timeout=12000)
            except Exception:
                pass
            await asyncio.sleep(2)
            return True

        # ── Check for Google OAuth running inside a page frame ───────────
        # Some implementations embed the Google account picker in an iframe
        # rather than opening a popup, so page.frames will show it.
        for frame in page.frames:
            try:
                if "accounts.google.com" in frame.url or "/o/oauth2" in frame.url:
                    logger.info(f"  Google OAuth: detected in-page frame — handling")
                    try:
                        acct = frame.locator(
                            f"[data-email='{email}'], [data-identifier='{email}']"
                        ).first
                        if await acct.is_visible(timeout=3000):
                            logger.info(f"  Google OAuth frame: selecting account '{email}'")
                            await acct.click()
                            await asyncio.sleep(1.5)
                    except Exception:
                        pass
                    try:
                        await page.wait_for_load_state("networkidle", timeout=20000)
                    except Exception:
                        pass
                    await asyncio.sleep(2)
                    return True
            except Exception:
                continue

        # ── Click had no effect — button still visible ────────────────────
        try:
            if await google_btn.is_visible(timeout=500):
                logger.info("  Google login: click had no effect — falling back to email")
                return False
        except Exception:
            pass

        # Button gone but no detectable redirect — assume auth in progress
        logger.info("  Google OAuth: button no longer visible — waiting for auth")
        try:
            await page.wait_for_load_state("networkidle", timeout=15000)
        except Exception:
            pass
        await asyncio.sleep(2)
        return True

    async def _handle_login_wall(self, page: Page, job: dict) -> bool:
        """
        Detect login / account-creation walls and attempt to auto-authenticate
        using the applicant's email and the shared password.  Falls back to
        _request_human_help only if auto-login fails (wrong password, 2FA, etc.).
        Returns True if a login wall was found (regardless of outcome).
        """
        # Try Google OAuth first — preferred when available since the browser
        # is already signed in and no password filling is needed.
        if await self._handle_google_login(page, job):
            return True

        PASSWORD = self.config.get("default_pass", "")
        email = self.profile["personal"]["email"]
        first_name = self.profile["personal"]["name"].split()[0]
        last_name = self.profile["personal"]["name"].split()[-1]

        # ── Detect ───────────────────────────────────────────────────────
        wd_sign_in = await page.query_selector("[data-automation-id='signInButton']")
        wd_create = await page.query_selector("[data-automation-id='createAccountLink']")
        password_field = await page.query_selector("input[type='password']")

        generic_sign_in = None
        if not wd_sign_in and not password_field:
            for sel in (
                "button:has-text('Sign In')", "a:has-text('Sign In')",
                "button:has-text('Log In')", "a:has-text('Log In')",
                "button:has-text('Login')", "a:has-text('Login')",
                "button:has-text('Sign in')",
            ):
                el = page.locator(sel).first
                try:
                    if await el.is_visible(timeout=400):
                        generic_sign_in = el
                        break
                except Exception:
                    pass

        if not (wd_sign_in or wd_create or password_field or generic_sign_in):
            return False

        logger.info("  Login wall detected — attempting auto-login...")

        # ── Step 1: Click sign-in link if no form is visible yet ─────────
        click_target = wd_sign_in or generic_sign_in
        if click_target and not password_field:
            try:
                await click_target.click()
                await asyncio.sleep(2.5)
                # Clicking sign-in may open a social-login picker modal
                # (e.g. Workday's "Sign in with Apple / Google / email").
                # Now that the picker is visible, try Google OAuth first.
                if await self._handle_google_login(page, job):
                    return True
                # No Google — click "Sign in with email" to expand email/password form.
                for _email_sel in (
                    "button:has-text('Sign in with email')",
                    "a:has-text('Sign in with email')",
                    "button:has-text('Continue with email')",
                    "[data-automation-id='signInWithEmail']",
                ):
                    _el = page.locator(_email_sel).first
                    try:
                        if await _el.is_visible(timeout=500):
                            await _el.click()
                            await asyncio.sleep(1.5)
                            break
                    except Exception:
                        pass
                password_field = await page.query_selector("input[type='password']")
            except Exception as e:
                logger.debug(f"    Sign-in click error: {e}")

        # ── Step 2: Workday two-step (email → Next → password) ───────────
        wd_email = await page.query_selector(
            "[data-automation-id='email'], [data-automation-id='username']"
        )
        if wd_email and await wd_email.is_visible():
            await wd_email.fill("")
            await wd_email.type(email, delay=50)
            next_btn = page.locator(
                "[data-automation-id='signInNextButton'], button:has-text('Next')"
            ).first
            try:
                if await next_btn.is_visible(timeout=2000):
                    await next_btn.click()
                    await asyncio.sleep(1.5)
                    password_field = await page.query_selector("input[type='password']")
            except Exception:
                pass

        # ── Step 3: Fill all password fields ─────────────────────────────
        pwd_fields = await page.query_selector_all("input[type='password']")
        for pf in pwd_fields:
            try:
                if await pf.is_visible():
                    await pf.fill("")
                    await pf.type(PASSWORD, delay=50)
                    await asyncio.sleep(0.2)
            except Exception:
                pass

        # ── Step 4: Create-account extras (two password fields = signup) ──
        if len(pwd_fields) >= 2:
            logger.info("  Looks like account creation — filling name fields...")
            for pattern, value in (
                (
                    "[data-automation-id='firstName'], input[name='firstName'], "
                    "input[name='first_name'], input[autocomplete='given-name']",
                    first_name,
                ),
                (
                    "[data-automation-id='lastName'], input[name='lastName'], "
                    "input[name='last_name'], input[autocomplete='family-name']",
                    last_name,
                ),
            ):
                f = await page.query_selector(pattern)
                if f and await f.is_visible():
                    if not (await f.evaluate("el => el.value") or "").strip():
                        await f.fill(value)

            # Email on create-account form
            email_f = await page.query_selector(
                "input[type='email'], input[name='email'], input[id*='email']"
            )
            if email_f and await email_f.is_visible():
                if not (await email_f.evaluate("el => el.value") or "").strip():
                    await email_f.fill(email)

            # Accept any T&C checkboxes
            for cb in await page.query_selector_all("input[type='checkbox']"):
                try:
                    if await cb.is_visible() and not await cb.is_checked():
                        await cb.check()
                except Exception:
                    pass

        # ── Step 5: Generic email field (non-Workday, no two-step) ───────
        elif not wd_email:
            email_f = await page.query_selector(
                "input[type='email'], input[name='email'], "
                "input[id*='email'], input[placeholder*='email']"
            )
            if email_f and await email_f.is_visible():
                if not (await email_f.evaluate("el => el.value") or "").strip():
                    await email_f.fill("")
                    await email_f.type(email, delay=50)

        # ── Step 6: Submit ────────────────────────────────────────────────
        submit = page.locator(
            "[data-automation-id='signInSubmitButton'], "
            "[data-automation-id='createAccountSubmitButton'], "
            "button[type='submit'], "
            "button:has-text('Sign In'), "
            "button:has-text('Log In'), "
            "button:has-text('Create Account'), "
            "input[type='submit']"
        ).first

        submitted = False
        try:
            if await submit.is_visible(timeout=3000):
                await submit.click()
                submitted = True
                await asyncio.sleep(3)
        except Exception:
            pass

        if not submitted:
            logger.warning("  Could not find login submit button — asking for human help")
            await self._request_human_help(
                page, job, "Login required — please sign in or create an account"
            )
            return True

        # ── Step 7: Verify success ────────────────────────────────────────
        try:
            still_login = await page.query_selector(
                "input[type='password'], [data-automation-id='signInButton']"
            )
        except Exception:
            # Page navigated away during login (e.g. OAuth redirect completed) —
            # treat as success rather than crashing.
            logger.info("  Page navigated during login verification — assuming success")
            still_login = None
        if still_login:
            logger.warning("  Auto-login may have failed — asking for human help")
            await self._request_human_help(
                page, job,
                "Login may have failed (wrong password / 2FA?) — please complete sign-in"
            )
        else:
            logger.info("  Auto-login succeeded")
            try:
                await page.wait_for_load_state("networkidle", timeout=10000)
            except Exception:
                pass
            await asyncio.sleep(1)

        return True

    # ── Field detection and filling ──────────────────────────────────────────

    async def _fill_all_fields(self, page: Page, job: dict) -> int:
        """
        Scan the current page for form fields and fill each one.
        Strategy: profile lookup → Gemini Flash (only if needed).
        Returns the count of fields that could not be filled.

        Radio inputs are excluded here and handled at group level by
        _fill_radio_groups so that one unanswered question = one failure.
        """
        inputs = await page.query_selector_all(
            "input:not([type='hidden']):not([type='submit'])"
            ":not([type='file']):not([type='radio'])"
        )
        textareas = await page.query_selector_all("textarea")
        selects = await page.query_selector_all("select")

        all_fields = inputs + textareas + selects
        fill_failures = 0

        for field in all_fields:
            filled = await self._fill_field(page, field, job)
            if not filled:
                fill_failures += 1

        await self._check_custom_checkboxes(page)
        fill_failures += await self._fill_radio_groups(page, job)
        fill_failures += await self._fill_workday_textboxes(page, job)
        fill_failures += await self._fill_workday_application_questions(page, job)

        # Safety net: if standard field detection came up empty, check for
        # Workday proprietary widgets (custom dropdowns, text areas, question sets)
        # that our selectors don't cover.  Fire even when all_fields is non-empty
        # because Workday pages can mix a few detected inputs with many undetected
        # custom widgets on the same page.
        if fill_failures < FIELD_FAILURE_THRESHOLD:
            undetected = await page.evaluate("""() => {
                const patterns = [
                    // radio / checkbox
                    'radioButton', 'checkboxPanel', 'radioGroup',
                    // generic question containers
                    'questionField', 'questionSet', 'formField',
                    // Workday dropdowns (selectWidget covers Yes/No work-auth questions)
                    'DropdownList', 'selectWidget', 'selectField', 'singleSelectDropdown',
                    // text input / area
                    'textInput', 'textAreaField', 'richText',
                    // multi-select
                    'multiSelectContainer',
                ];
                let count = 0;
                for (const pat of patterns) {
                    const els = document.querySelectorAll(
                        '[data-automation-id*="' + pat + '"]'
                    );
                    for (const el of els) {
                        if (el.offsetParent !== null) count++;
                    }
                }
                return count;
            }""")
            # Only fire when the gap between widget count and handled+known-failures
            # is large enough to indicate genuinely missed fields.
            # Using just (undetected > handled) is too sensitive because Workday
            # wraps each standard input in its own widget container, so the counts
            # often differ by 1-2 even on a fully-filled page.
            handled = len(all_fields)
            unexplained = undetected - handled - fill_failures
            if unexplained >= FIELD_FAILURE_THRESHOLD:
                logger.debug(
                    f"  Safety net: {undetected} Workday widget(s) found, "
                    f"{handled} standard field(s) handled — specialized handlers will cover remaining widgets."
                )

        return fill_failures

    async def _fill_radio_groups(self, page: Page, job: dict) -> int:
        """
        Fill all radio-button groups on the page.

        Handles two variants:
          1. Standard <input type='radio'> elements, grouped by their name attribute.
          2. Custom [role='radio'] widgets (Workday-style non-input divs).

        Processing at the group level means one unanswered question = one failure,
        which correctly triggers the human-help threshold.
        """
        failures = 0
        answered_names: set[str] = set()

        # ── 1. Standard HTML radio inputs ────────────────────────────────────
        all_std = await page.query_selector_all("input[type='radio']")
        groups: dict[str, list] = {}
        for radio in all_std:
            try:
                if not await radio.is_visible():
                    continue
                name = await radio.get_attribute("name") or ""
                if name:
                    groups.setdefault(name, []).append(radio)
            except Exception:
                continue

        for name, radios in groups.items():
            try:
                # Skip already-answered groups
                if any([await r.is_checked() for r in radios]):
                    answered_names.add(name)
                    continue

                # Find the question text by walking up from the first radio
                question = await radios[0].evaluate("""el => {
                    // fieldset > legend (standard HTML grouping)
                    const fs = el.closest('fieldset');
                    if (fs) {
                        const lg = fs.querySelector('legend');
                        if (lg) return lg.textContent.trim();
                    }
                    // role='group' with aria-labelledby
                    const grp = el.closest('[role="group"]');
                    if (grp) {
                        const lby = grp.getAttribute('aria-labelledby');
                        if (lby) {
                            const lbl = document.getElementById(lby);
                            if (lbl) return lbl.textContent.trim();
                        }
                    }
                    // Walk up ancestors for Workday formLabel or legend
                    let p = el.parentElement;
                    for (let i = 0; i < 7; i++) {
                        if (!p) break;
                        const wdl = p.querySelector('[data-automation-id="formLabel"]');
                        if (wdl) return wdl.textContent.trim();
                        const lg = p.querySelector(':scope > legend');
                        if (lg) return lg.textContent.trim();
                        p = p.parentElement;
                    }
                    return '';
                }""")

                if not question:
                    logger.debug(f"    Radio group '{name}': no question label found")
                    failures += 1
                    continue

                logger.debug(f"    Radio group '{name}': '{question[:80]}'")

                answer = lookup_from_profile(question, self.profile, job)
                if not answer:
                    answer = await self._ask_ai(question, job)
                if not answer:
                    failures += 1
                    continue

                answer_key = answer.lower().split(",")[0].split()[0].strip()
                clicked = False
                for radio in radios:
                    try:
                        lbl = await self._get_field_label(page, radio)
                        if lbl and lbl.lower().startswith(answer_key):
                            await radio.click()
                            answered_names.add(name)
                            clicked = True
                            logger.debug(f"    Radio: '{lbl}' ← '{question[:60]}'")
                            break
                    except Exception:
                        continue

                if not clicked:
                    failures += 1
                    logger.debug(f"    Radio: no match for '{answer}' on '{question[:60]}'")

            except Exception as e:
                logger.debug(f"    Radio group '{name}' error: {e}")

        # ── 2. Custom radio widgets (Workday non-input style) ───────────────
        # Workday uses several patterns: role='radio', data-automation-id='radioButton',
        # and sometimes neither — so we search by container first, then by any
        # recognisable child widget.
        try:
            containers = await page.query_selector_all(
                "[role='radiogroup'], fieldset, "
                "[data-automation-id*='radioGroup'], [data-automation-id*='RadioGroup'], "
                "[data-automation-id*='formField'], [data-automation-id*='questionField']"
            )
        except Exception:
            containers = []

        for container in containers:
            try:
                if not await container.is_visible():
                    continue

                # Accept role='radio' divs OR Workday's data-automation-id='radioButton'
                custom_radios = await container.query_selector_all(
                    "[role='radio']:not(input), "
                    "[data-automation-id='radioButton'], "
                    "[data-automation-id*='RadioButton']"
                )
                if not custom_radios:
                    continue

                # Skip already-answered (aria-checked="true" OR Workday's selected class)
                already_answered = False
                for r in custom_radios:
                    aria = (await r.get_attribute("aria-checked") or "false").lower()
                    selected = (await r.get_attribute("aria-selected") or "false").lower()
                    css_class = (await r.get_attribute("class") or "").lower()
                    if aria == "true" or selected == "true" or "selected" in css_class:
                        already_answered = True
                        break
                if already_answered:
                    continue

                question = ""
                for sel in ("legend", "[data-automation-id='formLabel']",
                            "[data-automation-id='questionTitle']", "label"):
                    q_el = await container.query_selector(sel)
                    if q_el:
                        q_text = (await q_el.inner_text()).strip()
                        if q_text:
                            question = q_text
                            break

                if not question:
                    failures += 1
                    continue

                logger.debug(f"    Custom radio group: '{question[:80]}'")

                answer = lookup_from_profile(question, self.profile, job)
                if not answer:
                    answer = await self._ask_ai(question, job)
                if not answer:
                    failures += 1
                    continue

                answer_key = answer.lower().split(",")[0].split()[0].strip()
                clicked = False
                for radio in custom_radios:
                    try:
                        if not await radio.is_visible():
                            continue
                        opt_text = await radio.get_attribute("aria-label") or ""
                        if not opt_text:
                            opt_text = (await radio.inner_text()).strip()
                        if not opt_text:
                            opt_text = await radio.evaluate("""el => {
                                const next = el.nextElementSibling;
                                if (next) return next.textContent.trim();
                                const p = el.closest('label');
                                return p ? p.textContent.trim() : '';
                            }""")
                        if opt_text and opt_text.lower().startswith(answer_key):
                            await radio.click()
                            await asyncio.sleep(0.3)
                            clicked = True
                            logger.debug(f"    Clicked '{opt_text}' for '{question[:60]}'")
                            break
                    except Exception:
                        continue

                if not clicked:
                    failures += 1
                    logger.debug(f"    Custom radio: no match '{answer}' for '{question[:60]}'")

            except Exception as e:
                logger.debug(f"    Custom radio group error: {e}")

        return failures

    async def _fill_workday_textboxes(self, page: Page, job: dict) -> int:
        """
        Fill Workday rich-text / contenteditable question fields that aren't
        captured by the standard <textarea>/<input> queries.  Workday uses
        role='textbox' on contenteditable divs for long free-response questions
        like 'Why [Company]?'.
        Returns the number of fields that could not be filled.
        """
        failures = 0
        try:
            boxes = await page.query_selector_all(
                "[role='textbox'][contenteditable='true'], "
                "[contenteditable='true']:not([role='combobox'])"
            )
        except Exception:
            return 0

        for box in boxes:
            try:
                if not await box.is_visible():
                    continue
                # Skip boxes that already have content
                current = (await box.evaluate("el => el.innerText") or "").strip()
                if current:
                    continue

                label = await self._get_field_label(page, box)
                if not label:
                    continue

                logger.debug(f"    Workday textbox: '{label}'")
                answer = lookup_from_profile(label, self.profile, job)
                if not answer:
                    answer = await self._ask_ai(label, job)
                if not answer:
                    failures += 1
                    continue

                # Click to focus, clear, then type
                try:
                    await box.click(timeout=3000)
                except Exception:
                    await box.evaluate("el => el.focus()")
                await asyncio.sleep(0.1)
                # Select-all + delete to clear existing content
                await box.evaluate("el => { el.focus(); document.execCommand('selectAll'); }")
                await box.type(answer, delay=20)
                logger.debug(f"    Textbox filled: '{label[:60]}'")

            except Exception as e:
                logger.debug(f"    Workday textbox error: {e}")

        return failures

    async def _fill_workday_application_questions(self, page: Page, job: dict) -> int:
        """
        Handle Workday's custom formField/questionField containers on pages like
        'Application Questions'.  These pages use Workday-specific widgets that
        are invisible to standard <input>/<textarea>/<select> queries:
          • Custom select dropdowns (button[aria-haspopup='listbox'])
          • Rich-text textareas (div.ql-editor or contenteditable divs)
          • Numeric text inputs that need currency symbols stripped

        Operates at the container level so we interact with VISIBLE widgets
        rather than the hidden backing inputs that standard selectors find.
        """
        failures = 0
        try:
            containers = await page.query_selector_all(
                "[data-automation-id='formField'], "
                "[data-automation-id='questionField']"
            )
        except Exception:
            return 0

        for container in containers:
            try:
                if not await container.is_visible():
                    continue

                # ── Get the question label ────────────────────────────────
                label = ""
                for lbl_sel in (
                    "[data-automation-id='formLabel']",
                    "[data-automation-id='questionTitle']",
                    "label", "legend",
                ):
                    lbl_el = await container.query_selector(lbl_sel)
                    if lbl_el:
                        t = (await lbl_el.inner_text()).strip()
                        if t:
                            label = t
                            break
                if not label:
                    continue

                # ── Case 1: Custom Workday select dropdown ────────────────
                select_btn = await container.query_selector(
                    "[data-automation-id='selectInput'], "
                    "button[aria-haspopup='listbox'], "
                    "[data-automation-id='selectWidget'] button"
                )
                if select_btn:
                    btn_text = (await select_btn.inner_text()).strip().lower()
                    # Skip if already answered (text is not a placeholder)
                    if btn_text and "select" not in btn_text:
                        continue

                    answer = lookup_from_profile(label, self.profile, job)
                    if not answer:
                        answer = await self._ask_ai(label, job)
                    if not answer:
                        failures += 1
                        continue

                    logger.debug(f"    WD select: '{label[:60]}' → '{answer[:40]}'")
                    await self._fill_workday_select_widget(page, select_btn, answer)
                    continue

                # ── Case 2: Rich-text / contenteditable textarea ──────────
                # Workday uses Quill (div.ql-editor) or plain contenteditable
                # divs for long free-response questions; neither is a <textarea>.
                text_el = await container.query_selector(
                    "div.ql-editor, "
                    "[role='textbox'], "
                    "[contenteditable='true']:not([role='combobox']), "
                    "textarea"
                )
                if text_el:
                    tag = await text_el.evaluate("el => el.tagName.toLowerCase()")
                    if tag == "textarea":
                        current = (await text_el.evaluate("el => el.value") or "").strip()
                    else:
                        current = (await text_el.evaluate("el => el.innerText") or "").strip()
                    if current:
                        continue  # already filled

                    answer = lookup_from_profile(label, self.profile, job)
                    if not answer:
                        answer = await self._ask_ai(label, job)
                    if not answer:
                        failures += 1
                        continue

                    logger.debug(f"    WD rich-text: '{label[:60]}' → {len(answer)} chars")
                    try:
                        await text_el.click(timeout=3000)
                    except Exception:
                        await text_el.evaluate("el => el.focus()")
                    await asyncio.sleep(0.1)
                    if tag == "textarea":
                        await text_el.fill(answer)
                    else:
                        await text_el.evaluate(
                            "el => { el.focus(); "
                            "document.execCommand('selectAll'); "
                            "document.execCommand('delete'); }"
                        )
                        await text_el.type(answer, delay=20)
                    continue

                # ── Case 3: Plain text / numeric input ────────────────────
                inp_el = await container.query_selector("input")
                if inp_el and await inp_el.is_visible():
                    current = (await inp_el.evaluate("el => el.value") or "").strip()
                    if current:
                        continue

                    answer = lookup_from_profile(label, self.profile, job)
                    if not answer:
                        answer = await self._ask_ai(label, job)
                    if not answer:
                        failures += 1
                        continue

                    # Strip currency symbols/commas when label requires a plain number
                    label_lower = label.lower()
                    if "numeric" in label_lower or re.search(r"salary|compensation|pay", label_lower):
                        answer = re.sub(r'[^\d.]', '', answer)

                    logger.debug(f"    WD input: '{label[:60]}' → '{answer[:40]}'")
                    await inp_el.fill(answer)

            except Exception as e:
                logger.debug(f"    WD question container error: {e}")

        return failures

    async def _fill_workday_select_widget(self, page: Page, select_btn, answer: str):
        """
        Click a Workday custom select button and choose the best matching option
        from the listbox that drops down.
        """
        try:
            await select_btn.click(timeout=3000)
        except Exception:
            await select_btn.evaluate("el => el.click()")
        await asyncio.sleep(0.5)

        try:
            await page.wait_for_selector(
                "[data-automation-id='promptOption'], "
                "[role='option'], "
                "li[role='presentation'][data-automation-id='menuItem']",
                timeout=3000,
            )
        except Exception:
            await page.keyboard.press("Escape")
            return

        options = await page.query_selector_all(
            "[data-automation-id='promptOption'], [role='option']"
        )
        # answer_key: first meaningful word of the answer ("Yes", "No", etc.)
        answer_key = answer.lower().split(",")[0].split()[0].strip()

        best = None
        for opt in options:
            try:
                opt_text = (await opt.inner_text()).strip()
                if not opt_text:
                    continue
                if opt_text.lower().startswith(answer_key):
                    best = opt
                    break
                if answer.lower() in opt_text.lower() and best is None:
                    best = opt
            except Exception:
                continue

        if best:
            try:
                await best.click(timeout=3000)
            except Exception:
                await best.evaluate("el => el.click()")
        else:
            logger.debug(f"    WD select: no match for '{answer}' — closing dropdown")
            await page.keyboard.press("Escape")
        await asyncio.sleep(0.3)

    async def _fill_field(self, page: Page, field: ElementHandle, job: dict) -> bool:
        """Fill a single form field. Returns True if filled/skipped-ok, False if unable to fill."""
        if self._interrupt_flag:
            return False  # bail out of field filling; page loop will pause for human
        try:
            tag = await field.evaluate("el => el.tagName.toLowerCase()")
            field_type = await field.get_attribute("type") or "text"
            is_visible = await field.is_visible()
            is_enabled = await field.is_enabled()

            if not is_visible or not is_enabled:
                return True  # Not applicable — not a failure

            label = await self._get_field_label(page, field)
            if not label:
                return True  # Can't determine label — skip silently

            logger.debug(f"    Filling field: '{label}' (type={field_type}, tag={tag})")

            if tag == "select":
                return await self._fill_select(field, label, job)

            if field_type in ("checkbox", "radio"):
                await self._fill_choice(field, label)
                return True

            # TEXT / TEXTAREA: try profile first, then Gemini/human
            answer = lookup_from_profile(label, self.profile, job)

            if answer is None:
                # No profile pattern matched — skip if field already has content
                current_value = await field.evaluate(
                    "el => el.value || el.textContent"
                ) or ""
                if current_value.strip():
                    return True
                # Grab stable attributes NOW before any blocking wait that could
                # stale the element handle (human input can take 30+ seconds).
                field_id = await field.get_attribute("id") or ""
                field_name = await field.get_attribute("name") or ""
                answer = await self._ask_ai(label, job)
                if answer:
                    # Re-query the element — the handle is likely stale after
                    # the blocking input() call.
                    refound = None
                    if field_id:
                        refound = await page.query_selector(f"#{field_id}")
                    if refound is None and field_name:
                        refound = await page.query_selector(f"[name='{field_name}']")
                    field = refound or field
            elif answer == "":
                # Profile explicitly says leave blank (e.g. no middle name, no apt number)
                return True
            else:
                # Profile has an answer — skip if the field is already filled with a
                # matching value. This avoids re-typing values Workday pre-populated
                # from the user's own profile and prevents visible cursor-jumping.
                current_value = (await field.evaluate("el => el.value") or "").strip()
                if _value_matches(current_value, answer):
                    return True

            if answer:
                role = await field.get_attribute("role") or ""
                if role == "combobox":
                    await self._fill_combobox_input(page, field, answer, label)
                else:
                    await self._type_into_field(field, answer)
                return True

            return False  # Could not determine an answer

        except Exception as e:
            if "not attached to the DOM" in str(e):
                return True  # stale element from SPA page transition — not a fill failure
            logger.debug(f"    Field fill error (non-fatal): {e}")
            return False

    async def _get_field_label(self, page: Page, field: ElementHandle) -> str:
        """
        Try several strategies to get the human-readable label for a field.
        Priority order: aria-label → label[for] → DOM traversal (aria-labelledby,
        ancestor labels, Workday formLabel) → placeholder (generic values skipped)
        → name attribute.
        """
        _GENERIC_PLACEHOLDERS = {
            "search", "type to search", "select...", "-- select --",
            "- select -", "please select", "choose...",
        }
        try:
            # aria-label (explicit, most reliable)
            aria = await field.get_attribute("aria-label")
            if aria and aria.strip().lower() not in _GENERIC_PLACEHOLDERS:
                return aria.strip()

            # <label for="id"> — check before DOM traversal
            field_id = await field.get_attribute("id") or ""
            if field_id:
                label_el = await page.query_selector(f"label[for='{field_id}']")
                if label_el:
                    text = (await label_el.inner_text()).strip()
                    if text:
                        return text

            # DOM traversal: aria-labelledby, ancestor labels, Workday formLabel
            label_text = await field.evaluate("""
                el => {
                    // aria-labelledby (Workday uses this heavily for comboboxes)
                    const lby = el.getAttribute('aria-labelledby');
                    if (lby) {
                        const texts = lby.split(' ')
                            .map(id => document.getElementById(id))
                            .filter(Boolean)
                            .map(n => n.textContent.trim())
                            .filter(Boolean);
                        if (texts.length) return texts.join(' ');
                    }
                    // Input nested inside a <label> (checkboxes)
                    const ancestor = el.closest('label');
                    if (ancestor) return ancestor.textContent.trim();
                    // Previous sibling <label>
                    let node = el.previousElementSibling;
                    while (node) {
                        if (node.tagName === 'LABEL') return node.textContent.trim();
                        node = node.previousElementSibling;
                    }
                    // Walk up to 5 ancestor levels — find <label> or Workday formLabel
                    let parent = el.parentElement;
                    for (let depth = 0; depth < 5; depth++) {
                        if (!parent) break;
                        const direct = parent.querySelector(':scope > label');
                        if (direct) return direct.textContent.trim();
                        const wdLabel = parent.querySelector('[data-automation-id="formLabel"]');
                        if (wdLabel) return wdLabel.textContent.trim();
                        parent = parent.parentElement;
                    }
                    return '';
                }
            """)
            if label_text:
                return label_text

            # placeholder — last resort; skip generic ATS filler text
            placeholder = await field.get_attribute("placeholder") or ""
            if placeholder and placeholder.strip().lower() not in _GENERIC_PLACEHOLDERS:
                return placeholder.strip()

            # name attribute
            return await field.get_attribute("name") or ""

        except Exception:
            return ""

    async def _fill_select(self, field: ElementHandle, label: str, job: dict = None) -> bool:
        """
        Fill a <select> dropdown.
        Strategy: profile lookup with bidirectional matching → Gemini fallback.
        Returns True if an option was selected, False if no match was found.
        """
        answer = lookup_from_profile(label, self.profile, job) or ""

        # State fields: prefer full name to avoid abbreviation ambiguity
        candidates = [answer] if answer else []
        label_lower = label.lower()
        if re.search(r"\bstate\b|province", label_lower):
            state_abbr = self.profile["personal"].get("state", "")
            state_full = self.profile["personal"].get("state_full", "")
            if state_full and state_abbr:
                candidates = [state_full, state_abbr]
            elif answer:
                candidates = [answer]

        try:
            options = await field.evaluate(
                "el => Array.from(el.options).map(o => ({value: o.value, text: o.text}))"
            )
            # Filter out blank/placeholder entries
            real_options = [
                o for o in options
                if o["value"] and o["text"].strip()
                and o["text"].strip().lower() not in (
                    "select...", "-- select --", "- select -", "select",
                    "please select", "choose...", "",
                )
            ]

            best_match = self._match_option(candidates, options)

            # Gemini fallback: ask AI to pick from available options
            if best_match is None and real_options and job is not None:
                ai_text = await self._ask_ai_for_select(label, real_options, job)
                if ai_text:
                    best_match = self._match_option([ai_text], options)

            if best_match is not None:
                await field.select_option(value=best_match)
                # Country selection often triggers an AJAX reload of the State
                # dropdown; wait for the new options to populate before proceeding.
                if re.search(r"^country", label_lower) and not re.search(r"phone|code|dial", label_lower):
                    await asyncio.sleep(1.5)
                return True

            if real_options:
                logger.debug(
                    f"    No option match for select '{label}' "
                    f"(available: {[o['text'] for o in real_options[:5]]})"
                )
                return False
            return True  # Empty select has no options — not a real failure

        except Exception as e:
            logger.debug(f"    Select fill error: {e}")
            return False

    def _match_option(self, candidates: list, options: list) -> Optional[str]:
        """
        Match candidate answer strings against select options.

        Three passes, in priority order:
          1. Exact match (case-insensitive, unicode-normalised).
          2. Option text is a substring of the candidate — prefer the LONGEST
             such option.  Handles profile answers like "Yes, I am willing to
             relocate." matching a dropdown option "Yes".
          3. Candidate is a substring of the option text — prefer the SHORTEST
             such option.  Handles "United States" matching both "United States"
             and "United States Minor Outlying Islands"; shortest wins so we
             never accidentally land on a territory or outlying-islands entry.
        """

        def norm(s: str) -> str:
            # Collapse all Unicode whitespace (including   non-breaking
            # spaces that Phenom ATS injects) and lowercase.
            return " ".join(s.split()).lower()

        # Short-form candidates: for long answers like "Yes, I am willing to
        # relocate.", prepend just the first word ("Yes") so Pass 1 can do an
        # exact match against simple Yes/No dropdowns.
        _YES_NO = {"yes", "no", "true", "false", "none", "n/a"}
        expanded: list = []
        for cand in candidates:
            if cand and len(cand) > 10:
                first = cand.split(",")[0].split()[0].rstrip(".").lower()
                if first in _YES_NO:
                    expanded.append(first.capitalize())
            expanded.append(cand)

        logger.debug(f"    _match_option: expanded={expanded!r} options_texts={[o['text'] for o in options]!r}")
        for cand in expanded:
            if not cand:
                continue
            cand_n = norm(cand)

            # Pass 1: exact text or value match
            for opt in options:
                if cand_n == norm(opt["text"]) or cand_n == norm(opt["value"]):
                    logger.debug(f"    _match_option P1 hit: cand={cand!r} -> {opt['value']!r}")
                    return opt["value"]

            # Pass 2: option is contained within the candidate string
            p2 = [
                opt for opt in options
                if (norm(opt["text"]) and norm(opt["text"]) in cand_n)
                or (norm(opt["value"]) and norm(opt["value"]) in cand_n)
            ]
            if p2:
                return max(p2, key=lambda o: len(o["text"]))["value"]

            # Pass 3: candidate is contained within the option string
            p3 = [
                opt for opt in options
                if (norm(opt["text"]) and cand_n in norm(opt["text"]))
                or (norm(opt["value"]) and cand_n in norm(opt["value"]))
            ]
            if p3:
                return min(p3, key=lambda o: len(o["text"]))["value"]

        return None

    async def _ask_ai_for_select(self, label: str, options: list, job: dict) -> str:
        """
        Choose a dropdown option when profile lookup fails.
        Checks ai_cache first; validates the cached answer against current options
        so a stale entry from a different site doesn't pick an invalid option.
        Uses human terminal prompt when use_ai=False.
        """
        if not self.use_ai:
            return await self._ask_human_for_select(label, options)

        option_texts = [o["text"] for o in options if o["text"].strip()]

        # ── Cache lookup (validate against current option list) ──────────
        key = f"select:{self._cache_key(label)}"
        cached = self.profile.get("ai_cache", {}).get(key)
        if cached and any(cached.lower() == t.lower() for t in option_texts):
            logger.debug(f"    AI cache hit for select '{label}': '{cached}'")
            return cached

        # ── Call Claude ──────────────────────────────────────────────────
        profile_for_prompt = {k: v for k, v in self.profile.items() if k != "ai_cache"}
        prompt = (
            f"You are filling a job application dropdown for {self.profile['personal']['name']} "
            f"applying to {job['title']} at {job['company']}.\n\n"
            f"Applicant profile:\n{json.dumps(profile_for_prompt, indent=2)}\n\n"
            f'Dropdown question: "{label}"\n\n'
            "Available options:\n"
            + "\n".join(f"- {t}" for t in option_texts)
            + "\n\nPick the single most appropriate option. "
            "Return ONLY the exact option text, nothing else."
        )
        result = await self.claude.complete(prompt, field_label=label)
        if result.strip():
            self._cache_ai_response(key, result.strip())
            return result.strip()
        logger.warning(f"    Claude returned empty for select '{label}' — asking human")
        return await self._ask_human_for_select(label, options)

    async def _fill_choice(self, field: ElementHandle, label: str):
        """Handle radio/checkbox fields."""
        field_type = await field.get_attribute("type")
        if field_type == "checkbox":
            label_lower = label.lower()
            TC_KEYWORDS = [
                "agree", "accept", "consent", "confirm", "acknowledge",
                "certify", "terms", "policy", "privacy", "authorize",
                "understand", "attest", "declaration", "have read",
                "eeo", "eeoc", "background check", "drug",
            ]
            if any(kw in label_lower for kw in TC_KEYWORDS):
                already_checked = await field.is_checked()
                if not already_checked:
                    await field.check()
        # Radio buttons are complex; skip for now unless we can determine the right value

    async def _fill_combobox_input(self, page: Page, field: ElementHandle, answer: str, label: str):
        """
        Fill a combobox input (role='combobox') by typing to filter the dropdown list,
        then clicking the best matching option.  Falls back to plain fill() if no
        listbox appears within 3 s (e.g. the field is autocomplete=off).
        """
        # State comboboxes almost always show full names — prefer state_full over abbreviation.
        label_lower = label.lower()
        if re.search(r"\bstate\b|province", label_lower):
            state_full = self.profile["personal"].get("state_full", "")
            if state_full:
                answer = state_full

        try:
            try:
                await field.click(timeout=3000)
            except Exception:
                await field.evaluate("el => el.click()")
            await asyncio.sleep(0.2)
            await field.fill("")
            # Type the first few characters to trigger the filter dropdown
            type_prefix = answer[:4] if len(answer) > 4 else answer
            await field.type(type_prefix, delay=50)
            await asyncio.sleep(0.8)

            # Wait for the listbox/options to render
            try:
                await page.wait_for_selector("[role='listbox'], [role='option']", timeout=3000)
            except Exception:
                # No dropdown appeared — just commit the full answer as typed text
                await field.fill(answer)
                return

            options = await page.query_selector_all("[role='option']")
            best = None
            answer_lower = answer.lower()
            for opt in options:
                try:
                    opt_text = (await opt.inner_text()).strip().lower()
                    if answer_lower == opt_text:
                        best = opt
                        break
                    if (answer_lower in opt_text or opt_text in answer_lower) and best is None:
                        best = opt
                except Exception:
                    continue

            if best:
                try:
                    # Short timeout so we don't hang 30 s if a sticky footer intercepts
                    await best.click(timeout=3000)
                except Exception:
                    # Workday pageFooter often covers dropdown options — JS click bypasses
                    # the overlay check and fires the event directly on the element
                    await best.evaluate("el => el.click()")
                await asyncio.sleep(0.3)
            else:
                await field.fill(answer)
                await field.press("Tab")

        except Exception as e:
            logger.debug(f"    Combobox fill error for '{label}': {e}")

    async def _check_custom_checkboxes(self, page: Page):
        """
        Handle non-native checkbox widgets (role='checkbox' on non-<input> elements)
        used by some ATS platforms for T&C acceptance at the bottom of form pages.
        """
        TC_KEYWORDS = [
            "agree", "accept", "consent", "confirm", "acknowledge",
            "certify", "terms", "policy", "privacy", "authorize",
            "understand", "attest", "declaration", "have read",
            "eeo", "eeoc", "background check", "drug",
        ]
        custom_cbs = await page.query_selector_all("[role='checkbox']:not(input)")
        for cb in custom_cbs:
            try:
                if not await cb.is_visible():
                    continue
                aria_checked = await cb.get_attribute("aria-checked") or "false"
                if aria_checked.lower() == "true":
                    continue
                label = await self._get_field_label(page, cb)
                if not label:
                    # Widen the search to the sibling/parent text node
                    label = await cb.evaluate("""
                        el => {
                            const next = el.nextElementSibling;
                            if (next) return next.textContent.trim();
                            const parent = el.parentElement;
                            return parent ? parent.textContent.trim() : '';
                        }
                    """)
                if not label:
                    continue
                label_lower = label.lower()
                if any(kw in label_lower for kw in TC_KEYWORDS):
                    logger.debug(f"    Checking custom T&C checkbox: '{label[:60]}'")
                    await cb.click()
                    await asyncio.sleep(0.2)
            except Exception as e:
                logger.debug(f"    Custom checkbox error: {e}")

    async def _type_into_field(self, field: ElementHandle, text: str):
        """Type text into a field with a human-like delay between keystrokes."""
        try:
            await field.click(timeout=3000)
        except Exception:
            # Sticky footer or dropdown overlay may be covering the field —
            # JS click bypasses Playwright's interactability check.
            await field.evaluate("el => el.click()")
        await asyncio.sleep(random.uniform(0.1, 0.3))
        await field.fill("")
        await field.type(text, delay=random.uniform(30, 80))

    async def _handle_add_buttons(self, page: Page, job: dict):
        """
        Detect and click 'Add website / link / URL' expandable-field buttons,
        then fill each newly revealed URL field with the appropriate profile value.

        Workday (and some other ATS) shows optional sections like 'Websites' that
        start collapsed behind an 'Add' button.  The bot has portfolio, GitHub, and
        LinkedIn URLs in the profile — this method clicks the Add button for each
        one and types the URL into the revealed field.
        """
        ADD_BUTTON_RE = re.compile(
            r"\badd\b.*(website|link|url|social|portfolio|web|address)",
            re.IGNORECASE,
        )
        URLS = [
            self.profile["personal"].get("portfolio", ""),
            self.profile["personal"].get("github", ""),
            self.profile["personal"].get("linkedin", ""),
        ]
        urls_to_add = [u for u in URLS if u]
        if not urls_to_add:
            return

        # Collect visible buttons/links whose text matches the add-website pattern
        add_btns = []
        for btn in await page.query_selector_all("button, a[role='button'], [role='button']"):
            try:
                if not await btn.is_visible():
                    continue
                text = (await btn.inner_text()).strip()
                if ADD_BUTTON_RE.search(text):
                    add_btns.append(btn)
            except Exception:
                continue

        if not add_btns:
            return

        # Use the first matching button — click it once per URL we want to add
        add_btn = add_btns[0]
        for url in urls_to_add:
            try:
                # Count existing URL-type inputs before clicking
                before = len(await page.query_selector_all(
                    "input[type='url'], input[type='text'][name*='url'], "
                    "input[type='text'][name*='link'], input[type='text'][name*='website']"
                ))

                try:
                    await add_btn.click(timeout=3000)
                except Exception:
                    await add_btn.evaluate("el => el.click()")
                await asyncio.sleep(0.8)

                # Find the newly revealed URL input(s)
                after_inputs = await page.query_selector_all(
                    "input[type='url'], input[type='text'][name*='url'], "
                    "input[type='text'][name*='link'], input[type='text'][name*='website']"
                )
                new_inputs = after_inputs[before:]

                # If no typed URL input appeared, look for any new visible text input
                if not new_inputs:
                    all_text = await page.query_selector_all("input[type='text']")
                    for inp in reversed(all_text):
                        try:
                            if await inp.is_visible():
                                val = (await inp.evaluate("el => el.value") or "").strip()
                                if not val:
                                    new_inputs = [inp]
                                    break
                        except Exception:
                            continue

                for inp in new_inputs:
                    try:
                        lbl = (await self._get_field_label(page, inp) or "").lower()
                        # Only fill if the label looks URL-related or has no label at all
                        if lbl and not any(kw in lbl for kw in
                                           ("url", "link", "website", "web", "address", "http", "portfolio")):
                            continue
                        logger.info(f"  Add-button field: filling URL '{url}'")
                        await inp.fill("")
                        await inp.type(url, delay=30)
                        await asyncio.sleep(0.3)
                    except Exception as e:
                        logger.debug(f"    Add-button URL fill error: {e}")

            except Exception as e:
                logger.debug(f"    Add-button click error for '{url}': {e}")

    async def _handle_resume_upload(self, page: Page):
        """Detect a file upload input and upload the configured resume PDF (once per application)."""
        if self._resume_uploaded:
            return
        if not self.resume_path.exists():
            logger.warning(f"  Resume not found at {self.resume_path}. Skipping upload.")
            return

        file_inputs = await page.query_selector_all("input[type='file']")
        for inp in file_inputs:
            try:
                accept = await inp.get_attribute("accept") or ""
                label = await self._get_field_label(page, inp) or ""
                is_resume = any(kw in label.lower() for kw in ["resume", "cv", "curriculum"])
                is_pdf_accept = ".pdf" in accept or "application/pdf" in accept or not accept

                if is_resume or is_pdf_accept:
                    logger.info(f"  Uploading resume: {self.resume_path}")
                    await inp.set_input_files(str(self.resume_path))
                    await asyncio.sleep(1)
                    self._resume_uploaded = True
                    break
            except Exception as e:
                logger.debug(f"    Resume upload error: {e}")

    # ── AI / human fallback ──────────────────────────────────────────────────

    async def _ask_human(self, field_label: str) -> str:
        """Pause and ask the user to type an answer for an unmatched text field."""
        print("\n" + "-" * 60)
        print(f"  FIELD NEEDS YOUR INPUT")
        print(f"  Label : {field_label}")
        print(f"  (Profile had no match for this field)")
        print("-" * 60)
        try:
            answer = await asyncio.to_thread(input, "  Your answer (blank to skip): ")
        except EOFError:
            answer = ""
        return answer.strip()

    async def _ask_human_for_select(self, field_label: str, options: list) -> str:
        """Pause and ask the user to pick a dropdown option by number."""
        option_texts = [o["text"] for o in options if o["text"].strip()]
        print("\n" + "-" * 60)
        print(f"  DROPDOWN NEEDS YOUR INPUT")
        print(f"  Label : {field_label}")
        print(f"  Options:")
        for i, text in enumerate(option_texts, 1):
            print(f"    [{i}] {text}")
        print("-" * 60)
        try:
            raw = await asyncio.to_thread(
                input, f"  Pick 1-{len(option_texts)} or type exact text (blank to skip): "
            )
        except EOFError:
            return ""
        raw = raw.strip()
        if not raw:
            return ""
        if raw.isdigit():
            idx = int(raw) - 1
            if 0 <= idx < len(option_texts):
                return option_texts[idx]
        return raw

    async def _ask_ai(self, field_label: str, job: dict) -> str:
        """
        Fill a text field not matched by the profile map.
        Checks ai_cache in profile.json first; calls Claude only on a cache miss.
        Uses human terminal prompt when use_ai=False.
        """
        if not self.use_ai:
            return await self._ask_human(field_label)

        # ── Cache lookup ─────────────────────────────────────────────────
        key = self._cache_key(field_label)
        cached = self.profile.get("ai_cache", {}).get(key)
        if cached:
            logger.debug(f"    AI cache hit for '{field_label}'")
            return cached

        # ── Call Claude ──────────────────────────────────────────────────
        # Build a profile copy that omits the cache itself (no need to send it)
        profile_for_prompt = {k: v for k, v in self.profile.items() if k != "ai_cache"}
        prompt = f"""You are filling out a job application on behalf of this person:

{json.dumps(profile_for_prompt, indent=2)}

Job being applied to: {job['title']} at {job['company']} ({job.get('location', '')})

Fill in the following form field. Write from the first person (as the applicant).
Be concise and professional. 100 words max unless the field clearly asks for more.
Return ONLY the answer text, nothing else.

Field label: "{field_label}"
Answer:"""
        answer = await self.claude.complete(prompt, field_label=field_label)
        if answer:
            self._cache_ai_response(key, answer)
            return answer
        logger.warning(f"    Claude returned empty for '{field_label}' — asking human")
        return await self._ask_human(field_label)

    # ── Submit handling ──────────────────────────────────────────────────────

    async def _do_submit(self, page: Page, job: dict, dry_run: bool, review: bool, submit_locator) -> dict:
        """Final step: optionally pause for review, then click submit."""
        if dry_run:
            logger.info("  [DRY RUN] Not submitting. Application visible in browser.")
            if self.debug:
                await page.screenshot(path=f"screenshots/dryrun_{job['id']}.png")
            send_system_notification(
                "Job Bot: Dry Run Complete",
                f"Dry run for {job['title']} @ {job['company']} — check browser."
            )
            await asyncio.to_thread(input, "  [DRY RUN] Press ENTER to close this application and continue... ")
            return {"status": "dry_run"}

        if review:
            logger.info("  [REVIEW] Please review the application before submitting.")
            send_system_notification(
                "Job Bot: Ready to Submit",
                f"{job['title']} @ {job['company']} — review and press ENTER to submit."
            )
            choice = (await asyncio.to_thread(input, "  Submit? [y/N]: ")).strip().lower()
            if choice != "y":
                return {"status": "skipped", "reason": "user chose not to submit"}

        # Click submit
        logger.info("  Submitting application...")
        if self.debug:
            await page.screenshot(path=f"screenshots/presubmit_{job['id']}.png")

        if isinstance(submit_locator, str):
            btn = page.locator(
                "button[aria-label='Submit application'], "
                "button[data-control-name='submit_unify'], "
                "button[type='submit']"
            ).last
        else:
            btn = submit_locator

        await btn.click()
        await asyncio.sleep(3)

        if self.debug:
            await page.screenshot(path=f"screenshots/postsubmit_{job['id']}.png")

        logger.info("  ✓ Application submitted!")
        return {"status": "applied"}

    # ── Human help request ───────────────────────────────────────────────────

    async def _request_human_help(self, page: Page, job: dict, reason: str):
        """
        Pause execution and ask for human intervention via system notification + terminal.
        The browser window will be visible (debug mode makes it easier to see).
        """
        message = f"Bot paused: {reason}\nJob: {job['title']} @ {job['company']}"
        logger.warning(f"  PAUSED — Human help needed: {reason}")
        send_system_notification("Job Bot: Needs Your Help", message)

        if self.debug:
            await page.screenshot(path=f"screenshots/help_needed_{job['id']}.png")

        print("\n" + "=" * 60)
        print("  BOT PAUSED — HUMAN HELP NEEDED")
        print(f"  Reason: {reason}")
        print(f"  Job: {job['title']} @ {job['company']}")
        print("  The browser window shows the current state.")
        print("  Fix the issue, then press ENTER to continue.")
        print("  (Ctrl+C here will exit the bot entirely)")
        print("=" * 60)

        # Restore the original SIGINT handler so Ctrl+C during this pause
        # exits the bot cleanly instead of just toggling the interrupt flag.
        try:
            signal.signal(signal.SIGINT, self._orig_sigint or signal.SIG_DFL)
        except (OSError, ValueError):
            pass
        try:
            await asyncio.to_thread(input, "  Press ENTER when ready... ")
        except EOFError:
            logger.warning("  Non-interactive stdin — pausing 10 s then auto-continuing")
            await asyncio.sleep(10)
        finally:
            # Re-install our flag-based handler for the rest of the run.
            try:
                signal.signal(signal.SIGINT, self._on_sigint)
            except (OSError, ValueError):
                pass
        await asyncio.sleep(1)
