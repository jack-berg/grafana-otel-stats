# Schema 4

Dates are UTC and intervals are `[start,end)`.

| File | Contents |
| --- | --- |
| `index.json` | Uncompressed metadata, people, role catalog, repositories, and monthly file index |
| `months/YYYY-MM.json.gz` | Gzipped JSON array of user-day records, ordered by day then username |

The index contains `people: [{id,login}]`, `roles: [{role,team_id,team}]`, and `repositories: [{id,name,archived}]`. People are only those qualifying for this export. The role catalog contains distinct assignments present in exported rows, not a membership roster.

Each `months` entry contains `month`, relative `file`, UTC `start`/`end`, `user_days`, and a `sha256` of the compressed bytes. Boundary months can be partial. Fetch only overlapping months, decompress gzip, and filter rows to the requested days. Month contents and gzip headers are deterministic for unchanged facts.

The index also records the schema version, export range, source observation dates, coverage, role policy, metric names, and counts. `users` is the qualifying person count. Rebuilds replace the selected range; pin a commit SHA when fetching multiple files.

## User-day records

Each row contains `login`, stable `user_id`, `day`, `roles`, `roles_status`, `roles_observed_at`, `activity_status`, `counts`, and `repositories`. Usernames are the latest observed names. Only people with a contribution or recognized role during their affiliation and the selected range qualify. Their eligible quiet days are included too.

Each repository entry contains its readable `repository` name, stable `repository_id`, `coverage_status`, `counts`, and `targets`.

| Count | Meaning |
| --- | --- |
| `issues_opened` | Non-PR issues authored |
| `prs_opened` | PRs authored |
| `prs_merged` | PRs merged, credited to their author |
| `reviews_submitted` | Submitted reviews, excluding pending reviews |
| `first_reviews` | Earliest observed submitted review per user/PR across all cached history |
| `issue_comments` | Conversation comments on known issues |
| `pr_comments` | Conversation comments on known PRs |
| `review_comments` | Inline review comments |
| `unclassified_comments` | Conversation comments with unresolved parent types |

Counts are observed contributions, additive within each metric. Different metrics can overlap: first reviews also count as submitted reviews. First reviews are selected before date-range and affiliation filtering; older history can move or remove a first-review count.

`targets` contains unique issue/PR numbers for submitted reviews and each comment category. Union these sets by repository across days for range-wide distinct participation. Do not sum daily distinct counts. Individual event payloads are not included.

## Roles and coverage

Roles contain `{role,team_id,team}` for recognized accessible GitHub teams: maintainer, approver, triager, governance committee, technical committee, and community manager.

The first completed role snapshot applies backward with `roles_status: assumed`. Later observations apply from their dates onward: `snapshot` on an observation day, `carried_forward` afterward. `roles_observed_at` retains the actual observation timestamp. Without a completed snapshot, roles are `unknown`. Affiliation-date filtering applies throughout.

`activity_status` covers the repository inventory; `coverage_status` covers one repository. Values are `verified`, `provisional`, or `unscanned`. Zero observed counts on provisional or unscanned dates do not prove inactivity. The index contains repository-specific scan/verified intervals, unresolved parents, and review accuracy. Collection can change the data on rebuild.
