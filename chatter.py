#!/usr/bin/env python3
"""Two clearly labelled demo bots chat with each other in a sandbox channel.

Each run (triggered every ~30 min by GitHub Actions) decides at random whether
to start a short conversation, but only during work hours on weekdays.
Messages are posted as bot personas, so they always show Slack's APP tag.
"""
import argparse
import logging
import os
import random
import time
from datetime import datetime
from zoneinfo import ZoneInfo

from dotenv import load_dotenv
from slack_sdk import WebClient
from slack_sdk.errors import SlackApiError

load_dotenv()
log = logging.getLogger("chatter")

WORK_TZ = ZoneInfo(os.getenv("WORK_TZ", "Europe/London"))
WORK_START = int(os.getenv("WORK_START", "10"))   # 10:00
WORK_END = int(os.getenv("WORK_END", "17"))       # until 17:00
START_PROBABILITY = float(os.getenv("START_PROBABILITY", "0.3"))
DELAY_MIN = int(os.getenv("DELAY_MIN", "15"))     # seconds between messages
DELAY_MAX = int(os.getenv("DELAY_MAX", "60"))
CHANNEL = os.getenv("CHATTER_CHANNEL_ID")
USE_THREADS = os.getenv("USE_THREADS", "true").lower() == "true"

PERSONAS = [
    {"username": os.getenv("BOT_A_NAME", "PR Pal"), "icon_emoji": os.getenv("BOT_A_ICON", ":robot_face:")},
    {"username": os.getenv("BOT_B_NAME", "Review Buddy"), "icon_emoji": os.getenv("BOT_B_ICON", ":mag:")},
]

# Each conversation alternates: Dev Bot, Reviewer Bot, Dev Bot, ...
CONVERSATIONS = [
    [
        "PR #214 is up. It adds retry logic to the payment webhook handler. Could you review it when you get a moment?",
        "Sure, opening it now. Quick question: are the retries capped, or can they loop forever?",
        "Capped at 5 attempts with exponential backoff. The constants are at the top of webhook.py.",
        "Found them. Could you add a test for the final failure case? Otherwise it looks good.",
        "Good catch, adding it now. I'll push in a few minutes.",
        "Nice. I'll approve once CI is green.",
    ],
    [
        "I'm stuck on the login redirect. After the OAuth callback it keeps sending users back to the sign-in page.",
        "Is the session cookie being set before the redirect? That tripped me up last time.",
        "Let me check... no, the redirect fires first. The cookie is set after.",
        "Move the cookie call above the redirect and try again. It should fix it.",
        "That did it, thanks. Logging in works now.",
        "Great. Maybe add a comment there so the next person doesn't hit the same thing.",
    ],
    [
        "Coffee break? The staging deploy has been running for ages.",
        "Yes please. I'll be there in two minutes.",
        "Did you see the build time dropped after the cache change?",
        "I did. Down from 9 minutes to about 4. Well worth the effort.",
        "Agreed. Let's keep an eye on it for a few days to make sure it stays stable.",
    ],
    [
        "Quick doubt on the new schema: should user_id be nullable for guest checkouts?",
        "I'd keep it non-null and create a guest user record. Nullable fields tend to spread into every query.",
        "Fair point. That also keeps the reporting queries simple.",
        "Exactly. I'll write a short note in the design doc so we don't forget why.",
        "Sounds good. I'll update the migration to match.",
    ],
    [
        "PR #221 is a small one: renames the config keys and updates the docs. About 40 lines changed.",
        "Looking now. One thing: the old key names are still used in the deploy script.",
        "Oh no, I missed that. Fixing it now.",
        "No rush. Ping me when it's pushed and I'll re-check.",
        "Pushed. The deploy script uses the new names now.",
        "Confirmed, all good. Approved.",
    ],
    [
        "Heads-up: the flaky test in the search module failed again on CI.",
        "That one fails when two tests share the same index name. I think we need unique names per run.",
        "Makes sense. Want me to open a ticket or just fix it?",
        "Just fix it, it's a two-line change. I'll review straight away.",
        "On it.",
    ],
    [
        "Any plans for the weekend?",
        "Nothing big. Probably a long walk and catching up on a book. You?",
        "Trying a new hiking trail. Fingers crossed for dry weather.",
        "Good luck with that. Share photos if you get a good view.",
    ],
    [
        "Do we have a rule for squashing commits before merge?",
        "Yes, squash and merge for feature branches. It keeps the main history readable.",
        "Got it. I'll update my PR settings.",
        "It's also in the contributing guide, section 3, if you want the details.",
        "Thanks, I'll read it now.",
    ],
    [
        "The API response time on the orders endpoint looks slower this week.",
        "I noticed too. Could be the new join we added. Have you profiled the query?",
        "Not yet. I'll run EXPLAIN and see whether an index is missing.",
        "Good idea. If it is, a composite index on customer_id and created_at should help.",
        "I'll try that on a copy of the database first and share the numbers.",
        "Perfect. Post the before/after in the channel when you have them.",
    ],
    [
        "Lunch order is going in. Anything you'd like?",
        "A veggie wrap, please. And a sparkling water.",
        "Done. It should arrive in about 20 minutes.",
        "Thanks. I'll finish this review before it gets here.",
    ],
]

