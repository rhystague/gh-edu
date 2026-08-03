from __future__ import annotations

import json
import subprocess

import pytest

from gh_edu.github import (
    GhCliClient,
    GitHubAuthError,
    GitHubInvitationLimitError,
    GitHubNetworkError,
    GitHubNotFoundError,
    GitHubRateLimitError,
    GitHubRepositoryGenerationLimitError,
    GitHubResponseError,
)


def _completed(
    *,
    stdout: str = "",
    stderr: str = "",
    returncode: int = 0,
) -> subprocess.CompletedProcess[str]:
    return subprocess.CompletedProcess(
        args=[],
        returncode=returncode,
        stdout=stdout,
        stderr=stderr,
    )


def _install_recorder(
    monkeypatch: pytest.MonkeyPatch,
    responses: list[subprocess.CompletedProcess[str]],
) -> list[tuple[list[str], dict[str, object]]]:
    calls: list[tuple[list[str], dict[str, object]]] = []

    def fake_run(args, **kwargs):
        calls.append((list(args), dict(kwargs)))
        assert responses, f"unexpected subprocess call: {args}"
        return responses.pop(0)

    monkeypatch.setattr("gh_edu.github.subprocess.run", fake_run)
    return calls


def test_check_auth_runs_gh_status_then_reads_login(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    calls = _install_recorder(
        monkeypatch,
        [
            _completed(stdout="github.com account course-admin\n"),
            _completed(stdout='{"login": "course-admin"}'),
        ],
    )
    client = GhCliClient(timeout=12)

    login = client.check_auth()

    assert login == "course-admin"
    assert calls[0][0] == [
        "gh",
        "auth",
        "status",
        "--active",
        "--hostname",
        "github.com",
    ]
    assert calls[1][0][:3] == ["gh", "api", "/user"]
    for _args, kwargs in calls:
        assert kwargs == {
            "shell": False,
            "check": False,
            "capture_output": True,
            "text": True,
            "timeout": 12,
        }


def test_get_organisation_parses_age_and_plan_metadata(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    calls = _install_recorder(
        monkeypatch,
        [
            _completed(
                stdout=json.dumps(
                    {
                        "login": "teaching-org",
                        "created_at": "2020-01-01T00:00:00Z",
                        "plan": {"name": "team"},
                    }
                )
            )
        ],
    )

    organisation = GhCliClient().get_organisation("teaching-org")

    assert organisation.login == "teaching-org"
    assert organisation.created_at == "2020-01-01T00:00:00Z"
    assert organisation.plan_name == "team"
    assert calls[0][0][2] == "/orgs/teaching-org"


def test_invitation_abuse_422_is_a_recoverable_limit(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _install_recorder(
        monkeypatch,
        [
            _completed(
                stdout='{"message":"Validation Failed: endpoint has been spammed"}',
                stderr="gh: request failed (HTTP 422)",
                returncode=1,
            )
        ],
    )

    with pytest.raises(GitHubInvitationLimitError):
        GhCliClient().invite_member("teaching-org", "student@example.edu.au", [123])


def test_ordinary_invitation_422_remains_a_permanent_response_error(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _install_recorder(
        monkeypatch,
        [
            _completed(
                stdout='{"message":"Validation Failed"}',
                stderr="gh: request failed (HTTP 422)",
                returncode=1,
            )
        ],
    )

    with pytest.raises(GitHubResponseError) as raised:
        GhCliClient().invite_member("teaching-org", "student@example.edu.au", [123])

    assert not isinstance(raised.value, GitHubInvitationLimitError)


def test_repository_generation_too_quickly_422_is_a_recoverable_limit(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _install_recorder(
        monkeypatch,
        [
            _completed(
                stdout='{"message":"Could not clone: was submitted too quickly"}',
                stderr=(
                    "gh: request failed (HTTP 422)\n"
                    "Retry-After: 90\n"
                    "X-RateLimit-Reset: 1785729600"
                ),
                returncode=1,
            )
        ],
    )

    with pytest.raises(GitHubRepositoryGenerationLimitError) as raised:
        GhCliClient().create_repository_from_template(
            "template-owner",
            "template",
            "teaching-org",
            "expected-repository",
        )

    assert raised.value.status_code == 422
    assert raised.value.retry_after_seconds == 90
    assert raised.value.reset_at_epoch == 1785729600


@pytest.mark.parametrize(
    ("operation", "expected_error"),
    [
        ("repository", GitHubResponseError),
        ("team", GitHubResponseError),
    ],
)
def test_repository_generation_limit_signature_is_narrowly_scoped(
    monkeypatch: pytest.MonkeyPatch,
    operation: str,
    expected_error: type[Exception],
) -> None:
    message = (
        "Validation Failed"
        if operation == "repository"
        else "Could not clone: was submitted too quickly"
    )
    _install_recorder(
        monkeypatch,
        [
            _completed(
                stdout=json.dumps({"message": message}),
                stderr="gh: request failed (HTTP 422)",
                returncode=1,
            )
        ],
    )

    with pytest.raises(expected_error) as raised:
        if operation == "repository":
            GhCliClient().create_repository_from_template(
                "template-owner",
                "template",
                "teaching-org",
                "expected-repository",
            )
        else:
            GhCliClient().create_team("teaching-org", "expected-team")

    assert not isinstance(raised.value, GitHubRepositoryGenerationLimitError)


def test_create_team_builds_exact_post_fields_and_parses_identity(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    calls = _install_recorder(
        monkeypatch,
        [
            _completed(
                stdout=json.dumps(
                    {
                        "id": 456789,
                        "name": "COMP3018-2026S2-G01",
                        "slug": "comp3018-2026s2-g01",
                        "privacy": "closed",
                    }
                )
            )
        ],
    )
    client = GhCliClient()

    team = client.create_team("teaching-org", "COMP3018-2026S2-G01")

    args = calls[0][0]
    assert team.id == 456789
    assert team.slug == "comp3018-2026s2-g01"
    assert args[:3] == ["gh", "api", "/orgs/teaching-org/teams"]
    assert args[args.index("--method") + 1] == "POST"
    assert "name=COMP3018-2026S2-G01" in args
    assert "privacy=closed" in args
    assert "GH_TOKEN" not in " ".join(args)


def test_invitation_uses_email_role_and_typed_numeric_team_ids(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    calls = _install_recorder(
        monkeypatch,
        [
            _completed(
                stdout=json.dumps(
                    {
                        "id": 987654,
                        "email": "student@example.edu.au",
                        "role": "direct_member",
                        "created_at": "2026-07-30T09:42:00Z",
                    }
                )
            )
        ],
    )
    client = GhCliClient()

    invitation = client.invite_member(
        "teaching-org",
        "student@example.edu.au",
        [456789, 456790],
    )

    args = calls[0][0]
    assert invitation.team_ids == (456789, 456790)
    assert args[:3] == ["gh", "api", "/orgs/teaching-org/invitations"]
    assert args[args.index("--method") + 1] == "POST"
    assert "email=student@example.edu.au" in args
    assert "role=direct_member" in args
    assert "team_ids[]=456789" in args
    assert "team_ids[]=456790" in args
    assert args[args.index("team_ids[]=456789") - 1] == "-F"


@pytest.mark.parametrize("team_ids", [[], [0], [-1], [True]])
def test_invitation_rejects_invalid_team_ids_without_subprocess(
    monkeypatch: pytest.MonkeyPatch,
    team_ids,
) -> None:
    called = False

    def fake_run(*_args, **_kwargs):
        nonlocal called
        called = True
        raise AssertionError("subprocess must not run")

    monkeypatch.setattr("gh_edu.github.subprocess.run", fake_run)

    with pytest.raises(ValueError, match="team ID"):
        GhCliClient().invite_member("teaching-org", "student@example.edu.au", team_ids)

    assert not called


def test_paginated_lists_use_slurp_and_flatten_pages(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    calls = _install_recorder(
        monkeypatch,
        [
            _completed(
                stdout=json.dumps(
                    [
                        [{"id": 1, "name": "Team One", "slug": "team-one"}],
                        [{"id": 2, "name": "Team Two", "slug": "team-two"}],
                    ]
                )
            )
        ],
    )

    teams = GhCliClient().list_teams("teaching-org")

    assert [team.id for team in teams] == [1, 2]
    args = calls[0][0]
    assert "/orgs/teaching-org/teams?per_page=100" in args
    assert "--paginate" in args
    assert "--slurp" in args


def test_invitation_team_lookup_returns_numeric_ids(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    calls = _install_recorder(
        monkeypatch,
        [
            _completed(
                stdout=json.dumps(
                    [
                        [
                            {
                                "id": 42,
                                "name": "Expected Team",
                                "slug": "expected-team",
                            }
                        ],
                        [
                            {
                                "id": 99,
                                "name": "Other Team",
                                "slug": "other-team",
                            }
                        ],
                    ]
                )
            )
        ],
    )

    team_ids = GhCliClient().list_invitation_team_ids("teaching-org", 700)

    assert team_ids == {42, 99}
    args = calls[0][0]
    assert "/orgs/teaching-org/invitations/700/teams?per_page=100" in args
    assert "--paginate" in args
    assert "--slurp" in args


def test_team_member_listing_returns_stable_identity_and_membership_metadata(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    calls = _install_recorder(
        monkeypatch,
        [
            _completed(
                stdout=json.dumps(
                    [
                        [
                            {
                                "id": 501,
                                "login": "Student-Login",
                                "role": "member",
                                "inherited": False,
                            },
                            {
                                "id": 502,
                                "login": "course-maintainer",
                                "role": "maintainer",
                                "inherited": True,
                            },
                        ]
                    ]
                )
            )
        ],
    )

    members = GhCliClient().list_team_members("teaching-org", "identity-team")

    assert [(member.id, member.login, member.role, member.inherited) for member in members] == [
        (501, "Student-Login", "member", False),
        (502, "course-maintainer", "maintainer", True),
    ]
    args = calls[0][0]
    assert ("/orgs/teaching-org/teams/identity-team/members?role=all&per_page=100") in args
    assert "--paginate" in args
    assert "--slurp" in args


def test_add_team_member_uses_current_login_and_parses_active_state(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    calls = _install_recorder(
        monkeypatch,
        [
            _completed(
                stdout=json.dumps(
                    {
                        "role": "member",
                        "state": "active",
                    }
                )
            )
        ],
    )

    membership = GhCliClient().add_team_member(
        "teaching-org",
        "group-team",
        "Student Login/With Slash",
    )

    assert membership.role == "member"
    assert membership.state == "active"
    args = calls[0][0]
    assert args[:3] == [
        "gh",
        "api",
        ("/orgs/teaching-org/teams/group-team/memberships/Student%20Login%2FWith%20Slash"),
    ]
    assert args[args.index("--method") + 1] == "PUT"
    assert "role=member" in args


def test_add_team_member_preserves_pending_response_for_core_safety_check(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _install_recorder(
        monkeypatch,
        [
            _completed(
                stdout=json.dumps(
                    {
                        "role": "member",
                        "state": "pending",
                    }
                )
            )
        ],
    )

    membership = GhCliClient().add_team_member(
        "teaching-org",
        "group-team",
        "student-login",
    )

    assert membership.state == "pending"


def test_permission_404_means_absent_only_for_known_team_and_repository(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    responses = [
        _completed(
            stdout=json.dumps([[{"id": 1, "name": "Expected Team", "slug": "expected-team"}]])
        ),
        _completed(
            stdout=json.dumps(
                [
                    [
                        {
                            "id": 2,
                            "name": "expected-repo",
                            "full_name": "teaching-org/expected-repo",
                            "private": True,
                        }
                    ]
                ]
            )
        ),
        _completed(stderr="gh: Not Found (HTTP 404)", returncode=1),
    ]
    _install_recorder(monkeypatch, responses)
    client = GhCliClient()
    client.list_teams("teaching-org")
    client.list_repositories("teaching-org")

    permission = client.get_team_repository_permission(
        "teaching-org",
        "expected-team",
        "expected-repo",
    )

    assert permission is None


def test_permission_404_for_unknown_resource_is_not_silently_swallowed(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _install_recorder(
        monkeypatch,
        [_completed(stderr="gh: Not Found (HTTP 404)", returncode=1)],
    )

    with pytest.raises(GitHubNotFoundError):
        GhCliClient().get_team_repository_permission(
            "teaching-org",
            "unknown-team",
            "unknown-repo",
        )


@pytest.mark.parametrize(
    ("stderr", "returncode", "error_type"),
    [
        (
            "gh: API rate limit exceeded (HTTP 403)\nRetry-After: 60",
            1,
            GitHubRateLimitError,
        ),
        ("gh: Resource not accessible (HTTP 403)", 1, GitHubAuthError),
        ("gh: Not Found (HTTP 404)", 1, GitHubNotFoundError),
        ("could not resolve host github.com", 1, GitHubNetworkError),
        ("gh: Internal Server Error (HTTP 500)", 1, GitHubResponseError),
    ],
    ids=["rate-limit", "authorisation", "not-found", "network", "response"],
)
def test_cli_error_output_is_mapped_to_typed_errors(
    monkeypatch: pytest.MonkeyPatch,
    stderr,
    returncode,
    error_type,
) -> None:
    _install_recorder(
        monkeypatch,
        [_completed(stderr=stderr, returncode=returncode)],
    )

    with pytest.raises(error_type) as raised:
        GhCliClient().get_repository("template-owner", "template")

    if error_type is GitHubRateLimitError:
        assert raised.value.retry_after_seconds == 60
        assert raised.value.status_code == 403


@pytest.mark.parametrize(
    ("exception", "error_type"),
    [
        (FileNotFoundError("gh"), GitHubAuthError),
        (
            subprocess.TimeoutExpired(cmd=["gh"], timeout=1),
            GitHubNetworkError,
        ),
        (OSError("transport unavailable"), GitHubNetworkError),
    ],
)
def test_subprocess_start_failures_are_sanitised_typed_errors(
    monkeypatch: pytest.MonkeyPatch,
    exception,
    error_type,
) -> None:
    def fail_run(*_args, **_kwargs):
        raise exception

    monkeypatch.setattr("gh_edu.github.subprocess.run", fail_run)

    with pytest.raises(error_type):
        GhCliClient().get_repository("template-owner", "template")


def test_invalid_json_is_reported_without_decoder_details(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _install_recorder(monkeypatch, [_completed(stdout="{bad-json")])

    with pytest.raises(GitHubResponseError, match="invalid JSON"):
        GhCliClient().get_repository("template-owner", "template")


def test_repository_generation_archive_and_relationship_removal_arguments(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    calls = _install_recorder(
        monkeypatch,
        [
            _completed(
                stdout=json.dumps(
                    {
                        "id": 10,
                        "name": "COMP3018-2026S2-G01",
                        "full_name": "teaching-org/COMP3018-2026S2-G01",
                        "private": True,
                    }
                )
            ),
            _completed(),
            _completed(),
        ],
    )
    client = GhCliClient()

    client.create_repository_from_template(
        "template-owner",
        "template",
        "teaching-org",
        "COMP3018-2026S2-G01",
        "Project repository",
    )
    client.archive_repository("teaching-org", "COMP3018-2026S2-G01")
    client.remove_team_repository(
        "teaching-org",
        "comp3018-2026s2-g01",
        "COMP3018-2026S2-G01",
    )

    generate, archive, remove = [call[0] for call in calls]
    assert generate[2] == "/repos/template-owner/template/generate"
    assert generate[generate.index("--method") + 1] == "POST"
    assert "owner=teaching-org" in generate
    assert "name=COMP3018-2026S2-G01" in generate
    assert "private=true" in generate
    assert archive[archive.index("--method") + 1] == "PATCH"
    assert "archived=true" in archive
    assert remove[remove.index("--method") + 1] == "DELETE"
    assert remove[2].endswith("/teams/comp3018-2026s2-g01/repos/teaching-org/COMP3018-2026S2-G01")
