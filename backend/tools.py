"""
Pure-Python tool implementations for the USB Assistant backend.
No external service dependencies — works fully offline.
"""
import asyncio
import json
import logging
import re
import time
import urllib.request
from pathlib import Path
from typing import Any

import httpx
from apscheduler.schedulers.asyncio import AsyncIOScheduler
from apscheduler.triggers.cron import CronTrigger

logger = logging.getLogger("usb_assistant")

# ---------------------------------------------------------------------------
# Scheduler (singleton, initialised in main.py lifespan)
# ---------------------------------------------------------------------------

scheduler = AsyncIOScheduler()


# ---------------------------------------------------------------------------
# Tool: watch_folder
# ---------------------------------------------------------------------------

async def watch_folder(folder_path: str, timeout_seconds: int = 30) -> dict:
    """
    Poll a folder for new or modified files for up to timeout_seconds.
    Returns the first new file path found, or a timeout message.
    """
    folder = Path(folder_path).expanduser()
    if not folder.exists():
        folder.mkdir(parents=True, exist_ok=True)

    before = {p: p.stat().st_mtime for p in folder.iterdir() if p.is_file()}
    deadline = time.monotonic() + timeout_seconds
    poll_interval = 1.0

    while time.monotonic() < deadline:
        await asyncio.sleep(poll_interval)
        current = {p: p.stat().st_mtime for p in folder.iterdir() if p.is_file()}
        new_files = [str(p) for p in current if p not in before]
        if new_files:
            return {"status": "triggered", "new_files": new_files, "folder": str(folder)}
        modified = [str(p) for p, mt in current.items() if before.get(p) and mt > before[p]]
        if modified:
            return {"status": "modified", "modified_files": modified, "folder": str(folder)}

    return {"status": "timeout", "folder": str(folder), "timeout_seconds": timeout_seconds}


# ---------------------------------------------------------------------------
# Tool: browser_scrape
# ---------------------------------------------------------------------------

def _strip_html(html: str) -> str:
    """Remove tags, collapse whitespace, return plain text."""
    text = re.sub(r"<script[^>]*>.*?</script>", " ", html, flags=re.DOTALL | re.IGNORECASE)
    text = re.sub(r"<style[^>]*>.*?</style>", " ", text, flags=re.DOTALL | re.IGNORECASE)
    text = re.sub(r"<[^>]+>", " ", text)
    text = re.sub(r"\s{2,}", " ", text).strip()
    return text


async def browser_scrape(url: str, instructions: str) -> dict:
    """
    Fetch web page content and return AI-readable plain text.
    Uses Playwright (headless Chromium) when available, otherwise urllib.
    Playwright handles JS-rendered pages; urllib handles static HTML only.
    """
    # Try Playwright first (optional dependency)
    try:
        from playwright.async_api import async_playwright  # type: ignore
        async with async_playwright() as p:
            browser = await p.chromium.launch(headless=True)
            page = await browser.new_page()
            await page.goto(url, timeout=20000, wait_until="domcontentloaded")
            await asyncio.sleep(2)
            html = await page.content()
            await browser.close()
        text = _strip_html(html)
        return {
            "url": url,
            "instructions": instructions,
            "snapshot": {"text": text[:10000]},
            "source": "playwright",
        }
    except ImportError:
        pass  # Playwright not installed — fall through to urllib
    except Exception as e:
        logger.warning("Playwright scrape failed (%s), falling back to urllib", e)

    # Fallback: plain HTTP fetch via urllib
    try:
        req = urllib.request.Request(url, headers={"User-Agent": "Mozilla/5.0"})
        with urllib.request.urlopen(req, timeout=20) as resp:
            html = resp.read().decode("utf-8", errors="replace")
        text = _strip_html(html)
        return {
            "url": url,
            "instructions": instructions,
            "snapshot": {"text": text[:8000]},
            "source": "urllib",
        }
    except Exception as e:
        return {"url": url, "instructions": instructions, "error": str(e), "source": "failed"}


# ---------------------------------------------------------------------------
# Tool: schedule_reminder
# ---------------------------------------------------------------------------

_BACKEND_URL = "http://localhost:8081"


