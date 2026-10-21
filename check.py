"""Sends a phone alert (ntfy) for each new internship on the SimplifyJobs Summer 2027 list.

Runs on GitHub Actions (see .github/workflows/alerts.yml). The IDs of roles already
alerted on are kept in seen.txt, which the workflow commits back to the repo.

Environment:
  NTFY_TOPIC   required, the ntfy topic your phone is subscribed to
  NTFY_SERVER  default https://ntfy.sh
  CATEGORY     default Software
  TERM_NAME    default Summer 2027
  GITHUB_TOKEN optional, raises the GitHub API rate limit (set automatically in Actions)
"""
import json
import os
import urllib.request
from datetime import datetime, timezone
from zoneinfo import ZoneInfo

REPO = "SimplifyJobs/Summer2027-Internships"
LISTINGS_PATH = ".github/scripts/listings.json"
LIST_PAGE = "https://github.com/SimplifyJobs/Summer2027-Internships/blob/dev/README.md#-software-engineering-internship-roles"
SEEN_FILE = os.path.join(os.path.dirname(os.path.abspath(__file__)), "seen.txt")
MAX_ALERTS = 4  # beyond this, send the newest few plus one summary

NTFY_TOPIC = os.environ["NTFY_TOPIC"].strip()
NTFY_SERVER = os.environ.get("NTFY_SERVER", "https://ntfy.sh").strip().rstrip("/")
CATEGORY = os.environ.get("CATEGORY", "Software")
TERM = os.environ.get("TERM_NAME", "Summer 2027")
LOCAL_TZ = ZoneInfo("America/New_York")


def send(title, message, apply_url, simplify_url=None):
    actions = [{"action": "view", "label": "Apply", "url": apply_url}]
    if simplify_url:
        actions.append({"action": "view", "label": "Simplify", "url": simplify_url})
    body = json.dumps({
        "topic": NTFY_TOPIC, "title": title, "message": message, "click": apply_url,
        "actions": actions, "tags": ["briefcase"], "priority": 4,
    }).encode("utf-8")
    req = urllib.request.Request(NTFY_SERVER + "/", data=body, headers={"Content-Type": "application/json"})
    urllib.request.urlopen(req, timeout=30).read()


def send_role(role):
    posted = datetime.fromtimestamp(role["date_posted"], timezone.utc).astimezone(LOCAL_TZ)
    message = " / ".join(role.get("locations") or []) + "\nAdded " + posted.strftime("%b %d, %I:%M %p ET")
    if role.get("sponsorship") and role["sponsorship"] != "Other":
        message += " - " + role["sponsorship"]
    send(f"{role['company_name']}: {role['title']}", message, role["url"], f"https://simplify.jobs/p/{role['id']}")


def listings_url():
    """URL of listings.json at the newest commit on dev.

    The branch URL is served from a cache that can be up to 5 minutes old; a URL
    pinned to a commit SHA never is. Falls back to the branch URL if the API fails.
    """
    headers = {"Accept": "application/vnd.github.sha"}
    if os.environ.get("GITHUB_TOKEN"):
        headers["Authorization"] = "Bearer " + os.environ["GITHUB_TOKEN"]
    ref = "dev"
    try:
        req = urllib.request.Request(f"https://api.github.com/repos/{REPO}/commits/dev", headers=headers)
        ref = urllib.request.urlopen(req, timeout=30).read().decode().strip()
    except Exception as e:
        print(f"couldn't get latest commit, using cached branch copy: {e}")
    return f"https://raw.githubusercontent.com/{REPO}/{ref}/{LISTINGS_PATH}"


def main():
    url = listings_url()
    print("reading", url)
    with urllib.request.urlopen(url, timeout=60) as r:
        listings = json.load(r)
    roles = [
        l for l in listings
        if l.get("active") and l.get("is_visible") and l.get("category") == CATEGORY and TERM in (l.get("terms") or [])
    ]

    first_run = not os.path.exists(SEEN_FILE)
    seen = set()
    if not first_run:
        with open(SEEN_FILE, encoding="utf-8") as f:
            seen = {line.strip() for line in f if line.strip()}
    new = sorted((r for r in roles if r["id"] not in seen), key=lambda r: r["date_posted"])

    if first_run:
        send("Cloud internship alerts are on",
             f"Tracking {len(roles)} open {CATEGORY} roles. These alerts keep coming when your laptop is off.", LIST_PAGE)
    elif new:
        for role in new[-MAX_ALERTS:]:
            send_role(role)
        if len(new) > MAX_ALERTS:
            send(f"{len(new)} new {CATEGORY} internships", f"Showing the newest {MAX_ALERTS}. See the full list.", LIST_PAGE)
        for role in new:
            print(f"new: {role['company_name']} - {role['title']}  {role['url']}")
    else:
        print(f"no new roles ({len(roles)} tracked)")

    # Only reached if every alert went out, so a failed send is retried next run.
    if new:
        with open(SEEN_FILE, "a", encoding="utf-8") as f:
            f.writelines(r["id"] + "\n" for r in new)
    elif first_run:
        open(SEEN_FILE, "w").close()


if __name__ == "__main__":
    main()
