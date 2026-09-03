"""In-memory GitHub REST double. No test in this suite touches the network."""

from __future__ import annotations

import json
import re
import urllib.parse
from dataclasses import dataclass, field
from typing import Any

API = "https://api.github.com"
UPLOADS = "https://uploads.github.com"

_RELEASES = re.compile(r"^/repos/(?P<repo>[^/]+/[^/]+)/releases$")
_RELEASE = re.compile(r"^/repos/(?P<repo>[^/]+/[^/]+)/releases/(?P<id>\d+)$")
_RELEASE_ASSETS = re.compile(r"^/repos/(?P<repo>[^/]+/[^/]+)/releases/(?P<id>\d+)/assets$")
_ASSET = re.compile(r"^/repos/(?P<repo>[^/]+/[^/]+)/releases/assets/(?P<id>\d+)$")
_REF = re.compile(r"^/repos/(?P<repo>[^/]+/[^/]+)/git/ref/(?P<ref>.+)$")
_REFS = re.compile(r"^/repos/(?P<repo>[^/]+/[^/]+)/git/refs$")
_REF_MUTATE = re.compile(r"^/repos/(?P<repo>[^/]+/[^/]+)/git/refs/(?P<ref>.+)$")
_TAG_OBJECT = re.compile(r"^/repos/(?P<repo>[^/]+/[^/]+)/git/tags/(?P<sha>[0-9a-f]+)$")


