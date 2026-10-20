"""Fill a likelyread database with fake data to preview the dashboard.

Usage: python seed_demo.py /path/to/demo.db
Never point this at your real database.
"""

import json
from pathlib import Path
import random
import sqlite3
import sys
import time
import uuid

GMAIL = "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/42.0.2311.135 Safari/537.36 Edge/12.246 Mozilla/5.0"
APPLE = "Mozilla/5.0"
OUTLOOK = "Mozilla/4.0 (compatible; ms-office; MSOffice 16)"

CAMPAIGNS = {
    "Seed round outreach": (["Quick question about {co}", "Intro: Northwind x {co}", "Following up, {co}"], 0.62),
    "Customer onboarding": (["Welcome to Northwind, {co}", "Your setup call", "Getting started checklist"], 0.81),
    "Podcast pitches": (["Guest idea for your show", "Pitch: AI search for SMBs"], 0.34),
    None: (["Invoice {n}", "Re: contract draft", "Coffee next week?"], 0.5),
}
COMPANIES = ["Acme", "Globex", "Initech", "Umbrella", "Hooli", "Stark", "Wayne", "Tyrell", "Cyberdyne", "Soylent"]


def main(path: str) -> None:
    sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
    from app import DB  # creates the schema

    db = DB(path)
    now = time.time()
    rnd = random.Random(7)
    for campaign, (subjects, rate) in CAMPAIGNS.items():
        for _ in range(rnd.randint(9, 16)):
            eid = str(uuid.uuid4())
            co = rnd.choice(COMPANIES)
            sent = now - rnd.uniform(0.2, 28) * 86400
            subject = rnd.choice(subjects).format(co=co, n=rnd.randint(100, 999))
            rcpt = [f"{rnd.choice(['ceo', 'hello', 'jane', 'tom'])}@{co.lower()}.com"]
            db.execute(
                "INSERT INTO emails(id, subject, recipients, sent_at, campaign, sender) VALUES (?,?,?,?,?,?)",
                (eid, subject, json.dumps(rcpt), sent, campaign, rnd.choice(["sam@northwind.co", "sam@northwind.co", "sales@northwind.co"])),
            )
            db.execute("INSERT INTO hits(email_id, ts, ip, ua) VALUES (?,?,?,?)", (eid, sent + 3, "203.0.113.7", "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/141.0.0.0 Safari/537.36"))
            if rnd.random() < 0.15:
                db.execute("INSERT INTO hits(email_id, ts, ip, ua) VALUES (?,?,?,?)", (eid, sent + 40, "17.58.0.1", APPLE))
            if rnd.random() < rate:
                t = sent + rnd.expovariate(1 / 7200)
                for _ in range(rnd.choice([1, 1, 1, 2, 3])):
                    if t < now:
                        ua = rnd.choice([GMAIL, GMAIL, OUTLOOK])
                        ip = "66.249.84.%d" % rnd.randint(2, 250) if ua == GMAIL else "203.0.113.%d" % rnd.randint(2, 250)
                        db.execute("INSERT INTO hits(email_id, ts, ip, ua) VALUES (?,?,?,?)", (eid, t, ip, ua))
                    t += rnd.expovariate(1 / 86400)
    print(f"seeded {path}")


if __name__ == "__main__":
    if len(sys.argv) != 2:
        sys.exit(__doc__)
    main(sys.argv[1])
