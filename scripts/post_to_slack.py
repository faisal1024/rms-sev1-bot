#!/usr/bin/env python3
"""
post_to_slack.py
----------------
Fetches live Sev 1 RMS tickets from Jira using the REST API with Basic Auth
(Atlassian email + API token), then posts a formatted message to Slack via
the RMS Sev 1 bot.

Credentials are loaded from /home/ubuntu/.env_secrets or
/sessions/focused-great-volta/.env_secrets.

Usage:
  python3 post_to_slack.py           # Fetch + post to Slack
  python3 post_to_slack.py --dry-run # Fetch + print preview only (no posting)
"""

import os
import sys
import json
import time
import requests
from requests.adapters import HTTPAdapter
from urllib3.util.retry import Retry
from datetime import datetime, timezone

DRY_RUN = "--dry-run" in sys.argv

# ---------------------------------------------------------------------------
# Load secrets
# ---------------------------------------------------------------------------

def load_secrets(path=None):
    """Load credentials from a secrets file, falling back to environment variables."""
    secrets = {}

    # 1. Try secrets file (local / Cowork runs)
    if path is None:
        for candidate in ["/home/ubuntu/.env_secrets", "/sessions/focused-great-volta/.env_secrets"]:
            if os.path.exists(candidate):
                path = candidate
                break

    if path and os.path.exists(path):
        try:
            with open(path) as f:
                for line in f:
                    line = line.strip()
                    if line and "=" in line and not line.startswith("#"):
                        key, _, val = line.partition("=")
                        secrets[key.strip()] = val.strip()
        except FileNotFoundError:
            pass

    # 2. Fall back to environment variables (GitHub Actions / CI runs)
    for key in ("SLACK_BOT_TOKEN", "JIRA_EMAIL", "JIRA_API_TOKEN"):
        if key not in secrets and os.environ.get(key):
            secrets[key] = os.environ[key]

    return secrets

_secrets = load_secrets()

# ---------------------------------------------------------------------------
# Configuration
# ---------------------------------------------------------------------------

SLACK_BOT_TOKEN   = _secrets.get("SLACK_BOT_TOKEN", "")
SLACK_CHANNEL_ID  = "C08NY0YQT9A"  # #amazon-deployment channel

JIRA_BASE_URL     = "https://molg.atlassian.net"
JIRA_QUEUE_URL    = f"{JIRA_BASE_URL}/jira/servicedesk/projects/RMS/queues/custom/100"
TICKET_BASE_URL   = f"{JIRA_BASE_URL}/jira/servicedesk/projects/RMS/queues/custom/34"

JIRA_EMAIL        = _secrets.get("JIRA_EMAIL", "faisal@molg.ai")
JIRA_API_TOKEN    = _secrets.get("JIRA_API_TOKEN", "")

SEV1_JQL = 'project = RMS AND resolution = Unresolved AND "severity[dropdown]" = "Severity 1" ORDER BY created ASC'

# Static name map - handles special cases (e.g. "office" -> Jennifer)
ASSIGNEE_SLACK_ID_MAP = {
    "abe maclean":          "U01RGB0J83C",
    "abraham maclean":      "U01RGB0J83C",
    "jennifer de phillips": "U09DZNPGLL9",
    "office":               "U09DZNPGLL9",
    "vineet pandey":        "U08KVUUV0B0",
    "dyllian powell":       "U07B8GKU0CE",
    "daniel cusumano":      "U02AH3B3Q3A",
}

# ---------------------------------------------------------------------------
# Step 1: Fetch live tickets from Jira REST API
# ---------------------------------------------------------------------------

def _make_session():
    session = requests.Session()
    retry = Retry(
        total=4,
        backoff_factor=2,
        status_forcelist=[429, 500, 502, 503, 504],
        allowed_methods=["GET"],
    )
    adapter = HTTPAdapter(max_retries=retry)
    session.mount("https://", adapter)
    session.mount("http://", adapter)
    return session

