"""Sends a phone alert (ntfy) for new US tech internships posted straight to company
careers sites, often hours before they reach the Simplify list.

Each company in COMPANIES is read through its careers site's public job feed. Posting IDs
already alerted on are kept in seen_direct.txt, along with a "#seeded:<company>" line per
company: a company's first run records what's already posted without alerting. check.py
reads the same file so a role doesn't alert again when Simplify lists it later.
"""
import json
import os
import re
import urllib.request
from collections import namedtuple
from datetime import datetime, timedelta, timezone

from check import LOCAL_TZ, log_new_roles, send

Job = namedtuple("Job", "id title location url posted")

UA = "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/130.0 Safari/537.36"
INTERN = re.compile(r"\bintern(ship)?s?\b|\bco-?op\b", re.I)
TECH = re.compile(r"software|engineer|developer|reliability|machine learning|\bml\b|\bai\b|data", re.I)
NOT_SWE = re.compile(r"marketing|sales|finance|account|legal|recruit|consultant|program management|hardware|mechanical|electrical", re.I)
NON_US = re.compile(
    r"\b(Canada|Toronto|Vancouver|Montreal|Ottawa|Waterloo|UK|United Kingdom|London|England|Ireland|Dublin|Germany|Berlin|Munich|"
    r"Switzerland|Zurich|France|Paris|Netherlands|Amsterdam|Eindhoven|India|Bengaluru|Bangalore|Hyderabad|Pune|Chennai|Israel|"
    r"Tel Aviv|China|Beijing|Shanghai|Japan|Tokyo|Korea|Seoul|Singapore|Australia|Sydney|Melbourne|Brazil|Sao Paulo|São Paulo|"
    r"Mexico|Poland|Warsaw|Krakow|Portugal|Lisbon|Spain|Madrid|Barcelona|Italy|Milan|Sweden|Stockholm|Denmark|Copenhagen|"
    r"Norway|Finland|Romania|Czech|Prague|Taiwan|Taipei|Hong Kong|Philippines|Vietnam|EMEA|APAC|LATAM)\b", re.I)
SEEN_FILE = os.path.join(os.path.dirname(os.path.abspath(__file__)), "seen_direct.txt")
MAX_AGE = timedelta(days=14)


def fetch(url, body=None, accept="application/json"):
    headers = {"User-Agent": UA, "Accept": accept}
    data = None
    if body is not None:
        data = json.dumps(body).encode()
        headers["Content-Type"] = "application/json"
    with urllib.request.urlopen(urllib.request.Request(url, data=data, headers=headers), timeout=30) as r:
        text = r.read().decode("utf-8", "replace")
    return json.loads(text) if accept == "application/json" else text


# --- one reader per careers-site platform; each returns a list of Job ---

def smartrecruiters(company_id):
    jobs, offset = [], 0
    while True:
        page = fetch(f"https://api.smartrecruiters.com/v1/companies/{company_id}/postings?limit=100&offset={offset}")
        for p in page["content"]:
            if (p.get("experienceLevel") or {}).get("id") == "internship" and (p.get("location") or {}).get("country") == "us":
                jobs.append(Job(p["id"], p["name"], p["location"].get("fullLocation", ""),
                                f"https://jobs.smartrecruiters.com/{company_id}/{p['id']}", p["releasedDate"]))
        offset += len(page["content"])
        if not page["content"] or offset >= page["totalFound"]:
            return jobs


def greenhouse(board):
    return [Job(str(j["id"]), j["title"], (j.get("location") or {}).get("name", ""), j["absolute_url"], j.get("first_published"))
            for j in fetch(f"https://boards-api.greenhouse.io/v1/boards/{board}/jobs")["jobs"]]


def ashby(board):
    jobs = []
    for j in fetch(f"https://api.ashbyhq.com/posting-api/job-board/{board}")["jobs"]:
        loc = j.get("location") or ""
        country = (((j.get("address") or {}).get("postalAddress") or {}).get("addressCountry") or "")
        code = re.match(r"([A-Z]{2})-", loc)  # e.g. "US-WA-Bellevue", "DE-Berlin-..."
        if (code and code.group(1) != "US") or (country and country not in ("United States", "USA", "US")):
            loc += " (non-US)"
        jobs.append(Job(j["id"], j["title"], loc, j["jobUrl"], j.get("publishedAt")))
    return jobs


