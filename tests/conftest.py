from __future__ import annotations

import csv
import json
import re
from collections import defaultdict, deque
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass, field, replace
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

import pytest
import yaml
from typer.testing import CliRunner

from gh_edu.core import ExecutionPacer, InvitationLedger, LedgerRecord
from gh_edu.github import (
    FailedInvitation,
    GitHubError,
    GitHubNotFoundError,
    Invitation,
    Organisation,
    Repository,
    Team,
    TeamMember,
    TeamMembership,
)

FIXED_NOW = datetime(2026, 7, 30, 9, 42, tzinfo=UTC)


@dataclass(frozen=True, slots=True)
class GitHubCall:
    operation: str
    method: str
    target: str
    payload: Mapping[str, object] = field(default_factory=dict)


class FakeGitHubClient:
    """A stateful in-memory implementation of the production protocol.

    The fake deliberately rejects duplicate creates and pending invitations.
    That makes repeated-run tests fail loudly if the planner loses its
    idempotency guarantees.
    """

    def __init__(self) -> None:
        self.login = "course-admin"
        self.organisation = Organisation(
            login="teaching-org",
            created_at="2020-01-01T00:00:00Z",
            plan_name="free",
        )
        self.template = Repository(
            id=1,
            name="teaching-template",
            name_with_owner="template-owner/teaching-template",
            is_private=True,
            is_template=True,
        )
        self.teams: dict[str, Team] = {}
        self.repositories: dict[str, Repository] = {}
        self.pending: dict[str, Invitation] = {}
        self.failed_invitations: list[FailedInvitation] = []
        self.members: dict[str, set[TeamMember]] = defaultdict(set)
        self.user_ids: dict[str, int] = {}
        self.pending_membership_logins: set[str] = set()
        self.permissions: dict[tuple[str, str], str] = {}
        self.calls: list[GitHubCall] = []
        self._failures: dict[tuple[str, str | None], deque[Exception]] = defaultdict(deque)
        self._next_team_id = 1000
        self._next_repository_id = 2000
        self._next_invitation_id = 3000
        self._next_user_id = 4000

    @property
    def hostname(self) -> str:
        return "github.com"

    @property
    def write_calls(self) -> list[GitHubCall]:
        return [call for call in self.calls if call.method in {"POST", "PUT", "PATCH", "DELETE"}]

    @property
    def read_calls(self) -> list[GitHubCall]:
        return [call for call in self.calls if call.method == "GET"]

    def clear_calls(self) -> None:
        self.calls.clear()

    def fail_next(
        self,
        operation: str,
        error: Exception,
        *,
        target: str | None = None,
    ) -> None:
        self._failures[(operation, target)].append(error)

    def add_team(self, name: str, *, team_id: int | None = None, slug: str | None = None) -> Team:
        if team_id is None:
            team_id = self._next_team_id
            self._next_team_id += 1
        if slug is None:
            slug = re.sub(r"[^a-z0-9]+", "-", name.casefold()).strip("-")
        team = Team(id=team_id, name=name, slug=slug, privacy="closed")
        self.teams[name] = team
        self._next_team_id = max(self._next_team_id, team_id + 1)
        return team

    def add_repository(
        self,
        name: str,
        *,
        archived: bool = False,
        private: bool = True,
    ) -> Repository:
        repository = Repository(
            id=self._next_repository_id,
            name=name,
            name_with_owner=f"teaching-org/{name}",
            is_private=private,
            is_archived=archived,
        )
        self._next_repository_id += 1
        self.repositories[name] = repository
        return repository

    def add_member(
        self,
        slug: str,
        login: str,
        *,
        user_id: int | None = None,
        role: str = "member",
        inherited: bool = False,
    ) -> TeamMember:
        login_key = login.casefold()
        known_user_id = self.user_ids.get(login_key)
        if user_id is None:
            user_id = known_user_id
        if user_id is None:
            user_id = self._next_user_id
            self._next_user_id += 1
        if known_user_id is not None and known_user_id != user_id:
            raise AssertionError("one fake GitHub login cannot have multiple user IDs")
        self.user_ids[login_key] = user_id
        self._next_user_id = max(self._next_user_id, user_id + 1)
        member = TeamMember(
            id=user_id,
            login=login,
            role=role,
            inherited=inherited,
        )
        self.members[slug] = {existing for existing in self.members[slug] if existing.id != user_id}
        self.members[slug].add(member)
        return member

    def add_pending(
        self,
        email: str,
        *,
        invitation_id: int | None = None,
        team_ids: Sequence[int] = (),
    ) -> Invitation:
        if invitation_id is None:
            invitation_id = self._next_invitation_id
            self._next_invitation_id += 1
        invitation = Invitation(
            id=invitation_id,
            email=email,
            role="direct_member",
            created_at="2026-07-30T09:42:00Z",
            team_ids=tuple(team_ids),
        )
        self.pending[email.casefold()] = invitation
        self._next_invitation_id = max(self._next_invitation_id, invitation_id + 1)
        return invitation

    def accept_invitation(
        self,
        email: str,
        *,
        login: str,
        team_slugs: Sequence[str],
    ) -> None:
        self.pending.pop(email.casefold(), None)
        for slug in team_slugs:
            self.add_member(slug, login)

    def _record(
        self,
        operation: str,
        method: str,
        target: str,
        payload: Mapping[str, object] | None = None,
    ) -> None:
        self.calls.append(
            GitHubCall(
                operation=operation,
                method=method,
                target=target,
                payload=payload or {},
            )
        )
        for key in ((operation, target), (operation, None)):
            failures = self._failures.get(key)
            if failures:
                raise failures.popleft()

    def check_auth(self) -> str:
        self._record("check_auth", "GET", "auth")
        return self.login

    def check_organisation(self, org: str) -> None:
        self._record("check_organisation", "GET", org)

    def get_organisation(self, org: str) -> Organisation:
        self._record("get_organisation", "GET", org)
        return replace(self.organisation, login=org)

    def get_repository(self, owner: str, name: str) -> Repository:
        target = f"{owner}/{name}"
        self._record("get_repository", "GET", target)
        if target.casefold() == self.template.name_with_owner.casefold():
            return self.template
        repository = self.repositories.get(name)
        if owner.casefold() == self.organisation.login.casefold() and repository is not None:
            return repository
        raise GitHubNotFoundError(
            f"repository {target} does not exist",
            status_code=404,
            operation=f"read repository {target}",
        )

    def list_teams(self, org: str) -> list[Team]:
        self._record("list_teams", "GET", org)
        return list(self.teams.values())

    def list_repositories(self, org: str) -> list[Repository]:
        self._record("list_repositories", "GET", org)
        return list(self.repositories.values())

    def list_pending_invitations(self, org: str) -> list[Invitation]:
        self._record("list_pending_invitations", "GET", org)
        return list(self.pending.values())

    def list_failed_invitations(self, org: str) -> list[FailedInvitation]:
        self._record("list_failed_invitations", "GET", org)
        return list(self.failed_invitations)

    def list_invitation_team_ids(self, org: str, invitation_id: int) -> set[int]:
        self._record(
            "list_invitation_team_ids",
            "GET",
            str(invitation_id),
        )
        invitation = next(
            (pending for pending in self.pending.values() if pending.id == invitation_id),
            None,
        )
        if invitation is None:
            return set()
        return set(invitation.team_ids)

    def list_team_members(self, org: str, slug: str) -> list[TeamMember]:
        self._record("list_team_members", "GET", slug)
        return sorted(
            self.members.get(slug, set()),
            key=lambda member: (member.id, member.login.casefold()),
        )

    def get_team_repository_permission(
        self,
        org: str,
        slug: str,
        repo: str,
    ) -> str | None:
        target = f"{slug}/{repo}"
        self._record("get_team_repository_permission", "GET", target)
        return self.permissions.get((slug, repo))

    def create_team(self, org: str, name: str) -> Team:
        self._record(
            "create_team",
            "POST",
            name,
            {"name": name, "privacy": "closed"},
        )
        if name in self.teams:
            raise GitHubError(f"team {name} already exists")
        return self.add_team(name)

    def create_repository_from_template(
        self,
        template_owner: str,
        template_name: str,
        org: str,
        repository: str,
        description: str = "",
    ) -> Repository:
        self._record(
            "create_repository_from_template",
            "POST",
            repository,
            {
                "template": f"{template_owner}/{template_name}",
                "owner": org,
                "name": repository,
                "description": description,
                "private": True,
            },
        )
        if repository in self.repositories:
            raise GitHubError(f"repository {repository} already exists")
        created = Repository(
            id=self._next_repository_id,
            name=repository,
            name_with_owner=f"{org}/{repository}",
            is_private=True,
            description=description,
        )
        self._next_repository_id += 1
        self.repositories[repository] = created
        return created

    def set_team_repository_permission(
        self,
        org: str,
        slug: str,
        repo: str,
        permission: str,
    ) -> None:
        target = f"{slug}/{repo}"
        self._record(
            "set_team_repository_permission",
            "PUT",
            target,
            {"permission": permission},
        )
        if not any(team.slug == slug for team in self.teams.values()):
            raise GitHubError(f"unknown team slug {slug}")
        if repo not in self.repositories:
            raise GitHubError(f"unknown repository {repo}")
        self.permissions[(slug, repo)] = permission

    def invite_member(
        self,
        org: str,
        email: str,
        team_ids: Sequence[int],
    ) -> Invitation:
        numeric_team_ids = tuple(team_ids)
        self._record(
            "invite_member",
            "POST",
            email.casefold(),
            {
                "email": email,
                "role": "direct_member",
                "team_ids": numeric_team_ids,
            },
        )
        if not numeric_team_ids or any(
            isinstance(team_id, bool) or not isinstance(team_id, int)
            for team_id in numeric_team_ids
        ):
            raise AssertionError("invitation team IDs must be numeric")
        known_team_ids = {team.id for team in self.teams.values()}
        if not set(numeric_team_ids).issubset(known_team_ids):
            raise AssertionError("invitation referred to an unknown team ID")
        if email.casefold() in self.pending:
            raise GitHubError(f"a pending invitation already exists for {email}")
        return self.add_pending(email, team_ids=numeric_team_ids)

    def add_team_member(
        self,
        org: str,
        slug: str,
        username: str,
    ) -> TeamMembership:
        target = f"{slug}/{username.casefold()}"
        self._record(
            "add_team_member",
            "PUT",
            target,
            {"role": "member"},
        )
        if not any(team.slug == slug for team in self.teams.values()):
            raise GitHubError(f"unknown team slug {slug}")
        if username.casefold() in self.pending_membership_logins:
            return TeamMembership(role="member", state="pending")
        user_id = self.user_ids.get(username.casefold())
        if user_id is None:
            return TeamMembership(role="member", state="pending")
        self.add_member(slug, username, user_id=user_id)
        return TeamMembership(role="member", state="active")

    def archive_repository(self, org: str, repo: str) -> None:
        self._record(
            "archive_repository",
            "PATCH",
            repo,
            {"archived": True},
        )
        repository = self.repositories.get(repo)
        if repository is None:
            raise GitHubError(f"unknown repository {repo}")
        self.repositories[repo] = replace(repository, is_archived=True)

    def remove_team_repository(self, org: str, slug: str, repo: str) -> None:
        target = f"{slug}/{repo}"
        self._record("remove_team_repository", "DELETE", target)
        self.permissions.pop((slug, repo), None)


