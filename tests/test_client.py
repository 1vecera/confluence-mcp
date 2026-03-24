"""Tests for the Confluence API client."""

import json

import httpx
import pytest
import respx

from confluence_mcp.client import ConfluenceClient

BASE_URL = "https://test.atlassian.net"


@pytest.fixture
def client():
    return ConfluenceClient(BASE_URL, "user@test.com", "test-token")


@pytest.fixture
def mock_api():
    with respx.mock(base_url=BASE_URL) as api:
        yield api


class TestGetPage:
    @pytest.mark.asyncio
    async def test_get_page(self, client, mock_api):
        mock_api.get("/wiki/api/v2/pages/123").mock(
            return_value=httpx.Response(200, json={
                "id": "123",
                "title": "Test Page",
                "status": "current",
                "spaceId": "456",
                "version": {"number": 5, "createdAt": "2026-01-01"},
                "body": {"storage": {"value": "<p>Hello</p>"}},
            })
        )
        page = await client.get_page("123", body_format="storage")
        assert page["id"] == "123"
        assert page["title"] == "Test Page"
        assert page["body"]["storage"]["value"] == "<p>Hello</p>"

    @pytest.mark.asyncio
    async def test_get_page_not_found(self, client, mock_api):
        mock_api.get("/wiki/api/v2/pages/999").mock(
            return_value=httpx.Response(404, json={"error": "not found"})
        )
        with pytest.raises(httpx.HTTPStatusError):
            await client.get_page("999")


class TestUpdatePage:
    @pytest.mark.asyncio
    async def test_update_page(self, client, mock_api):
        mock_api.put("/wiki/api/v2/pages/123").mock(
            return_value=httpx.Response(200, json={
                "id": "123",
                "title": "Updated",
                "version": {"number": 6},
            })
        )
        result = await client.update_page(
            "123", title="Updated", body="<p>New</p>", version_number=6
        )
        assert result["title"] == "Updated"
        assert result["version"]["number"] == 6


class TestGetPageTree:
    @pytest.mark.asyncio
    async def test_tree_no_children(self, client, mock_api):
        mock_api.get("/wiki/api/v2/pages/100/children").mock(
            return_value=httpx.Response(200, json={"results": [], "_links": {}})
        )
        tree = await client.get_page_tree("100")
        assert tree == []

    @pytest.mark.asyncio
    async def test_tree_with_children(self, client, mock_api):
        mock_api.get("/wiki/api/v2/pages/100/children").mock(
            return_value=httpx.Response(200, json={
                "results": [
                    {"id": "101", "title": "Child 1"},
                    {"id": "102", "title": "Child 2"},
                ],
                "_links": {},
            })
        )
        mock_api.get("/wiki/api/v2/pages/101/children").mock(
            return_value=httpx.Response(200, json={"results": [], "_links": {}})
        )
        mock_api.get("/wiki/api/v2/pages/102/children").mock(
            return_value=httpx.Response(200, json={"results": [], "_links": {}})
        )
        tree = await client.get_page_tree("100")
        assert len(tree) == 2
        assert tree[0]["title"] == "Child 1"
        assert tree[0]["_depth"] == 1


class TestAttachments:
    @pytest.mark.asyncio
    async def test_get_attachments(self, client, mock_api):
        mock_api.get("/wiki/rest/api/content/123/child/attachment").mock(
            return_value=httpx.Response(200, json={
                "results": [
                    {"id": "att1", "title": "file.png", "extensions": {"mediaType": "image/png"}},
                ]
            })
        )
        data = await client.get_attachments("123")
        assert len(data["results"]) == 1
        assert data["results"][0]["title"] == "file.png"

    @pytest.mark.asyncio
    async def test_search(self, client, mock_api):
        mock_api.get("/wiki/rest/api/content/search").mock(
            return_value=httpx.Response(200, json={
                "results": [{"content": {"id": "1", "title": "Found"}}]
            })
        )
        data = await client.search('title="Test"')
        assert len(data["results"]) == 1
