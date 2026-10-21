"""Sends a phone alert (ntfy) for new US engineering internships posted straight to a
company's careers site, often hours before they reach the Simplify list.

Works for companies that post on SmartRecruiters (LinkedIn's internships are there).
Posting IDs already alerted on are kept in seen_direct.txt; check.py reads it so the
same role doesn't alert again when Simplify lists it later.
"""
import json
import os
import re
import urllib.request
from datetime import datetime

from check import LOCAL_TZ, send

# Display name -> SmartRecruiters company identifier (from jobs.smartrecruiters.com/<identifier>/...)
COMPANIES = {"LinkedIn": "LinkedIn3"}
COUNTRY = "us"
TECH_TITLE = re.compile(r"software|engineer|developer|reliability|machine learning|\bml\b|\bai\b|data", re.I)
SEEN_FILE = os.path.join(os.path.dirname(os.path.abspath(__file__)), "seen_direct.txt")


def postings(identifier):
    offset = 0
    while True:
        url = f"https://api.smartrecruiters.com/v1/companies/{identifier}/postings?limit=100&offset={offset}"
        with urllib.request.urlopen(url, timeout=30) as r:
            page = json.load(r)
        yield from page["content"]
        offset += len(page["content"])
        if not page["content"] or offset >= page["totalFound"]:
            return


def is_match(p):
    return (
        (p.get("experienceLevel") or {}).get("id") == "internship"
        and (p.get("location") or {}).get("country") == COUNTRY
        and ((p.get("function") or {}).get("id") == "engineering" or TECH_TITLE.search(p["name"]))
    )


def main():
    first_run = not os.path.exists(SEEN_FILE)
    seen = set()
    if not first_run:
        with open(SEEN_FILE, encoding="utf-8") as f:
            seen = {line.strip() for line in f if line.strip()}

    new = []
    for company, identifier in COMPANIES.items():
        matches = [p for p in postings(identifier) if is_match(p)]
        print(f"{company}: {len(matches)} matching internships")
        new += [(company, identifier, p) for p in matches if p["id"] not in seen]

    if first_run:
        send("Careers page alerts are on",
             "Watching " + ", ".join(COMPANIES) + " directly for new US engineering internships.",
             "https://jobs.smartrecruiters.com/" + next(iter(COMPANIES.values())))
    else:
        for company, identifier, p in new:
            released = datetime.fromisoformat(p["releasedDate"].replace("Z", "+00:00")).astimezone(LOCAL_TZ)
            url = f"https://jobs.smartrecruiters.com/{identifier}/{p['id']}"
            send(f"{company} careers: {p['name']}",
                 f"{p['location'].get('fullLocation', '')}\nPosted {released.strftime('%b %d, %I:%M %p ET')} on the careers page",
                 url)
            print(f"new: {company} - {p['name']}  {url}")

    if new or first_run:
        with open(SEEN_FILE, "a", encoding="utf-8") as f:
            f.writelines(p["id"] + "\n" for _, _, p in new)


if __name__ == "__main__":
    main()
