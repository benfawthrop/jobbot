"""
state_manager.py — Persistent State Tracking
=============================================
Keeps a JSON file in state/ that records:
  - Which job IDs you've already applied to (so we never apply twice).
  - Timestamps of when each application was submitted.
  - Run history for debugging.

This file is updated after every successful application and after every run.

If a run is interrupted (Ctrl+C, crash, etc.), the state is saved up to that
point. On the next run, already-applied jobs are skipped automatically.
"""

import json
import logging
from datetime import datetime, timezone
from pathlib import Path
from urllib.parse import urlparse

logger = logging.getLogger(__name__)

STATE_FILE = Path("state/applied_jobs.json")


class StateManager:
    """
    Manages persistent state across bot runs.
    
    Structure of state file:
    {
        "applied_jobs": {
            "job_id_123": {
                "title": "Software Engineer",
                "company": "Acme Corp",
                "applied_at": "2024-01-15T14:32:00Z"
            }
        },
        "run_history": [
            {"run_at": "...", "applied": 3, "errors": 1}
        ]
    }
    """

    def __init__(self, resume_from: str = None):
        self.state_path = Path(resume_from) if resume_from else STATE_FILE
        self.state_path.parent.mkdir(parents=True, exist_ok=True)
        self.state = self._load()
        self._run_start = datetime.now(timezone.utc).isoformat()

    def _load(self) -> dict:
        if self.state_path.exists():
            try:
                with open(self.state_path) as f:
                    return json.load(f)
            except (json.JSONDecodeError, IOError) as e:
                logger.warning(f"Could not load state file: {e}. Starting fresh.")
        return {"applied_jobs": {}, "run_history": []}

    @staticmethod
    def _normalize_url(url: str) -> str:
        """Strip query params and fragment so tracking-param variants match."""
        if not url:
            return ""
        p = urlparse(url)
        return f"{p.scheme}://{p.netloc}{p.path}".rstrip("/").lower()

    def get_seen_job_ids(self) -> set:
        """Return the set of LinkedIn job IDs already applied to. Used by scraper to skip."""
        return set(self.state.get("applied_jobs", {}).keys())

    def is_apply_url_seen(self, apply_url: str) -> bool:
        """Return True if we've already applied via this ATS URL (ignores tracking params)."""
        if not apply_url:
            return False
        normalized = self._normalize_url(apply_url)
        for record in self.state.get("applied_jobs", {}).values():
            if self._normalize_url(record.get("apply_url", "")) == normalized:
                return True
        return False

    def mark_applied(self, job: dict):
        """Record a successfully submitted application."""
        job_id = str(job.get("id", ""))
        if not job_id:
            return
        self.state.setdefault("applied_jobs", {})[job_id] = {
            "title": job.get("title", ""),
            "company": job.get("company", ""),
            "location": job.get("location", ""),
            "url": job.get("url", ""),
            "apply_url": job.get("apply_url", ""),
            "applied_at": datetime.now(timezone.utc).isoformat(),
        }
        # Save immediately so we don't lose it on crash
        self._write()

    def save(self, results: dict = None):
        """
        Save state to disk. Optionally record run summary in history.
        Call this at the end of each run.
        """
        if results:
            self.state.setdefault("run_history", []).append({
                "run_at": self._run_start,
                "applied": len(results.get("applied", [])),
                "skipped": len(results.get("skipped", [])),
                "errors": len(results.get("errors", [])),
            })
            # Keep only last 50 run history entries
            self.state["run_history"] = self.state["run_history"][-50:]
        self._write()

    def _write(self):
        try:
            with open(self.state_path, "w") as f:
                json.dump(self.state, f, indent=2)
        except IOError as e:
            logger.error(f"Failed to save state: {e}")

    def print_stats(self):
        """Print a summary of all-time application stats."""
        applied = self.state.get("applied_jobs", {})
        logger.info(f"All-time applications submitted: {len(applied)}")
        history = self.state.get("run_history", [])
        if history:
            logger.info(f"Total runs recorded: {len(history)}")
