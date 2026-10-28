#!/usr/bin/env python3
"""HR holiday reminder bot for Slack.

Reads UK bank holidays for a given year from GOV.UK and sends reminders
to employees, using UK time (Europe/London) for every date calculation.
"""
import argparse
import json
import logging
import os
import time
from dataclasses import dataclass
from datetime import date, datetime
from pathlib import Path
from zoneinfo import ZoneInfo

import requests
from apscheduler.schedulers.blocking import BlockingScheduler
from apscheduler.triggers.cron import CronTrigger
from dotenv import load_dotenv
from slack_sdk import WebClient
from slack_sdk.errors import SlackApiError

load_dotenv()

UK_TZ = ZoneInfo("Europe/London")
HOLIDAY_URL = "https://www.gov.uk/bank-holidays.json"
BASE = Path(__file__).parent
CACHE_FILE = BASE / "holidays_cache.json"
SENT_LOG = BASE / "sent_log.json"

log = logging.getLogger("hr-holiday-bot")


# ---------- config ----------
def cfg(name, default=None):
    return os.getenv(name, default)


YEAR = int(cfg("HOLIDAY_YEAR", "2027"))
REGION = cfg("HOLIDAY_REGION", "england-and-wales")
SEND_MODE = cfg("SEND_MODE", "channel")
CHANNEL_ID = cfg("SLACK_CHANNEL_ID")
OFFSETS = sorted({int(x) for x in cfg("REMIND_DAYS_BEFORE", "14,7,1").split(",") if x.strip()}, reverse=True)
SEND_HOUR = int(cfg("SEND_HOUR", "9"))
OFFICE_NOTE = cfg("OFFICE_NOTE", "")
SENDER_NAME = cfg("HR_SENDER_NAME", "")
USER_TOKEN = cfg("SLACK_USER_TOKEN")

@dataclass
class Holiday:
    title: str
    day: date
    notes: str = ""


# ---------- holidays ----------
def load_holidays(year=YEAR, region=REGION):
    """Fetch holidays from GOV.UK, falling back to the last cached copy."""
    try:
        r = requests.get(HOLIDAY_URL, timeout=15)
        r.raise_for_status()
        data = r.json()
        CACHE_FILE.write_text(json.dumps(data))
    except Exception as exc:
        log.warning("Could not fetch GOV.UK data (%s); trying cache", exc)
        if not CACHE_FILE.exists():
            raise
        data = json.loads(CACHE_FILE.read_text())

    holidays = [
        Holiday(e["title"], date.fromisoformat(e["date"]), e.get("notes", ""))
        for e in data[region]["events"]
        if date.fromisoformat(e["date"]).year == year
    ]
    if not holidays:
        log.warning("No holidays found for %s in %s. GOV.UK may not have published it yet.", year, region)
    return sorted(holidays, key=lambda h: h.day)


# ---------- messages ----------
def fmt_date(d):
    return f"{d:%A}, {d.day} {d:%B %Y}"


def when_phrase(offset):
    if offset == 0:
        return "That is *today*"
    if offset == 1:
        return "That is *tomorrow*"
    return f"That is in *{offset} days*"


def build_message(h, offset):
    if offset == 0:
        when = "today"
    elif offset == 1:
        when = "tomorrow"
    else:
        when = f"in {offset} days"
    lines = [
        "Hi team,",
        f"Just a quick heads-up that {h.title} is {when}, on {fmt_date(h.day)}.",
    ]
    if h.notes:
        lines.append(f"({h.notes})")
    if OFFICE_NOTE:
        lines.append(OFFICE_NOTE)
    lines.append("Warm regards,\nHR Team")
    return "\n\n".join(lines)