def _deep_merge(target: dict[str, Any], changes: Mapping[str, Any]) -> dict[str, Any]:
    for key, value in changes.items():
        if isinstance(value, Mapping) and isinstance(target.get(key), dict):
            _deep_merge(target[key], value)
        else:
            target[key] = value
    return target


@pytest.fixture
def fixed_now() -> datetime:
    return FIXED_NOW


@pytest.fixture
def fake_client() -> FakeGitHubClient:
    return FakeGitHubClient()


@pytest.fixture
def runner() -> CliRunner:
    return CliRunner()


@pytest.fixture
def config_factory(tmp_path: Path) -> Callable[..., Path]:
    counter = 0

    def make_config(
        *,
        repositories: list[dict[str, str]] | None = None,
        overrides: Mapping[str, Any] | None = None,
        filename: str | None = None,
    ) -> Path:
        nonlocal counter
        counter += 1
        data: dict[str, Any] = {
            "schema_version": 1,
            "organisation": "teaching-org",
            "subject": "COMP3018",
            "term": "2026S2",
            "template": "template-owner/teaching-template",
            "naming": {
                "group_team": "{subject}-{term}-{group_id}",
                "individual_team": "IND-{student_id}",
            },
            "repositories": {
                "permission": "push",
                "group": repositories
                or [
                    {
                        "name": "{subject}-{term}-{group_id}",
                        "description": "{subject} {term} project for {group_id}",
                    }
                ],
                "individual_description": (
                    "{subject} {term} individual repository for {student_id}"
                ),
            },
            "paths": {
                "ledger": ".gh-edu/invitations.json",
                "reports": "reports",
            },
            "roster": {"github_login_column": None},
        }
        if overrides:
            _deep_merge(data, overrides)
        path = tmp_path / (filename or f"config-{counter}.yml")
        path.write_text(yaml.safe_dump(data, sort_keys=False), encoding="utf-8")
        return path

    return make_config


