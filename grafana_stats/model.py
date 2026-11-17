from datetime import date, timedelta
import json
from pathlib import Path


METRICS = ("issues_opened", "prs_opened", "prs_merged", "reviews_submitted", "first_reviews",
           "issue_comments", "pr_comments", "review_comments", "unclassified_comments")
TARGET_METRICS = ("reviews_submitted", "issue_comments", "pr_comments", "review_comments", "unclassified_comments")
FORMAT = "grafana-otel-user-days"


def days(start, end):
    current, stop = date.fromisoformat(start), date.fromisoformat(end)
    if current >= stop:
        raise ValueError("start must precede end (UTC, end exclusive)")
    while current < stop:
        yield current.isoformat()
        current += timedelta(days=1)


def affiliations(path, company):
    data = json.loads(Path(path).read_text())
    if data["schema_version"] != 1:
        raise ValueError("Unsupported affiliation schema version")
    result = {}
    for row in data["records"]:
        if row["company"] != company:
            continue
        uid = row["user_id"]
        if not isinstance(uid, str) or not uid:
            raise ValueError("Affiliations require stable GitHub user IDs")
        start = date.fromisoformat(row["start"] or "0001-01-01").isoformat()
        end = date.fromisoformat(row["end"] or "9999-12-31").isoformat()
        if start >= end or any(start < b and a < end for a, b in result.get(uid, [])):
            raise ValueError("Invalid or overlapping affiliation intervals")
        result.setdefault(uid, []).append((start, end))
    return result, data.get("observed_at")


def eligible(intervals, uid, day):
    return any(start <= day < end for start, end in intervals.get(uid, []))


def role(slug):
    special = {"governance-committee": "governance_committee", "technical-committee": "technical_committee",
               "community-manager": "community_manager", "community-managers": "community_manager"}
    if slug in special:
        return special[slug]
    for value in ("maintainer", "approver", "triager"):
        if slug.endswith(("-" + value, "-" + value + "s")):
            return value
    return None