# ---------- slack ----------
def post(client, channel, text, dry_run):
    if dry_run:
        log.info("[dry-run] -> %s\n%s\n", channel, text)
        return
    kwargs = {"channel": channel, "text": text}
    if SENDER_NAME and not USER_TOKEN:
        kwargs["username"] = SENDER_NAME
    for attempt in range(3):
        try:
            client.chat_postMessage(**kwargs)
            return
        except SlackApiError as e:
            if e.response.status_code == 429:
                time.sleep(int(e.response.headers.get("Retry-After", 5)))
                continue
            if "missing_scope" in str(e) and "username" in kwargs:
                kwargs.pop("username")  # chat:write.customize not granted
                continue
            raise


def dm_recipients(client):
    ids, cursor = [], None
    while True:
        resp = client.users_list(limit=200, cursor=cursor)
        for u in resp["members"]:
            if u.get("deleted") or u.get("is_bot") or u["id"] == "USLACKBOT":
                continue
            if u.get("is_restricted") or u.get("is_ultra_restricted"):  # guests
                continue
            ids.append(u["id"])
        cursor = resp.get("response_metadata", {}).get("next_cursor")
        if not cursor:
            return ids


# ---------- sent log (prevents duplicates if the script restarts) ----------
def already_sent(key):
    return SENT_LOG.exists() and key in json.loads(SENT_LOG.read_text())


def mark_sent(key):
    data = json.loads(SENT_LOG.read_text()) if SENT_LOG.exists() else []
    data.append(key)
    SENT_LOG.write_text(json.dumps(data))


# ---------- main job ----------
def run_check(today=None, dry_run=False):
    today = today or datetime.now(UK_TZ).date()
    log.info("Checking holidays for UK date %s", today)
    client = WebClient(token=USER_TOKEN or cfg("SLACK_BOT_TOKEN"))

    for h in load_holidays():
        offset = (h.day - today).days
        if offset not in OFFSETS:
            continue
        key = f"{REGION}:{h.day}:{offset}:{SEND_MODE}"
        if already_sent(key) and not dry_run:
            log.info("Already sent: %s", key)
            continue

        text = build_message(h, offset)
        if SEND_MODE == "dm":
            if dry_run:
                post(client, "<every active employee>", text, True)
            else:
                for uid in dm_recipients(client):
                    dm = client.conversations_open(users=uid)["channel"]["id"]
                    post(client, dm, text, False)
                    time.sleep(1.2)  # stay under Slack rate limits
        else:
            post(client, CHANNEL_ID, text, dry_run)

        if not dry_run:
            mark_sent(key)
        log.info("Handled %s (%d day(s) before)", h.title, offset)


def main():
    p = argparse.ArgumentParser(description="HR holiday reminder bot")
    p.add_argument("--list", action="store_true", help="print the holidays and exit")
    p.add_argument("--run-now", action="store_true", help="run one check now and exit")
    p.add_argument("--dry-run", action="store_true", help="print messages instead of sending")
    p.add_argument("--at-send-hour", action="store_true", help="only run if it is SEND_HOUR in UK time")
    p.add_argument("--date", help="pretend today is this date (YYYY-MM-DD), for testing")
    args = p.parse_args()
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")

    if args.list:
        for h in load_holidays():
            note = f" ({h.notes})" if h.notes else ""
            print(f"{h.day}  {h.day:%a}  {h.title}{note}")
        return

    if args.run_now:
        now = datetime.now(UK_TZ)
        if args.at_send_hour and now.hour != SEND_HOUR:
            log.info("UK time is %s, not the %02d:00 hour; skipping", now.strftime("%H:%M"), SEND_HOUR)
            return
        today = date.fromisoformat(args.date) if args.date else None
        run_check(today, args.dry_run)
        return

    sched = BlockingScheduler(timezone=UK_TZ)
    sched.add_job(run_check, CronTrigger(hour=SEND_HOUR, minute=0, timezone=UK_TZ), kwargs={"dry_run": args.dry_run})
    log.info("Scheduler started: daily at %02d:00 UK time", SEND_HOUR)
    sched.start()


if __name__ == "__main__":
    main()
