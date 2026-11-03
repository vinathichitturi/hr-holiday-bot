#!/usr/bin/env python3
"""Start a Cursor background agent and post its link to a Slack channel.

Usage:
  python cursor_to_slack.py "Add a README with install steps"
  python cursor_to_slack.py "Fix the login bug" --auto-pr
  python cursor_to_slack.py "Test only" --dry-run
"""
import argparse
import json
import os
import sys

import requests
from dotenv import load_dotenv
from slack_sdk import WebClient

load_dotenv()
API_URL = "https://api.cursor.com/v0/agents"


def launch_agent(prompt, repo, ref, auto_pr):
    body = {
        "prompt": {"text": prompt},
        "source": {"repository": repo, "ref": ref},
        "target": {"autoCreatePr": auto_pr},
    }
    resp = requests.post(
        API_URL,
        headers={
            "Authorization": f"Bearer {os.environ['CURSOR_API_KEY']}",
            "Content-Type": "application/json",
        },
        json=body,
        timeout=60,
    )
    if not resp.ok:
        sys.exit(f"Cursor API error {resp.status_code}: {resp.text}")
    return resp.json()


def main():
    p = argparse.ArgumentParser()
    p.add_argument("prompt", help="what the Cursor agent should do")
    p.add_argument("--repo", default=os.getenv("CURSOR_REPO"), help="GitHub repo URL")
    p.add_argument("--ref", default=os.getenv("CURSOR_REF", "main"))
    p.add_argument("--auto-pr", action="store_true", help="let the agent open a pull request")
    p.add_argument("--dry-run", action="store_true", help="show what would be sent, do nothing")
    args = p.parse_args()

    if not args.repo:
        sys.exit("Set CURSOR_REPO in .env or pass --repo https://github.com/you/repo")

    if args.dry_run:
        print(json.dumps({"prompt": args.prompt, "repo": args.repo, "ref": args.ref, "autoCreatePr": args.auto_pr}, indent=2))
        return

    agent = launch_agent(args.prompt, args.repo, args.ref, args.auto_pr)
    url = agent.get("target", {}).get("url", "(no link returned)")
    text = (
        f"Cursor agent started: *{agent.get('name', 'New agent')}*\n"
        f"Task: {args.prompt}\n"
        f"Status: {agent.get('status', 'unknown')}\n"
        f"Link: {url}"
    )
    print(text)

    channel = os.getenv("CURSOR_NOTIFY_CHANNEL_ID")
    if channel:
        WebClient(token=os.environ["SLACK_BOT_TOKEN"]).chat_postMessage(channel=channel, text=text)
        print("Posted to Slack")


if __name__ == "__main__":
    main()
