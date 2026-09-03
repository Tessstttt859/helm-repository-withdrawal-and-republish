"""A small, injectable GitHub REST client with redaction and compare-and-swap."""

from __future__ import annotations

import json
import urllib.error
import urllib.parse
import urllib.request
from collections.abc import Iterator
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Protocol

from chartpub.errors import PublicationError, RemoteConflict
from chartpub.security import redact

USER_AGENT = "chartpub/0.1 (+https://github.com/)"
API_VERSION = "2022-11-28"
PER_PAGE = 100


@dataclass(frozen=True)
class Response:
    status: int
    body: Any
    headers: dict[str, str]
    raw: bytes = b""

    def link(self, rel: str) -> str | None:
        header = self.headers.get("Link") or self.headers.get("link") or ""
        for part in header.split(","):
            section = part.split(";")
            if len(section) < 2:
                continue
            url = section[0].strip().lstrip("<").rstrip(">")
            for attribute in section[1:]:
                if attribute.strip() == f'rel="{rel}"':
                    return url
        return None


class Transport(Protocol):
    def __call__(
        self, method: str, url: str, headers: dict[str, str], body: bytes | None
    ) -> tuple[int, dict[str, str], bytes]: ...


def urllib_transport(
    method: str, url: str, headers: dict[str, str], body: bytes | None
) -> tuple[int, dict[str, str], bytes]:
    request = urllib.request.Request(url, data=body, method=method, headers=headers)
    try:
        with urllib.request.urlopen(request, timeout=60) as response:  # noqa: S310 - https only
            return response.status, dict(response.headers.items()), response.read()
    except urllib.error.HTTPError as exc:
        return exc.code, dict(exc.headers.items()), exc.read()


