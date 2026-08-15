"""Mattermost REST client -- the only network code in the bridge.

Three methods, behind a Protocol so the daemon runs against a fake in tests.
Deliberately no ``update_post`` (nothing the bridge writes is ever edited) and no
``upload_file`` (chunking covers long output) -- see D10.
"""

from pathlib import Path
from typing import Protocol

import httpx

from cld.log import get_logger

log = get_logger(__name__)


class MattermostClient(Protocol):
    def posts_since(self, channel_id: str, since_ms: int) -> list[dict]: ...
    def create_post(self, channel_id: str, message: str, root_id: str = "") -> dict: ...
    def whoami(self) -> dict: ...


class TokenPermissionError(RuntimeError):
    pass


def read_token(path: Path) -> str:
    """Read the PAT, refusing a file anyone but the owner can read."""
    path = path.expanduser()
    if not path.is_file():
        raise FileNotFoundError(f"mattermost_token_file not found: {path}")
    if path.stat().st_mode & 0o077:
        raise TokenPermissionError(f"{path} is group- or world-readable; run `chmod 600 {path}`")
    return path.read_text().strip()


class HttpMattermostClient:
    """Thin wrapper over ``/api/v4``. Errors surface as ``httpx.HTTPStatusError``."""

    def __init__(self, base_url: str, token: str, timeout: float = 30.0):
        self._client = httpx.Client(
            base_url=base_url.rstrip("/") + "/api/v4",
            headers={"Authorization": f"Bearer {token}"},
            timeout=timeout,
        )

    def close(self) -> None:
        self._client.close()

    def whoami(self) -> dict:
        r = self._client.get("/users/me")
        r.raise_for_status()
        return r.json()

    def posts_since(self, channel_id: str, since_ms: int) -> list[dict]:
        """Posts created or modified after *since_ms*, oldest first.

        ``since`` also returns *modified* posts, which is why the edit filter in
        ``routing.rejection_reason`` is load-bearing rather than paranoid.
        """
        params = {"since": since_ms} if since_ms else {"per_page": 20}
        r = self._client.get(f"/channels/{channel_id}/posts", params=params)
        r.raise_for_status()
        posts = (r.json().get("posts") or {}).values()
        return sorted(posts, key=lambda p: p.get("create_at", 0))

    def create_post(self, channel_id: str, message: str, root_id: str = "") -> dict:
        payload = {"channel_id": channel_id, "message": message}
        if root_id:
            payload["root_id"] = root_id
        r = self._client.post("/posts", json=payload)
        r.raise_for_status()
        return r.json()
