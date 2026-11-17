from contextlib import closing
from datetime import datetime, timezone
import gzip
import json
from pathlib import Path
import sqlite3


SCHEMA = """
CREATE TABLE users (id TEXT PRIMARY KEY, login TEXT);
CREATE TABLE repositories (id TEXT PRIMARY KEY, name TEXT, archived INTEGER);
CREATE TABLE parents (repo TEXT, number INTEGER, id TEXT, kind TEXT, author TEXT, created_at TEXT, merged_at TEXT, PRIMARY KEY(repo,number));
CREATE TABLE reviews (id TEXT PRIMARY KEY, repo TEXT, number INTEGER, author TEXT, submitted_at TEXT, state TEXT);
CREATE TABLE comments (id TEXT PRIMARY KEY, repo TEXT, number INTEGER, author TEXT, created_at TEXT, kind TEXT);
CREATE TABLE snapshots (id TEXT PRIMARY KEY, data TEXT);
CREATE TABLE common_backfills (id TEXT PRIMARY KEY, observed_at TEXT, data TEXT);
CREATE TABLE teams (snapshot TEXT, id TEXT, slug TEXT, PRIMARY KEY(snapshot,id));
CREATE TABLE team_members (snapshot TEXT, team_id TEXT, user_id TEXT, PRIMARY KEY(snapshot,team_id,user_id));
"""


def now():
    return datetime.now(timezone.utc).isoformat(timespec="microseconds").replace("+00:00", "Z")


def raw_records(root):
    raw = Path(root) / "raw"
    paths = sorted([*raw.glob("*/*.json"), *raw.glob("*/*.json.gz")])
    for path in paths:
        if path.suffix == ".gz" and path.with_suffix("").exists():
            continue
        opener = gzip.open if path.suffix == ".gz" else open
        try:
            with opener(path, "rt", encoding="utf-8") as stream:
                record = json.load(stream)
            if not isinstance(record, dict) or not {"status", "kind", "data", "repo", "task", "fetched_at"} <= record.keys():
                raise ValueError("Invalid raw envelope")
        except (OSError, EOFError, ValueError) as error:
            raise ValueError(f"Cannot read {path.relative_to(root)}: {error}") from error
        yield str(path.relative_to(root)), record


def identity(db, obj):
    if not obj:
        return None
    uid = obj.get("node_id") or obj.get("id")
    if uid is None:
        return None
    uid = str(uid)
    db.execute("INSERT INTO users VALUES (?,?) ON CONFLICT(id) DO UPDATE SET login=COALESCE(excluded.login,users.login)", (uid, obj.get("login")))
    return uid


def retained_author(db, table, oid, author):
    previous = db.execute(f'SELECT author FROM "{table}" WHERE id=?', (oid,)).fetchone()
    return previous[0] if previous and previous[0] is not None else author


def save_review(db, obj, repo, number, graphql=False):
    oid = obj["id"] if graphql else obj["node_id"]
    author = retained_author(db, "reviews", oid, identity(db, obj.get("author" if graphql else "user")))
    db.execute("INSERT OR REPLACE INTO reviews VALUES (?,?,?,?,?,?)", (
        oid, repo, number, author, obj.get("submittedAt" if graphql else "submitted_at"), obj["state"].upper()))


def normalize(root, target, progress=None):
    with closing(sqlite3.connect(target)) as db:
        with db:
            db.executescript(SCHEMA)
            for source, record in raw_records(root):
                if progress:
                    progress(source, record)
                data = record["data"]
                if record["status"] != 200 or isinstance(data, dict) and data.get("errors"):
                    continue
                kind, repo = record["kind"], record["repo"]
                values = data if isinstance(data, list) else [data]
                if kind == "repos":
                    for obj in values:
                        if not obj.get("private"):
                            db.execute("INSERT OR REPLACE INTO repositories VALUES (?,?,?)", (obj["node_id"], obj["full_name"], obj["archived"]))
                elif kind in ("issues", "pulls", "issue", "pull"):
                    for obj in values:
                        author = identity(db, obj.get("user"))
                        previous = db.execute("SELECT merged_at,author FROM parents WHERE repo=? AND number=?", (repo, obj["number"])).fetchone()
                        if previous and previous[1] is not None:
                            author = previous[1]
                        pull = kind in ("pull", "pulls") or "pull_request" in obj
                        merged = obj.get("merged_at") if kind in ("pull", "pulls") else (obj.get("pull_request") or {}).get("merged_at")
                        if merged is None and previous:
                            merged = previous[0]
                        db.execute("INSERT OR REPLACE INTO parents VALUES (?,?,?,?,?,?,?)", (
                            repo, obj["number"], obj["node_id"], "pr" if pull else "issue", author, obj["created_at"], merged))
                elif kind == "reviews":
                    number = int(record["request"]["path"].split("/")[-2])
                    for obj in values:
                        save_review(db, obj, repo, number)
                elif kind in ("graphql_reviews", "window_reviews"):
                    for alias, pr in data["data"].items():
                        if alias == "rateLimit" or not pr or "reviews" not in pr:
                            continue
                        for obj in pr["reviews"]["nodes"]:
                            if obj is not None:
                                save_review(db, obj, repo, pr["number"], graphql=True)
                elif kind in ("conversation_comments", "review_comments"):
                    for obj in values:
                        number = int((obj.get("issue_url") or obj["pull_request_url"]).rsplit("/", 1)[-1])
                        author = retained_author(db, "comments", obj["node_id"], identity(db, obj.get("user")))
                        db.execute("INSERT OR REPLACE INTO comments VALUES (?,?,?,?,?,?)", (
                            obj["node_id"], repo, number, author, obj["created_at"],
                            "review_comment" if kind == "review_comments" else "conversation_comment"))
                elif kind == "members":
                    for obj in values:
                        identity(db, obj)
                elif kind in ("teams", "team_members"):
                    snapshot = record["task"].split("/")[1]
                    for obj in values:
                        if kind == "teams":
                            db.execute("INSERT OR REPLACE INTO teams VALUES (?,?,?)", (snapshot, str(obj["id"]), obj["slug"]))
                        else:
                            uid = identity(db, obj)
                            db.execute("INSERT OR IGNORE INTO team_members VALUES (?,?,?)", (snapshot, repo, uid))
                elif kind == "snapshot":
                    snapshot = {key: data[key] for key in ("snapshot", "completed_at", "activity_org")}
                    db.execute("INSERT OR REPLACE INTO snapshots VALUES (?,?)", (snapshot["snapshot"], json.dumps(snapshot)))
                elif kind == "common_backfill":
                    db.execute("INSERT OR REPLACE INTO common_backfills VALUES (?,?,?)", (data["id"], record["fetched_at"], json.dumps(data)))
            db.execute("""UPDATE comments SET kind=CASE
                WHEN (SELECT kind FROM parents p WHERE p.repo=comments.repo AND p.number=comments.number)='issue' THEN 'issue_comment'
                WHEN (SELECT kind FROM parents p WHERE p.repo=comments.repo AND p.number=comments.number)='pr' THEN 'pr_comment'
                ELSE 'unclassified_comment' END WHERE kind='conversation_comment'""")
