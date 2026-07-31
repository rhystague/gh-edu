"""Typed GitHub access through the authenticated GitHub CLI.

This module is the only place where the application needs to know how to invoke
``gh`` or how GitHub REST response fields are represented.
"""

from __future__ import annotations

import json
import re
import subprocess
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from typing import Protocol, cast
from urllib.parse import quote


@dataclass(frozen=True, slots=True)
class Team:
    """The GitHub team fields needed by the provisioning workflow."""

    id: int
    name: str
    slug: str
    privacy: str | None = None


@dataclass(frozen=True, slots=True)
class TeamMember:
    """An active GitHub account visible through a team membership."""

    id: int
    login: str
    role: str
    inherited: bool


@dataclass(frozen=True, slots=True)
class TeamMembership:
    """The state returned after adding or updating a team membership."""

    role: str
    state: str


@dataclass(frozen=True, slots=True)
class Repository:
    """The GitHub repository fields needed by the provisioning workflow."""

    name: str
    name_with_owner: str = ""
    is_private: bool = False
    is_archived: bool = False
    is_template: bool = False
    id: int | None = None
    description: str | None = None

    @property
    def full_name(self) -> str:
        """Return GitHub's REST ``full_name`` value."""

        return self.name_with_owner


@dataclass(frozen=True, slots=True)
class Invitation:
    """A pending or newly created organisation invitation."""

    id: int
    email: str | None
    login: str | None = None
    role: str | None = None
    created_at: str | None = None
    team_ids: tuple[int, ...] = ()


@dataclass(frozen=True, slots=True)
class FailedInvitation:
    """An organisation invitation GitHub explicitly reports as failed."""

    id: int
    email: str | None
    login: str | None = None
    role: str | None = None
    created_at: str | None = None
    failed_at: str | None = None
    failed_reason: str | None = None
    team_ids: tuple[int, ...] = ()


class GitHubError(RuntimeError):
    """Base class for sanitised GitHub adapter failures."""


class GitHubResponseError(GitHubError):
    """GitHub CLI returned an unexpected non-success response."""

    def __init__(
        self,
        message: str,
        *,
        status_code: int | None = None,
        operation: str | None = None,
    ) -> None:
        super().__init__(message)
        self.status_code = status_code
        self.operation = operation

    @property
    def status(self) -> int | None:
        """Alias retained for callers that use the shorter HTTP term."""

        return self.status_code


class GitHubAuthError(GitHubResponseError):
    """GitHub CLI authentication or authorisation is unavailable."""


class GitHubRateLimitError(GitHubResponseError):
    """A primary or secondary GitHub API rate limit blocked the request."""

    def __init__(
        self,
        message: str,
        *,
        status_code: int | None = None,
        operation: str | None = None,
        retry_after_seconds: int | None = None,
        reset_at_epoch: int | None = None,
    ) -> None:
        super().__init__(message, status_code=status_code, operation=operation)
        self.retry_after_seconds = retry_after_seconds
        self.reset_at_epoch = reset_at_epoch


class GitHubNotFoundError(GitHubResponseError):
    """The requested GitHub resource was not found or was not visible."""


class GitHubNetworkError(GitHubError):
    """The GitHub CLI could not complete a request because of transport failure."""

    def __init__(self, message: str, *, operation: str | None = None) -> None:
        super().__init__(message)
        self.operation = operation


