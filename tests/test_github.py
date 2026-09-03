from __future__ import annotations

from pathlib import Path

import pytest

from chartpub.errors import PublicationError, RemoteConflict
from chartpub.github import GitHubClient, Response
from tests.fakes import FakeGitHub

TOKEN = "ghp_0123456789abcdefghijklmnopqrstuvwxyz"


def client(fake: FakeGitHub, token: str = TOKEN) -> GitHubClient:
    return GitHubClient(repository=fake.repository, token=token, transport=fake, secrets=(token,))


def test_list_releases_follows_every_page() -> None:
    fake = FakeGitHub(page_size=1)
    for tag in ("chart-v0.1.0", "chart-v0.2.0", "chart-v0.3.0"):
        fake.add_release(tag)
    assert [r["tag_name"] for r in client(fake).list_releases()] == [
        "chart-v0.1.0",
        "chart-v0.2.0",
        "chart-v0.3.0",
    ]


def test_list_assets_follows_every_page() -> None:
    fake = FakeGitHub(page_size=1)
    release = fake.add_release("chart-v0.4.1", assets={"a.tgz": b"a", "b.tgz": b"b"})
    names = [a["name"] for a in client(fake).list_assets(int(release["id"]))]
    assert names == ["a.tgz", "b.tgz"]


def test_pagination_is_bounded() -> None:

    def endless(
        method: str, url: str, headers: dict[str, str], body: bytes | None
    ) -> tuple[int, dict[str, str], bytes]:
        return 200, {"Link": '<https://api.github.com/next>; rel="next"'}, b"[]"

    gh = GitHubClient(repository="owner/repo", token=TOKEN, transport=endless)
    with pytest.raises(PublicationError, match="100 pages"):
        gh.list_releases()


def test_non_list_payload_is_rejected() -> None:
    def scalar(
        method: str, url: str, headers: dict[str, str], body: bytes | None
    ) -> tuple[int, dict[str, str], bytes]:
        return 200, {}, b"{}"

    gh = GitHubClient(repository="owner/repo", token=TOKEN, transport=scalar)
    with pytest.raises(PublicationError, match="expected a list"):
        gh.list_releases()


def test_errors_redact_the_token() -> None:
    fake = FakeGitHub()
    fake.faults[("GET", "/releases")] = (500, f"boom token={TOKEN} leaked")
    with pytest.raises(PublicationError) as excinfo:
        client(fake).list_releases()
    message = str(excinfo.value)
    assert TOKEN not in message
    assert "[REDACTED]" in message


def test_error_message_drops_the_query_string() -> None:
    fake = FakeGitHub()
    release = fake.add_release("chart-v0.4.1")
    fake.faults[("POST", "/assets")] = (413, "too large")
    with pytest.raises(PublicationError) as excinfo:
        client(fake).upload_asset(int(release["id"]), "x.tgz", Path("/dev/null"))
    assert "?" not in str(excinfo.value)


def test_missing_ref_is_none_not_an_error() -> None:
    assert client(FakeGitHub()).get_ref("tags/absent") is None


def test_annotated_tags_are_peeled() -> None:
    fake = FakeGitHub()
    fake.refs["tags/chart-v0.4.0"] = "f" * 40
    fake.annotated["f" * 40] = "c" * 40
    assert client(fake).get_ref("tags/chart-v0.4.0") == "c" * 40


def test_require_ref_detects_a_moved_tip() -> None:
    fake = FakeGitHub()
    fake.refs["heads/gh-pages"] = "1" * 40
    gh = client(fake)
    gh.require_ref("heads/gh-pages", "1" * 40)
    with pytest.raises(RemoteConflict, match="expected"):
        gh.require_ref("heads/gh-pages", "2" * 40)


def test_delete_tag_is_guarded_and_idempotent() -> None:
    fake = FakeGitHub()
    fake.refs["tags/chart-v0.4.0"] = "b" * 40
    gh = client(fake)
    with pytest.raises(RemoteConflict):
        gh.delete_tag("chart-v0.4.0", expected="9" * 40)
    assert "tags/chart-v0.4.0" in fake.refs
    gh.delete_tag("chart-v0.4.0", expected="b" * 40)
    assert "tags/chart-v0.4.0" not in fake.refs
    gh.delete_ref("tags/chart-v0.4.0")


def test_create_ref_round_trips() -> None:
    fake = FakeGitHub()
    gh = client(fake)
    gh.create_ref("tags/chart-v0.4.1", "d" * 40)
    assert gh.get_ref("tags/chart-v0.4.1") == "d" * 40
    with pytest.raises(PublicationError, match="already exists"):
        gh.create_ref("tags/chart-v0.4.1", "d" * 40)


def test_asset_upload_download_and_delete(tmp_path: Path) -> None:
    fake = FakeGitHub()
    release = fake.add_release("chart-v0.4.1")
    payload = b"chart-bytes"
    source = tmp_path / "ledger-api-0.4.1.tgz"
    source.write_bytes(payload)
    gh = client(fake)
    asset = gh.upload_asset(int(release["id"]), source.name, source)
    assert gh.download_asset(int(asset["id"])) == payload
    gh.delete_asset(int(asset["id"]))
    assert gh.list_assets(int(release["id"])) == []


def test_quarantine_preserves_identity_and_is_idempotent() -> None:
    fake = FakeGitHub()
    release = fake.add_release("chart-v0.4.0", assets={"ledger-api-0.4.0.tgz": b"x"})
    gh = client(fake)
    first = gh.quarantine_release(release, "WITHDRAWN: note")
    second = gh.quarantine_release(gh.get_release(int(first["id"])), "WITHDRAWN: note")
    assert first["id"] == release["id"] == second["id"]
    assert first["tag_name"] == "chart-v0.4.0"
    assert second["draft"] is True
    assert second["body"].count("WITHDRAWN") == 1
    assert len(gh.list_assets(int(second["id"]))) == 1


def test_find_release_and_update() -> None:
    fake = FakeGitHub()
    fake.add_release("chart-v0.4.1", draft=True)
    gh = client(fake)
    found = gh.find_release("chart-v0.4.1")
    assert found is not None
    assert gh.update_release(int(found["id"]), draft=False)["draft"] is False
    assert gh.find_release("chart-v9.9.9") is None


def test_create_release_returns_the_new_object() -> None:
    gh = client(FakeGitHub())
    release = gh.create_release("chart-v0.4.1", target="e" * 40, name="n", body="b")
    assert release["tag_name"] == "chart-v0.4.1"
    assert gh.get_release(int(release["id"]))["name"] == "chart-v0.4.1"


def test_get_release_reports_missing_ids() -> None:
    with pytest.raises(PublicationError, match="failed \\(404\\)"):
        client(FakeGitHub()).get_release(1)


def test_response_link_parsing() -> None:
    response = Response(
        200,
        None,
        {"Link": '<https://api.github.com/a?page=2>; rel="next", <https://x>; rel="last"'},
    )
    assert response.link("next") == "https://api.github.com/a?page=2"
    assert response.link("prev") is None
    assert Response(200, None, {}).link("next") is None