@dataclass
class FakeGitHub:
    """A deterministic stand-in for the endpoints chartpub actually uses."""

    repository: str = "owner/repo"
    page_size: int = 1
    releases: list[dict[str, Any]] = field(default_factory=list)
    assets: dict[int, bytes] = field(default_factory=dict)
    refs: dict[str, str] = field(default_factory=dict)
    annotated: dict[str, str] = field(default_factory=dict)
    calls: list[tuple[str, str]] = field(default_factory=list)
    #: (method, path-substring) -> (status, body) forced failure, consumed once.
    faults: dict[tuple[str, str], tuple[int, str]] = field(default_factory=dict)
    #: asset name -> bytes actually served back on download (corruption tests).
    served: dict[str, bytes] = field(default_factory=dict)
    next_id: int = 1000

    # -- helpers ----------------------------------------------------------
    def add_release(
        self,
        tag: str,
        *,
        draft: bool = False,
        assets: dict[str, bytes] | None = None,
        body: str = "",
    ) -> dict[str, Any]:
        release_id = self._id()
        release = {
            "id": release_id,
            "tag_name": tag,
            "name": tag,
            "draft": draft,
            "prerelease": False,
            "body": body,
            "html_url": f"https://github.com/{self.repository}/releases/tag/{tag}",
            "assets": [],
        }
        self.releases.append(release)
        for name, payload in (assets or {}).items():
            self._attach(release, name, payload)
        return release

    def _id(self) -> int:
        self.next_id += 1
        return self.next_id

    def _attach(self, release: dict[str, Any], name: str, payload: bytes) -> dict[str, Any]:
        asset = {
            "id": self._id(),
            "name": name,
            "size": len(payload),
            "created_at": "2026-01-01T00:00:00Z",
            "browser_download_url": (
                f"https://github.com/{self.repository}/releases/download/"
                f"{release['tag_name']}/{name}"
            ),
        }
        release["assets"].append(asset)
        self.assets[int(asset["id"])] = payload
        return asset

    def release_by_tag(self, tag: str) -> dict[str, Any] | None:
        return next((r for r in self.releases if r["tag_name"] == tag), None)

    def _find_release(self, release_id: int) -> dict[str, Any] | None:
        return next((r for r in self.releases if r["id"] == release_id), None)

    def _find_asset(self, asset_id: int) -> tuple[dict[str, Any], dict[str, Any]] | None:
        for release in self.releases:
            for asset in release["assets"]:
                if asset["id"] == asset_id:
                    return release, asset
        return None

    # -- transport --------------------------------------------------------
    def __call__(
        self, method: str, url: str, headers: dict[str, str], body: bytes | None
    ) -> tuple[int, dict[str, str], bytes]:
        assert headers["Authorization"].startswith("Bearer ")
        parsed = urllib.parse.urlsplit(url)
        path = parsed.path
        query = urllib.parse.parse_qs(parsed.query)
        self.calls.append((method, path))
        for (fault_method, marker), response in list(self.faults.items()):
            if fault_method == method and marker in path:
                del self.faults[(fault_method, marker)]
                return response[0], {}, response[1].encode("utf-8")
        return self._dispatch(method, path, query, headers, body)

    def _dispatch(
        self,
        method: str,
        path: str,
        query: dict[str, list[str]],
        headers: dict[str, str],
        body: bytes | None,
    ) -> tuple[int, dict[str, str], bytes]:
        if (match := _RELEASES.match(path)) and method == "GET":
            return self._paginate(self.releases, path, query)
        if (match := _RELEASES.match(path)) and method == "POST":
            payload = json.loads(body or b"{}")
            release = self.add_release(
                payload["tag_name"], draft=bool(payload.get("draft")), body=payload.get("body", "")
            )
            return 201, {}, _json(release)
        if (match := _RELEASE.match(path)) and method == "GET":
            release = self._find_release(int(match.group("id")))
            return (200, {}, _json(release)) if release else _missing()
        if (match := _RELEASE.match(path)) and method == "PATCH":
            release = self._find_release(int(match.group("id")))
            if release is None:
                return _missing()
            release.update(json.loads(body or b"{}"))
            return 200, {}, _json(release)
        if (match := _RELEASE_ASSETS.match(path)) and method == "GET":
            release = self._find_release(int(match.group("id")))
            return self._paginate(list(release["assets"]) if release else [], path, query)
        if (match := _RELEASE_ASSETS.match(path)) and method == "POST":
            release = self._find_release(int(match.group("id")))
            if release is None:
                return _missing()
            name = query["name"][0]
            if any(asset["name"] == name for asset in release["assets"]):
                return 422, {}, _json({"message": "already_exists"})
            asset = self._attach(release, name, body or b"")
            return 201, {}, _json(asset)
        if (match := _ASSET.match(path)) and method == "GET":
            found = self._find_asset(int(match.group("id")))
            if found is None:
                return _missing()
            _release, asset = found
            payload = self.served.get(str(asset["name"]), self.assets[int(asset["id"])])
            return 200, {"Content-Type": "application/octet-stream"}, payload
        if (match := _ASSET.match(path)) and method == "DELETE":
            found = self._find_asset(int(match.group("id")))
            if found is None:
                return _missing()
            release, asset = found
            release["assets"].remove(asset)
            self.assets.pop(int(asset["id"]), None)
            return 204, {}, b""
        if (match := _REF.match(path)) and method == "GET":
            ref = match.group("ref")
            sha = self.refs.get(ref)
            if sha is None:
                return _missing()
            kind = "tag" if sha in self.annotated else "commit"
            return 200, {}, _json({"ref": f"refs/{ref}", "object": {"sha": sha, "type": kind}})
        if (match := _TAG_OBJECT.match(path)) and method == "GET":
            sha = match.group("sha")
            target = self.annotated.get(sha)
            if target is None:
                return _missing()
            return 200, {}, _json({"sha": sha, "object": {"sha": target, "type": "commit"}})
        if _REFS.match(path) and method == "POST":
            payload = json.loads(body or b"{}")
            ref = str(payload["ref"]).removeprefix("refs/")
            if ref in self.refs:
                return 422, {}, _json({"message": "Reference already exists"})
            self.refs[ref] = str(payload["sha"])
            return 201, {}, _json({"ref": payload["ref"], "object": {"sha": payload["sha"]}})
        if (match := _REF_MUTATE.match(path)) and method == "DELETE":
            ref = match.group("ref")
            if ref not in self.refs:
                return _missing()
            del self.refs[ref]
            return 204, {}, b""
        return 404, {}, _json({"message": f"unhandled {method} {path}"})

    def _paginate(
        self, items: list[dict[str, Any]], path: str, query: dict[str, list[str]]
    ) -> tuple[int, dict[str, str], bytes]:
        page = int(query.get("page", ["1"])[0])
        size = max(1, self.page_size)
        start = (page - 1) * size
        chunk = items[start : start + size]
        headers: dict[str, str] = {}
        if start + size < len(items):
            headers["Link"] = f'<{API}{path}?per_page={size}&page={page + 1}>; rel="next"'
        return 200, headers, _json(chunk)


def _json(value: Any) -> bytes:
    return json.dumps(value).encode("utf-8")


def _missing() -> tuple[int, dict[str, str], bytes]:
    return 404, {}, _json({"message": "Not Found"})