def workday(host, tenant, site, public_base):
    jobs, offset = [], 0
    while True:
        page = fetch(f"https://{host}/wday/cxs/{tenant}/{site}/jobs",
                     {"appliedFacets": {}, "limit": 20, "offset": offset, "searchText": "intern"})
        for j in page["jobPostings"]:
            req_id = (j.get("bulletFields") or [j["externalPath"].rsplit("_", 1)[-1]])[0]
            jobs.append(Job(req_id, j["title"], j.get("locationsText", ""), public_base + j["externalPath"], j.get("postedOn")))
        offset += len(page["jobPostings"])
        if not page["jobPostings"] or offset >= page.get("total", 0) or offset >= 400:
            return jobs


def oracle(host, site):
    jobs, offset = [], 0
    while offset < 400:
        page = fetch(f"https://{host}/hcmRestApi/resources/latest/recruitingCEJobRequisitions?onlyData=true"
                     f"&expand=requisitionList.secondaryLocations&finder=findReqs;siteNumber={site},keyword=intern,"
                     f"limit=50,offset={offset},sortBy=POSTING_DATES_DESC")
        reqs = page["items"][0]["requisitionList"] if page.get("items") else []
        for r in reqs:
            loc = r.get("PrimaryLocation") or ""
            if r.get("PrimaryLocationCountry") not in (None, "US"):
                loc += " (non-US)"
            jobs.append(Job(str(r["Id"]), r["Title"], loc,
                            f"https://{host}/hcmUI/CandidateExperience/en/sites/{site}/job/{r['Id']}", r.get("PostedDate")))
        if len(reqs) < 50:
            break
        offset += 50
    return jobs


def apple(_):
    # Apple's search pages intermittently come back empty, so retry each empty page a few times.
    def results_on(page):
        for _ in range(3):
            html = fetch(f"https://jobs.apple.com/en-us/search?team=internships-STDNT-INTRN&location=united-states-USA&sort=newest&page={page}",
                         accept="text/html")
            m = re.search(r"window\.__staticRouterHydrationData\s*=\s*JSON\.parse\((\".*?\")\);", html, re.S)
            results = json.loads(json.loads(m.group(1)))["loaderData"]["search"]["searchResults"] if m else []
            if results:
                return results
        return []

    jobs, page = {}, 1
    while page <= 5:
        results = results_on(page)
        if not results and page == 1:
            raise RuntimeError("search page came back without job data")
        for r in results:
            locs = ", ".join(l.get("name", "") for l in r.get("locations") or [])
            jobs.setdefault(r["positionId"], Job(r["positionId"], r["postingTitle"], locs,
                                                 f"https://jobs.apple.com/en-us/details/{r['positionId']}", r.get("postingDate")))
        if len(results) < 20:
            break
        page += 1
    return list(jobs.values())


def ibm(_):
    body = {"appId": "careers", "scopes": ["careers2"], "size": 100, "sort": [{"dcdate": "desc"}, "_score"], "lang": "zz",
            "query": {"bool": {"must": [{"simple_query_string": {"query": "intern", "fields": ["title^3", "description^2"]}}]}},
            "_source": ["_id", "title", "url", "dcdate", "field_keyword_19"]}
    jobs = []
    for h in fetch("https://www-api.ibm.com/search/api/v2", body)["hits"]["hits"]:
        s = h["_source"]
        m = re.search(r"jobId=(\d+)", s.get("url", ""))
        loc = str(s.get("field_keyword_19") or "")
        code = re.search(r", ([A-Z]{2})$", loc)  # "Austin, US", "Markham, CA" (Canada)
        if code and code.group(1) != "US":
            loc += " (non-US)"
        jobs.append(Job(m.group(1) if m else h["_id"], s["title"], loc, s["url"], s.get("dcdate")))
    return jobs


def shopify(_):
    # Shopify's internships site links to each open internship posting on shopify.com/careers.
    html = fetch("https://internships.shopify.com/", accept="text/html")
    jobs = {}
    for slug, uuid in re.findall(r"shopify\.com/careers/([a-z0-9-]*intern[a-z0-9-]*)_([0-9a-f-]{36})", html):
        jobs[uuid] = Job(uuid, slug.replace("-", " ").title(), "", f"https://www.shopify.com/careers/{slug}_{uuid}", None)
    return list(jobs.values())


