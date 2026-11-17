# Grafana OpenTelemetry contributions

Public JSON data for reporting tools: [index](output/dataset/index.json), [monthly files](output/dataset/months/), [schema](SCHEMA.md).

## Consume

Clone this repository or fetch files from:

```text
https://raw.githubusercontent.com/OWNER/REPO/REF/output/dataset/
```

Fetch `index.json` for people, roles, repositories, and available months. Fetch only monthly files whose UTC `[start,end)` interval overlaps the requested range. Each `months/YYYY-MM.json.gz` decompresses to an array of user-day records with readable usernames and repository names. In the browser, decompress with `DecompressionStream('gzip')` or a gzip library before parsing JSON. Use a commit SHA as `REF` for a consistent snapshot.

## Update

Requires Docker and private `github-otel-stats` data containing `raw/` and `affiliations.json`:

```sh
GITHUB_OTEL_DATA=/absolute/path/to/github-otel-stats/data ./scripts/build.sh
# Optional range:
./scripts/build.sh --start 2026-01-01 --end 2026-11-17
```

The container runs offline with read-only inputs. By default the range starts at the shared backfill frontier and ends at today's UTC boundary. Each run replaces `output/dataset/`. This directory is tracked: commit and push its updates to publish them at the URLs above. Private inputs and temporary files are excluded.