def fetch_tickets():
    print("Fetching Sev 1 tickets from Jira...")
    url    = f"{JIRA_BASE_URL}/rest/api/3/search/jql"
    params = {"jql": SEV1_JQL, "fields": "summary,assignee,created", "maxResults": 200}
    auth    = (JIRA_EMAIL, JIRA_API_TOKEN)
    headers = {"Accept": "application/json"}
    session = _make_session()

    for attempt in range(1, 4):
        try:
            resp = session.get(url, params=params, headers=headers, auth=auth, timeout=30)
            if resp.status_code == 200:
                issues = resp.json().get("issues", [])
                print(f"Found {len(issues)} tickets.")
                return issues
            print(f"API v3 returned {resp.status_code}, trying v2...")
            url2  = f"{JIRA_BASE_URL}/rest/api/2/search"
            resp2 = session.get(url2, params=params, headers=headers, auth=auth, timeout=30)
            if resp2.status_code == 200:
                issues = resp2.json().get("issues", [])
                print(f"Found {len(issues)} tickets via v2.")
                return issues
            print(f"Both API calls failed: {resp.status_code}, {resp2.status_code}")
            return []
        except Exception as e:
            print(f"Attempt {attempt} failed: {e}")
            if attempt < 3:
                wait = 5 * attempt
                print(f"Retrying in {wait}s...")
                time.sleep(wait)

    print("All retry attempts exhausted.")
    return []

# ---------------------------------------------------------------------------
# Step 2: Days open
# ---------------------------------------------------------------------------

def days_open(created_str):
    if not created_str:
        return "?"
    for fmt in ("%Y-%m-%dT%H:%M:%S.%f%z", "%Y-%m-%dT%H:%M:%S%z"):
        try:
            created_dt = datetime.strptime(created_str, fmt)
            if created_dt.tzinfo is None:
                created_dt = created_dt.replace(tzinfo=timezone.utc)
            delta = datetime.now(timezone.utc) - created_dt
            return max(delta.days, 0)
        except ValueError:
            continue
    return "?"

# ---------------------------------------------------------------------------
# Step 2b: Slack user lookup (dynamic fallback via users:read)
# ---------------------------------------------------------------------------

_slack_user_cache = None

def _load_slack_users():
    global _slack_user_cache
    if _slack_user_cache is not None:
        return _slack_user_cache
    print("Loading Slack workspace members for dynamic name lookup...")
    _slack_user_cache = {}
    cursor = None
    while True:
        params = {"limit": 200}
        if cursor:
            params["cursor"] = cursor
        try:
            resp = requests.get(
                "https://slack.com/api/users.list",
                headers={"Authorization": f"Bearer {SLACK_BOT_TOKEN}"},
                params=params,
                timeout=15,
            )
            data = resp.json()
            if not data.get("ok"):
                print(f"Warning: could not load Slack users: {data.get('error')}")
                break
            for member in data.get("members", []):
                if member.get("deleted") or member.get("is_bot"):
                    continue
                uid = member["id"]
                profile = member.get("profile", {})
                for name_field in ("real_name", "display_name"):
                    name = profile.get(name_field, "").strip().lower()
                    if name:
                        _slack_user_cache.setdefault(name, uid)
            cursor = data.get("response_metadata", {}).get("next_cursor")
            if not cursor:
                break
        except Exception as e:
            print(f"Warning: error loading Slack users: {e}")
            break
    print(f"Loaded {len(_slack_user_cache)} Slack user name entries.")
    return _slack_user_cache

def _find_slack_uid(name_lower):
    cache = _load_slack_users()
    if name_lower in cache:
        return cache[name_lower]
    parts = name_lower.split()
    if len(parts) >= 2:
        reversed_name = " ".join(reversed(parts))
        if reversed_name in cache:
            return cache[reversed_name]
        for key in cache:
            if all(p in key for p in parts):
                return cache[key]
        first_name = parts[0]
        for key in cache:
            if key.split()[0] == first_name:
                return cache[key]
    return None