COMPANIES = [
    ("LinkedIn", smartrecruiters, ("LinkedIn3",)),
    ("Snowflake", ashby, ("snowflake",)),
    ("OpenAI", ashby, ("openai",)),
    ("Airbnb", greenhouse, ("airbnb",)),
    ("Instacart", greenhouse, ("instacart",)),
    ("Samsara", greenhouse, ("samsara",)),
    ("Snap", workday, ("wd1.myworkdaysite.com", "snapchat", "snap", "https://wd1.myworkdaysite.com/recruiting/snapchat/snap")),
    ("CrowdStrike", workday, ("crowdstrike.wd5.myworkdayjobs.com", "crowdstrike", "crowdstrikecareers",
                              "https://crowdstrike.wd5.myworkdayjobs.com/crowdstrikecareers")),
    ("Uber", oracle, ("iaziqy.fa.ocs.oraclecloud.com", "CX_1")),
    ("Oracle", oracle, ("eeho.fa.us2.oraclecloud.com", "CX_45001")),
    ("Apple", apple, (None,)),
    ("IBM", ibm, (None,)),
    ("Shopify", shopify, (None,)),
]


def posted_at(text):
    """Best-effort parse of the posting date each site reports; None if unknown."""
    if not text:
        return None
    now = datetime.now(timezone.utc)
    rel = re.match(r"Posted (Today|Yesterday|(\d+)\+? Days? Ago)", text)
    if rel:
        days = 0 if rel.group(1) == "Today" else 1 if rel.group(1) == "Yesterday" else int(rel.group(2))
        return now - timedelta(days=days)
    for parse in (lambda t: datetime.fromisoformat(t.replace("Z", "+00:00")),
                  lambda t: datetime.strptime(t, "%b %d, %Y")):
        try:
            d = parse(text)
            return d if d.tzinfo else d.replace(tzinfo=timezone.utc)
        except ValueError:
            pass
    return None


def is_match(job):
    return INTERN.search(job.title) and TECH.search(job.title) and not NOT_SWE.search(job.title) \
        and not NON_US.search(f"{job.title} {job.location}") and "(non-US)" not in job.location


def main():
    seen, seeded = set(), set()
    if os.path.exists(SEEN_FILE):
        with open(SEEN_FILE, encoding="utf-8") as f:
            for line in f:
                line = line.strip()
                if line.startswith("#seeded:"):
                    seeded.add(line[len("#seeded:"):])
                elif line:
                    seen.add(line)

    record, newly_seeded, failed, alerted = [], [], [], []
    for company, reader, args in COMPANIES:
        try:
            jobs = reader(*args)
        except Exception as e:
            print(f"{company}: couldn't read careers site ({e})")
            failed.append(company)
            continue
        matches = [j for j in jobs if is_match(j)]
        new = [j for j in matches if j.id not in seen]
        print(f"{company}: {len(jobs)} read, {len(matches)} matching internships, {len(new)} new")
        if company not in seeded:
            # A site that glitches and returns nothing would otherwise be recorded as empty,
            # and every role on it would alert on the next good read.
            if not jobs:
                print(f"{company}: empty read, will record its current roles next run")
                continue
            newly_seeded.append(company)
        else:
            for j in new:
                posted = posted_at(j.posted)
                # An old posting showing up as "new" means the site skipped it on an earlier read; record it quietly.
                if posted and posted < datetime.now(timezone.utc) - MAX_AGE:
                    print(f"old, not alerting: {company} - {j.title} (posted {j.posted})")
                    continue
                has_time = posted and "T" in j.posted
                when = ("\nPosted " + (posted.astimezone(LOCAL_TZ).strftime("%b %d, %I:%M %p ET") if has_time
                                       else posted.strftime("%b %d"))) if posted else ""
                send(f"{company} careers: {j.title}", f"{j.location or 'See posting for location'}{when}", j.url)
                alerted.append((company, j.title, j.location, j.url, "careers page"))
                print(f"new: {company} - {j.title}  {j.url}")
        record += [j.id for j in new]
        seen.update(j.id for j in new)

    log_new_roles(alerted)
    if newly_seeded:
        send("Careers page alerts are on", "Now watching: " + ", ".join(newly_seeded) + ". You'll get an alert for each new US tech internship.",
             "https://github.com/SimplifyJobs/Summer2027-Internships")
    if record or newly_seeded:
        with open(SEEN_FILE, "a", encoding="utf-8") as f:
            f.writelines(i + "\n" for i in record)
            f.writelines(f"#seeded:{c}\n" for c in newly_seeded)
    if failed and len(failed) == len(COMPANIES):
        raise SystemExit("every careers site failed")


if __name__ == "__main__":
    main()