@dataclass
class GitHubClient:
    """All remote reads and writes go through here so they can be mocked."""

    repository: str
    token: str
    api_url: str = "https://api.github.com"
    uploads_url: str = "https://uploads.github.com"
    transport: Transport = urllib_transport
    secrets: tuple[str, ...] = field(default_factory=tuple)

    def __post_init__(self) -> None:
        self.api_url = self.api_url.rstrip("/")
        self.uploads_url = self.uploads_url.rstrip("/")
        if self.token and self.token not in self.secrets:
            self.secrets = (*self.secrets, self.token)

    # -- plumbing ---------------------------------------------------------
    def _headers(self, accept: str, content_type: str | None) -> dict[str, str]:
        headers = {
            "Accept": accept,
            "Authorization": f"Bearer {self.token}",
            "User-Agent": USER_AGENT,
            "X-GitHub-Api-Version": API_VERSION,
        }
        if content_type:
            headers["Content-Type"] = content_type
        return headers

    def scrub(self, message: str) -> str:
        return redact(message, self.secrets)

    def request(
        self,
        method: str,
        path: str,
        *,
        payload: dict[str, Any] | None = None,
        data: bytes | None = None,
        accept: str = "application/vnd.github+json",
        content_type: str | None = "application/vnd.github+json",
        base: str | None = None,
        allow_status: tuple[int, ...] = (),
    ) -> Response:
        url = path if path.startswith("http") else f"{base or self.api_url}{path}"
        body = (
            data
            if data is not None
            else (None if payload is None else json.dumps(payload).encode("utf-8"))
        )
        status, headers, raw = self.transport(
            method, url, self._headers(accept, content_type), body
        )
        decoded: Any = None
        if raw and accept.endswith("json"):
            try:
                decoded = json.loads(raw)
            except json.JSONDecodeError:
                decoded = None
        if status >= 400 and status not in allow_status:
            detail = self.scrub(raw.decode("utf-8", errors="replace"))[:400]
            raise PublicationError(
                f"GitHub {method} {self.scrub(_strip_query(url))} failed ({status}): {detail}"
            )
        return Response(status, decoded, headers, raw)

    def paginate(self, path: str) -> Iterator[dict[str, Any]]:
        separator = "&" if "?" in path else "?"
        url: str | None = f"{self.api_url}{path}{separator}per_page={PER_PAGE}"
        pages = 0
        while url:
            response = self.request("GET", url)
            pages += 1
            if pages > 100:
                raise PublicationError("refusing to paginate beyond 100 pages")
            body = response.body
            if not isinstance(body, list):
                raise PublicationError(f"expected a list from {self.scrub(_strip_query(url))}")
            for item in body:
                if isinstance(item, dict):
                    yield item
            url = response.link("next")

    # -- releases ---------------------------------------------------------
    def list_releases(self) -> list[dict[str, Any]]:
        """Every release, drafts included, across all pages."""
        return list(self.paginate(f"/repos/{self.repository}/releases"))

    def find_release(self, tag: str) -> dict[str, Any] | None:
        for release in self.list_releases():
            if release.get("tag_name") == tag:
                return release
        return None

    def get_release(self, release_id: int) -> dict[str, Any]:
        body = self.request("GET", f"/repos/{self.repository}/releases/{release_id}").body
        if not isinstance(body, dict):
            raise PublicationError(f"unexpected release payload for {release_id}")
        return body

    def create_release(
        self, tag: str, *, target: str, name: str, body: str, draft: bool = False
    ) -> dict[str, Any]:
        payload = {
            "tag_name": tag,
            "target_commitish": target,
            "name": name,
            "body": body,
            "draft": draft,
            "prerelease": False,
        }
        response = self.request("POST", f"/repos/{self.repository}/releases", payload=payload)
        if not isinstance(response.body, dict):
            raise PublicationError(f"unexpected response creating release {tag}")
        return response.body

    def update_release(self, release_id: int, **fields: Any) -> dict[str, Any]:
        response = self.request(
            "PATCH", f"/repos/{self.repository}/releases/{release_id}", payload=dict(fields)
        )
        if not isinstance(response.body, dict):
            raise PublicationError(f"unexpected response updating release {release_id}")
        return response.body

    def quarantine_release(self, release: dict[str, Any], note: str) -> dict[str, Any]:
        """Convert a published release to a draft without changing its identity.

        The release id, tag name and assets are preserved so the withdrawal
        stays auditable and reversible.
        """
        release_id = int(release["id"])
        body = str(release.get("body") or "")
        if note not in body:
            body = f"{body}\n\n{note}".strip()
        return self.update_release(release_id, draft=True, body=body)

    # -- assets -----------------------------------------------------------
    def list_assets(self, release_id: int) -> list[dict[str, Any]]:
        return list(self.paginate(f"/repos/{self.repository}/releases/{release_id}/assets"))

    def upload_asset(self, release_id: int, name: str, path: Path) -> dict[str, Any]:
        query = urllib.parse.urlencode({"name": name})
        response = self.request(
            "POST",
            f"/repos/{self.repository}/releases/{release_id}/assets?{query}",
            data=path.read_bytes(),
            content_type="application/gzip",
            base=self.uploads_url,
        )
        if not isinstance(response.body, dict):
            raise PublicationError(f"unexpected response uploading {name}")
        return response.body

    def download_asset(self, asset_id: int) -> bytes:
        return self.request(
            "GET",
            f"/repos/{self.repository}/releases/assets/{asset_id}",
            accept="application/octet-stream",
            content_type=None,
        ).raw

    def delete_asset(self, asset_id: int) -> None:
        self.request(
            "DELETE", f"/repos/{self.repository}/releases/assets/{asset_id}", content_type=None
        )

    # -- refs -------------------------------------------------------------
    def get_ref(self, ref: str) -> str | None:
        """Resolve refs/<ref> to a commit sha, or None when it does not exist."""
        encoded = urllib.parse.quote(ref.strip("/"), safe="/")
        response = self.request(
            "GET", f"/repos/{self.repository}/git/ref/{encoded}", allow_status=(404,)
        )
        if response.status == 404 or not isinstance(response.body, dict):
            return None
        obj = response.body.get("object") or {}
        if obj.get("type") == "tag":
            return self._peel_tag(str(obj["sha"]))
        return str(obj["sha"])

    def _peel_tag(self, sha: str) -> str:
        body = self.request("GET", f"/repos/{self.repository}/git/tags/{sha}").body
        if not isinstance(body, dict):
            raise PublicationError("unexpected annotated tag payload")
        return str(body["object"]["sha"])

    def require_ref(self, ref: str, expected: str | None) -> None:
        """Compare-and-swap guard: stop rather than overwrite concurrent work."""
        actual = self.get_ref(ref)
        if actual != expected:
            raise RemoteConflict(
                f"refs/{ref} is {actual or 'absent'} but {expected or 'absent'} was expected; "
                "re-run `chartpub audit` and resolve the conflict before retrying"
            )

    def delete_ref(self, ref: str, *, expected: str | None = None) -> None:
        if expected is not None:
            self.require_ref(ref, expected)
        encoded = urllib.parse.quote(ref.strip("/"), safe="/")
        self.request(
            "DELETE",
            f"/repos/{self.repository}/git/refs/{encoded}",
            content_type=None,
            allow_status=(404,),
        )

    def create_ref(self, ref: str, sha: str) -> None:
        self.request(
            "POST",
            f"/repos/{self.repository}/git/refs",
            payload={"ref": f"refs/{ref.strip('/')}", "sha": sha},
        )

    def delete_tag(self, tag: str, *, expected: str | None = None) -> None:
        self.delete_ref(f"tags/{tag}", expected=expected)


def _strip_query(url: str) -> str:
    return url.split("?", 1)[0]