class GitHubClient(Protocol):
    """GitHub operations used by discovery, planning, and execution."""

    def check_auth(self) -> str:
        """Validate the active CLI session and return its GitHub login."""

        ...

    def check_organisation(self, org: str) -> None:
        """Require active administrator/owner membership of ``org``."""

        ...

    def get_repository(self, owner: str, name: str) -> Repository:
        """Return one repository."""

        ...

    def list_teams(self, org: str) -> list[Team]:
        """Return every visible team in an organisation."""

        ...

    def list_repositories(self, org: str) -> list[Repository]:
        """Return every visible repository in an organisation."""

        ...

    def list_pending_invitations(self, org: str) -> list[Invitation]:
        """Return every pending organisation invitation."""

        ...

    def list_failed_invitations(self, org: str) -> list[FailedInvitation]:
        """Return invitations GitHub explicitly reports as failed."""

        ...

    def list_invitation_team_ids(self, org: str, invitation_id: int) -> set[int]:
        """Return numeric team IDs attached to an organisation invitation."""

        ...

    def list_team_members(self, org: str, slug: str) -> list[TeamMember]:
        """Return active members and their stable GitHub identities."""

        ...

    def get_team_repository_permission(
        self,
        org: str,
        slug: str,
        repo: str,
    ) -> str | None:
        """Return the exact team repository permission, if a relationship exists."""

        ...

    def create_team(self, org: str, name: str) -> Team:
        """Create a closed organisation team."""

        ...

    def create_repository_from_template(
        self,
        template_owner: str,
        template_name: str,
        org: str,
        repository: str,
        description: str = "",
    ) -> Repository:
        """Create a private organisation repository from a template."""

        ...

    def set_team_repository_permission(
        self,
        org: str,
        slug: str,
        repo: str,
        permission: str,
    ) -> None:
        """Create or update a team/repository relationship."""

        ...

    def invite_member(
        self,
        org: str,
        email: str,
        team_ids: Sequence[int],
    ) -> Invitation:
        """Invite an email address as a direct member of one or more teams."""

        ...

    def add_team_member(
        self,
        org: str,
        slug: str,
        username: str,
    ) -> TeamMembership:
        """Add an existing organisation member to a team."""

        ...

    def archive_repository(self, org: str, repo: str) -> None:
        """Archive an organisation repository."""

        ...

    def remove_team_repository(self, org: str, slug: str, repo: str) -> None:
        """Remove a team/repository relationship."""

        ...


@dataclass(frozen=True, slots=True)
class _CommandResult:
    stdout: str
    stderr: str


_HTTP_STATUS_PATTERNS = (
    re.compile(r"\(\s*HTTP\s+([1-5]\d{2})\s*\)", re.IGNORECASE),
    re.compile(
        r"(?:^|\s)HTTP(?:/\d(?:\.\d)?)?[\s:=]+([1-5]\d{2})\b",
        re.IGNORECASE,
    ),
    re.compile(r"(?:status|status code)\s*[:=]?\s*([1-5]\d{2})", re.IGNORECASE),
)
_RETRY_AFTER_PATTERN = re.compile(r"retry-after\s*:\s*(\d+)", re.IGNORECASE)
_RESET_AT_PATTERN = re.compile(r"x-ratelimit-reset\s*:\s*(\d+)", re.IGNORECASE)
_NETWORK_MARKERS = (
    "check your internet connection",
    "connection refused",
    "connection reset",
    "connection timed out",
    "could not resolve host",
    "dial tcp",
    "dns",
    "error connecting",
    "i/o timeout",
    "network is unreachable",
    "no such host",
    "proxyconnect",
    "temporary failure",
    "tls handshake timeout",
    "unexpected eof",
)
_AUTH_MARKERS = (
    "authentication required",
    "authentication failed",
    "bad credentials",
    "forbidden",
    "gh auth login",
    "insufficient permission",
    "must have admin",
    "no active account",
    "not authenticated",
    "not logged",
    "permission denied",
    "resource not accessible",
    "saml",
    "unauthorized",
)
_RATE_LIMIT_MARKERS = (
    "abuse detection",
    "rate limit",
    "retry-after",
    "secondary rate",
    "x-ratelimit-remaining: 0",
)
_ROLE_NAMES = {
    "read": "pull",
    "write": "push",
}