@pytest.fixture
def roster_factory(tmp_path: Path) -> Callable[..., Path]:
    counter = 0

    def make_roster(
        rows: Sequence[Mapping[str, str]] | None = None,
        *,
        headers: Sequence[str] | None = None,
        filename: str | None = None,
    ) -> Path:
        nonlocal counter
        counter += 1
        actual_headers = list(headers or ("student_id", "email", "group_id"))
        actual_rows = list(
            rows
            or [
                {
                    "student_id": "12345678",
                    "email": "12345678@student.example.edu.au",
                    "group_id": "G01",
                }
            ]
        )
        path = tmp_path / (filename or f"roster-{counter}.csv")
        with path.open("w", encoding="utf-8", newline="") as handle:
            writer = csv.DictWriter(handle, fieldnames=actual_headers)
            writer.writeheader()
            writer.writerows(actual_rows)
        return path

    return make_roster


@pytest.fixture
def record_factory() -> Callable[..., LedgerRecord]:
    def make_record(
        *,
        student_id: str = "12345678",
        email: str = "12345678@student.example.edu.au",
        group_id: str = "G01",
        team_name: str = "COMP3018-2026S2-G01",
        team_id: int | None = 1000,
        invitation_id: int | None = 3000,
        status: str = "pending",
        attempt_count: int = 1,
        github_login: str | None = None,
        invited_at: datetime = FIXED_NOW,
    ) -> LedgerRecord:
        return LedgerRecord(
            student_id=student_id,
            email=email,
            group_id=group_id,
            team_name=team_name,
            team_id=team_id,
            invitation_id=invitation_id,
            invited_at=invited_at,
            last_seen_pending_at=FIXED_NOW,
            status=status,
            attempt_count=attempt_count,
            github_login=github_login,
        )

    return make_record


