# gh-edu User Guide

This guide is for teaching staff and course administrators who use `gh-edu`
to provision GitHub teams, repositories, invitations and memberships for a
teaching term.

It focuses on complete operational workflows. For installation, configuration
schema details, reconciliation rules and exit codes, see the
[project README](../README.md).

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

## Prerequisites

Before provisioning:

1. Install Python 3.11 or later and `gh-edu`.
2. Install and authenticate the GitHub CLI:

   ```console
   gh auth login
   gh auth status
   ```

3. Confirm the authenticated account can administer the target organisation.
4. Create or select a GitHub template repository and enable its template
   repository setting.
5. Prepare the term configuration and CSV rosters.

## Example term configuration

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

roster:
  github_login_column: null
```

The `template` value identifies the source template. Repository names in the
configuration or CSV identify the new target repositories that will be
created.

All repositories created during one run use the configured source template.
To use a different template, use a different configuration or change
`template` before the run.

The current configuration schema requires at least one group repository.
Consequently, group provisioning is not a team-only operation.

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

## Validate before provisioning

Validate configuration, CSV rows and generated names:

```console
gh-edu roster validate \
  --config config.yml \
  --roster groups.csv
```

Inspect current GitHub state without writing:

```console
gh-edu status \
  --config config.yml \
  --roster groups.csv
```

## Workflow 1: provision individual teams

Input: `individuals.csv`

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

Run:

```console
gh-edu provision individuals \
  --config config.yml \
  --roster individual-repositories.csv \
  --add-repository \
  --apply
```

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
