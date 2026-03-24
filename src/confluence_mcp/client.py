"""Async Confluence REST API client (v1 + v2) using httpx."""

from __future__ import annotations

import logging
from base64 import b64encode
from typing import Any

import httpx

logger = logging.getLogger("confluence-mcp.client")

DEFAULT_TIMEOUT = 30.0
MAX_PAGE_LIMIT = 100


class ConfluenceClient:
    """Thin async wrapper around Confluence Cloud REST API."""

    def __init__(
        self,
        base_url: str,
        username: str,
        api_token: str,
        *,
        timeout: float = DEFAULT_TIMEOUT,
    ) -> None:
        base_url = base_url.rstrip("/")
        # All API paths include /wiki prefix, so strip it from base_url if present
        if base_url.endswith("/wiki"):
            base_url = base_url[:-5]
        self.base_url = base_url
        creds = b64encode(f"{username}:{api_token}".encode()).decode()
        self._headers = {
            "Authorization": f"Basic {creds}",
            "Accept": "application/json",
        }
        self._client = httpx.AsyncClient(
            headers=self._headers,
            timeout=timeout,
            follow_redirects=True,
        )

    async def close(self) -> None:
        await self._client.aclose()

    # ------------------------------------------------------------------
    # Low-level helpers
    # ------------------------------------------------------------------

    async def _get(self, path: str, **params: Any) -> dict:
        url = f"{self.base_url}{path}"
        resp = await self._client.get(url, params={k: v for k, v in params.items() if v is not None})
        resp.raise_for_status()
        return resp.json()

    async def _put(self, path: str, json_body: dict) -> dict:
        url = f"{self.base_url}{path}"
        resp = await self._client.put(url, json=json_body)
        resp.raise_for_status()
        return resp.json()

    async def _post(self, path: str, json_body: dict | None = None, **kwargs: Any) -> dict:
        url = f"{self.base_url}{path}"
        resp = await self._client.post(url, json=json_body, **kwargs)
        resp.raise_for_status()
        return resp.json()

    async def _post_multipart(self, path: str, files: dict, data: dict | None = None) -> dict:
        url = f"{self.base_url}{path}"
        headers = {**self._headers, "X-Atlassian-Token": "nocheck"}
        headers.pop("Accept", None)
        resp = await self._client.post(url, files=files, data=data or {}, headers=headers)
        resp.raise_for_status()
        return resp.json()

    async def _delete(self, path: str) -> None:
        url = f"{self.base_url}{path}"
        resp = await self._client.delete(url)
        resp.raise_for_status()

    async def _get_binary(self, url: str) -> bytes:
        resp = await self._client.get(url)
        resp.raise_for_status()
        return resp.content

    # ------------------------------------------------------------------
    # Pages — v2 API
    # ------------------------------------------------------------------

    async def get_page(
        self,
        page_id: str,
        *,
        body_format: str = "storage",
    ) -> dict:
        """Get page by ID. body_format: storage | atlas_doc_format | view."""
        return await self._get(
            f"/wiki/api/v2/pages/{page_id}",
            **{"body-format": body_format},
        )

    async def get_page_children(
        self,
        page_id: str,
        *,
        limit: int = 50,
        cursor: str | None = None,
    ) -> dict:
        """List direct child pages."""
        params: dict[str, Any] = {"limit": limit}
        if cursor:
            params["cursor"] = cursor
        return await self._get(f"/wiki/api/v2/pages/{page_id}/children", **params)

    async def get_page_tree(self, page_id: str) -> list[dict]:
        """Recursively get all descendants. Returns flat list with depth info."""
        result: list[dict] = []
        await self._collect_children(page_id, depth=0, result=result)
        return result

    async def _collect_children(self, page_id: str, depth: int, result: list[dict]) -> None:
        cursor = None
        while True:
            data = await self.get_page_children(page_id, limit=MAX_PAGE_LIMIT, cursor=cursor)
            children = data.get("results", [])
            for child in children:
                child["_depth"] = depth + 1
                result.append(child)
                await self._collect_children(child["id"], depth + 1, result)
            next_link = data.get("_links", {}).get("next")
            if not next_link:
                break
            # Extract cursor from next link
            import re
            m = re.search(r"cursor=([^&]+)", next_link)
            cursor = m.group(1) if m else None
            if not cursor:
                break

    async def update_page(
        self,
        page_id: str,
        *,
        title: str,
        body: str,
        version_number: int,
        body_format: str = "storage",
        status: str = "current",
        version_message: str | None = None,
    ) -> dict:
        """Full update via PUT. Caller must provide incremented version_number."""
        payload: dict[str, Any] = {
            "id": page_id,
            "status": status,
            "title": title,
            "body": {
                "representation": body_format,
                "value": body,
            },
            "version": {
                "number": version_number,
            },
        }
        if version_message:
            payload["version"]["message"] = version_message
        return await self._put(f"/wiki/api/v2/pages/{page_id}", payload)

    async def create_page(
        self,
        space_id: str,
        *,
        title: str,
        body: str,
        parent_id: str | None = None,
        body_format: str = "storage",
    ) -> dict:
        payload: dict[str, Any] = {
            "spaceId": space_id,
            "status": "current",
            "title": title,
            "body": {
                "representation": body_format,
                "value": body,
            },
        }
        if parent_id:
            payload["parentId"] = parent_id
        return await self._post("/wiki/api/v2/pages", payload)

    # ------------------------------------------------------------------
    # Search
    # ------------------------------------------------------------------

    async def search(self, cql: str, *, limit: int = 25) -> dict:
        """CQL search."""
        return await self._get("/wiki/rest/api/content/search", cql=cql, limit=limit)

    # ------------------------------------------------------------------
    # Attachments — v1 API (more reliable for upload)
    # ------------------------------------------------------------------

    async def get_attachments(self, page_id: str, *, limit: int = 100) -> dict:
        """List attachments on a page."""
        return await self._get(
            f"/wiki/rest/api/content/{page_id}/child/attachment",
            limit=limit,
        )

    async def upload_attachment(
        self,
        page_id: str,
        filename: str,
        content: bytes,
        content_type: str = "application/octet-stream",
        comment: str | None = None,
    ) -> dict:
        """Upload or update an attachment on a page."""
        files = {"file": (filename, content, content_type)}
        data = {}
        if comment:
            data["comment"] = comment
        return await self._post_multipart(
            f"/wiki/rest/api/content/{page_id}/child/attachment",
            files=files,
            data=data,
        )

    async def download_attachment(self, download_path: str) -> bytes:
        """Download attachment binary by its download path."""
        url = download_path if download_path.startswith("http") else f"{self.base_url}{download_path}"
        return await self._get_binary(url)

    async def delete_attachment(self, attachment_id: str) -> None:
        await self._delete(f"/wiki/rest/api/content/{attachment_id}")

    # ------------------------------------------------------------------
    # Spaces
    # ------------------------------------------------------------------

    async def get_spaces(self, *, limit: int = 50) -> dict:
        return await self._get("/wiki/api/v2/spaces", limit=limit)

    async def get_space(self, space_id: str) -> dict:
        return await self._get(f"/wiki/api/v2/spaces/{space_id}")

    # ------------------------------------------------------------------
    # Labels
    # ------------------------------------------------------------------

    async def get_labels(self, page_id: str) -> dict:
        return await self._get(f"/wiki/rest/api/content/{page_id}/label")

    async def add_label(self, page_id: str, label: str) -> dict:
        return await self._post(
            f"/wiki/rest/api/content/{page_id}/label",
            [{"prefix": "global", "name": label}],
        )
