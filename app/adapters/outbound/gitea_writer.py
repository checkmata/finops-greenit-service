"""
GitOpsWriterPort on Gitea's REST API (V4) — the agent's ONLY write path.

    read   GET  /repos/{owner}/{repo}/contents/{path}?ref=main
    commit POST /repos/{owner}/{repo}/contents   ("change files": one atomic commit,
           several files; update/delete require the SHA read before → a file modified
           by someone else in between makes the commit fail instead of overwriting it)

Identity: the `finops-agent` Gitea user (write collaborator on this repository only,
token scope write:repository), token from a mounted secret file, never logged.
"""

from __future__ import annotations

import base64
from pathlib import Path
from typing import Sequence

import httpx

from app.ports.agent_ports import FileChange, GitOpsWriterPort


class GiteaWriter(GitOpsWriterPort):
    def __init__(self, base_url: str, repo: str, token_file: str, branch: str = "main",
                 author: tuple[str, str] = ("finops-agent", "agent@localhost"), timeout: float = 20) -> None:
        self._repo, self._branch, self._author = repo, branch, author
        self._token_file = Path(token_file)
        self._client = httpx.AsyncClient(base_url=base_url.rstrip("/") + "/api/v1", timeout=timeout)
        self._shas: dict[str, str] = {}

    def __repr__(self) -> str:
        return f"GiteaWriter({self._repo}@{self._branch})"

    def _headers(self) -> dict:
        return {"Authorization": f"token {self._token_file.read_text().strip()}"}

    async def read(self, path: str) -> str | None:
        r = await self._client.get(f"/repos/{self._repo}/contents/{path}", params={"ref": self._branch},
                                   headers=self._headers())
        if r.status_code == 404:
            self._shas.pop(path, None)
            return None
        r.raise_for_status()
        body = r.json()
        self._shas[path] = body["sha"]
        return base64.b64decode(body["content"]).decode()

    async def commit(self, changes: Sequence[FileChange], message: str) -> str:
        files = []
        for ch in changes:
            if ch.new_content is None:
                files.append({"operation": "delete", "path": ch.path, "sha": self._shas[ch.path]})
            elif ch.old_content is None:
                files.append({"operation": "create", "path": ch.path,
                              "content": base64.b64encode(ch.new_content.encode()).decode()})
            else:
                files.append({"operation": "update", "path": ch.path, "sha": self._shas[ch.path],
                              "content": base64.b64encode(ch.new_content.encode()).decode()})
        name, email = self._author
        r = await self._client.post(f"/repos/{self._repo}/contents", headers=self._headers(), json={
            "branch": self._branch, "message": message, "files": files,
            "author": {"name": name, "email": email}, "committer": {"name": name, "email": email}})
        r.raise_for_status()
        return r.json()["commit"]["sha"]

    async def close(self) -> None:
        await self._client.aclose()