class GhCliClient:
    """A subprocess-backed :class:`GitHubClient` using existing ``gh`` auth."""

    def __init__(
        self,
        *,
        hostname: str = "github.com",
        api_version: str = "2022-11-28",
        timeout: float = 30.0,
        executable: str = "gh",
    ) -> None:
        if not hostname.strip():
            raise ValueError("hostname must not be blank")
        if not api_version.strip():
            raise ValueError("api_version must not be blank")
        if timeout <= 0:
            raise ValueError("timeout must be greater than zero")
        if not executable.strip():
            raise ValueError("executable must not be blank")

        self._hostname = hostname
        self._api_version = api_version
        self._timeout = timeout
        self._executable = executable
        self._known_repositories: set[tuple[str, str]] = set()

    @property
    def hostname(self) -> str:
        """The GitHub hostname supplied to every CLI operation."""

        return self._hostname

    @property
    def api_version(self) -> str:
        """The explicitly pinned GitHub REST API version."""

        return self._api_version

    def check_auth(self) -> str:
        self._run(
            [
                self._executable,
                "auth",
                "status",
                "--active",
                "--hostname",
                self._hostname,
            ],
            operation="check GitHub CLI authentication",
        )
        payload = self._api_json("/user", operation="read the authenticated GitHub user")
        user = _expect_object(payload, "authenticated GitHub user")
        return _required_string(user, "login", "authenticated GitHub user")

    def check_organisation(self, org: str) -> None:
        endpoint = f"/user/memberships/orgs/{_path_segment(org)}"
        try:
            payload = self._api_json(
                endpoint,
                operation=f"check membership of organisation {org}",
            )
        except GitHubNotFoundError as exc:
            raise GitHubAuthError(
                f"Active administrator membership of organisation {org!r} is required",
                status_code=exc.status_code,
                operation=exc.operation,
            ) from exc

        membership = _expect_object(payload, f"membership of organisation {org}")
        state = _required_string(membership, "state", "organisation membership")
        role = _required_string(membership, "role", "organisation membership")
        if state.casefold() != "active" or role.casefold() not in {
            "admin",
            "owner",
        }:
            raise GitHubAuthError(
                f"Active administrator membership of organisation {org!r} is required",
                operation=f"check membership of organisation {org}",
            )

    def get_repository(self, owner: str, name: str) -> Repository:
        endpoint = f"/repos/{_path_segment(owner)}/{_path_segment(name)}"
        payload = self._api_json(endpoint, operation=f"read repository {owner}/{name}")
        repository = _parse_repository(payload, owner_hint=owner)
        self._remember_repository(owner, repository.name)
        return repository

    def list_teams(self, org: str) -> list[Team]:
        endpoint = f"/orgs/{_path_segment(org)}/teams?per_page=100"
        payload = self._api_json(
            endpoint,
            operation=f"list teams in organisation {org}",
            paginate=True,
        )
        teams = [
            _parse_team(item)
            for item in _expect_paginated_objects(payload, f"teams in organisation {org}")
        ]
        return teams

    def list_repositories(self, org: str) -> list[Repository]:
        endpoint = f"/orgs/{_path_segment(org)}/repos?type=all&per_page=100"
        payload = self._api_json(
            endpoint,
            operation=f"list repositories in organisation {org}",
            paginate=True,
        )
        repositories = [
            _parse_repository(item, owner_hint=org)
            for item in _expect_paginated_objects(
                payload,
                f"repositories in organisation {org}",
            )
        ]
        self._known_repositories.update(
            (org.casefold(), repository.name.casefold()) for repository in repositories
        )
        return repositories

    def list_pending_invitations(self, org: str) -> list[Invitation]:
        endpoint = f"/orgs/{_path_segment(org)}/invitations?per_page=100"
        payload = self._api_json(
            endpoint,
            operation=f"list pending invitations in organisation {org}",
            paginate=True,
        )
        return [
            _parse_invitation(item)
            for item in _expect_paginated_objects(
                payload,
                f"pending invitations in organisation {org}",
            )
        ]

    def list_failed_invitations(self, org: str) -> list[FailedInvitation]:
        endpoint = f"/orgs/{_path_segment(org)}/failed_invitations?per_page=100"
        payload = self._api_json(
            endpoint,
            operation=f"list failed invitations in organisation {org}",
            paginate=True,
        )
        return [
            _parse_failed_invitation(item)
            for item in _expect_paginated_objects(
                payload,
                f"failed invitations in organisation {org}",
            )
        ]

    def list_invitation_team_ids(self, org: str, invitation_id: int) -> set[int]:
        endpoint = f"/orgs/{_path_segment(org)}/invitations/{invitation_id}/teams?per_page=100"
        payload = self._api_json(
            endpoint,
            operation=(f"list teams for invitation {invitation_id} in organisation {org}"),
            paginate=True,
        )
        teams = [
            _parse_team(item)
            for item in _expect_paginated_objects(
                payload,
                f"teams for invitation {invitation_id} in organisation {org}",
            )
        ]
        return {team.id for team in teams}

    def list_team_members(self, org: str, slug: str) -> list[TeamMember]:
        endpoint = (
            f"/orgs/{_path_segment(org)}/teams/{_path_segment(slug)}"
            "/members?role=all&per_page=100"
        )
        payload = self._api_json(
            endpoint,
            operation=f"list members of team {org}/{slug}",
            paginate=True,
        )
        members = _expect_paginated_objects(payload, f"members of team {org}/{slug}")
        return [
            _parse_team_member(member, context=f"member of team {org}/{slug}")
            for member in members
        ]

    def get_team_repository_permission(
        self,
        org: str,
        slug: str,
        repo: str,
    ) -> str | None:
        endpoint = (
            f"/orgs/{_path_segment(org)}/teams/{_path_segment(slug)}"
            f"/repos/{_path_segment(org)}/{_path_segment(repo)}"
        )
        try:
            payload = self._api_json(
                endpoint,
                operation=f"read permission for team {org}/{slug} on repository {repo}",
                accept="application/vnd.github.v3.repository+json",
            )
        except GitHubNotFoundError:
            repository_known = (org.casefold(), repo.casefold()) in self._known_repositories
            if repository_known:
                return None
            raise

        relationship = _expect_object(
            payload,
            f"permission for team {org}/{slug} on repository {repo}",
        )
        role_name = _optional_string(relationship, "role_name")
        if role_name is not None:
            return _ROLE_NAMES.get(role_name.casefold(), role_name.casefold())

        permission = _optional_string(relationship, "permission")
        if permission is not None:
            return _ROLE_NAMES.get(permission.casefold(), permission.casefold())

        permissions_value = relationship.get("permissions")
        if permissions_value is not None:
            permissions = _expect_object(
                permissions_value,
                f"permissions for team {org}/{slug} on repository {repo}",
            )
            for candidate in ("admin", "maintain", "push", "triage", "pull"):
                if permissions.get(candidate) is True:
                    return candidate

        raise GitHubResponseError(
            f"GitHub did not return a permission for team {org}/{slug} on repository {repo}",
            operation=f"read permission for team {org}/{slug} on repository {repo}",
        )

    def create_team(self, org: str, name: str) -> Team:
        endpoint = f"/orgs/{_path_segment(org)}/teams"
        payload = self._api_json(
            endpoint,
            operation=f"create team {name} in organisation {org}",
            method="POST",
            string_fields=(("name", name), ("privacy", "closed")),
        )
        team = _parse_team(payload)
        return team

    def create_repository_from_template(
        self,
        template_owner: str,
        template_name: str,
        org: str,
        repository: str,
        description: str = "",
    ) -> Repository:
        endpoint = f"/repos/{_path_segment(template_owner)}/{_path_segment(template_name)}/generate"
        payload = self._api_json(
            endpoint,
            operation=(
                f"create repository {org}/{repository} from template "
                f"{template_owner}/{template_name}"
            ),
            method="POST",
            string_fields=(
                ("owner", org),
                ("name", repository),
                ("description", description),
            ),
            typed_fields=(("private", "true"),),
        )
        created = _parse_repository(payload, owner_hint=org)
        self._remember_repository(org, created.name)
        return created

    def set_team_repository_permission(
        self,
        org: str,
        slug: str,
        repo: str,
        permission: str,
    ) -> None:
        endpoint = (
            f"/orgs/{_path_segment(org)}/teams/{_path_segment(slug)}"
            f"/repos/{_path_segment(org)}/{_path_segment(repo)}"
        )
        self._api(
            endpoint,
            operation=f"set permission for team {org}/{slug} on repository {repo}",
            method="PUT",
            string_fields=(("permission", permission),),
        )

    def invite_member(
        self,
        org: str,
        email: str,
        team_ids: Sequence[int],
    ) -> Invitation:
        numeric_team_ids = tuple(team_ids)
        if not numeric_team_ids:
            raise ValueError("at least one team ID is required")
        if any(isinstance(team_id, bool) or team_id <= 0 for team_id in numeric_team_ids):
            raise ValueError("team IDs must be positive integers")

        endpoint = f"/orgs/{_path_segment(org)}/invitations"
        payload = self._api_json(
            endpoint,
            operation=f"invite {email} to organisation {org}",
            method="POST",
            string_fields=(("email", email), ("role", "direct_member")),
            typed_fields=tuple(("team_ids[]", str(team_id)) for team_id in numeric_team_ids),
        )
        return _parse_invitation(payload, team_ids=numeric_team_ids)

    def add_team_member(
        self,
        org: str,
        slug: str,
        username: str,
    ) -> TeamMembership:
        endpoint = (
            f"/orgs/{_path_segment(org)}/teams/{_path_segment(slug)}"
            f"/memberships/{_path_segment(username)}"
        )
        payload = self._api_json(
            endpoint,
            operation=f"add {username} to team {org}/{slug}",
            method="PUT",
            string_fields=(("role", "member"),),
        )
        return _parse_team_membership(payload)

    def archive_repository(self, org: str, repo: str) -> None:
        endpoint = f"/repos/{_path_segment(org)}/{_path_segment(repo)}"
        self._api(
            endpoint,
            operation=f"archive repository {org}/{repo}",
            method="PATCH",
            typed_fields=(("archived", "true"),),
        )

    def remove_team_repository(self, org: str, slug: str, repo: str) -> None:
        endpoint = (
            f"/orgs/{_path_segment(org)}/teams/{_path_segment(slug)}"
            f"/repos/{_path_segment(org)}/{_path_segment(repo)}"
        )
        self._api(
            endpoint,
            operation=f"remove team {org}/{slug} from repository {repo}",
            method="DELETE",
        )

    def _remember_repository(self, owner: str, name: str) -> None:
        self._known_repositories.add((owner.casefold(), name.casefold()))

    def _api_json(
        self,
        endpoint: str,
        *,
        operation: str,
        method: str | None = None,
        paginate: bool = False,
        accept: str = "application/vnd.github+json",
        string_fields: Sequence[tuple[str, str]] = (),
        typed_fields: Sequence[tuple[str, str]] = (),
    ) -> object:
        result = self._api(
            endpoint,
            operation=operation,
            method=method,
            paginate=paginate,
            accept=accept,
            string_fields=string_fields,
            typed_fields=typed_fields,
        )
        if not result.stdout.strip():
            raise GitHubResponseError(
                f"GitHub returned an empty response while attempting to {operation}",
                operation=operation,
            )
        try:
            return cast(object, json.loads(result.stdout))
        except json.JSONDecodeError as exc:
            raise GitHubResponseError(
                f"GitHub returned invalid JSON while attempting to {operation}",
                operation=operation,
            ) from exc

    def _api(
        self,
        endpoint: str,
        *,
        operation: str,
        method: str | None = None,
        paginate: bool = False,
        accept: str = "application/vnd.github+json",
        string_fields: Sequence[tuple[str, str]] = (),
        typed_fields: Sequence[tuple[str, str]] = (),
    ) -> _CommandResult:
        args = [
            self._executable,
            "api",
            endpoint,
            "--hostname",
            self._hostname,
            "-H",
            f"Accept: {accept}",
            "-H",
            f"X-GitHub-Api-Version: {self._api_version}",
        ]
        if method is not None:
            args.extend(("--method", method))
        if paginate:
            args.extend(("--paginate", "--slurp"))
        for key, value in string_fields:
            args.extend(("-f", f"{key}={value}"))
        for key, value in typed_fields:
            args.extend(("-F", f"{key}={value}"))
        return self._run(args, operation=operation)

    def _run(self, args: Sequence[str], *, operation: str) -> _CommandResult:
        try:
            completed = subprocess.run(
                list(args),
                shell=False,
                check=False,
                capture_output=True,
                text=True,
                timeout=self._timeout,
            )
        except FileNotFoundError as exc:
            raise GitHubAuthError(
                "GitHub CLI executable was not found",
                operation=operation,
            ) from exc
        except subprocess.TimeoutExpired as exc:
            raise GitHubNetworkError(
                f"GitHub CLI timed out while attempting to {operation}",
                operation=operation,
            ) from exc
        except OSError as exc:
            raise GitHubNetworkError(
                f"GitHub CLI could not run while attempting to {operation}",
                operation=operation,
            ) from exc

        stdout = completed.stdout or ""
        stderr = completed.stderr or ""
        if completed.returncode != 0:
            self._raise_cli_error(
                returncode=completed.returncode,
                stdout=stdout,
                stderr=stderr,
                operation=operation,
            )
        return _CommandResult(stdout=stdout, stderr=stderr)

    def _raise_cli_error(
        self,
        *,
        returncode: int,
        stdout: str,
        stderr: str,
        operation: str,
    ) -> None:
        status_code = _extract_http_status(stderr)
        diagnostic = f"{stderr}\n{stdout}".casefold()
        detail = _extract_error_detail(stdout, stderr)
        message = (
            f"GitHub could not {operation}: {detail}" if detail else f"GitHub could not {operation}"
        )

        rate_limit_reported = any(marker in diagnostic for marker in _RATE_LIMIT_MARKERS)
        if status_code == 429 or (rate_limit_reported and status_code in {None, 403, 429}):
            raise GitHubRateLimitError(
                message,
                status_code=status_code,
                operation=operation,
                retry_after_seconds=_extract_header_integer(
                    f"{stderr}\n{stdout}",
                    _RETRY_AFTER_PATTERN,
                ),
                reset_at_epoch=_extract_header_integer(
                    f"{stderr}\n{stdout}",
                    _RESET_AT_PATTERN,
                ),
            )

        if status_code == 404:
            raise GitHubNotFoundError(
                message,
                status_code=status_code,
                operation=operation,
            )

        if (
            returncode == 4
            or status_code in {401, 403}
            or any(marker in diagnostic for marker in _AUTH_MARKERS)
        ):
            raise GitHubAuthError(
                message,
                status_code=status_code,
                operation=operation,
            )

        if status_code is None and any(marker in diagnostic for marker in _NETWORK_MARKERS):
            raise GitHubNetworkError(message, operation=operation)

        raise GitHubResponseError(
            message,
            status_code=status_code,
            operation=operation,
        )


