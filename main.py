"""
main.py — Job Bot Orchestrator
==============================
Entry point for the semi-autonomous LinkedIn job application bot.

CLI flags:
  --scrape-only         Run only the scraper; print found jobs, no applications.
  --dry-run             Fill forms but do NOT submit. Pause at the submit button for review.
  --review              Pause and show you the filled application before submitting.
  --limit N             Only process N jobs this run (default: 5, use 0 for unlimited).
  --no-notify           Skip SMS/email notifications.
  --debug               Verbose logging + screenshot on every page.
  --resume-from FILE    Resume from a saved state JSON (see state/ folder).
  --keywords "..."      Override default job search keywords.
  --location "..."      Override default search location.
  --fill-only URL       Skip scraping; run the filler on this external apply URL directly.
  --job-title "..."     Job title label used with --fill-only (default: "Test Job").
  --job-company "..."   Company label used with --fill-only (default: "Test Company").

Examples:
  python main.py --scrape-only                        # Just find jobs, see what's out there
  python main.py --dry-run --limit 1                  # Test one full application without submitting
  python main.py --review --limit 3                   # Apply to 3 jobs but pause before each submit
  python main.py --limit 10                           # Fully autonomous, apply to 10 jobs
  python main.py --fill-only "https://careers.chewy.com/..." --job-title "Software Engineer I" --job-company "Chewy" --dry-run --debug
"""

import argparse
import asyncio
import json
import logging
import sys
from pathlib import Path
from datetime import datetime

from scraper import LinkedInScraper
from filler import ApplicationFiller
from notifier import Notifier
from state_manager import StateManager

# ── Logging setup ────────────────────────────────────────────────────────────
def setup_logging(debug: bool) -> logging.Logger:
    level = logging.DEBUG if debug else logging.INFO
    log_file = Path("logs") / f"run_{datetime.now().strftime('%Y%m%d_%H%M%S')}.log"

    # Windows consoles default to cp1252 which can't encode Unicode symbols like
    # checkmarks. Reconfigure stdout to UTF-8 so log messages don't crash.
    try:
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    except AttributeError:
        pass

    logging.basicConfig(
        level=level,
        format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
        handlers=[
            logging.StreamHandler(sys.stdout),
            logging.FileHandler(log_file, encoding="utf-8"),
        ],
    )
    return logging.getLogger("main")


# ── CLI argument parsing ──────────────────────────────────────────────────────
def parse_args():
    parser = argparse.ArgumentParser(
        description="Semi-autonomous LinkedIn job application bot",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog=__doc__,
    )
    parser.add_argument("--scrape-only", action="store_true",
                        help="Run scraper only, print results, do not apply")
    parser.add_argument("--dry-run", action="store_true",
                        help="Fill forms but never click Submit")
    parser.add_argument("--review", action="store_true",
                        help="Pause before submitting each application for manual review")
    parser.add_argument("--limit", type=int, default=5,
                        help="Max number of jobs to apply to this run (0 = unlimited, default 5)")
    parser.add_argument("--no-notify", action="store_true",
                        help="Disable SMS/email notifications")
    parser.add_argument("--debug", action="store_true",
                        help="Enable verbose logging and per-page screenshots")
    parser.add_argument("--resume-from", type=str, default=None,
                        help="Path to a state JSON file to resume a previous run")
    parser.add_argument("--keywords", type=str, default=None,
                        help='Override job search keywords, e.g. "software engineer"')
    parser.add_argument("--location", type=str, default=None,
                        help='Override search location, e.g. "Boston, MA"')
    parser.add_argument("--no-ai", action="store_true",
                        help="Skip Claude; prompt you in the terminal for every unmatched field")
    parser.add_argument("--fill-only", type=str, default=None, metavar="APPLY_URL",
                        help="Skip scraping; run the filler directly on this external apply URL")
    parser.add_argument("--job-title", type=str, default="Test Job",
                        help='Job title label used with --fill-only (default: "Test Job")')
    parser.add_argument("--job-company", type=str, default="Test Company",
                        help='Company label used with --fill-only (default: "Test Company")')
    return parser.parse_args()


