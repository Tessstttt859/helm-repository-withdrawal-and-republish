from __future__ import annotations

import json
import urllib.error
import urllib.parse
import urllib.request
from dataclasses import dataclass
from typing import Any

from chartpub.errors import PublicationError


@dataclass(frozen=True)
class Response:
    status: int
    body: Any
    headers: dict[str, str]


class GitHubClient:
    def __init__(
        self, repository: str, token: str, api_url: str = "https://api.github.com"
    ) -> None:
        self.repository = repository
        self.token = token
        self.api_url = api_url.rstrip("/")

    def request(
        self,
        method: str,
        path: str,
        *,
        payload: dict[str, Any] | None = None,
        content_type: str = "application/vnd.github+json",
    ) -> Response:
        data = None if payload is None else json.dumps(payload).encode("utf-8")
        request = urllib.request.Request(
            f"{self.api_url}{path}",
            data=data,
            method=method,
            headers={
                "Accept": "application/vnd.github+json",
                "Authorization": f"Bearer {self.token}",
                "Content-Type": content_type,
                "X-GitHub-Api-Version": "2022-11-28",
            },
        )
        try:
            with urllib.request.urlopen(request, timeout=30) as response:
                body = response.read()
                decoded = json.loads(body) if body else None
                return Response(response.status, decoded, dict(response.headers.items()))
        except urllib.error.HTTPError as exc:
            body = exc.read().decode("utf-8", errors="replace")
            raise PublicationError(f"GitHub {method} {path} failed ({exc.code}): {body}") from exc

    def list_releases(self) -> list[dict[str, Any]]:
        response = self.request("GET", f"/repos/{self.repository}/releases?per_page=2")
        return list(response.body)

    def get_ref(self, ref: str) -> str | None:
        encoded = urllib.parse.quote(ref, safe="/")
        try:
            response = self.request("GET", f"/repos/{self.repository}/git/ref/{encoded}")
        except PublicationError:
            return None
        return str(response.body["object"]["sha"])

    def delete_tag(self, tag: str) -> None:
        encoded = urllib.parse.quote(f"tags/{tag}", safe="/")
        self.request("DELETE", f"/repos/{self.repository}/git/refs/{encoded}")