def _path_segment(value: str) -> str:
    if not value:
        raise ValueError("GitHub resource names must not be empty")
    return quote(value, safe="")


def _extract_http_status(stderr: str) -> int | None:
    for pattern in _HTTP_STATUS_PATTERNS:
        matches = pattern.findall(stderr)
        if matches:
            return int(matches[-1])
    return None


def _extract_header_integer(text: str, pattern: re.Pattern[str]) -> int | None:
    match = pattern.search(text)
    return int(match.group(1)) if match is not None else None


def _extract_error_detail(stdout: str, stderr: str) -> str | None:
    try:
        payload = json.loads(stdout)
    except (json.JSONDecodeError, TypeError):
        payload = None
    if isinstance(payload, dict):
        message = payload.get("message")
        if isinstance(message, str) and message.strip():
            return message.strip()

    for raw_line in stderr.splitlines():
        line = raw_line.strip()
        if not line:
            continue
        if line.casefold().startswith("gh:"):
            line = line[3:].strip()
        for pattern in _HTTP_STATUS_PATTERNS:
            line = pattern.sub("", line).strip()
        if line:
            return line
    return None


def _expect_object(value: object, context: str) -> Mapping[str, object]:
    if not isinstance(value, dict) or not all(isinstance(key, str) for key in value):
        raise GitHubResponseError(f"GitHub returned an invalid {context} response")
    return cast(dict[str, object], value)


