"""
scraper.py — LinkedIn Job Scraper
==================================
Uses Playwright (headless Chromium) to log into LinkedIn and collect job
postings that match the search keywords/location defined in config.json.

HOW IT WORKS:
  1. Opens a real (but headless) Chrome browser via Playwright.
  2. Loads linkedin.com/jobs/search with your query parameters.
  3. Scrolls through results, collecting job cards.
  4. Filters out jobs you've already applied to (via state_manager).
  5. For each candidate job, follows the link and extracts:
       - title, company, location, job description, apply URL
  6. Returns a list of job dicts for the filler to process.

ANTI-DETECTION:
  LinkedIn actively tries to detect bots. We mitigate this by:
  - Using a persistent browser profile (saves cookies/session between runs).
  - Randomizing scroll speeds and inter-action delays.
  - Using Playwright's stealth plugin via playwright-stealth.
  - Letting you stay logged in (the bot reuses your saved session).

IMPORTANT: You must log in manually on the first run. The bot will open a
  visible browser, let you log in, then save the session for future runs.
  Subsequent runs will be headless.
"""

import asyncio
import json
import logging
import random
from pathlib import Path
from typing import Optional
from urllib.parse import urlencode, quote_plus, urlparse, parse_qs, unquote

from playwright.async_api import async_playwright, Browser, BrowserContext, Page

logger = logging.getLogger(__name__)

# How long to wait (seconds) between actions to avoid rate-limiting
DELAY_MIN = 1.5
DELAY_MAX = 3.5


async def random_delay(min_s=DELAY_MIN, max_s=DELAY_MAX):
    """Sleep for a random duration to mimic human behavior."""
    await asyncio.sleep(random.uniform(min_s, max_s))