async def _reminder_job(name: str, message: str) -> None:
    """Callback executed by APScheduler — sends a system message to /chat."""
    logger.info("Reminder fired: %s — %s", name, message)
    try:
        async with httpx.AsyncClient(timeout=30.0) as client:
            await client.post(
                f"{_BACKEND_URL}/chat",
                json={
                    "message": f"[定时提醒] {message}",
                    "history": [],
                    "shop_config": {},
                },
            )
    except Exception as e:
        logger.warning("Reminder job %s failed to POST /chat: %s", name, e)


async def schedule_reminder(cron_expr: str, message: str, name: str = "") -> dict:
    """
    Schedule a recurring reminder using a 5-field cron expression.
    Triggers by POSTing a system message to /chat.
    """
    job_id = name or f"reminder_{int(time.time())}"

    # Replace existing job with same id if any
    if scheduler.get_job(job_id):
        scheduler.remove_job(job_id)

    try:
        fields = cron_expr.strip().split()
        if len(fields) != 5:
            return {"status": "error", "error": "cron_expr must have 5 fields"}
        minute, hour, day, month, day_of_week = fields
        trigger = CronTrigger(
            minute=minute,
            hour=hour,
            day=day,
            month=month,
            day_of_week=day_of_week,
        )
        scheduler.add_job(
            _reminder_job,
            trigger=trigger,
            id=job_id,
            kwargs={"name": job_id, "message": message},
            replace_existing=True,
        )
        return {"status": "scheduled", "job_id": job_id, "cron": cron_expr, "message": message}
    except Exception as e:
        return {"status": "error", "error": str(e)}


async def list_reminders() -> dict:
    """Return all scheduled reminder jobs."""
    jobs = []
    for job in scheduler.get_jobs():
        jobs.append({
            "job_id": job.id,
            "next_run": str(job.next_run_time) if job.next_run_time else None,
            "trigger": str(job.trigger),
        })
    return {"jobs": jobs, "count": len(jobs)}


async def cancel_reminder(job_id: str) -> dict:
    """Cancel a scheduled reminder by job_id."""
    if scheduler.get_job(job_id):
        scheduler.remove_job(job_id)
        return {"status": "cancelled", "job_id": job_id}
    return {"status": "not_found", "job_id": job_id}


def scheduler_status() -> str:
    """Return 'running' or 'stopped'."""
    return "running" if scheduler.running else "stopped"


# ---------------------------------------------------------------------------
# Tool registry (used by main.py for LLM function-calling)
# ---------------------------------------------------------------------------

TOOL_DEFINITIONS = [
    {
        "name": "watch_folder",
        "description": (
            "Watch a local folder for new or modified files. "
            "Returns when a new file appears or after timeout_seconds. "
            "Use this when the user asks to monitor a directory."
        ),
        "parameters": {
            "type": "object",
            "properties": {
                "folder_path": {"type": "string", "description": "Absolute or ~ path to folder"},
                "timeout_seconds": {
                    "type": "integer",
                    "description": "How long to wait (default 30)",
                    "default": 30,
                },
            },
            "required": ["folder_path"],
        },
    },
    {
        "name": "browser_scrape",
        "description": (
            "Fetch a web page and return its plain-text content for analysis. "
            "Uses headless Chromium (Playwright) when available, otherwise plain HTTP. "
            "Use this for web searches or reading web pages."
        ),
        "parameters": {
            "type": "object",
            "properties": {
                "url": {"type": "string", "description": "Full URL to fetch"},
                "instructions": {
                    "type": "string",
                    "description": "What information to extract from the page",
                },
            },
            "required": ["url", "instructions"],
        },
    },
    {
        "name": "schedule_reminder",
        "description": (
            "Schedule a recurring reminder using a 5-field cron expression. "
            "The message will be delivered as a chat message at the scheduled time."
        ),
        "parameters": {
            "type": "object",
            "properties": {
                "cron_expr": {
                    "type": "string",
                    "description": "5-field cron expression, e.g. '0 9 * * 1-5' (weekdays at 9am)",
                },
                "message": {"type": "string", "description": "Reminder message text"},
                "name": {"type": "string", "description": "Optional unique job name"},
            },
            "required": ["cron_expr", "message"],
        },
    },
]

TOOL_HANDLERS: dict[str, Any] = {
    "watch_folder": watch_folder,
    "browser_scrape": browser_scrape,
    "schedule_reminder": schedule_reminder,
}
