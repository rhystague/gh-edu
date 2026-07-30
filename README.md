# gh-edu

`gh-edu` is a terminal-only tool for provisioning GitHub teams, private
repositories and student invitations for a teaching cohort. It reads a strict
YAML configuration and an LMS-style CSV roster, uses the authenticated GitHub
CLI for GitHub operations, keeps a local invitation ledger, and writes detailed
Markdown reports.

Every mutating command is a dry run unless `--apply` is supplied. Run the plan,
read its report, and only then apply it.

## What it manages

For each project group, `gh-edu` can:

- create or reuse a deterministically named GitHub team;
- create one or more private repositories from a GitHub template;
- grant the team the configured permission on every group repository; and
- invite each student by university email with the team's numeric GitHub ID.

Group provisioning can also create a separate one-student team for every
student. In that mode, each student receives one organisation invitation that
assigns both the shared group team and their individual team. The individual
team is not granted access to the group repositories or to any other
repository.

There is also an exception workflow that creates one team for one student and
grants it access to an explicitly named repository. A GitHub login is not
required to send any kind of invitation. A separate batch workflow can create
individual teams from a roster. That batch workflow is team-only by default,
but can optionally create or reuse one repository named by each roster row and
grant the corresponding individual team access.

The tool is deliberately limited in scope. It does not provision supervisors or
staff, manage organisation owners, run as a service, or use a remote database.
Supervisor access must be managed separately.

## Requirements