def slack_mention(assignee_name):
    if not assignee_name or assignee_name.lower() in ("unassigned", "none", ""):
        return "Unassigned"
    name_lower = assignee_name.lower()
    uid = ASSIGNEE_SLACK_ID_MAP.get(name_lower)
    if uid:
        return f"<@{uid}>"
    uid = _find_slack_uid(name_lower)
    if uid:
        print(f"  Dynamically resolved '{assignee_name}' -> <@{uid}>")
        return f"<@{uid}>"
    print(f"  Warning: no Slack user found for '{assignee_name}' - using plain name.")
    return assignee_name

# ---------------------------------------------------------------------------
# Step 3: Build Slack Block Kit message
# ---------------------------------------------------------------------------

def build_slack_blocks(issues):
    total = len(issues)
    lines = []
    for issue in issues:
        key          = issue.get("key", "?")
        fields       = issue.get("fields", {})
        assignee_obj = fields.get("assignee")
        assignee     = assignee_obj.get("displayName") if assignee_obj else "Unassigned"
        created_str  = fields.get("created", "")
        summary      = fields.get("summary", "")
        ticket_url   = f"{TICKET_BASE_URL}/{key}"
        mention      = slack_mention(assignee)
        d_open       = days_open(created_str)
        if d_open == 0:
            age = "today"
        elif d_open == 1:
            age = "1 day"
        else:
            age = f"{d_open} days"
        lines.append(f"- <{ticket_url}|{key}> - {summary} - {mention} [{age}]")

    ticket_list = "\n".join(lines) if lines else "_No open Sev 1 tickets._"

    blocks = [
        {
            "type": "section",
            "text": {
                "type": "mrkdwn",
                "text": (
                    f"*Open Sev 1 RMS Tickets (Total: {total})*\n"
                    f"Jira List: <{JIRA_QUEUE_URL}|View all Sev 1 tickets>"
                )
            }
        },
        {"type": "divider"},
        {
            "type": "section",
            "text": {"type": "mrkdwn", "text": ticket_list}
        }
    ]
    return blocks

# ---------------------------------------------------------------------------
# Step 4: Post to Slack (or dry-run preview)
# ---------------------------------------------------------------------------

def preview_message(blocks):
    print("\n" + "="*60)
    print("DRY RUN - Message preview (nothing has been posted)")
    print("="*60)
    print(f"Channel: #amazon-deployment ({SLACK_CHANNEL_ID})")
    print("-"*60)
    for block in blocks:
        if block.get("type") == "section":
            text = block.get("text", {}).get("text", "")
            print(text)
        elif block.get("type") == "divider":
            print("-"*40)
    print("="*60 + "\n")

def post_to_slack(blocks):
    print(f"Posting to Slack channel: {SLACK_CHANNEL_ID}")
    resp = requests.post(
        "https://slack.com/api/chat.postMessage",
        headers={
            "Authorization": f"Bearer {SLACK_BOT_TOKEN}",
            "Content-Type":  "application/json; charset=utf-8",
        },
        json={
            "channel": SLACK_CHANNEL_ID,
            "blocks":  blocks,
            "text":    "Open Sev 1 RMS Tickets",
        },
        timeout=15,
    )
    data = resp.json()
    if data.get("ok"):
        print("Message posted successfully!")
    else:
        print(f"Slack API error: {data.get('error')}")
        print(json.dumps(data, indent=2))

# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------

if __name__ == "__main__":
    issues = fetch_tickets()
    if issues:
        blocks = build_slack_blocks(issues)
        if DRY_RUN:
            preview_message(blocks)
        else:
            post_to_slack(blocks)
    else:
        print("No tickets fetched - aborting.")