def _expect_paginated_objects(value: object, context: str) -> list[Mapping[str, object]]:
    if not isinstance(value, list):
        raise GitHubResponseError(f"GitHub returned an invalid {context} response")

    flattened: list[object] = []
    if all(isinstance(page, list) for page in value):
        for page in value:
            flattened.extend(cast(list[object], page))
    else:
        flattened.extend(cast(list[object], value))
    return [_expect_object(item, context) for item in flattened]


def _required_string(data: Mapping[str, object], key: str, context: str) -> str:
    value = data.get(key)
    if not isinstance(value, str) or not value:
        raise GitHubResponseError(
            f"GitHub returned an invalid {context} response: {key!r} is missing"
        )
    return value


def _optional_string(data: Mapping[str, object], key: str) -> str | None:
    value = data.get(key)
    if value is None:
        return None
    if not isinstance(value, str):
        raise GitHubResponseError(f"GitHub returned an invalid response: {key!r} is not a string")
    return value


def _required_integer(data: Mapping[str, object], key: str, context: str) -> int:
    value = data.get(key)
    if not isinstance(value, int) or isinstance(value, bool):
        raise GitHubResponseError(
            f"GitHub returned an invalid {context} response: {key!r} is missing"
        )
    return value


def _optional_integer(data: Mapping[str, object], key: str) -> int | None:
    value = data.get(key)
    if value is None:
        return None
    if not isinstance(value, int) or isinstance(value, bool):
        raise GitHubResponseError(f"GitHub returned an invalid response: {key!r} is not an integer")
    return value