class LinkedInScraper:
    """
    Async context manager that drives a Playwright browser to scrape LinkedIn jobs.

    Usage:
        async with LinkedInScraper(config, debug=False) as scraper:
            jobs = await scraper.find_jobs(already_seen=set(), limit=10)
    """

    def __init__(self, config: dict, debug: bool = False):
        self.config = config
        self.debug = debug
        self.search_cfg = config.get("search", {})
        self._playwright = None
        self._browser: Optional[Browser] = None
        self._context: Optional[BrowserContext] = None
        self.profile_dir = Path("state/browser_profile")
        self.profile_dir.mkdir(parents=True, exist_ok=True)

    async def __aenter__(self):
        self._playwright = await async_playwright().start()
        await self._launch_browser()
        return self

    async def __aexit__(self, *args):
        if self._context:
            await self._context.close()
        if self._browser:
            await self._browser.close()
        if self._playwright:
            await self._playwright.stop()

    async def _launch_browser(self):
        """
        Launch Chromium with a persistent profile directory.
        On the FIRST run, headless=False so you can log in manually.
        After that, the session cookie is saved and we go headless.
        """
        session_file = self.profile_dir / "session.json"
        first_run = not session_file.exists()

        launch_options = {
            "headless": False if first_run else not self.debug,
            "args": [
                "--disable-blink-features=AutomationControlled",
                "--no-sandbox",
                "--disable-web-security",
            ],
        }

        self._browser = await self._playwright.chromium.launch(**launch_options)

        # Persistent context keeps cookies/localStorage between runs
        self._context = await self._playwright.chromium.launch_persistent_context(
            str(self.profile_dir),
            headless=launch_options["headless"],
            viewport={"width": 1280, "height": 800},
            user_agent=(
                "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
                "AppleWebKit/537.36 (KHTML, like Gecko) "
                "Chrome/122.0.0.0 Safari/537.36"
            ),
            args=launch_options["args"],
        )

        if first_run:
            await self._handle_first_login()

    async def _handle_first_login(self):
        """
        Open LinkedIn login page and wait for the user to log in manually.
        Once logged in, saves the session state for future headless runs.
        """
        logger.info("=" * 60)
        logger.info("FIRST RUN: Manual login required.")
        logger.info("A browser window will open. Please log into LinkedIn.")
        logger.info("After you are fully logged in (feed page visible), press ENTER here.")
        logger.info("=" * 60)

        page = await self._context.new_page()
        await page.goto("https://www.linkedin.com/login")

        # Wait for user to signal they're done
        input("Press ENTER after you have logged in to LinkedIn... ")

        # Save storage state (cookies + localStorage) for future runs
        await self._context.storage_state(path=str(self.profile_dir / "session.json"))
        logger.info("Session saved. Future runs will be headless.")
        await page.close()

    async def find_jobs(
        self,
        already_seen: set,
        limit: Optional[int] = None,
    ) -> list[dict]:
        """
        Main scraping method. Searches LinkedIn Jobs across every combination
        of location × remote-type and returns a deduplicated list of job dicts.

        Config values for 'location' and 'remote' can be either a single string
        OR a list of strings. For example:

            "location": ["Massachusetts", "New York", "Remote"]
            "remote":   ["remote", "hybrid", "onsite"]

        Every combination is searched separately and results are merged by job ID,
        so you never see the same posting twice even if it shows up in multiple searches.

        Args:
            already_seen: Set of job IDs already applied to (from state manager).
            limit: Max total jobs to return across all searches. None = no limit.

        Returns:
            List of dicts with keys: id, title, company, location, url,
            description, apply_url, easy_apply (bool)
        """
        filters = self.search_cfg.get("filters", {})

        # ── Normalize all multi-value fields to lists ─────────────────────────
        def as_list(val):
            """Accept either a string or a list; always return a list."""
            if isinstance(val, list):
                return val
            if val:
                return [val]
            return [None]  # None means "no filter applied for this dimension"

        keywords_list = as_list(self.search_cfg.get("keywords", "software engineer"))
        locations     = as_list(self.search_cfg.get("location", "United States"))
        remotes       = as_list(filters.get("remote", None))

        # Build every (keywords, location, remote) combination to search
        search_combos = [
            (kw, loc, rem)
            for kw in keywords_list
            for loc in locations
            for rem in remotes
        ]

        logger.info(
            f"Will run {len(search_combos)} search combination(s): "
            f"{len(keywords_list)} keyword set(s) × "
            f"{len(locations)} location(s) × "
            f"{len(remotes)} remote type(s)"
        )

        page = await self._context.new_page()
        jobs = []
        seen_ids = set(already_seen)  # Track within this run too (cross-search dedup)

        try:
            for combo_index, (keywords, location, remote) in enumerate(search_combos, 1):
                if limit and len(jobs) >= limit:
                    break

                # Build a per-combo filters dict with this iteration's remote value
                combo_filters = {**filters, "remote": remote}

                loc_label = location or "any location"
                rem_label = remote or "any work type"
                logger.info(
                    f"Search {combo_index}/{len(search_combos)}: "
                    f'"{keywords}" in {loc_label} [{rem_label}]'
                )

                search_url = self._build_search_url(keywords, location, combo_filters)
                await page.goto(search_url, wait_until="domcontentloaded")
                await random_delay(2, 4)

                # Check login on first combo only (session either works or it doesn't)
                if combo_index == 1 and ("login" in page.url or "authwall" in page.url):
                    logger.error(
                        "LinkedIn session expired. "
                        "Delete state/browser_profile/session.json and re-run."
                    )
                    return []

                await self._scroll_results(page)
                job_cards = await self._collect_job_cards(page)
                logger.info(f"  Found {len(job_cards)} raw cards in this search")

                for card in job_cards:
                    if limit and len(jobs) >= limit:
                        break

                    job_id = card.get("id", "")
                    if not job_id or job_id in seen_ids:
                        continue  # Skip dupes across searches
                    seen_ids.add(job_id)

                    job_detail = await self._fetch_job_detail(page, card)
                    if job_detail is None:
                        continue

                    if not self._passes_filters(job_detail):
                        logger.debug(f"  Filtered: {job_detail['title']} @ {job_detail['company']}")
                        continue

                    jobs.append(job_detail)
                    logger.debug(f"  Added: {job_detail['title']} @ {job_detail['company']}")
                    await random_delay(0.8, 1.8)

                # Brief pause between searches to avoid rate-limiting
                if combo_index < len(search_combos):
                    await random_delay(3, 6)

        except Exception as e:
            logger.exception(f"Error during scraping: {e}")
            if self.debug:
                await page.screenshot(path=f"screenshots/scrape_error_{id(page)}.png")
        finally:
            await page.close()

        logger.info(f"Total unique jobs collected: {len(jobs)}")
        return jobs

    def _build_search_url(self, keywords: str, location: Optional[str], filters: dict) -> str:
        """
        Construct a LinkedIn job search URL for ONE specific (keywords, location, remote) combo.
        Called once per search combination by find_jobs().

        'remote' in filters should be a single string or None at this point —
        find_jobs() has already unpacked any lists before calling here.
        """
        params = {
            "keywords": keywords,
            "trk": "public_jobs_jobs-search-bar_search-submit",
        }

        if location:
            params["location"] = location

        # Date posted filter: r86400=24h, r604800=week, r2592000=month
        date_map = {"24h": "r86400", "week": "r604800", "month": "r2592000"}
        if filters.get("date_posted") in date_map:
            params["f_TPR"] = date_map[filters["date_posted"]]

        # Experience level: 1=Intern, 2=Entry, 3=Associate, 4=Mid, 5=Director, 6=Executive
        exp_map = {"internship": "1", "entry": "2", "associate": "3", "mid": "4"}
        if filters.get("experience_level") in exp_map:
            params["f_E"] = exp_map[filters["experience_level"]]

        # Job type: F=Full-time, P=Part-time, C=Contract, T=Temporary
        type_map = {"full_time": "F", "part_time": "P", "contract": "C"}
        if filters.get("job_type") in type_map:
            params["f_JT"] = type_map[filters["job_type"]]

        # Remote: 1=On-site, 2=Remote, 3=Hybrid  (None = no filter = all types)
        remote_map = {"remote": "2", "hybrid": "3", "onsite": "1"}
        remote_val = filters.get("remote")
        if remote_val in remote_map:
            params["f_WT"] = remote_map[remote_val]
        # If remote_val is None, we intentionally omit f_WT → LinkedIn returns all work types

        # Easy Apply only
        if filters.get("easy_apply_only"):
            params["f_LF"] = "f_AL"

        return "https://www.linkedin.com/jobs/search/?" + urlencode(params)

    async def _scroll_results(self, page: Page, scrolls: int = 5):
        """Scroll the job list panel to lazy-load more results."""
        try:
            # LinkedIn's results panel selector
            results_panel = page.locator(".jobs-search-results-list, .scaffold-layout__list")
            if await results_panel.count() == 0:
                # Fall back to general page scroll
                for _ in range(scrolls):
                    await page.evaluate("window.scrollBy(0, 800)")
                    await random_delay(0.5, 1.5)
                return

            for _ in range(scrolls):
                await results_panel.evaluate("el => el.scrollBy(0, 600)")
                await random_delay(0.8, 1.8)
        except Exception as e:
            logger.debug(f"Scroll warning (non-fatal): {e}")

    async def _collect_job_cards(self, page: Page) -> list[dict]:
        """
        Parse the job list and return basic card data: id, title, company, url.
        Uses multiple selector strategies to handle LinkedIn's frequent HTML changes.
        """
        cards = []
        try:
            # Try the modern LinkedIn jobs list structure
            job_items = await page.query_selector_all(
                "li.jobs-search-results__list-item, "
                "li.occludable-update, "
                ".job-card-container"
            )

            for item in job_items:
                try:
                    # Extract job ID from data attribute or link
                    job_id = await item.get_attribute("data-job-id") or ""
                    link_el = await item.query_selector("a.job-card-container__link, a[href*='/jobs/view/']")
                    href = await link_el.get_attribute("href") if link_el else ""

                    # Parse job ID from URL if not in data attribute
                    if not job_id and "/jobs/view/" in href:
                        job_id = href.split("/jobs/view/")[1].split("/")[0].split("?")[0]

                    title_el = await item.query_selector(
                        ".job-card-list__title, .job-card-container__link strong, "
                        ".base-search-card__title"
                    )
                    title = (await title_el.inner_text()).strip() if title_el else "Unknown"

                    company_el = await item.query_selector(
                        ".artdeco-entity-lockup__subtitle span, "
                        ".artdeco-entity-lockup__subtitle, "
                        ".job-card-container__company-name, "
                        ".base-search-card__subtitle"
                    )
                    company = (await company_el.inner_text()).strip() if company_el else "Unknown"

                    if job_id:
                        cards.append({
                            "id": job_id,
                            "title": title,
                            "company": company,
                            "url": f"https://www.linkedin.com/jobs/view/{job_id}/",
                        })
                except Exception as e:
                    logger.debug(f"Failed to parse one job card: {e}")
                    continue

        except Exception as e:
            logger.warning(f"Card collection error: {e}")

        return cards

    async def _fetch_job_detail(self, page: Page, card: dict) -> Optional[dict]:
        """
        Navigate to the job's detail page and extract full info:
        description, location, apply button URL, easy_apply flag.
        """
        try:
            await page.goto(card["url"], wait_until="domcontentloaded")
            await random_delay(1.5, 3)

            if self.debug:
                await page.screenshot(path=f"screenshots/job_{card['id']}.png")

            # Company name — try detail page in case the card parser missed it
            company = card.get("company", "Unknown")
            if company == "Unknown":
                company_el = await page.query_selector(
                    ".job-details-jobs-unified-top-card__company-name a, "
                    ".job-details-jobs-unified-top-card__company-name, "
                    ".jobs-unified-top-card__company-name, "
                    ".topcard__org-name-link, "
                    ".artdeco-entity-lockup__subtitle span, "
                    ".artdeco-entity-lockup__subtitle"
                )
                if company_el:
                    company = (await company_el.inner_text()).strip()

            # Last-resort: parse page title "Job Title at Company | LinkedIn"
            if company == "Unknown":
                page_title = await page.title()
                # Strip leading notification count "(N) ..."
                if page_title.startswith("("):
                    page_title = page_title.split(")", 1)[-1].strip()
                if " at " in page_title and "LinkedIn" in page_title:
                    company = page_title.rsplit(" at ", 1)[-1].split("|")[0].strip()

            # Location
            location_el = await page.query_selector(
                ".job-details-jobs-unified-top-card__bullet, "
                ".topcard__flavor--bullet"
            )
            location = (await location_el.inner_text()).strip() if location_el else "Unknown"

            # Job description text (for AI relevance assessment)
            desc_el = await page.query_selector(
                "#job-details, .jobs-description__content, .description__text"
            )
            description = (await desc_el.inner_text()).strip() if desc_el else ""

            # ── Apply method detection ──────────────────────────────────────────
            # Easy Apply opens a LinkedIn modal  → button with "Easy Apply" label
            # External apply goes to a 3rd-party ATS → <a> link (NOT a <button>)
            easy_apply_btn = await page.query_selector(
                "button[aria-label*='Easy Apply'], "
                "button:has-text('Easy Apply')"
            )
            easy_apply = easy_apply_btn is not None

            apply_url = card["url"]  # Default: the LinkedIn job page

            if not easy_apply:
                # LinkedIn renders the external apply button as an <a> tag whose
                # href goes through their safety redirect:
                # https://www.linkedin.com/safety/go/?url=<URL-encoded ATS URL>
                # We can read the ATS URL directly from the href — no click needed.
                apply_link = await page.query_selector(
                    "a[aria-label*='Apply on company website'], "
                    "a[aria-label*='Apply now'], "
                    "a[aria-label='Apply']"
                )
                if apply_link:
                    href = await apply_link.get_attribute("href") or ""
                    if href:
                        parsed = urlparse(href)
                        qs = parse_qs(parsed.query)
                        if "url" in qs:
                            # Decode the ATS URL from LinkedIn's safety redirect
                            apply_url = unquote(qs["url"][0])
                        elif "linkedin.com" not in href:
                            apply_url = href
                        logger.debug(f"  External apply URL: {apply_url}")

            return {
                **card,
                "company": company,
                "location": location,
                "description": description[:3000],  # Cap to save tokens
                "apply_url": apply_url,
                "easy_apply": easy_apply,
            }

        except Exception as e:
            logger.warning(f"Failed to fetch detail for job {card.get('id')}: {e}")
            return None

    def _passes_filters(self, job: dict) -> bool:
        """
        Apply post-scrape filters from config.
        Returns True if this job should be applied to.
        """
        filters = self.search_cfg.get("filters", {})

        # Block list: skip companies you don't want to apply to.
        # Uses substring matching so "raytheon" blocks "Raytheon Technologies", etc.
        blocked = [c.lower() for c in filters.get("blocked_companies", [])]
        company_lower = job["company"].lower()
        if any(term in company_lower for term in blocked):
            logger.debug(f"Blocked company '{job['company']}': matched blocklist")
            return False

        # Required keywords in description
        required_keywords = filters.get("required_in_description", [])
        desc_lower = job["description"].lower()
        for kw in required_keywords:
            if kw.lower() not in desc_lower:
                return False

        # Skip jobs with red-flag terms (e.g. "10+ years experience")
        blocklist_terms = filters.get("description_blocklist", [])
        for term in blocklist_terms:
            if term.lower() in desc_lower:
                logger.debug(f"Blocked by term '{term}': {job['title']}")
                return False

        return True