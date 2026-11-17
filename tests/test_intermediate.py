from contextlib import redirect_stderr, redirect_stdout
from datetime import datetime, timedelta, timezone
import fcntl
import gzip
import hashlib
import io
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from grafana_stats.build import build
from grafana_stats.cli import main


class IntermediateTest(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)
        self.source = self.root / "private"
        self.source.mkdir()
        self.output = self.root / "public"
        self.sequence = 0
        self.alice = {"node_id": "U_1", "id": "U_1", "login": "alice", "private_bio": "PRIVATE_BODY_SENTINEL"}
        self.bob = {"node_id": "U_2", "id": "U_2", "login": "PRIVATE_ROSTER_ONLY"}
        self.charlie = {"node_id": "U_3", "id": "U_3", "login": "charlie"}
        self.outsider = {"node_id": "U_4", "id": "U_4", "login": "OUTSIDE_COMPANY"}
        (self.source / "affiliations.json").write_text(json.dumps({"schema_version": 1, "observed_at": "2024-01-04T00:00:00Z", "records": [
            {"user_id": "U_1", "company": "grafana", "start": "2024-01-02", "end": "2024-01-05"},
            {"user_id": "U_2", "company": "grafana", "start": None, "end": None},
            {"user_id": "U_3", "company": "grafana", "start": None, "end": None}]}))
        self.save("repos", [{"node_id": "R_1", "full_name": "open-telemetry/java", "archived": False},
                            {"node_id": "R_2", "full_name": "open-telemetry/empty", "archived": True}])
        self.save("issues", [{"node_id": "I_1", "number": 1, "user": self.alice,
                              "created_at": "2024-01-01T12:00:00Z", "updated_at": "2024-01-03T00:00:00Z"},
                             {"node_id": "PR_10", "number": 10, "user": self.alice, "created_at": "2024-01-01T00:00:00Z",
                              "updated_at": "2024-01-04T00:00:00Z", "pull_request": {"merged_at": "2024-01-03T00:00:00Z"}}])
        self.save("conversation_comments", [{"node_id": "C_1", "user": self.alice, "created_at": "2024-01-02T00:00:00Z",
                                             "updated_at": "2024-01-02T00:00:00Z", "issue_url": "https://api.github.com/repos/open-telemetry/java/issues/999"},
                                            {"node_id": "C_2", "user": self.outsider, "created_at": "2024-01-02T00:00:00Z",
                                             "updated_at": "2024-01-02T00:00:00Z", "issue_url": "https://api.github.com/repos/open-telemetry/java/issues/1"}])
        self.reviews([("RV_1", "2024-01-03T00:00:00Z"), ("RV_2", "2024-01-04T00:00:00Z"), ("PENDING", None)], more=True)
        self.snapshot("2024-01-03T00:00:00Z", include_alice=True)
        self.snapshot("2024-01-04T00:00:00Z", include_alice=False)
        self.job = {"id": "job", "scan_version": 3, "target_start": "2024-01-01", "end": "2024-01-05",
                    "repositories": [{"node_id": "R_1"}, {"node_id": "R_2"}], "review_discovery_complete": False,
                    "repository_coverage": {"R_1": {"collected_start": "2024-01-03", "end": "2024-01-05"},
                                            "R_2": {"collected_start": "2024-01-04", "end": "2024-01-05"}},
                    "unavailable_parents": {"R_1": {"999": {"number": 999, "status": 404}}}}
        self.save("common_backfill", self.job)

    def tearDown(self):
        self.temp.cleanup()

    def save(self, kind, data, task=None, repo="R_1", request=None):
        self.sequence += 1
        stamp = (datetime(2024, 1, 5, tzinfo=timezone.utc) + timedelta(seconds=self.sequence)).isoformat().replace("+00:00", "Z")
        path = self.source / "raw" / stamp[:10] / (stamp.replace(":", "") + ".json")
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps({"kind": kind, "data": data, "task": task or kind, "repo": repo, "fetched_at": stamp,
                                    "status": 200, "headers": {}, "request": request}))

    def reviews(self, values, more=False):
        nodes = [{"id": ident, "author": self.alice, "state": "PENDING" if stamp is None else "APPROVED", "submittedAt": stamp}
                 for ident, stamp in values]
        self.save("window_reviews", {"data": {"p0": {"number": 10, "reviews": {"nodes": nodes,
                  "pageInfo": {"hasPreviousPage": more, "startCursor": "older" if more else None}}}}})

    def snapshot(self, stamp, include_alice, include_bob=False):
        prefix = "snapshot/" + stamp
        self.save("members", [self.alice, self.bob, self.charlie], prefix + "/members")
        self.save("teams", [{"id": 7, "slug": "java-approvers", "parent": None},
                            {"id": 8, "slug": "technical-committee", "parent": None}], prefix + "/teams")
        approvers = [self.alice] if include_alice else []
        if include_bob:
            approvers.append(self.bob)
        self.save("team_members", approvers, prefix + "/team/7", "7")
        self.save("team_members", [self.charlie], prefix + "/team/8", "8")
        self.save("snapshot", {"snapshot": stamp, "completed_at": stamp, "activity_org": "open-telemetry", "member_org": "grafana"})

    def run_build(self, **kwargs):
        return build(self.source, self.output, start="2024-01-01", end="2024-01-05", **kwargs)

    def month_rows(self, month):
        with gzip.open(self.output / "months" / (month + ".json.gz"), "rt", encoding="utf-8") as stream:
            return json.load(stream)

    def rows(self, day):
        return {row["user_id"]: row for row in self.month_rows(day[:7]) if row["day"] == day}

    def exported_text(self):
        index = (self.output / "index.json").read_text()
        return index + "".join(json.dumps(self.month_rows(path.name[:7])) for path in (self.output / "months").iterdir())

    def hashes(self, directory):
        return {str(p.relative_to(directory)): hashlib.sha256(p.read_bytes()).hexdigest() for p in directory.rglob("*") if p.is_file()}

    def test_live_read_only_filtering_roles_coverage_and_range_distinct_references(self):
        with (self.source / ".writer.lock").open("a") as lock:
            fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
            before = self.hashes(self.source)
            with patch("socket.create_connection", side_effect=AssertionError("No network")):
                manifest = self.run_build()
            self.assertEqual(before, self.hashes(self.source))
        self.assertEqual((manifest["users"], manifest["user_days"]), (2, 7))
        self.assertEqual(set(self.rows("2024-01-01")), {"U_3"})
        jan2, jan3, jan4 = (self.rows(day)["U_1"] for day in ("2024-01-02", "2024-01-03", "2024-01-04"))
        self.assertEqual(jan2["roles_status"], "assumed")
        self.assertEqual(jan2["roles"], jan3["roles"])
        self.assertEqual(jan2["roles_observed_at"], "2024-01-03T00:00:00Z")
        self.assertEqual(jan2["counts"]["unclassified_comments"], 1)
        self.assertEqual(jan3["roles"][0]["role"], "approver")
        self.assertEqual(jan4["roles"], [])
        self.assertEqual(self.rows("2024-01-03")["U_3"]["roles"][0]["role"], "technical_committee")
        self.assertEqual(jan3["activity_status"], "unscanned")
        self.assertEqual(jan4["activity_status"], "provisional")
        self.assertEqual(jan3["counts"]["first_reviews"], 1)
        self.assertEqual(jan4["counts"]["first_reviews"], 0)
        self.assertEqual(jan3["login"], "alice")
        self.assertEqual(jan3["repositories"][0]["repository"], "open-telemetry/java")
        self.assertEqual(sum(row["counts"]["reviews_submitted"] for row in (jan3, jan4)), 2)
        targets = {number for row in (jan3, jan4) for repo in row["repositories"] for number in repo["targets"].get("reviews_submitted", [])}
        self.assertEqual(targets, {10})
        self.assertEqual(jan2["repositories"][0]["targets"]["unclassified_comments"], [999])
        self.assertTrue(all("events" not in repo for row in (jan3, jan4) for repo in row["repositories"]))
        self.assertEqual(manifest["schema_version"], 4)
        self.assertEqual({p.name for p in self.output.iterdir()}, {"index.json", "months"})
        self.assertEqual(set(jan3["roles"][0]), {"role", "team_id", "team"})
        self.assertEqual(set(manifest["repositories"][0]), {"id", "name", "archived"})
        self.assertEqual(manifest["roles"], [{"role": "approver", "team_id": "7", "team": "java-approvers"},
                                             {"role": "technical_committee", "team_id": "8", "team": "technical-committee"}])
        self.assertEqual(manifest["role_summary"], {"first_observed_day": "2024-01-03", "users_with_roles": 2,
                                                   "user_days_with_roles": 6, "unknown_user_days": 0, "assumed_user_days": 3})
        exported = self.exported_text()
        for private in ("PRIVATE_ROSTER_ONLY", "PRIVATE_BODY_SENTINEL", "OUTSIDE_COMPANY", "affiliations.json", "RV_1", "PENDING", str(self.source)):
            self.assertNotIn(private, exported)

    def test_role_only_users_qualify_before_the_first_snapshot_with_affiliation_filtering(self):
        self.snapshot("2024-01-05T00:00:00Z", include_alice=False, include_bob=True)
        manifest = build(self.source, self.output, start="2024-01-01", end="2024-01-02")
        rows = self.rows("2024-01-01")
        self.assertEqual(set(rows), {"U_3"})
        self.assertEqual(rows["U_3"]["roles"][0]["role"], "technical_committee")
        self.assertEqual(rows["U_3"]["roles_status"], "assumed")
        self.assertEqual(rows["U_3"]["roles_observed_at"], "2024-01-03T00:00:00Z")
        self.assertTrue(all(value == 0 for value in rows["U_3"]["counts"].values()))
        self.assertEqual(manifest["role_observations"], ["2024-01-03T00:00:00Z"])
        self.assertEqual(manifest["role_summary"]["assumed_user_days"], 1)
        self.assertEqual(manifest["role_policy"], "first-snapshot-backward")
        build(self.source, self.output, start="2024-01-04", end="2024-01-06")
        self.assertEqual(self.rows("2024-01-04")["U_2"]["roles"], [])
        self.assertEqual(self.rows("2024-01-05")["U_2"]["roles"][0]["role"], "approver")

    def test_roles_stay_unknown_without_a_completed_snapshot(self):
        for path in (self.source / "raw").glob("*/*.json"):
            if json.loads(path.read_text())["kind"] == "snapshot":
                path.unlink()
        manifest = self.run_build()
        self.assertEqual({user["id"] for user in manifest["people"]}, {"U_1"})
        self.assertEqual(self.rows("2024-01-02")["U_1"]["roles"], [])
        self.assertEqual(self.rows("2024-01-02")["U_1"]["roles_status"], "unknown")
        self.assertIsNone(self.rows("2024-01-02")["U_1"]["roles_observed_at"])
        self.assertEqual(manifest["role_summary"]["unknown_user_days"], 3)
        self.assertEqual(manifest["role_summary"]["assumed_user_days"], 0)

    def test_review_transports_gzip_and_deleted_authors_preserve_deduplicated_counts(self):
        self.save("graphql_reviews", {"data": {"p0": {"number": 10, "reviews": {"nodes": [None,
            {"id": "RV_1", "author": {"id": "U_1", "login": "alice-renamed"}, "state": "APPROVED", "submittedAt": "2024-01-03T00:00:00Z"}]}}}})
        self.save("reviews", [{"node_id": "RV_2", "user": None, "state": "DISMISSED", "submitted_at": "2024-01-04T00:00:00Z"}],
                  request={"path": "/repos/open-telemetry/java/pulls/10/reviews"})
        paths = sorted((self.source / "raw").glob("*/*.json"))
        for path in (paths[0], paths[-1]):
            with gzip.open(str(path) + ".gz", "wb") as stream:
                stream.write(path.read_bytes())
        paths[-1].unlink()
        manifest = self.run_build()
        self.assertEqual(manifest["source_snapshot"]["envelopes"], len(paths))
        for day in ("2024-01-03", "2024-01-04"):
            self.assertEqual(self.rows(day)["U_1"]["counts"]["reviews_submitted"], 1)
            self.assertEqual(self.rows(day)["U_1"]["login"], "alice-renamed")
        self.assertEqual(self.rows("2024-01-03")["U_1"]["counts"]["first_reviews"], 1)

    def test_private_repository_metadata_is_not_exported(self):
        self.save("repos", [{"node_id": "PRIVATE", "full_name": "PRIVATE_REPOSITORY", "archived": False, "private": True}])
        self.run_build()
        self.assertNotIn("PRIVATE_REPOSITORY", self.exported_text())

    def test_earlier_history_retracts_first_review_and_replaces_stale_days(self):
        self.reviews([("RV_EXTRA", "2024-01-03T11:00:00Z")], more=True)
        self.run_build()
        self.assertEqual(self.rows("2024-01-03")["U_1"]["repositories"][0]["targets"]["reviews_submitted"], [10])
        self.reviews([("RV_0", "2024-01-01T00:00:00Z")])
        self.run_build()
        self.assertEqual(self.rows("2024-01-03")["U_1"]["counts"]["first_reviews"], 0)
        self.assertEqual(self.rows("2024-01-03")["U_1"]["counts"]["reviews_submitted"], 2)
        build(self.source, self.output, start="2024-01-03", end="2024-01-04")
        self.assertEqual([p.name for p in (self.output / "months").iterdir()], ["2024-01.json.gz"])
        self.assertEqual({row["day"] for row in self.month_rows("2024-01")}, {"2024-01-03"})

    def test_month_boundaries_deterministic_compression_and_stale_month_removal(self):
        index = build(self.source, self.output, start="2023-12-31", end="2024-03-02")
        self.assertEqual([(item["month"], item["start"], item["end"]) for item in index["months"]], [
            ("2023-12", "2023-12-31", "2024-01-01"), ("2024-01", "2024-01-01", "2024-02-01"),
            ("2024-02", "2024-02-01", "2024-03-01"), ("2024-03", "2024-03-01", "2024-03-02")])
        keys = []
        for item in index["months"]:
            payload = (self.output / item["file"]).read_bytes()
            self.assertEqual(hashlib.sha256(payload).hexdigest(), item["sha256"])
            self.assertEqual(payload[4:8], b"\x00\x00\x00\x00")
            rows = json.loads(gzip.decompress(payload))
            self.assertEqual(len(rows), item["user_days"])
            self.assertEqual(sum(row["user_id"] == "U_3" for row in rows), (datetime.fromisoformat(item["end"]) - datetime.fromisoformat(item["start"])).days)
            self.assertTrue(all(item["start"] <= row["day"] < item["end"] for row in rows))
            order = [(row["day"], row["login"].casefold(), row["user_id"]) for row in rows]
            self.assertEqual(order, sorted(order))
            keys.extend((row["day"], row["user_id"]) for row in rows)
        self.assertEqual(len(keys), index["user_days"])
        self.assertEqual(len(keys), len(set(keys)))
        self.assertIn(("2024-02-29", "U_3"), keys)
        before = self.hashes(self.output / "months")
        rebuilt = build(self.source, self.output, start="2023-12-31", end="2024-03-02")
        self.assertEqual(index["months"], rebuilt["months"])
        self.assertEqual(before, self.hashes(self.output / "months"))
        build(self.source, self.output, start="2024-01-01", end="2024-01-02")
        self.assertEqual({p.name for p in (self.output / "months").iterdir()}, {"2024-01.json.gz"})
        self.assertEqual({row["day"] for row in self.month_rows("2024-01")}, {"2024-01-01"})

    def test_daily_export_migrates_without_overwriting_unknown_files(self):
        self.output.mkdir()
        (self.output / "manifest.json").write_text(json.dumps({"format": "grafana-otel-user-days", "schema_version": 3}))
        for name in ("users.json", "repositories.json"):
            (self.output / name).write_text("[]")
        (self.output / "days").mkdir()
        (self.output / "days/2024-01-01.json").write_text("[]")
        unknown = self.output / "keep.txt"
        unknown.write_text("user work")
        with self.assertRaisesRegex(ValueError, "unexpected files"):
            self.run_build()
        self.assertEqual(unknown.read_text(), "user work")
        unknown.unlink()
        index = self.run_build()
        self.assertEqual(index["schema_version"], 4)
        self.assertEqual({p.name for p in self.output.iterdir()}, {"index.json", "months"})
        self.assertEqual(len(self.month_rows("2024-01")), index["user_days"])

    def test_failure_preserves_existing_output_and_rejects_unknown_files(self):
        self.run_build()
        before = self.hashes(self.output)
        with patch("grafana_stats.build.gzip.GzipFile", side_effect=OSError("Disk full")):
            with self.assertRaises(OSError):
                self.run_build()
        self.assertEqual(before, self.hashes(self.output))
        path = self.source / "raw/2024-01-05/broken.json"
        path.write_text("{")
        with self.assertRaises(ValueError):
            self.run_build()
        self.assertEqual(before, self.hashes(self.output))
        path.unlink()
        (self.output / "keep.txt").write_text("user work")
        with self.assertRaisesRegex(ValueError, "unexpected files"):
            self.run_build()
        self.assertEqual((self.output / "keep.txt").read_text(), "user work")

    def test_affiliation_boundaries_and_incomplete_role_snapshots(self):
        self.save("teams", [{"id": 9, "slug": "java-maintainers", "parent": None}], "snapshot/pending/teams")
        self.save("team_members", [self.bob], "snapshot/pending/team/9", "9")
        self.run_build()
        index = json.loads((self.output / "index.json").read_text())
        self.assertNotIn("U_2", {user["id"] for user in index["people"]})
        with self.assertRaisesRegex(ValueError, "non-nested"):
            build(self.source, self.source / "output")
        data = json.loads((self.source / "affiliations.json").read_text())
        data["records"].append(data["records"][0])
        (self.source / "affiliations.json").write_text(json.dumps(data))
        with self.assertRaisesRegex(ValueError, "overlapping"):
            self.run_build()

    def test_cli_is_tokenless_and_role_only_days_can_be_verified(self):
        for value in self.job["repository_coverage"].values():
            value["full_start"] = "2024-01-01"
        self.job["unavailable_parents"] = {}
        self.save("common_backfill", self.job)
        with redirect_stdout(io.StringIO()) as output, redirect_stderr(io.StringIO()):
            code = main(["--source", str(self.source), "--output", str(self.output), "--start", "2024-01-01", "--end", "2024-01-06"])
        self.assertEqual(code, 0)
        self.assertEqual(json.loads(output.getvalue())["users"], 2)
        self.assertEqual(self.rows("2024-01-03")["U_3"]["activity_status"], "verified")
        self.assertTrue(all(value == 0 for value in self.rows("2024-01-03")["U_3"]["counts"].values()))
        self.assertEqual(set(self.rows("2024-01-05")), {"U_3"})
        self.assertEqual(self.rows("2024-01-05")["U_3"]["roles_status"], "carried_forward")
        self.assertEqual(self.rows("2024-01-05")["U_3"]["activity_status"], "unscanned")


if __name__ == "__main__":
    unittest.main()
