import argparse
import json
from pathlib import Path
import sqlite3
import sys

from .source import now
from .build import build


def main(argv=None):
    parser = argparse.ArgumentParser(description="Build Grafana's monthly gzipped user-day JSON dataset offline")
    parser.add_argument("--source", type=Path, required=True, help="Read-only github-otel-stats data directory")
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--affiliations", type=Path, help="Private affiliation JSON, defaults to SOURCE/affiliations.json")
    parser.add_argument("--company", default="grafana")
    parser.add_argument("--start", help="First UTC day, defaults to the shared provisional scan start (or yesterday)")
    parser.add_argument("--end", help="Exclusive UTC day, defaults to today (completed days only)")
    args = parser.parse_args(argv)
    try:
        result = build(args.source, args.output, affiliation_file=args.affiliations, start=args.start, end=args.end, company=args.company,
                       progress=lambda message: print(f"[{now()}] {message}", file=sys.stderr, flush=True))
        print(json.dumps({key: result[key] for key in ("range", "users", "user_days", "observed_active_user_days", "role_summary")}, indent=2))
        return 0
    except (OSError, ValueError, KeyError, TypeError, sqlite3.Error) as error:
        print(f"Error: {error}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
