"""Sends a phone alert (ntfy) for each new internship on the SimplifyJobs Summer 2027 list.

Runs on GitHub Actions (see .github/workflows/alerts.yml). The IDs of roles already
alerted on are kept in seen.txt, which the workflow commits back to the repo.

Environment:
  NTFY_TOPIC   required, the ntfy topic your phone is subscribed to
  NTFY_SERVER  default https://ntfy.sh
  CATEGORY     default Software
  TERM_NAME    default Summer 2027 (roles with no term listed are included too)
  GITHUB_TOKEN optional, raises the GitHub API rate limit (set automatically in Actions)
"""
import json
import os
import re
import time
import urllib.request
from datetime import datetime, timezone
from zoneinfo import ZoneInfo

REPO = "SimplifyJobs/Summer2027-Internships"
LISTINGS_PATH = ".github/scripts/listings.json"
LIST_PAGE = "https://github.com/SimplifyJobs/Summer2027-Internships/blob/dev/README.md#-software-engineering-internship-roles"
SEEN_FILE = os.path.join(os.path.dirname(os.path.abspath(__file__)), "seen.txt")
# Written by watch_direct.py: careers-site posting IDs already alerted on.
DIRECT_SEEN_FILE = os.path.join(os.path.dirname(os.path.abspath(__file__)), "seen_direct.txt")
MAX_ALERTS = 4  # beyond this, send the newest few plus one summary that names the rest
# Every alerted role, newest first, with apply links; the summary alert opens it.
NEW_ROLES_FILE = os.path.join(os.path.dirname(os.path.abspath(__file__)), "new_roles.md")
NEW_ROLES_PAGE = f"https://github.com/{os.environ.get('GITHUB_REPOSITORY', 'aniketsambher/internship-alerts')}/blob/main/new_roles.md"
NEW_ROLES_KEEP = 1500  # lines

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
    urlopen_retry(req, 30).read()


def urlopen_retry(req, timeout, attempts=3):
    """urlopen that retries brief network errors, which otherwise fail the whole run."""
    for i in range(attempts):
        try:
            return urllib.request.urlopen(req, timeout=timeout)
        except Exception as e:
            if i == attempts - 1:
                raise
            print(f"retrying after error: {e}")
            time.sleep(5 * (i + 1))


def log_new_roles(rows):
    """Prepend a dated section to new_roles.md. rows: (company, title, location, url, source)."""
    if not rows:
        return
    esc = lambda s: s.replace("[", "(").replace("]", ")")
    lines = [f"## {datetime.now(LOCAL_TZ).strftime('%b %d, %I:%M %p ET')}\n", "\n"]
    lines += [f"- **{esc(c)}**: [{esc(t)}]({u}){' - ' + l if l else ''} _({s})_\n" for c, t, l, u, s in rows]
    lines.append("\n")
    old = []
    if os.path.exists(NEW_ROLES_FILE):
        with open(NEW_ROLES_FILE, encoding="utf-8") as f:
            old = f.readlines()[2:]  # drop the header
    with open(NEW_ROLES_FILE, "w", encoding="utf-8") as f:
        f.write("# New internships, newest first\n\n")
        f.writelines((lines + old)[:NEW_ROLES_KEEP])


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
        ref = urlopen_retry(req, 30).read().decode().strip()
    except Exception as e:
        print(f"couldn't get latest commit, using cached branch copy: {e}")
    return f"https://raw.githubusercontent.com/{REPO}/{ref}/{LISTINGS_PATH}"


def main():
    url = listings_url()
    print("reading", url)
    with urlopen_retry(url, 60) as r:
        listings = json.load(r)
    # Simplify leaves some roles without a term (e.g. Meta's and Uber's 2027 SWE interns), so count those too.
    def term_ok(l):
        terms = [t for t in (l.get("terms") or []) if t != "N/A"]
        return TERM in terms or not terms
    roles = [l for l in listings if l.get("active") and l.get("is_visible") and l.get("category") == CATEGORY and term_ok(l)]

    first_run = not os.path.exists(SEEN_FILE)
    seen = set()
    if not first_run:
        with open(SEEN_FILE, encoding="utf-8") as f:
            seen = {line.strip() for line in f if line.strip()}
    new = sorted((r for r in roles if r["id"] not in seen), key=lambda r: r["date_posted"])

    # Roles watch_direct.py already alerted on get recorded as seen without a second alert.
    direct_seen = set()
    if os.path.exists(DIRECT_SEEN_FILE):
        with open(DIRECT_SEEN_FILE, encoding="utf-8") as f:
            direct_seen = {line.strip() for line in f if line.strip()}
    def already_alerted(role):
        # Careers-site IDs (Greenhouse job IDs, Workday R-numbers, Ashby UUIDs...) appear inside the apply URL.
        tokens = set(re.split(r"[^A-Za-z0-9-]+", role["url"])) | set(re.split(r"[^A-Za-z0-9]+", role["url"]))
        return any(len(t) >= 6 and t in direct_seen for t in tokens)
    to_alert = [r for r in new if not already_alerted(r)]
    for role in new:
        if role not in to_alert:
            print(f"already alerted from careers page: {role['company_name']} - {role['title']}")

    if first_run:
        send("Cloud internship alerts are on",
             f"Tracking {len(roles)} open {CATEGORY} roles. These alerts keep coming when your laptop is off.", LIST_PAGE)
    elif to_alert:
        for role in to_alert[-MAX_ALERTS:]:
            send_role(role)
        rest = to_alert[:-MAX_ALERTS]
        if rest:
            # Name every other role in the summary (ntfy caps a message at 4096 bytes); tapping it opens new_roles.md.
            body, shown = "", 0
            for r in reversed(rest):
                line = f"• {r['company_name']}: {r['title']} ({' / '.join(r.get('locations') or [])})\n"
                if len((body + line).encode("utf-8")) > 3600:
                    break
                body, shown = body + line, shown + 1
            if shown < len(rest):
                body += f"…and {len(rest) - shown} more"
            send(f"{len(rest)} more new {CATEGORY} internships", body.strip() + "\nTap for apply links.", NEW_ROLES_PAGE)
        log_new_roles([(r["company_name"], r["title"], " / ".join(r.get("locations") or []), r["url"], "Simplify")
                       for r in reversed(to_alert)])
        for role in to_alert:
            print(f"new: {role['company_name']} - {role['title']}  {role['url']}")
    elif not new:
        print(f"no new roles ({len(roles)} tracked)")

    # Only reached if every alert went out, so a failed send is retried next run.
    if new:
        with open(SEEN_FILE, "a", encoding="utf-8") as f:
            f.writelines(r["id"] + "\n" for r in new)
    elif first_run:
        open(SEEN_FILE, "w").close()


if __name__ == "__main__":
    main()