def _boolean_alias(
    data: Mapping[str, object],
    *keys: str,
    default: bool = False,
) -> bool:
    for key in keys:
        if key not in data:
            continue
        value = data[key]
        if not isinstance(value, bool):
            raise GitHubResponseError(
                f"GitHub returned an invalid response: {key!r} is not a boolean"
            )
        return value
    return default


def _team_ids(data: Mapping[str, object]) -> tuple[int, ...]:
    value = data.get("team_ids")
    if value is None:
        return ()
    if not isinstance(value, list):
        raise GitHubResponseError(
            "GitHub returned an invalid invitation response: 'team_ids' is not a list"
        )
    result: list[int] = []
    for team_id in value:
        if not isinstance(team_id, int) or isinstance(team_id, bool):
            raise GitHubResponseError(
                "GitHub returned an invalid invitation response: team ID is not an integer"
            )
        result.append(team_id)
    return tuple(result)


def _parse_team(value: object) -> Team:
    data = _expect_object(value, "team")
    return Team(
        id=_required_integer(data, "id", "team"),
        name=_required_string(data, "name", "team"),
        slug=_required_string(data, "slug", "team"),
        privacy=_optional_string(data, "privacy"),
    )


def _parse_team_member(
    value: object,
    *,
    context: str = "team member",
) -> TeamMember:
    data = _expect_object(value, context)
    role = _required_string(data, "role", context).casefold()
    if role not in {"member", "maintainer"}:
        raise GitHubResponseError(
            f"GitHub returned an invalid {context} response: unsupported role {role!r}"
        )
    inherited = data.get("inherited")
    if not isinstance(inherited, bool):
        raise GitHubResponseError(
            f"GitHub returned an invalid {context} response: 'inherited' is missing"
        )
    user_id = _required_integer(data, "id", context)
    if user_id <= 0:
        raise GitHubResponseError(
            f"GitHub returned an invalid {context} response: 'id' must be positive"
        )
    return TeamMember(
        id=user_id,
        login=_required_string(data, "login", context),
        role=role,
        inherited=inherited,
    )