- Python 3.11 or later
- [GitHub CLI](https://cli.github.com/) available as `gh`
- an authenticated GitHub CLI session with sufficient access to manage the
  target organisation, its teams, repositories and invitations
- a template repository whose GitHub `is_template` setting is enabled

Authenticate with GitHub before using the tool:

```console
gh auth login
gh auth status
```

`gh-edu` uses the existing `gh` authentication. It does not require a token in
the configuration file.

## Install

From this repository:

```console
python -m pip install .
gh-edu --help
```

For development:

```console
python -m venv .venv
source .venv/bin/activate
python -m pip install -e '.[dev]'
pytest
ruff check .
mypy
```

You can also run the package from an activated development environment with
`python -m gh_edu`.

## Quick start

Copy and edit the supplied examples:

```console
cp examples/config.yml config.yml
cp examples/students.csv students.csv
```

Validate the local inputs, inspect the live GitHub status, and generate a
provisioning plan:

```console
gh-edu roster validate --config config.yml --roster students.csv
gh-edu status --config config.yml --roster students.csv
gh-edu provision groups --config config.yml --roster students.csv
```

The last command is a dry run. Read the Markdown report path printed in the
terminal. When the plan is correct, run the same operation with `--apply`:

```console
gh-edu provision groups \
  --config config.yml \
  --roster students.csv \
  --apply
```

## Configuration

Configuration uses a strict schema. `schema_version` must be the integer `1`;
missing fields, unsupported values and unknown fields are validation errors.
This prevents a misspelt safety-sensitive setting from being silently ignored.

```yaml
schema_version: 1
organisation: example-teaching-org
subject: COMP3018
term: 2026S2
template: example-teaching-org/teaching-template

naming:
  group_team: "{subject}-{term}-{group_id}"
  individual_team: "IND-{student_id}"

repositories:
  permission: push
  group:
    - name: "{subject}-{term}-{group_id}"
      description: "{subject} {term} project repository for {group_id}"
    - name: "{subject}-{term}-{group_id}-documentation"
      description: "{subject} {term} documentation repository for {group_id}"
  individual_description: "{subject} {term} individual repository for {student_id}"

paths:
  ledger: ".gh-edu/{subject}-{term}-invitations.json"
  reports: reports

roster:
  github_login_column: github_login
```

The top-level fields are:

| Field | Purpose |
|---|---|
| `schema_version` | Configuration format version; currently exactly `1`. |
| `organisation` | Target GitHub organisation login. |
| `subject` | Subject or course identifier used by name templates and reports. |
| `term` | Teaching-period identifier and semester-close confirmation value. |
| `template` | Source template repository in `owner/repository` form. |
| `naming.group_team` | Format string for a shared project-group team. |
| `naming.individual_team` | Format string for a one-student team. |
| `repositories.permission` | Desired team permission for configured repositories. |
| `repositories.group` | Non-empty list of repository name and description templates created for every group. |
| `repositories.individual_description` | Description template for an individual repository. Its name comes from singular `--repository` or the plural CSV `repository` column used with `--add-repository`. |
| `paths.ledger` | Local JSON invitation ledger. |
| `paths.reports` | Directory for Markdown reports. |
| `roster.github_login_column` | Optional CSV column containing a verified GitHub login. |

Group templates can use `subject`, `term` and `group_id`. Individual templates
can use `subject`, `term` and `student_id`. Generated GitHub names are validated
before any writes are attempted.

`repositories.group` is a list because one group team may need access to
multiple repositories. Every list item is created from the same configured
template, and the group team receives `repositories.permission` on each one.
Remove the second item in the example if each group needs only one repository.

The entire `roster` section is optional. Configure
`roster.github_login_column` only when the roster contains a trusted mapping
from student to GitHub login. Logins help confirm accepted invitations; email
invitations do not depend on them.

## Roster format

Group workflows require:

```csv
student_id,email,group_id
12345678,12345678@student.example.edu.au,G01
23456789,23456789@student.example.edu.au,G01
34567890,34567890@student.example.edu.au,G02
```

If `roster.github_login_column` is configured, that column must also be present:

```csv
student_id,email,group_id,github_login
12345678,12345678@student.example.edu.au,G01,
23456789,23456789@student.example.edu.au,G01,verified-login
```

Blank GitHub logins are allowed. Student IDs are treated as text so leading
zeroes are preserved. Validation rejects malformed rows, invalid email
addresses, duplicate or conflicting student identities, and resource-name
collisions before any GitHub mutation.

The university student ID is retained in the roster, ledger and reports. It is
never included in a GitHub invitation payload.

`provision individuals` requires only `student_id` and `email` (plus the
configured GitHub-login column, when enabled):

```csv
student_id,email
12345678,12345678@student.example.edu.au
23456789,23456789@student.example.edu.au
```

The command derives the internal group identifier `IND-{student_id}` and
generates the actual team name through `naming.individual_team`. Existing CSVs
may still include a `group_id` column; when present, every value must exactly
match `IND-{student_id}`.

To provision one repository for every individual team, add a non-empty
`repository` column:

```csv
student_id,email,repository
12345678,12345678@student.example.edu.au,CAPSTONE-12345678
23456789,23456789@student.example.edu.au,CAPSTONE-23456789
```

This column is used only when `provision individuals` is run with
`--add-repository`.

## Commands

### Check authentication

```console
gh-edu auth check --config config.yml
```

Checks that `gh` is installed and authenticated and that the configured
organisation is accessible. It does not mutate GitHub.

### Validate a roster

```console
gh-edu roster validate \
  --config config.yml \
  --roster students.csv
```

Validates configuration, CSV structure, student data and generated names
without requiring a GitHub write. A validation report is written to the
configured reports directory.

### Discover current status

```console
gh-edu status \
  --config config.yml \
  --roster students.csv
```

Performs read-only discovery of the template, teams, repositories, permissions,
pending invitations, relevant team members and local ledger. It writes a status
report describing current and desired state.

### Provision project groups

Dry run:

```console
gh-edu provision groups \
  --config config.yml \
  --roster students.csv
```

Apply:

```console
gh-edu provision groups \
  --config config.yml \
  --roster students.csv \
  --apply
```

The command plans all configured repositories for every group. Apply mode
creates missing teams first, then repositories, permissions and eligible
invitations. Existing matching resources are reused. It never overwrites a
repository, changes repository visibility, or automatically unarchives one.
An exact-name repository that is public is reported as an error and blocks
dependent access and invitation actions.

Add `--add-individual` to create a separate individual team for every student:

```console
gh-edu provision groups \
  --config config.yml \
  --roster students.csv \
  --add-individual \
  --apply
```

This flag is opt-in and does not change ordinary group provisioning when
omitted. With the flag, each student receives one invitation containing both
the shared group team's ID and their individual team's ID. The shared group
team retains the configured access to the group repositories. The individual
team receives no repository access: it is not attached to the group
repositories, and no individual repository is created.

### Provision individual teams from a roster

Dry run:

```console
gh-edu provision individuals \
  --config config.yml \
  --roster individual-students.csv
```

Apply:

```console
gh-edu provision individuals \
  --config config.yml \
  --roster individual-students.csv \
  --apply
```

This batch workflow derives `IND-{student_id}` internally, creates or reuses
the team produced by `naming.individual_team`, and sends one team invitation
per student. It does not create a repository or grant repository access unless
`--add-repository` is supplied.

To plan creation or assignment of the repository named in each roster row:

```console
gh-edu provision individuals \
  --config config.yml \
  --roster individual-students-with-repositories.csv \
  --add-repository
```

Apply:

```console
gh-edu provision individuals \
  --config config.yml \
  --roster individual-students-with-repositories.csv \
  --add-repository \
  --apply
```

With `--add-repository`, every row must contain a non-empty `repository`
value. A missing repository is created from the configured `template`, using
`repositories.individual_description`; an existing private, active repository
with the exact name is reused. The individual team is then granted
`repositories.permission` on that repository.

This flag affects only `provision individuals`. It does not change
`provision groups --add-individual`: individual teams created during group
provisioning remain team-only and receive no access to group or individual
repositories.

### Provision one individual student

Dry run:

```console
gh-edu provision individual \
  --config config.yml \
  --student-id 12345678 \
  --email 12345678@student.example.edu.au \
  --repository CAPSTONE-SPECIAL-12345678
```

Apply:

```console
gh-edu provision individual \
  --config config.yml \
  --student-id 12345678 \
  --email 12345678@student.example.edu.au \
  --repository CAPSTONE-SPECIAL-12345678 \
  --apply
```

This exception workflow creates or reuses the configured individual team and
the explicitly named repository, grants access, then invites the student by
email with that team's numeric ID. Repeating the command reuses existing
resources and does not duplicate a pending or previously reconciled
invitation.

### Retry explicitly expired invitations

Dry run:

```console
gh-edu invitations retry-expired \
  --config config.yml \
  --roster students.csv
```

Apply:

```console
gh-edu invitations retry-expired \
  --config config.yml \
  --roster students.csv \
  --apply
```

This dedicated command considers only ledger records whose reconciled status is
explicitly `EXPIRED`. It does not invite students with no prior record and does
not resend pending, accepted, inferred, unresolved or failed invitations.

For invitations originally provisioned with both shared and individual teams,
use the matching opt-in flag:

```console
gh-edu invitations retry-expired \
  --config config.yml \
  --roster students.csv \
  --add-individual \
  --apply
```

An eligible retry sends one replacement invitation containing both team IDs.
It does not retry only one side of a combined invitation. Omitting
`--add-individual` preserves the ordinary group-only retry behaviour.

### Close a semester

Dry run:

```console
gh-edu semester close \
  --config config.yml \
  --roster students.csv \
  --archive-repositories \
  --remove-team-access \
  --confirm-term 2026S2
```

Apply:

```console
gh-edu semester close \
  --config config.yml \
  --roster students.csv \
  --archive-repositories \
  --remove-team-access \
  --confirm-term 2026S2 \
  --apply
```

`--archive-repositories` archives repositories generated from the roster.
`--remove-team-access` removes the corresponding team-to-repository
relationships. The flags are independent, and only selected actions are
planned. When both are selected, access is removed before archival because
GitHub does not allow team relationships to be changed after a repository is
archived. `--confirm-term` must exactly match `term` in the configuration.

Semester closure never deletes a repository or team, and it never removes
organisation owners.

## Dry-run and apply behaviour

Commands that can mutate GitHub construct a complete action plan without
writing anything. Without `--apply`, they save and report that plan only. Dry
run never invokes GitHub `POST`, `PUT`, `PATCH` or `DELETE` operations and does
not update the invitation ledger.

With `--apply`, `gh-edu` saves the plan before the first write, executes only
actions from that plan in dependency order, and writes an apply report. A
failure in one group's prerequisite blocks dependent work for that group, but
unrelated groups continue where safe. Partial execution is reported with exit
code `3`, and an apply report is still written.

Repeated application against unchanged inputs and unchanged GitHub state is
idempotent: existing exact-name teams and repositories are reused, matching
permissions are skipped, and invitations are not duplicated.

## Invitation ledger and reconciliation

GitHub removes accepted invitations from its pending-invitations endpoint, and
an organisation member record may not reveal the invited university email.
`gh-edu` therefore combines live GitHub discovery with the local JSON ledger at
`paths.ledger`. The default and example paths include `subject` and `term` so
cohorts do not accidentally share invitation history.

The ledger records the student and group mapping, team ID, GitHub invitation
ID, timestamps, status and attempt count. It is written atomically after each
successful invitation so a later failure does not lose earlier progress. An
organisation mismatch, unsupported ledger schema or corrupt JSON is a
validation failure.

A combined group-and-individual invitation is represented by one ledger record
for each team assignment. Both records share the same GitHub invitation ID.

Reconciliation is deliberately conservative:

| Evidence | Status | Automatic result |
|---|---|---|
| A pending email contains every expected team ID | `PENDING` | Skip and adopt every assignment into the ledger; never resend. |
| A pending email is missing an expected team ID | `PENDING` with review | Record the partial mapping, require review and never replace or duplicate the invitation. |
| No pending invitation and no ledger record | `NOT_INVITED` | Normal provisioning may send once. |
| Expected individual team contains one active non-maintainer member, or a verified login is active in the expected team | `ACCEPTED_CONFIRMED` | Skip as accepted. |
| A shared team has additional active members but the individual mapping cannot be proved | `ACCEPTED_INFERRED` | Skip as accepted and report the inference. |
| A ledger record exists, no invite is pending, and acceptance cannot be confirmed | `UNRESOLVED` | Require review; never resend. |
| Expiry is explicitly established | `EXPIRED` | Only `retry-expired` may resend. |
| A controlled operation failed | `FAILED` | Require review; never resend automatically. |

For a combined group-and-individual assignment, acceptance is confirmed only
when the same verified login—or the individual team's sole safely inferred
member—is active in both teams. Membership in only one team requires review.

A missing pending invitation is not proof of expiry: it may have been
accepted. Age alone is also not expiry evidence, even though GitHub invitations
normally expire after a limited period. `gh-edu` does not change an old
`UNRESOLVED` record to `EXPIRED` merely because seven or more days have passed.
Only a status explicitly established as `EXPIRED` by reliable GitHub evidence
or a controlled operator reconciliation is retryable.

Normal group or individual provisioning sends only for `NOT_INVITED`.
`invitations retry-expired` sends only for `EXPIRED`. This policy favours a
manual review over accidentally issuing duplicate invitations.

The ledger and reports contain student IDs and email addresses. Store them in an
appropriately protected location, exclude them from version control, and retain
them according to your institution's privacy policy.

## Reports

Detailed results are Markdown files under `paths.reports`. The CLI writes
reports for validation, status discovery, dry-run plans, apply results,
expired-invitation retries and semester closure. Terminal output stays brief
and includes the report path and summary counts.

Reports identify the organisation, subject, term and execution mode, then list
planned, completed, skipped, blocked, failed and review-required actions as
applicable. Apply reports are written even after a partial failure.

## Exit codes

| Code | Meaning |
|---:|---|
| `0` | Success, including a successful no-op. |
| `1` | Unexpected error. |
| `2` | Configuration, roster, naming or ledger validation failure. |
| `3` | Partial success; some independent actions completed. |
| `4` | GitHub authentication or authorisation failure, including a missing `gh` executable. |
| `5` | A GitHub rate limit prevented completion. |
| `6` | Required semester confirmation is missing or does not match. |

## Safety guarantees

- GitHub writes require `--apply`.
- A Markdown plan is saved before apply mode begins.
- Semester closure requires an exact `--confirm-term`.
- Repositories and teams are never deleted.
- Existing repository contents are never overwritten.
- Existing public repositories are never accepted as private cohort resources.
- Archived repositories are never automatically unarchived.
- Unresolved invitations are never automatically resent.
- Authentication material is never written to configuration, ledgers or
  reports.
- No supervisor-management workflow is implemented.