# ── Main ─────────────────────────────────────────────────────────────────────
async def main():
    args = parse_args()
    logger = setup_logging(args.debug)

    # Load config
    config_path = Path("config.json")
    if not config_path.exists():
        logger.error("config.json not found. Run setup or copy config.example.json → config.json")
        sys.exit(1)
    with open(config_path, encoding="utf-8") as f:
        config = json.load(f)

    # Load profile
    profile_path = Path("profile.json")
    if not profile_path.exists():
        logger.error("profile.json not found. Place your profile JSON in the project root.")
        sys.exit(1)
    with open(profile_path, encoding="utf-8") as f:
        profile = json.load(f)

    # Apply CLI overrides to config
    if args.keywords:
        config["search"]["keywords"] = args.keywords
    if args.location:
        config["search"]["location"] = args.location

    logger.info("=" * 60)
    logger.info("  LinkedIn Job Bot Starting")
    if args.fill_only:
        logger.info("  Mode: FILL ONLY (scraper skipped)")
    else:
        logger.info(f"  Mode: {'SCRAPE ONLY' if args.scrape_only else 'DRY RUN' if args.dry_run else 'REVIEW' if args.review else 'FULL AUTO'}")
    logger.info(f"  Job limit: {args.limit if args.limit > 0 else 'unlimited'}")
    logger.info("=" * 60)

    state_mgr = StateManager(resume_from=args.resume_from)
    notifier = Notifier(config, disabled=args.no_notify)

    # ── Fill-only shortcut (skip scraper) ─────────────────────────────────────
    if args.fill_only:
        job = {
            "id": "fill_only_test",
            "title": args.job_title,
            "company": args.job_company,
            "location": "Unknown",
            "url": args.fill_only,
            "apply_url": args.fill_only,
            "easy_apply": False,
        }
        logger.info(f"--fill-only: targeting {args.job_title} @ {args.job_company}")
        logger.info(f"  URL: {args.fill_only}")
        filler = ApplicationFiller(config, profile, debug=args.debug,
                                   use_ai=not args.no_ai, profile_path=str(profile_path))
        async with filler:
            result = await filler.apply(job=job, dry_run=args.dry_run, review=args.review)
        logger.info(f"Fill-only result: {result.get('status')}")
        return

    # ── Phase 1: Scrape ───────────────────────────────────────────────────────
    logger.info("Phase 1: Scraping LinkedIn for job postings...")
    async with LinkedInScraper(config, debug=args.debug) as scraper:
        jobs = await scraper.find_jobs(
            already_seen=state_mgr.get_seen_job_ids(),
            limit=args.limit if args.limit > 0 else None,
        )

    logger.info(f"Found {len(jobs)} new job(s) matching your criteria.")

    if not jobs:
        logger.info("No new jobs found. Try broadening your search keywords or check back later.")
        return

    # Print scrape results
    for i, job in enumerate(jobs, 1):
        logger.info(f"  [{i}] {job['title']} @ {job['company']} — {job['location']}")
        logger.info(f"       {job['url']}")

    if args.scrape_only:
        logger.info("--scrape-only flag set. Stopping before applications.")
        # Save results to a timestamped JSON for review
        out_path = Path("logs") / f"scraped_{datetime.now().strftime('%Y%m%d_%H%M%S')}.json"
        with open(out_path, "w") as f:
            json.dump(jobs, f, indent=2)
        logger.info(f"Scraped jobs saved to {out_path}")
        return

    # ── Phase 2: Apply ────────────────────────────────────────────────────────
    logger.info("Phase 2: Starting application loop...")
    filler = ApplicationFiller(config, profile, debug=args.debug,
                               use_ai=not args.no_ai, profile_path=str(profile_path))

    results = {"applied": [], "skipped": [], "errors": [], "manual": []}

    async with filler:
        for job in jobs:
            logger.info("-" * 50)
            logger.info(f"Applying to: {job['title']} @ {job['company']}")
            logger.info(f"URL: {job['url']}")

            # Skip if we've already applied via this ATS URL (catches re-posts
            # and jobs applied to via test_filler.py or manually).
            if state_mgr.is_apply_url_seen(job.get("apply_url", "")):
                logger.info(f"  [ALREADY APPLIED] Skipping — ATS URL already in state.")
                results["skipped"].append(job)
                continue

            if job.get("easy_apply") or "linkedin.com" in job.get("apply_url", ""):
                logger.info("  [EASY APPLY] Intercepted. Sending direct link for manual application.")
                results["manual"].append(job)

                # Mark as applied in state so the bot doesn't text you about this same job tomorrow
                state_mgr.mark_applied(job)

                if not args.no_notify:
                    await notifier.send_manual_action(job)
                continue  # Skip the rest of the loop and move to the next job

            try:
                result = await filler.apply(
                    job=job,
                    dry_run=args.dry_run,
                    review=args.review,
                )

                if result["status"] == "applied":
                    results["applied"].append(job)
                    state_mgr.mark_applied(job)
                    logger.info(f"✓ Successfully applied to {job['title']} @ {job['company']}")
                    if not args.no_notify:
                        await notifier.send_success(job)

                elif result["status"] == "dry_run":
                    results["skipped"].append(job)
                    logger.info(f"  [DRY RUN] Form filled but not submitted for {job['title']}")

                elif result["status"] == "skipped":
                    results["skipped"].append(job)
                    logger.info(f"  Skipped: {result.get('reason', 'user choice')}")

                elif result["status"] == "error":
                    results["errors"].append({**job, "error": result.get("error")})
                    logger.error(f"✗ Error applying to {job['title']}: {result.get('error')}")
                    if not args.no_notify:
                        await notifier.send_error(job, result.get("error", "Unknown error"))

            except KeyboardInterrupt:
                logger.info("Interrupted by user. Saving state and exiting.")
                state_mgr.save()
                sys.exit(0)
            except Exception as e:
                logger.exception(f"Unexpected error on {job['title']}: {e}")
                results["errors"].append({**job, "error": str(e)})

    # ── Summary ───────────────────────────────────────────────────────────────
    logger.info("=" * 60)
    logger.info("Run complete.")
    logger.info(f"  Applied:  {len(results['applied'])}")
    logger.info(f"  Skipped:  {len(results['skipped'])}")
    logger.info(f"  Errors:   {len(results['errors'])}")
    logger.info("=" * 60)

    state_mgr.save()

    # Final summary notification
    if not args.no_notify and results["applied"]:
        await notifier.send_summary(results)


if __name__ == "__main__":
    asyncio.run(main())
