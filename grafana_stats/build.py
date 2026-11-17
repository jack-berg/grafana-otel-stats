from bisect import bisect_right
from collections import Counter, defaultdict
from contextlib import closing
from datetime import datetime, timedelta, timezone
import fcntl
import hashlib
import json
import os
from pathlib import Path
import shutil
import sqlite3
from tempfile import TemporaryDirectory
from uuid import uuid4

from .source import normalize, now
from .model import FORMAT, METRICS, TARGET_METRICS, affiliations, days, eligible, role


EVENTS = """
CREATE TEMP VIEW events AS
SELECT author,repo,number,id,created_at at,'issues_opened' metric FROM parents WHERE kind='issue'
UNION ALL SELECT author,repo,number,id,created_at,'prs_opened' FROM parents WHERE kind='pr'
UNION ALL SELECT author,repo,number,id,merged_at,'prs_merged' FROM parents WHERE kind='pr' AND merged_at IS NOT NULL
UNION ALL SELECT author,repo,number,id,submitted_at,'reviews_submitted' FROM reviews WHERE state!='PENDING' AND submitted_at IS NOT NULL
UNION ALL SELECT author,repo,number,id,submitted_at,'first_reviews'
FROM (SELECT *,ROW_NUMBER() OVER (PARTITION BY author,repo,number ORDER BY submitted_at,id) rank
      FROM reviews WHERE state!='PENDING' AND submitted_at IS NOT NULL) WHERE rank=1
UNION ALL SELECT author,repo,number,id,created_at,
CASE kind WHEN 'issue_comment' THEN 'issue_comments' WHEN 'pr_comment' THEN 'pr_comments'
WHEN 'review_comment' THEN 'review_comments' ELSE 'unclassified_comments' END FROM comments;
"""


def write_json(path, value):
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(value, indent=2, ensure_ascii=False) + "\n")


def role_snapshots(db, intervals):
    by_day = {}
    for raw, in db.execute("SELECT data FROM snapshots"):
        data = json.loads(raw)
        if data["activity_org"] != "open-telemetry":
            continue
        day = data["completed_at"][:10]
        if day not in by_day or data["completed_at"] > by_day[day]["completed_at"]:
            by_day[day] = data
    result = []
    for day, snapshot in sorted(by_day.items()):
        assignments = defaultdict(list)
        query = """SELECT m.user_id,t.id,t.slug FROM team_members m JOIN teams t
                   ON m.snapshot=t.snapshot AND m.team_id=t.id WHERE m.snapshot=? ORDER BY m.user_id,t.slug"""
        for uid, tid, slug in db.execute(query, (snapshot["snapshot"],)):
            value = role(slug)
            if value and uid in intervals:
                assignments[uid].append({"role": value, "team_id": tid, "team": slug})
        result.append({"day": day, "observed_at": snapshot["completed_at"], "assignments": assignments})
    return result


def roles_at(snapshots, snapshot_days, day):
    if not snapshots:
        return None, "unknown"
    index = bisect_right(snapshot_days, day) - 1
    if index < 0:
        return snapshots[0], "assumed"
    snapshot = snapshots[index]
    return snapshot, "snapshot" if snapshot["day"] == day else "carried_forward"


def coverage(job, repositories):
    current = job.get("repository_coverage", {}) if job.get("scan_version") == 3 else {}
    result = {}
    for rid in repositories:
        value = current.get(rid, {})
        result[rid] = {"scan": {"start": value["collected_start"], "end": value["end"]} if value.get("collected_start") else None,
                       "verified": {"start": value["full_start"], "end": value["end"]} if value.get("full_start") and not job.get("unavailable_parents", {}).get(rid) else None,
                       "unavailable_parents": [{"number": item["number"], "status": item["status"]}
                                               for item in job.get("unavailable_parents", {}).get(rid, {}).values()]}
    return result


def activity_status(day, bounds, inventory):
    if inventory and all(bounds[rid]["verified"] and bounds[rid]["verified"]["start"] <= day < bounds[rid]["verified"]["end"] for rid in inventory):
        return "verified"
    if inventory and all(bounds[rid]["scan"] and bounds[rid]["scan"]["start"] <= day < bounds[rid]["scan"]["end"] for rid in inventory):
        return "provisional"
    return "unscanned"


def validate_output(output):
    if output.is_symlink():
        raise ValueError("Output cannot be a symbolic link")
    if not output.exists():
        return
    manifest = output / "manifest.json"
    if manifest.is_symlink() or not manifest.is_file() or json.loads(manifest.read_text()).get("format") != FORMAT:
        raise ValueError("Existing output is not an intermediate dataset; refusing to replace it")
    for path in output.rglob("*"):
        relative = path.relative_to(output)
        allowed = path.is_dir() and relative.parts == ("days",) or path.is_file() and (
            len(relative.parts) == 1 and path.name in {"manifest.json", "users.json", "repositories.json"}
            or len(relative.parts) == 2 and relative.parts[0] == "days" and path.suffix == ".json" and len(path.stem) == 10)
        if path.is_symlink() or not allowed:
            raise ValueError("Output contains unexpected files; refusing to replace it")


