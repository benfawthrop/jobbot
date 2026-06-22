"""
test_filler.py — Run the filler directly on a single job for debugging.

Edit the JOB dict below, then run:
    python test_filler.py
    python test_filler.py --dry-run
    python test_filler.py --dry-run --debug
"""

import argparse
import asyncio
import json
import logging
import sys
from datetime import datetime
from pathlib import Path

from filler import ApplicationFiller
from state_manager import StateManager


JOB = {
    "id": "4426648526",
    "title": "Junior Embedded Software Development Engineer",
    "company": "Sonos, Inc.",
    "location": "Boston, MA",
    "url": "https://www.linkedin.com/jobs/view/4426648526/",
    "apply_url": "https://sonos.wd1.myworkdayjobs.com/Sonos/job/Boston-MA/Junior-Embedded-Software-Development-Engineer_R2728",
    "easy_apply": False,
}


def parse_args():
    p = argparse.ArgumentParser(description="Run the filler on a single hardcoded job")
    p.add_argument("--dry-run", action="store_true", help="Fill but do not submit")
    p.add_argument("--review", action="store_true", help="Pause before submitting")
    p.add_argument("--debug", action="store_true", help="Verbose logging + visible browser")
    p.add_argument("--no-ai", action="store_true", help="Skip Claude; prompt you in terminal instead")
    return p.parse_args()


async def main():
    args = parse_args()

    level = logging.DEBUG if args.debug else logging.INFO
    log_file = Path("logs") / f"run_{datetime.now().strftime('%Y%m%d_%H%M%S')}.log"
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
    log = logging.getLogger("test_filler")

    for path, name in [("config.json", "config"), ("profile.json", "profile")]:
        if not Path(path).exists():
            log.error(f"{path} not found.")
            sys.exit(1)

    with open("config.json", encoding="utf-8") as f:
        config = json.load(f)
    with open("profile.json", encoding="utf-8") as f:
        profile = json.load(f)

    state_mgr = StateManager()

    if state_mgr.is_apply_url_seen(JOB["apply_url"]):
        log.warning(f"Already applied to this job — ATS URL is in state. Aborting.")
        log.warning(f"  {JOB['apply_url']}")
        log.warning("Edit JOB in test_filler.py to point to a different posting.")
        return

    log.info(f"Testing filler on: {JOB['title']} @ {JOB['company']}")
    log.info(f"URL: {JOB['apply_url']}")
    log.info(f"dry_run={args.dry_run}  review={args.review}  debug={args.debug}")

    async with ApplicationFiller(config, profile, debug=args.debug,
                                  use_ai=not args.no_ai, profile_path="profile.json") as filler:
        result = await filler.apply(job=JOB, dry_run=args.dry_run, review=args.review)

    log.info(f"Result: {result}")

    if result.get("status") == "applied" and not args.dry_run:
        state_mgr.mark_applied(JOB)
        log.info("Recorded in state/applied_jobs.json")


if __name__ == "__main__":
    asyncio.run(main())