@pytest.fixture
def ledger_factory(tmp_path: Path) -> Callable[..., tuple[Path, InvitationLedger]]:
    counter = 0

    def make_ledger(
        records: Sequence[LedgerRecord] = (),
        *,
        organisation: str = "teaching-org",
        filename: str | None = None,
    ) -> tuple[Path, InvitationLedger]:
        nonlocal counter
        counter += 1
        ledger = InvitationLedger(
            organisation=organisation,
            records=list(records),
        )
        path = tmp_path / (filename or f"ledger-{counter}.json")
        path.write_text(
            json.dumps(ledger.model_dump(mode="json"), indent=2) + "\n",
            encoding="utf-8",
        )
        return path, ledger

    return make_ledger


@pytest.fixture
def patch_cli_client(
    monkeypatch: pytest.MonkeyPatch,
) -> Callable[[FakeGitHubClient], None]:
    def patch(client: FakeGitHubClient) -> None:
        clock = [FIXED_NOW]

        def now() -> datetime:
            return clock[0]

        def sleep(seconds: float) -> None:
            clock[0] += timedelta(seconds=seconds)

        def make_test_pacer(**kwargs):
            return ExecutionPacer(**kwargs, now=now, sleep=sleep)

        monkeypatch.setattr(
            "gh_edu.cli.make_client",
            lambda _config, _github_timeout_seconds=None: client,
        )
        monkeypatch.setattr("gh_edu.cli.ExecutionPacer", make_test_pacer)

    return patch


@pytest.fixture
def invoke_cli(
    runner: CliRunner,
    patch_cli_client: Callable[[FakeGitHubClient], None],
) -> Callable[[FakeGitHubClient, Sequence[str]], Any]:
    from gh_edu.cli import app

    def invoke(client: FakeGitHubClient, args: Sequence[str]) -> Any:
        patch_cli_client(client)
        return runner.invoke(app, list(args))

    return invoke


@pytest.fixture
def operation_names() -> Callable[[FakeGitHubClient], list[str]]:
    def names(client: FakeGitHubClient) -> list[str]:
        return [call.operation for call in client.write_calls]

    return names


@pytest.fixture
def read_json() -> Callable[[Path], dict[str, Any]]:
    def read(path: Path) -> dict[str, Any]:
        return json.loads(path.read_text(encoding="utf-8"))

    return read
