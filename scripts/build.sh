#!/usr/bin/env bash
set -euo pipefail
root="$(cd "$(dirname "$0")/.." && pwd)"
source="${GITHUB_OTEL_DATA:-$root/../github-otel-stats/data}"
source="$(cd "$source" && pwd -P)"
output="${GRAFANA_OTEL_OUTPUT:-$root/output}"
if [[ -d "$output" ]]; then
  output="$(cd "$output" && pwd -P)"
else
  output="$(cd "$(dirname "$output")" && pwd -P)/$(basename "$output")"
fi
if [[ ! -f "$source/affiliations.json" ]]; then
  echo 'Need private SOURCE/affiliations.json. See README.md.' >&2
  exit 1
fi
case "$output/" in "$source/"*) echo 'Output cannot be inside the private source directory' >&2; exit 1 ;; esac
case "$source/" in "$output/"*) echo 'Output cannot contain the private source directory' >&2; exit 1 ;; esac
mkdir -p "$output"
revision=unversioned-local
if git -C "$root" rev-parse HEAD >/dev/null 2>&1; then
  revision="$(git -C "$root" rev-parse HEAD)"
  if [[ -n "$(git -C "$root" status --porcelain)" ]]; then revision+='+dirty'; fi
fi
echo 'Building self-contained Docker image (cached when unchanged)...' >&2
image="$(docker build --quiet --build-arg "SCRIPTS_REVISION=$revision" "$root")"
exec docker run --rm --init --network none --read-only --tmpfs /tmp:rw,mode=1777 \
  --user "$(id -u):$(id -g)" --env HOME=/tmp \
  --mount "type=bind,source=$source,target=/source,readonly" \
  --mount "type=bind,source=$output,target=/output" \
  "$image" --source /source --output /output/dataset "$@"