HR_CONVERSATIONS = [
    [
        "Hi, could you share your updated timesheet for this week?",
        "Sure, let me have a look and send it across.",
        "Thanks. End of day works, no rush.",
        "Will do. I'll keep you posted.",
    ],
    [
        "A quick reminder that the policy acknowledgement form is still pending.",
        "Thanks for the reminder. Let me have a look.",
        "It takes about 5 minutes. The link is in your email.",
        "Found it. I'll complete it today and keep you posted.",
    ],
    [
        "Your leave request is waiting for approval. Could you confirm the dates?",
        "Let me check my calendar and get back to you.",
        "Sure, please keep me posted.",
        "Confirmed, the dates are the same. Thanks!",
    ],
    [
        "Quick check-in: how is onboarding going? Is there anything you need?",
        "Going well, thanks. Let me note down a couple of things and keep you posted.",
        "Great, happy to help whenever you're ready.",
    ],
]

def in_work_hours(now):
    return now.weekday() < 5 and WORK_START <= now.hour < WORK_END


def say(client, persona, text, thread_ts, dry_run):
    if dry_run:
        print(f"[{persona['username']}] {text}")
        return {"ts": "dry-run"}
    kwargs = {"channel": CHANNEL, "text": text, **persona}
    if thread_ts:
        kwargs["thread_ts"] = thread_ts
    try:
        return client.chat_postMessage(**kwargs)
    except SlackApiError as e:
        if "missing_scope" in str(e):
            # chat:write.customize not granted: fall back to a name prefix
            kwargs.pop("username"), kwargs.pop("icon_emoji")
            kwargs["text"] = f"*{persona['username']}:* {text}"
            return client.chat_postMessage(**kwargs)
        raise


def run_conversation(client, dry_run):
    pool = HR_CONVERSATIONS if os.getenv("CONVO_SET", "dev") == "hr" else CONVERSATIONS
    convo = random.choice(pool)
    thread_ts = None
    for i, line in enumerate(convo):
        resp = say(client, PERSONAS[i % 2], line, thread_ts, dry_run)
        if i == 0 and USE_THREADS:
            thread_ts = resp["ts"]
        if i < len(convo) - 1:
            time.sleep(0 if dry_run else random.uniform(DELAY_MIN, DELAY_MAX))


def main():
    p = argparse.ArgumentParser()
    p.add_argument("--test", action="store_true", help="ignore time window and probability; run one conversation")
    p.add_argument("--dry-run", action="store_true", help="print instead of posting")
    args = p.parse_args()
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")

    now = datetime.now(WORK_TZ)
    if not args.test:
        if not in_work_hours(now):
            log.info("Outside work hours (%s); skipping", now.strftime("%a %H:%M"))
            return
        if random.random() > START_PROBABILITY:
            log.info("Not starting a conversation this time")
            return

    if not CHANNEL and not args.dry_run:
        raise SystemExit("Set CHATTER_CHANNEL_ID")
    client = WebClient(token=os.getenv("SLACK_BOT_TOKEN"))
    run_conversation(client, args.dry_run)
    log.info("Conversation finished")


if __name__ == "__main__":
    main()
