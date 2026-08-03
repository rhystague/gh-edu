# gh-edu

`gh-edu` is a terminal tool for provisioning GitHub teams, private template
repositories, permissions, memberships, and student invitations for a teaching
cohort. It reconciles a strict YAML configuration and LMS-style CSV roster with
an organisation through the authenticated GitHub CLI.

Every mutating command is a dry run unless `--apply` is supplied. A Markdown
plan is saved before the first GitHub write.

The [user guide](docs/user-guide.md) is the complete operational manual,
including configuration, CSV schemas, command reference, workflows, reports,
reconciliation, exit codes, and safety boundaries.

## Capabilities

- Shared project teams with one or more private repositories per group.
- Individual student teams, optionally with one repository per student.
- Invitations by institutional email or direct membership for safely resolved
  existing organisation members.
- Idempotent reruns backed by live discovery and a local invitation ledger.
- Expired-invitation retry and conservative term closure.
- Cohort-scale pacing, shared local rate state, progress, and resumable runs.

The tool does not delete teams or repositories, overwrite repository contents,
manage supervisors, or remove organisation members.

## Requirements and installation

- Python 3.11 or later
- [GitHub CLI](https://cli.github.com/) installed and authenticated
- Owner access to the target organisation
- A repository with GitHub's template-repository setting enabled

For a fresh browser-based GitHub CLI login, request the supported scope set:

```console
gh auth login --hostname github.com --web --scopes admin:org,read:org,repo
```

If GitHub CLI already has a stored login, add the missing organisation scope
without replacing its existing scopes:

```console
gh auth refresh --hostname github.com --scopes admin:org
```

Verify the active account without displaying its token, then run the `gh-edu`
authentication check:

```console
gh auth status --active --hostname github.com
gh-edu auth check --config config.yml
```

Do not use `gh auth status --show-token` in logs or support transcripts. See
the [user-guide authentication contract](docs/user-guide.md#github-authentication)
for scope purposes, environment-token precedence, and SAML SSO requirements.

Install `gh-edu` after authentication:

```console
python -m pip install .
```

The `template` setting uses `owner/repository` syntax. For example,
`example-teaching-org/teaching-template` means organisation or user
`example-teaching-org`, repository `teaching-template`; it is not a URL or a
filesystem path.

## Group quick start

Copy the examples, then edit them for the subject and cohort:

```console
cp examples/config.yml config.yml
cp examples/students.csv groups.csv
```

Validate, create a read-only plan, review the printed report, then apply that
same operation:

```console
gh-edu roster validate --config config.yml --roster groups.csv --mode groups
gh-edu provision groups --config config.yml --roster groups.csv
gh-edu provision groups --config config.yml --roster groups.csv --apply
```

`--mode groups` is the compatibility default and requires `group_id`.

## Individual quick start

An individual roster needs `student_id,email`; add `repository` when each
student also needs a repository. Individual mode derives `IND-{student_id}` and
does not require `group_id`.

```console
gh-edu roster validate \
  --config config.yml \
  --roster individuals.csv \
  --mode individuals \
  --add-repository

gh-edu provision individuals \
  --config config.yml \
  --roster individuals.csv \
  --add-repository

gh-edu provision individuals \
  --config config.yml \
  --roster individuals.csv \
  --add-repository \
  --apply
```

Omit `--add-repository` for team-only individual provisioning. If
`roster.github_login_column` is configured, that CSV column is required in
both workflows; set it to `null` when verified logins are unavailable.

## Large cohorts

Each complete GitHub CLI invocation has a configurable 180-second deadline,
including paginated discovery. Set `execution.github_timeout_seconds` in YAML
or use `--github-timeout-seconds N` on a GitHub-backed command for a one-run
override.

GitHub mutations are separated by at least one second. `gh-edu` also enforces
buffered rolling budgets of 450 content writes per hour and, automatically, 45
or 450 invitations per 24 hours according to the organisation metadata.

Without explicit consent to a long wait, apply stops safely with exit code `5`
and prints the earliest retry time. To let a reviewed plan wait and continue
unattended, add:

```console
gh-edu provision individuals \
  --config config.yml \
  --roster students.csv \
  --add-repository \
  --apply \
  --wait-for-limits
```

TTY output shows live progress; redirected output receives periodic permanent
summaries. Execution timestamps are persisted and locked per organisation, so
an interrupted command can be rerun safely on the same machine. See
[Large cohorts and GitHub limits](docs/user-guide.md#large-cohorts-and-github-limits)
for budgets, runtime examples, shared state, and external-activity caveats.

## Safety highlights

- GitHub writes require `--apply`; discovery and dry runs are unthrottled.
- Apply executes only actions in the saved and reviewed plan.
- Existing resources are reused only after safety checks.
- Pending or unresolved invitations are not duplicated automatically.
- Repositories and teams are never deleted.
- Term closure requires an exact `--confirm-term` value.
- Reports and ledgers contain student data and should remain protected.

## Development

```console
python -m venv .venv
source .venv/bin/activate
python -m pip install -e '.[dev]'
pytest
ruff check .
mypy
```

For all commands and examples, continue with the
[gh-edu User Guide](docs/user-guide.md).
