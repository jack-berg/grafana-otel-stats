# Grafana OpenTelemetry contributions

Public JSON data for reporting tools: [manifest](output/dataset/manifest.json), [daily records](output/dataset/days/), [schema](SCHEMA.md).

## Consume

Clone this repository or fetch files from:

```text
https://raw.githubusercontent.com/OWNER/REPO/REF/output/dataset/
```

Read `manifest.json` for the schema version and UTC `[start,end)` range, then `days/YYYY-MM-DD.json` for each day. Rows include usernames, repository names, contribution counts, and roles. Use a commit SHA as `REF` for a consistent snapshot.

## Update

Requires Docker and private `github-otel-stats` data containing `raw/` and `affiliations.json`:

```sh
GITHUB_OTEL_DATA=/absolute/path/to/github-otel-stats/data ./scripts/build.sh
# Optional range:
./scripts/build.sh --start 2026-01-01 --end 2026-11-17
```

The container runs offline with read-only inputs. By default the range starts at the shared backfill frontier and ends at today's UTC boundary. Each run replaces `output/dataset/`. This directory is tracked: commit and push its updates to publish them at the URLs above. Private inputs and temporary files are excluded.