def publish(staging, output):
    validate_output(output)
    previous = output.with_name("." + output.name + "." + uuid4().hex + ".previous")
    existed = output.exists()
    if existed:
        output.rename(previous)
    try:
        staging.rename(output)
    except OSError:
        if existed:
            previous.rename(output)
        raise
    if existed:
        shutil.rmtree(previous)


def build(source, output, affiliation_file=None, start=None, end=None, company="grafana", progress=None):
    source, output = Path(source).resolve(), Path(output).absolute()
    if not source.is_dir() or source == output.resolve() or source in output.resolve().parents or output.resolve() in source.parents:
        raise ValueError("Source and output must be separate, non-nested directories")
    intervals, affiliation_observed_at = affiliations(affiliation_file or source / "affiliations.json", company)
    output.parent.mkdir(parents=True, exist_ok=True)
    with (output.parent / ("." + output.name + ".lock")).open("a") as lock:
        try:
            fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            raise ValueError("Another intermediate builder is writing this output") from None
        validate_output(output)
        with TemporaryDirectory(prefix="grafana-otel-private-") as temporary:
            temporary = Path(temporary)
            database = temporary / "source.sqlite"
            digest, count, latest = hashlib.sha256(), 0, None
            job_path = source / "backfill.json"
            job = json.loads(job_path.read_text()) if job_path.exists() else {}
            def observe(path, record):
                nonlocal count, latest
                count += 1
                latest = max(latest or "", record["fetched_at"])
                digest.update(json.dumps([path, record["fetched_at"], (source / path).stat().st_size]).encode())
                if progress and count % 100 == 0:
                    progress(f"indexed {count} raw envelopes")
            if progress:
                progress("rebuilding a private temporary index from raw data")
            normalize(source, database, observe)
            if not count:
                raise ValueError("No raw data found")
            with closing(sqlite3.connect(database)) as db:
                if db.execute("PRAGMA integrity_check").fetchall() != [("ok",)]:
                    raise ValueError("Temporary source index failed its integrity check")
                if not job:
                    row = db.execute("SELECT data FROM common_backfills ORDER BY observed_at DESC LIMIT 1").fetchone()
                    job = json.loads(row[0]) if row else {}
                db.executescript(EVENTS)
                end = end or datetime.now(timezone.utc).date().isoformat()
                if end > datetime.now(timezone.utc).date().isoformat():
                    raise ValueError("Use completed UTC days")
                start = start or job.get("common_start") or (datetime.fromisoformat(end) - timedelta(days=1)).date().isoformat()
                period = list(days(start, end))
                snapshots = role_snapshots(db, intervals)
                snapshot_days = [item["day"] for item in snapshots]
                activity = defaultdict(lambda: defaultdict(lambda: {"counts": Counter(), "targets": defaultdict(set)}))
                qualified = set()
                for uid, rid, number, _, stamp, metric in db.execute("SELECT * FROM events WHERE at>=? AND at<? AND author IS NOT NULL ORDER BY at,id,metric", (start, end)):
                    day = stamp[:10]
                    if not eligible(intervals, uid, day):
                        continue
                    value = activity[(day, uid)][rid]
                    value["counts"][metric] += 1
                    if metric in TARGET_METRICS:
                        value["targets"][metric].add(number)
                    qualified.add(uid)
                for day in period:
                    snapshot, _ = roles_at(snapshots, snapshot_days, day)
                    if snapshot:
                        qualified.update(uid for uid, roles in snapshot["assignments"].items() if roles and eligible(intervals, uid, day))
                users = {uid: {"id": uid, "login": login} for uid, login in db.execute("SELECT id,login FROM users") if uid in qualified}
                repositories = {}
                for rid, name, archived in db.execute("SELECT id,name,archived FROM repositories"):
                    repositories[rid] = {"id": rid, "name": name, "archived": bool(archived)}
                inventory = [repo["node_id"] for repo in job.get("repositories", [])] or list(repositories)
                for rid in inventory:
                    if rid not in repositories:
                        raise ValueError("Frozen inventory contains a repository without metadata")
                bounds = coverage(job, repositories)
                unresolved = {table: db.execute(f"SELECT COUNT(*) FROM {table} c LEFT JOIN parents p ON c.repo=p.repo AND c.number=p.number WHERE p.repo IS NULL").fetchone()[0]
                              for table in ("comments", "reviews")}
            with TemporaryDirectory(prefix="." + output.name + "-", dir=output.parent) as staging_directory:
                staging = Path(staging_directory) / "dataset"
                staging.mkdir()
                row_count, active_days, role_days, unknown_role_days, assumed_role_days = 0, 0, 0, 0, 0
                role_users = set()
                for day_index, day in enumerate(period):
                    if progress and day_index % 100 == 0:
                        progress(f"writing user-days: {day_index}/{len(period)} UTC days")
                    snapshot, roles_status = roles_at(snapshots, snapshot_days, day)
                    status = activity_status(day, bounds, inventory)
                    rows = []
                    for uid in sorted(qualified, key=lambda key: ((users[key]["login"] or "").casefold(), key)):
                        if not eligible(intervals, uid, day):
                            continue
                        contributions = []
                        totals = dict.fromkeys(METRICS, 0)
                        for rid, value in sorted(activity.get((day, uid), {}).items()):
                            if rid not in repositories:
                                raise ValueError("Contribution repository metadata is missing")
                            counts = {metric: value["counts"][metric] for metric in METRICS}
                            for metric, amount in counts.items():
                                totals[metric] += amount
                            contributions.append({"repository": repositories[rid]["name"], "repository_id": rid,
                                                  "coverage_status": activity_status(day, bounds, [rid]), "counts": counts,
                                                  "targets": {metric: sorted(numbers) for metric, numbers in value["targets"].items()}})
                        roles = snapshot["assignments"].get(uid, []) if snapshot else []
                        role_days += bool(roles)
                        unknown_role_days += roles_status == "unknown"
                        assumed_role_days += roles_status == "assumed"
                        if roles:
                            role_users.add(uid)
                        rows.append({"login": users[uid]["login"], "user_id": uid, "day": day, "roles": roles,
                                     "roles_status": roles_status,
                                     "roles_observed_at": snapshot["observed_at"] if snapshot else None,
                                     "activity_status": status, "counts": totals, "repositories": contributions})
                        row_count += 1
                        active_days += bool(contributions)
                    write_json(staging / "days" / (day + ".json"), rows)
                write_json(staging / "users.json", [users[uid] for uid in sorted(users)])
                write_json(staging / "repositories.json", [repositories[rid] for rid in sorted(repositories)])
                manifest = {"format": FORMAT, "schema_version": 3, "generated_at": now(), "company": company,
                            "range": {"start": start, "end": end}, "users": len(users), "user_days": row_count,
                            "observed_active_user_days": active_days, "source_observed_at": latest,
                            "source_snapshot": {"envelopes": count, "file_list_fingerprint": digest.hexdigest()},
                            "affiliation_observed_at": affiliation_observed_at,
                            "scripts_revision": os.environ.get("OTEL_SCRIPTS_REVISION", "unversioned-local"),
                            "coverage": bounds, "inventory": inventory,
                            "review_accuracy": {"policy": "earliest-observed", "discovery_complete": bool(job.get("review_discovery_complete"))},
                            "unresolved_source_parents": unresolved,
                            "role_policy": "first-snapshot-backward",
                            "role_observations": [item["observed_at"] for index, item in enumerate(snapshots) if index == 0 or item["day"] < end],
                            "role_summary": {"first_observed_day": snapshot_days[0] if snapshot_days else None,
                                             "users_with_roles": len(role_users), "user_days_with_roles": role_days,
                                             "unknown_user_days": unknown_role_days, "assumed_user_days": assumed_role_days},
                            "metrics": list(METRICS),
                            "limitations": ["Scanned frontiers are provisional until full verification; unseen records can change any metric.",
                                            "Zero counts on provisional or unscanned days do not establish inactivity.",
                                            "First reviews are computed across all observed history before affiliation/range filtering; earlier history can move them to another day.",
                                            "Roles cover recognized accessible teams only. The first completed snapshot is assumed backward; subsequent observations apply from their observation day and are carried forward until replaced. Roles are unknown only when no completed snapshot exists.",
                                            "Daily rows include quiet eligible days only for people with an eligible contribution or role somewhere in this export range.",
                                            "Private affiliations are attribution inputs, not verified employment history. No affiliation roster or intervals are exported.",
                                            "Raw-file and progress snapshots are read separately while collection may continue. File-list fingerprints are not content authenticity proofs.",
                                            "Rebuilds replace the whole selected interval, including retractions and earlier first-review dates."]}
                write_json(staging / "manifest.json", manifest)
                publish(staging, output)
            if progress:
                progress(f"built {row_count} user-days for {len(users)} qualifying users")
                progress(f"roles: {len(role_users)} users with roles; {assumed_role_days} user-days assumed, {unknown_role_days} unknown; first observation {snapshot_days[0] if snapshot_days else 'unavailable'}")
            return manifest
