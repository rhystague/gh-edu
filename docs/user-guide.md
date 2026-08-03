# gh-edu User Guide

This guide is for teaching staff and course administrators who use `gh-edu`
to provision GitHub teams, repositories, invitations and memberships for a
teaching term.

It is the authoritative operational manual. The [project README](../README.md)
is a short landing page; installation, configuration, CSV formats, commands,
workflows, reconciliation, reports, exit codes, and safety rules are all
documented here.

## Contents

- [Operating model](#operating-model)
- [Prerequisites and installation](#prerequisites-and-installation)
- [Configuration](#configuration)
- [CSV formats](#csv-formats)
- [Validation](#validate-before-provisioning)
- [Command reference](#command-reference)
- [Large cohorts and GitHub limits](#large-cohorts-and-github-limits)
- [Dry runs and apply](#dry-run-and-apply-behaviour)
- [Invitation ledger and reconciliation](#invitation-ledger-and-reconciliation)
- [Reports](#reports)
- [Exit codes](#exit-codes)
- [Operational workflows](#workflow-1-provision-individual-teams)
- [Repeated runs](#repeated-runs)
- [Safety guarantees](#safety-guarantees)
- [Current boundaries](#current-boundaries)

## Operating model

`gh-edu` reconciles a desired teaching roster with the current GitHub
organisation state.

- Commands are dry runs unless `--apply` is supplied.
- Existing matching resources are reused.
- Missing resources are created only during apply mode.
- Invitations are never duplicated when pending or prior evidence exists.
- Individual teams can act as permanent identity anchors for accepted
  students.
- Detailed plans and results are written as Markdown reports.

Use this operating sequence for every mutating command:

1. Run the command without `--apply`.
2. Open and review the generated plan report.
3. Run the same command with `--apply`.
4. Review the apply report and any review-required actions.

## Prerequisites and installation

Before provisioning:

1. Install Python 3.11 or later.
2. Install and authenticate the GitHub CLI:

   ```console
   gh auth login
   gh auth status
   ```

3. Confirm the authenticated account can administer the target organisation.
4. Create or select a GitHub template repository and enable its template
   repository setting.
5. Prepare the term configuration and CSV rosters.

Install `gh-edu` from this repository:

```console
python -m pip install .
gh-edu --help
```

For a development installation:

```console
python -m venv .venv
source .venv/bin/activate
python -m pip install -e '.[dev]'
pytest
ruff check .
mypy
```

`gh-edu` uses the existing `gh` authentication. Do not put a GitHub token in
the YAML configuration.

## Configuration

The configuration schema is strict. `schema_version` must be the integer `1`;
unknown fields, missing required fields, and invalid values stop before any
GitHub mutation. Copy `examples/config.yml` as a starting point.

```yaml
schema_version: 1
organisation: teaching-org
subject: COMP3018
term: 2026S2

# Source repository in owner/name format. Do not provide a URL.
template: teaching-org/course-template

naming:
  group_team: "{subject}-{term}-{group_id}"
  individual_team: "IND-{student_id}"

repositories:
  permission: push
  group:
    - name: "{subject}-{term}-{group_id}"
      description: "{subject} {term} repository for {group_id}"
  individual_description: "{subject} {term} repository for {student_id}"

paths:
  ledger: ".gh-edu/{subject}-{term}-invitations.json"
  reports: reports
  execution_state: ".gh-edu/{organisation}-execution-state.json"

execution:
  content_writes_per_hour: 450
  invitation_budget_per_24_hours: auto
  github_timeout_seconds: 180

roster:
  github_login_column: null
```

The top-level and nested settings are:

| Setting | Purpose |
|---|---|
| `schema_version` | Configuration format version; currently exactly `1`. |
| `organisation` | Target GitHub organisation login. |
| `subject` | Course identifier used in names, paths, and reports. |
| `term` | Teaching-period identifier and semester-close confirmation value. |
| `template` | Source template in `owner/repository` form. |
| `naming.group_team` | Shared project-team name template. |
| `naming.individual_team` | One-student-team name template. |
| `repositories.permission` | Desired team permission: `pull`, `triage`, `push`, `maintain`, or `admin`. |
| `repositories.group` | Non-empty list of name and description templates created for each group. |
| `repositories.individual_description` | Description template for individual repositories; repository names come from CLI or CSV input. |
| `paths.ledger` | Atomic local invitation ledger path. |
| `paths.reports` | Markdown report directory. |
| `paths.execution_state` | Atomic, organisation-scoped write-attempt state used for rolling budgets. |
| `execution.content_writes_per_hour` | Local rolling content-write budget, from `1` to `450`; default `450`. |
| `execution.invitation_budget_per_24_hours` | `auto`, or an explicit local rolling invitation budget from `1` to `500`. |
| `execution.github_timeout_seconds` | Deadline for each complete GitHub CLI invocation, from `1` to `3600`; default `180`. |
| `roster.github_login_column` | Optional CSV column containing trusted, verified GitHub logins. |

The slash in `template: teaching-org/course-template` separates the GitHub
owner from the repository name. It is not a URL or local path. Repository names
elsewhere in the configuration or CSV are the new target repositories.

All repositories created during one run use the configured source template.
To use a different template, use a different configuration or change
`template` before the run.

The current configuration schema requires at least one group repository.
Consequently, group provisioning is not a team-only operation.

Group templates can use `subject`, `term`, and `group_id`. Individual templates
can use `subject`, `term`, and `student_id`. Path templates can use
`organisation`, `subject`, and `term`. Generated GitHub names and resolved paths
are validated before writes.

The entire `roster` section is optional. Configure `github_login_column` only
when the mapping is trusted. Blank values in that column are allowed, but the
header itself is required in every roster workflow while the setting is not
`null`. Email invitations do not require GitHub logins.

## CSV formats

### Individual teams only

```csv
student_id,email
12345678,12345678@student.example.edu.au
23456789,23456789@student.example.edu.au
```

The command derives the internal group marker `IND-{student_id}` and generates
the actual team name through `naming.individual_team`.

### Individual teams with repositories

```csv
student_id,email,repository
12345678,12345678@student.example.edu.au,CAPSTONE-12345678
23456789,23456789@student.example.edu.au,CAPSTONE-23456789
```

The `repository` value is the exact target repository name. It is not the
template name or template URL.

### Shared project groups

```csv
student_id,email,group_id
12345678,12345678@student.example.edu.au,G01
23456789,23456789@student.example.edu.au,G01
34567890,34567890@student.example.edu.au,G01
45678901,45678901@student.example.edu.au,G02
56789012,56789012@student.example.edu.au,G02
67890123,67890123@student.example.edu.au,G02
```

`group_id` is the logical roster group. It is not necessarily the GitHub team
name. With the example configuration, `G01` generates the team
`COMP3018-2026S2-G01`.

If `roster.github_login_column` is configured, that CSV column is also
required. Only configure it when the login mapping is already verified.

Student IDs are text, so leading zeroes are preserved. The student ID is kept
in local ledgers and reports but is never sent in a GitHub invitation payload.
Validation rejects malformed rows, invalid email addresses, duplicate or
conflicting identities, unsafe generated names, and name collisions.

An individual CSV may contain `group_id` for compatibility, but every value
must exactly match the derived `IND-{student_id}`. A `repository` column is
read only when `--add-repository` is used. In that mode it must be present,
non-empty, valid as a GitHub repository name, and unique to the intended
student assignment.

## Validate before provisioning

Validate a group roster:

```console
gh-edu roster validate \
  --config config.yml \
  --roster groups.csv \
  --mode groups
```

Validate an individual team roster:

```console
gh-edu roster validate \
  --config config.yml \
  --roster individuals.csv \
  --mode individuals
```

Validate individual repository assignments:

```console
gh-edu roster validate \
  --config config.yml \
  --roster individuals-with-repositories.csv \
  --mode individuals \
  --add-repository
```

The default mode is `groups` for compatibility with earlier versions. Group
mode requires `group_id`. Individual mode derives `IND-{student_id}` and does
not require a `group_id` column. Adding `--add-repository` requires and
validates the individual `repository` column.

When `roster.github_login_column` is configured, that column remains required
in both modes. Set it to `null` when the roster has no trusted GitHub-login
mapping.

Inspect current GitHub state without writing:

```console
gh-edu status \
  --config config.yml \
  --roster groups.csv
```

Roster validation performs no GitHub calls in either mode. `status` and all
dry-run provisioning commands perform GitHub reads but no mutations.

## Command reference

| Command | Purpose | Mutating flags |
|---|---|---|
| `gh-edu auth check` | Check `gh` authentication and organisation access. | None. |
| `gh-edu roster validate` | Validate local configuration, CSV data, and generated resources. | None. |
| `gh-edu status` | Discover and report current versus desired group state. | None. |
| `gh-edu provision groups` | Provision shared teams, configured group repositories, access, memberships, and invitations. | `--add-individual`, `--apply`, `--wait-for-limits`. |
| `gh-edu provision individuals` | Provision a roster of individual teams and optional per-row repositories. | `--add-repository`, `--apply`, `--wait-for-limits`. |
| `gh-edu provision individual` | Provision one exceptional individual team and named repository. | `--apply`, `--wait-for-limits`. |
| `gh-edu invitations retry-expired` | Retry only invitations explicitly reconciled as expired. | `--add-individual`, `--apply`, `--wait-for-limits`. |
| `gh-edu semester close` | Remove selected group repository access and/or archive group repositories. | `--remove-team-access`, `--archive-repositories`, `--confirm-term`, `--apply`, `--wait-for-limits`. |

All commands that accept `--apply` default to dry-run mode. Use
`gh-edu COMMAND --help` for the full option list. `--wait-for-limits` changes
only how an already reviewed apply handles pacing windows; it does not imply
`--apply` and does not authorize new actions.

Every command that contacts GitHub accepts `--github-timeout-seconds`. This
temporarily overrides `execution.github_timeout_seconds`; roster validation
does not expose the option because it performs no GitHub calls.

`--config FILE` is required by every command. Roster-based commands also
require `--roster FILE`. `provision individual` instead requires
`--student-id`, `--email`, and the exact target `--repository`. It is the
exception workflow for one student:

```console
gh-edu provision individual \
  --config config.yml \
  --student-id 12345678 \
  --email 12345678@student.example.edu.au \
  --repository CAPSTONE-SPECIAL-12345678
```

Run it once as shown to review the plan, then repeat with `--apply`. It creates
or reuses the individual team and named private repository, grants the
configured permission, and sends an eligible invitation.

`invitations retry-expired` uses a group roster and considers only ledger
records explicitly reconciled as `EXPIRED`. It never retries missing, pending,
accepted, inferred, unresolved, or failed records. Include `--add-individual`
only when the original invitation assigned both the group and individual team:

```console
gh-edu invitations retry-expired \
  --config config.yml \
  --roster groups.csv \
  --add-individual
```

Review that plan, then repeat it with `--apply` and, for a large retry batch,
`--wait-for-limits`.

## Large cohorts and GitHub limits

Each GitHub CLI invocation has a default 180-second deadline. A paginated
`gh api --paginate --slurp` discovery fetches its pages sequentially inside one
invocation, so the deadline covers the complete paginated command. It is reset
for the next GitHub CLI invocation and is not a total-run timeout.

For an organisation whose discovery needs longer, change the durable setting:

```yaml
execution:
  github_timeout_seconds: 300
```

Or override it for one command without editing YAML:

```console
gh-edu status \
  --config config.yml \
  --roster groups.csv \
  --github-timeout-seconds 300
```

Values must be whole seconds from 1 to 3600. A timeout error names the GitHub
operation and effective deadline and repeats both remediation choices.

Large cohorts can also encounter GitHub write limits. `gh-edu` treats these as
scheduling constraints:

- Every `POST`, `PUT`, `PATCH`, or `DELETE` is followed by at least one second
  before the next mutation begins. Reads and dry runs are unthrottled.
- A local rolling budget permits at most 450 content-generating writes per
  hour by default. Team creation, repositories, permissions, memberships, and
  invitations all consume this budget.
- GitHub permits 50 organisation invitations per 24 hours, rising to 500 when
  the organisation is more than one month old or is on a paid plan. Automatic
  mode uses buffered budgets of 45 or 450 respectively.
- `execution.invitation_budget_per_24_hours` may override automatic selection
  with an integer from 1 to 500. Administrators should normally lower it only;
  setting it higher does not change GitHub's server-side limit.

The tool reads organisation age and available plan metadata. Missing or
uncertain metadata selects the conservative 45-invitation budget. These rules
follow GitHub's [organisation invitation limits](https://docs.github.com/en/organizations/managing-membership-in-your-organization/inviting-users-to-join-your-organization),
[REST API rate limits](https://docs.github.com/en/rest/using-the-rest-api/rate-limits-for-the-rest-api),
and [REST API best practices](https://docs.github.com/en/rest/using-the-rest-api/best-practices-for-using-the-rest-api).

### Runtime examples

The exact dry-run report is authoritative because existing resources can make
many actions no-ops. For a new, established organisation where each student
needs a team, repository, permission, and invitation, the rough lower bounds
are:

| Students | Planned writes | Limiting local windows | Approximate minimum |
|---:|---:|---|---:|
| 100 | 400 | One content and one invitation window | 6 minutes 39 seconds |
| 300 | 1,200 | Three hourly content windows | About 2 hours 5 minutes |
| 600 | 2,400 | Six hourly content windows and two invitation windows | At least 24 hours |

A new free organisation using the automatic 45-invitation budget spans three,
seven, or fourteen rolling invitation windows for those same cohorts. Existing
teams, repositories, permissions, memberships, or invitation evidence reduce
the writes and may reduce the runtime substantially.

### Waiting, interruption, and resumption

Without `--wait-for-limits`, an apply that reaches a local hourly or daily
window stops safely with exit code `5` and prints the earliest local retry
time. Run the same command again at or after that time; live GitHub state, the
invitation ledger, and idempotent planning prevent completed resources from
being duplicated.

Use unattended waiting only after reviewing the dry-run report:

```console
gh-edu provision individuals \
  --config config.yml \
  --roster students.csv \
  --add-repository \
  --apply \
  --wait-for-limits
```

With the flag, the process keeps the saved plan and waits through local
windows and recoverable GitHub responses. It uses `Retry-After` first, then a
GitHub reset timestamp, then exponential waits from one minute to one hour.
An invitation-window rejection without reset metadata waits for the local
rolling invitation slot, or conservatively for 24 hours. The command retries
the same reviewed action; it never silently replans during a wait. External
drift is handled by the existing execution and verification safety checks.

GitHub can also reject repository generation from a template with HTTP 422 and
`Could not clone: was submitted too quickly`. `gh-edu` treats only that exact,
repository-generation-specific response as recoverable. With
`--wait-for-limits`, it waits for the server-provided or exponential cooldown,
then reads the exact target repository before submitting another create. A
private active repository found by that read is adopted; a missing repository
is retried; and a public or archived repository fails the existing safety
checks. Without the flag, the apply stops with exit code `5` before retrying.

The terminal shows discovery and verification phases. On a TTY, one live line
shows processed and total writes, percentage, successes, failures, elapsed
time, and minimum ETA. Redirected output writes a permanent summary every ten
writes or thirty seconds. Long-wait messages refresh at least once per minute
and include the exact local resume time. Progress lines contain no student
email addresses.

Write-attempt timestamps and any remote-limit retry deadline are saved
atomically in `paths.execution_state`; old timestamps are pruned from the
rolling windows. A restart honors an unexpired remote cooldown before making
another write. Apply holds an exclusive lock, so two local processes sharing
the path cannot consume the same budget. The file records the GitHub hostname
and organisation and is rejected if reused for a different target. Keep one
execution-state path shared by every local configuration that targets that
organisation.

`Ctrl-C` is safe: the remote resources, ledger, and execution state remain, and
the same command can be rerun. Another machine or writes made directly in
GitHub are not visible in the local ledger. The 10% buffers reduce that risk,
but server-provided retry information always takes precedence when GitHub
reports a limit.

## Dry-run and apply behaviour

Mutating commands first perform read-only discovery and construct a complete
action plan. Without `--apply`, the plan is saved and no GitHub mutation or
invitation-ledger update occurs. Dry-run reports include planned writes,
invitations, minimum pacing time, and whether hourly or daily windows are
crossed.

With `--apply`, the tool saves the plan before the first write and executes only
its reviewed actions in dependency order. A failed prerequisite blocks its
dependent work. Independent work continues when safe; authentication and rate
limits stop further writes. Verification uses reads after execution. Apply
reports are written after successful, partial, or limit-stopped runs.

Repeated execution against unchanged inputs and remote state is idempotent:
matching resources, permissions, memberships, and pending invitation evidence
are reused or skipped.

## Invitation ledger and reconciliation

GitHub removes accepted invitations from the pending endpoint, and an
organisation member record may not reveal the invited university email. The
atomic JSON ledger at `paths.ledger` combines local invitation history with
live discovery. Its default path includes `subject` and `term`, preventing
cohorts from accidentally sharing invitation history.

The ledger records student/group mapping, numeric team and invitation IDs,
timestamps, status, and attempt count. A combined shared-and-individual invite
has one record per team assignment, both using the same invitation ID. A
corrupt schema or organisation mismatch is a validation failure.

| Evidence | Reconciled result |
|---|---|
| Pending email invitation contains every expected team ID. | Record `PENDING` and skip; never resend. |
| Pending invitation lacks an expected team ID. | Record the partial mapping and require review. |
| Individual team contains exactly one direct active non-maintainer member. | Record `ACCEPTED_CONFIRMED`; use the numeric user ID for safe direct group membership. |
| Verified login or resolved numeric user is active in the expected team. | Record `ACCEPTED_CONFIRMED` and skip invitation. |
| Shared team membership implies acceptance but identity cannot be proved. | Record `ACCEPTED_INFERRED`; skip and report the inference. |
| Ledger record exists, no invite is pending, and acceptance is unconfirmed. | Record `UNRESOLVED`; require review and never resend. |
| Expiry is explicitly established. | Record `EXPIRED`; only `retry-expired` may resend. |
| Ordinary validation or controlled operation fails. | Record `FAILED` where applicable; require review. |
| GitHub reports a recoverable invitation/rate limit. | Do not mark the student permanently failed; stop or wait and retry the same action. |

A missing pending invitation is not evidence of expiry: it may have been
accepted. Age alone is also insufficient. Normal provisioning sends only for
`NOT_INVITED`; `invitations retry-expired` sends only for `EXPIRED`.

Ledgers and reports contain student IDs and emails. Exclude them from version
control, restrict access, and retain them under institutional privacy policy.

## Reports

Markdown reports are written under `paths.reports` for validation, status,
dry-run plans, applies, invitation retry, and term closure. The terminal prints
the path and compact counts.

Reports identify the organisation, subject, term, mode, action outcomes, and
warnings. Plan reports include minimum duration and rolling-window estimates.
Apply reports also include one-second pacing time, accumulated limit-wait time,
rate-limit retries, and any next-eligible timestamp.

## Exit codes

| Code | Meaning |
|---:|---|
| `0` | Success, including a successful no-op. |
| `1` | Unexpected error. |
| `2` | Configuration, roster, naming, execution-state, or ledger validation failure. |
| `3` | Partial success; some independent actions completed. |
| `4` | GitHub authentication/authorisation failure or missing `gh`. |
| `5` | A local or GitHub rate window stopped completion; retry at the reported time or use `--wait-for-limits`. |
| `6` | Required term confirmation is missing or does not match. |

## Workflow 1: provision individual teams

Input: `individuals.csv`

Validate:

```console
gh-edu roster validate \
  --config config.yml \
  --roster individuals.csv \
  --mode individuals
```

Plan:

```console
gh-edu provision individuals \
  --config config.yml \
  --roster individuals.csv
```

After reviewing the plan:

```console
gh-edu provision individuals \
  --config config.yml \
  --roster individuals.csv \
  --apply
```

For each student, this creates or reuses:

- one individual team;
- one organisation invitation attached to that team, when eligible.

It creates no repositories.

## Workflow 2: provision individuals with repositories

Input: `individuals-with-repositories.csv`

Validate:

```console
gh-edu roster validate \
  --config config.yml \
  --roster individuals-with-repositories.csv \
  --mode individuals \
  --add-repository
```

Plan:

```console
gh-edu provision individuals \
  --config config.yml \
  --roster individuals-with-repositories.csv \
  --add-repository
```

Apply:

```console
gh-edu provision individuals \
  --config config.yml \
  --roster individuals-with-repositories.csv \
  --add-repository \
  --apply
```

For each student, this creates or reuses:

- the individual team;
- the exact target repository named in the CSV;
- repository contents generated from the configured source template;
- the configured team-to-repository permission;
- an eligible individual invitation.

## Workflow 3: provision shared groups

Input: `groups.csv`

Validate:

```console
gh-edu roster validate \
  --config config.yml \
  --roster groups.csv \
  --mode groups
```

Plan:

```console
gh-edu provision groups \
  --config config.yml \
  --roster groups.csv
```

Apply:

```console
gh-edu provision groups \
  --config config.yml \
  --roster groups.csv \
  --apply
```

For every distinct `group_id`, this creates or reuses:

- the generated shared team;
- every repository configured under `repositories.group`;
- the configured repository permissions;
- eligible invitations or safely resolved direct memberships.

There is currently no separate team-only group mode. Even without
`--add-individual`, configured group repositories are provisioned.

## Workflow 4: provision multiple repositories for every group

Configure each desired repository under `repositories.group`:

```yaml
repositories:
  permission: push
  group:
    - name: "{subject}-{term}-{group_id}"
      description: "{subject} {term} main repository for {group_id}"

    - name: "{subject}-{term}-{group_id}-documentation"
      description: "{subject} {term} documentation for {group_id}"
```

Run ordinary group provisioning:

```console
gh-edu provision groups \
  --config config.yml \
  --roster groups.csv \
  --apply
```

For `G01`, this creates:

```text
COMP3018-2026S2-G01
COMP3018-2026S2-G01-documentation
```

To add another repository later, add another configuration entry and rerun
group provisioning. Existing resources are unchanged; only missing
repositories and permissions are written.

## Workflow 5: provision groups, repositories and individual teams together

```console
gh-edu provision groups \
  --config config.yml \
  --roster groups.csv \
  --add-individual
```

Apply:

```console
gh-edu provision groups \
  --config config.yml \
  --roster groups.csv \
  --add-individual \
  --apply
```

For a new student in `G01`, this creates:

- shared team `COMP3018-2026S2-G01`;
- individual team `IND-{student_id}`;
- all configured group repositories;
- repository access for the shared team;
- one invitation containing both numeric team IDs.

The individual team is not granted access to the group repositories.

If the individual team already identifies an accepted student, the command
adds that GitHub account directly to the shared team instead of sending
another invitation.

## Workflow 6: provision groups after provisioning individuals

First provision individual teams:

```console
gh-edu provision individuals \
  --config config.yml \
  --roster individuals.csv \
  --apply
```

After students accept their invitations, run normal group provisioning:

```console
gh-edu provision groups \
  --config config.yml \
  --roster groups.csv \
  --apply
```

Do not add `--add-individual`; the individual teams already exist.

For each accepted student, group provisioning:

1. Derives the expected individual team name.
2. Requires exactly one direct active non-maintainer student member.
3. Reads the stable numeric GitHub user ID and current login.
4. Revalidates that identity immediately before writing.
5. Adds the existing organisation member directly to the shared team.
6. Sends no new organisation invitation.

If no individual team or individual-invitation history exists, ordinary group
invitation behaviour remains available. An empty, ambiguous or conflicting
individual team requires review. A still-pending individual invitation is not
duplicated.

## Workflow 7: add a repository to existing individual teams

Prepare a CSV containing the new target repository names:

```csv
student_id,email,repository
12345678,12345678@student.example.edu.au,ASSIGNMENT-12345678
23456789,23456789@student.example.edu.au,ASSIGNMENT-23456789
```

Use the validation, plan, and apply sequence from
[Workflow 2](#workflow-2-provision-individuals-with-repositories), substituting
`individual-repositories.csv` as the roster. Existing resources make this an
additive reconciliation rather than a separate command.

Existing teams and accepted invitation state are reused. The command creates
or reuses the named repositories and grants access without reinviting accepted
students.

One repository can be supplied per student row in one run. To add another
repository, change the `repository` values and run again. This is additive.

There is no separate repository-only command; idempotent individual
provisioning provides the repository-assignment workflow.

## Workflow 8: add another repository to existing groups

Add the repository definition to `repositories.group`:

```yaml
repositories:
  permission: push
  group:
    - name: "{subject}-{term}-{group_id}"
      description: "Main repository for {group_id}"

    - name: "{subject}-{term}-{group_id}-presentation"
      description: "Presentation repository for {group_id}"
```

Rerun group provisioning:

```console
gh-edu provision groups \
  --config config.yml \
  --roster groups.csv \
  --apply
```

The command reuses existing teams, repositories, memberships and invitation
evidence. It creates only the missing repository and permission relationships.

There is no dedicated group-repository-only command.

## Workflow 9: close a teaching term

Plan removal of group repository access and repository archival:

```console
gh-edu semester close \
  --config config.yml \
  --roster groups.csv \
  --archive-repositories \
  --remove-team-access \
  --confirm-term 2026S2
```

Apply:

```console
gh-edu semester close \
  --config config.yml \
  --roster groups.csv \
  --archive-repositories \
  --remove-team-access \
  --confirm-term 2026S2 \
  --apply
```

This operation:

- removes shared group-team access from configured group repositories;
- archives configured group repositories;
- never deletes a repository or team;
- does not remove team or organisation members;
- does not close individual repositories;
- does not remove individual-team repository access;
- does not delete the invitation ledger or reports.

For the next term, create a new configuration with a new `term`, such as
`2027S1`. Term-specific names and ledger paths keep cohorts separate.

This is a safe group-repository closure workflow, not a complete destructive
reset.

## Individual identity outcomes during group provisioning

| Individual-team evidence | Result |
|---|---|
| No individual team or individual history | Preserve ordinary group invitation behaviour. |
| Exactly one direct active non-maintainer member | Add that numeric GitHub user directly to the shared team. |
| The same numeric user ID is already in the shared team | Skip without changing the existing role. |
| Pending invitation already contains the shared-team ID | Skip and reconcile the pending invitation. |
| Empty, inherited-only, ambiguous or conflicting identity | Require review; do not guess or send a replacement invitation. |

## Repeated runs

Provisioning is designed to be rerun.

- Existing exact-name teams and private active repositories are reused.
- Matching repository permissions are skipped.
- Active direct memberships are skipped.
- Pending invitations are not duplicated.
- Prior unresolved invitation evidence is not treated as permission to resend.
- New repositories can be added by extending configuration or changing the
  individual repository column and running again.

Always inspect the generated report because a successful no-op and a run
containing review-required students can both avoid GitHub writes.

## Safety guarantees

- GitHub writes require `--apply`, and a Markdown plan is saved first.
- Apply executes only the actions in that plan; waiting never silently replans.
- Every mutation is paced and recorded before it is attempted.
- Semester closure requires an exact `--confirm-term` match.
- Teams and repositories are never deleted.
- Existing repository contents are never overwritten.
- Public repositories are never accepted as private cohort resources.
- Archived repositories are never unarchived automatically.
- Pending and unresolved invitations are never automatically resent.
- Invitation-specific rate or abuse-limit responses are recoverable limits,
  not permanent student failures.
- Direct group membership requires a stable numeric user identity and
  execution-time revalidation.
- Group membership changes are additive; existing roles and prior memberships
  are not removed.
- Authentication material is never written to configuration, ledgers, state,
  or reports.

## Requirements coverage

| Required operation | Current support |
|---|---|
| Provision individual teams | Supported |
| Provision individuals with repositories | Supported |
| Provision team-only shared groups | Not currently supported; group repositories are mandatory |
| Provision shared groups with repositories | Supported |
| Provision groups, repositories and individual teams together | Supported |
| Provision groups after individual invitation acceptance | Supported through direct membership |
| Add repositories to existing individual teams | Supported through an idempotent individual provisioning rerun |
| Add repositories to existing shared groups | Supported through configuration and an idempotent group provisioning rerun |
| Fully reset and clear a term | Partially supported; group repository closure only |

## Current boundaries

The following operations are intentionally not performed:

- deleting teams or repositories;
- overwriting repository contents;
- unarchiving repositories automatically;
- cancelling or replacing unresolved invitations;
- removing students from old shared teams;
- changing an existing team role;
- removing organisation members;
- cleaning up individual repositories at term close;
- deleting local ledgers or reports.

These boundaries should be treated as separate feature requirements if the
course workflow needs them.