def _parse_team_membership(value: object) -> TeamMembership:
    data = _expect_object(value, "team membership")
    role = _required_string(data, "role", "team membership").casefold()
    state = _required_string(data, "state", "team membership").casefold()
    if role not in {"member", "maintainer"}:
        raise GitHubResponseError(
            f"GitHub returned an invalid team membership response: "
            f"unsupported role {role!r}"
        )
    if state not in {"active", "pending"}:
        raise GitHubResponseError(
            f"GitHub returned an invalid team membership response: "
            f"unsupported state {state!r}"
        )
    return TeamMembership(role=role, state=state)


def _parse_repository(value: object, *, owner_hint: str) -> Repository:
    data = _expect_object(value, "repository")
    name = _required_string(data, "name", "repository")
    name_with_owner = _optional_string(data, "full_name")
    if name_with_owner is None:
        name_with_owner = _optional_string(data, "nameWithOwner")
    if name_with_owner is None:
        name_with_owner = f"{owner_hint}/{name}"
    return Repository(
        name=name,
        name_with_owner=name_with_owner,
        is_private=_boolean_alias(data, "private", "isPrivate"),
        is_archived=_boolean_alias(data, "archived", "isArchived"),
        is_template=_boolean_alias(data, "is_template", "isTemplate"),
        id=_optional_integer(data, "id"),
        description=_optional_string(data, "description"),
    )


def _parse_invitation(
    value: object,
    *,
    team_ids: tuple[int, ...] | None = None,
) -> Invitation:
    data = _expect_object(value, "invitation")
    return Invitation(
        id=_required_integer(data, "id", "invitation"),
        email=_optional_string(data, "email"),
        login=_optional_string(data, "login"),
        role=_optional_string(data, "role"),
        created_at=_optional_string(data, "created_at"),
        team_ids=_team_ids(data) if team_ids is None else team_ids,
    )


def _parse_failed_invitation(value: object) -> FailedInvitation:
    data = _expect_object(value, "failed invitation")
    return FailedInvitation(
        id=_required_integer(data, "id", "failed invitation"),
        email=_optional_string(data, "email"),
        login=_optional_string(data, "login"),
        role=_optional_string(data, "role"),
        created_at=_optional_string(data, "created_at"),
        failed_at=_optional_string(data, "failed_at"),
        failed_reason=_optional_string(data, "failed_reason"),
        team_ids=_team_ids(data),
    )
