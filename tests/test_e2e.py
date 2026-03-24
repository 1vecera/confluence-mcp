#!/usr/bin/env python3
# /// script
# requires-python = ">=3.10"
# dependencies = ["httpx", "beautifulsoup4", "markdownify", "lxml"]
# ///
"""End-to-end tests against a real Confluence instance.

Creates a temporary test page, exercises all operations, then cleans up.
Run with: uv run tests/test_e2e.py

Requires env vars: CONFLUENCE_URL, CONFLUENCE_USERNAME, CONFLUENCE_API_TOKEN
Also needs: E2E_SPACE_ID (numeric space ID to create test pages in)
"""

from __future__ import annotations

import asyncio
import json
import os
import sys
import tempfile
import time
from pathlib import Path

# Add src to path so we can import directly
sys.path.insert(0, str(Path(__file__).parent.parent / "src"))

from confluence_mcp.client import ConfluenceClient
from confluence_mcp.content import (
    get_sections,
    get_section_content,
    replace_section,
    append_to_section,
    find_and_replace,
    markdown_to_storage,
    storage_to_markdown,
    extract_images,
)


class E2ETestRunner:
    """Runs end-to-end tests against real Confluence."""

    def __init__(self):
        self.url = os.environ["CONFLUENCE_URL"]
        self.username = os.environ["CONFLUENCE_USERNAME"]
        self.token = os.environ["CONFLUENCE_API_TOKEN"]
        self.space_id = os.environ.get("E2E_SPACE_ID", "")
        self.client = ConfluenceClient(self.url, self.username, self.token)
        self.created_page_ids: list[str] = []
        self.passed = 0
        self.failed = 0
        self.errors: list[str] = []

    def _check(self, name: str, condition: bool, detail: str = ""):
        if condition:
            self.passed += 1
            print(f"  ✅ {name}")
        else:
            self.failed += 1
            msg = f"  ❌ {name}" + (f" — {detail}" if detail else "")
            print(msg)
            self.errors.append(msg)

    async def run_all(self):
        print(f"\n{'='*60}")
        print(f"E2E Tests — {self.url}")
        print(f"{'='*60}\n")

        try:
            # Find a space to work in
            if not self.space_id:
                await self._find_space()

            # Create test pages
            parent = await self._test_create_page()
            child = await self._test_create_child_page(parent["id"])

            # Read operations
            await self._test_get_page(parent["id"])
            await self._test_get_page_to_file(parent["id"])
            await self._test_get_page_tree(parent["id"])
            await self._test_get_page_tree_to_dir(parent["id"])
            await self._test_get_page_sections(parent["id"])
            await self._test_get_section(parent["id"])
            await self._test_get_section_to_file(parent["id"])
            await self._test_search()

            # Write operations
            await self._test_update_page(parent["id"])
            await self._test_update_page_from_file(parent["id"])
            await self._test_update_section(parent["id"])
            await self._test_update_section_from_file(parent["id"])
            await self._test_append_to_section(parent["id"])
            await self._test_find_replace(parent["id"])

            # Attachment operations
            await self._test_upload_attachment(parent["id"])
            await self._test_upload_attachment_from_file(parent["id"])
            await self._test_list_attachments(parent["id"])
            await self._test_download_attachment(parent["id"])
            await self._test_download_attachment_to_file(parent["id"])

            # Image operations
            await self._test_list_page_images(parent["id"])

            # Label operations
            await self._test_add_label(parent["id"])
            await self._test_get_labels(parent["id"])

        except Exception as e:
            print(f"\n💥 FATAL: {e}")
            self.failed += 1
            import traceback
            traceback.print_exc()
        finally:
            await self._cleanup()
            await self.client.close()

        print(f"\n{'='*60}")
        print(f"Results: {self.passed} passed, {self.failed} failed")
        if self.errors:
            print("\nFailed tests:")
            for e in self.errors:
                print(e)
        print(f"{'='*60}\n")
        return self.failed == 0

    async def _find_space(self):
        """Find a space to create test pages in."""
        print("Finding a space...")
        data = await self.client.get_spaces(limit=5)
        spaces = data.get("results", [])
        if not spaces:
            raise RuntimeError("No spaces found! Set E2E_SPACE_ID manually.")
        # Use the first space
        self.space_id = spaces[0]["id"]
        print(f"  Using space: {spaces[0].get('name', '')} (id={self.space_id})\n")

    async def _test_create_page(self) -> dict:
        print("📝 Create test page")
        ts = int(time.time())
        body = markdown_to_storage(
            "## Introduction\n\nThis is a test page created by confluence-mcp E2E tests.\n\n"
            "## Details\n\nSome details here.\n\n- Item 1\n- Item 2\n\n"
            "## Status\n\nAll good.\n\n"
            "## Cleanup Note\n\nThis page should be automatically deleted after testing."
        )
        page = await self.client.create_page(
            self.space_id,
            title=f"[E2E-TEST] confluence-mcp {ts}",
            body=body,
            body_format="storage",
        )
        self.created_page_ids.append(page["id"])
        self._check("create_page", "id" in page, f"id={page.get('id')}")
        return page

    async def _test_create_child_page(self, parent_id: str) -> dict:
        print("📝 Create child page")
        ts = int(time.time())
        body = markdown_to_storage("Child page content for tree testing.")
        page = await self.client.create_page(
            self.space_id,
            title=f"[E2E-TEST] Child {ts}",
            body=body,
            parent_id=parent_id,
            body_format="storage",
        )
        self.created_page_ids.append(page["id"])
        self._check("create_child_page", "id" in page)
        return page

    async def _test_get_page(self, page_id: str):
        print("📖 Get page (in context)")
        page = await self.client.get_page(page_id, body_format="storage")
        self._check("get_page returns id", page.get("id") == page_id)
        self._check("get_page has title", bool(page.get("title")))
        self._check("get_page has body", bool(page.get("body", {}).get("storage", {}).get("value")))
        self._check("get_page has version", isinstance(page.get("version", {}).get("number"), int))

    async def _test_get_page_to_file(self, page_id: str):
        print("📖 Get page (to file)")
        with tempfile.NamedTemporaryFile(suffix=".md", delete=False, mode="w") as f:
            tmp = f.name
        try:
            page = await self.client.get_page(page_id, body_format="storage")
            body = page.get("body", {}).get("storage", {}).get("value", "")
            md = storage_to_markdown(body)
            Path(tmp).write_text(md, encoding="utf-8")
            content = Path(tmp).read_text(encoding="utf-8")
            self._check("get_page_to_file writes content", len(content) > 10)
            self._check("get_page_to_file has markdown", "#" in content or "test" in content.lower())
        finally:
            Path(tmp).unlink(missing_ok=True)

    async def _test_get_page_tree(self, page_id: str):
        print("🌳 Get page tree (in context)")
        children = await self.client.get_page_tree(page_id)
        self._check("get_page_tree returns list", isinstance(children, list))
        self._check("get_page_tree has child", len(children) >= 1)
        if children:
            self._check("child has _depth", children[0].get("_depth") == 1)

    async def _test_get_page_tree_to_dir(self, page_id: str):
        print("🌳 Get page tree (to directory)")
        with tempfile.TemporaryDirectory() as tmpdir:
            root = await self.client.get_page(page_id, body_format="storage")
            children = await self.client.get_page_tree(page_id)
            all_pages = [root] + children

            for i, pg in enumerate(all_pages):
                pid = pg["id"]
                full = await self.client.get_page(pid, body_format="storage")
                body = full.get("body", {}).get("storage", {}).get("value", "")
                md = storage_to_markdown(body)
                fname = f"{i:03d}_{pg.get('title', 'untitled')}.md"
                fname = fname.replace("/", "_").replace("\\", "_")
                (Path(tmpdir) / fname).write_text(md, encoding="utf-8")

            files = list(Path(tmpdir).glob("*.md"))
            self._check("tree_to_dir creates files", len(files) >= 2)
            self._check("tree_to_dir files have content", all(f.stat().st_size > 0 for f in files))

    async def _test_get_page_sections(self, page_id: str):
        print("📋 Get page sections")
        page = await self.client.get_page(page_id, body_format="storage")
        body = page.get("body", {}).get("storage", {}).get("value", "")
        sections = get_sections(body)
        headings = [s["heading"] for s in sections if s["heading"]]
        self._check("sections found", len(headings) >= 3)
        self._check("Introduction section exists", "Introduction" in headings)
        self._check("Details section exists", "Details" in headings)

    async def _test_get_section(self, page_id: str):
        print("📋 Get specific section")
        page = await self.client.get_page(page_id, body_format="storage")
        body = page.get("body", {}).get("storage", {}).get("value", "")
        content = get_section_content(body, "Details")
        self._check("get_section returns content", content is not None)
        self._check("section has expected text", "details" in (content or "").lower() or "item" in (content or "").lower())

    async def _test_get_section_to_file(self, page_id: str):
        print("📋 Get section (to file)")
        with tempfile.NamedTemporaryFile(suffix=".md", delete=False, mode="w") as f:
            tmp = f.name
        try:
            page = await self.client.get_page(page_id, body_format="storage")
            body = page.get("body", {}).get("storage", {}).get("value", "")
            content = get_section_content(body, "Details")
            if content:
                md = storage_to_markdown(content)
                Path(tmp).write_text(md, encoding="utf-8")
                self._check("section_to_file writes content", Path(tmp).stat().st_size > 0)
            else:
                self._check("section_to_file writes content", False, "section not found")
        finally:
            Path(tmp).unlink(missing_ok=True)

    async def _latest_version(self, page_id: str) -> int:
        """Always fetch the latest version number to avoid 409 Conflicts."""
        page = await self.client.get_page(page_id, body_format="storage")
        return page.get("version", {}).get("number", 1)

    async def _test_search(self):
        print("🔍 Search pages")
        # Search for something that definitely exists (any page in the space)
        data = await self.client.search('type=page', limit=3)
        results = data.get("results", [])
        self._check("search returns results", len(results) >= 1)

    async def _test_update_page(self, page_id: str):
        print("✏️  Update page (inline content)")
        page = await self.client.get_page(page_id, body_format="storage")
        ver = await self._latest_version(page_id)
        new_body = markdown_to_storage(
            "## Introduction\n\nUpdated intro.\n\n## Details\n\nUpdated details.\n\n"
            "- Updated item 1\n- Updated item 2\n\n## Status\n\nStill good.\n\n"
            "## Cleanup Note\n\nThis page should be automatically deleted after testing."
        )
        updated = await self.client.update_page(
            page_id, title=page.get("title", ""), body=new_body,
            version_number=ver + 1, body_format="storage",
            version_message="E2E test: update_page")
        self._check("update_page increments version", updated.get("version", {}).get("number") == ver + 1)

    async def _test_update_page_from_file(self, page_id: str):
        print("✏️  Update page (from file)")
        with tempfile.NamedTemporaryFile(suffix=".md", delete=False, mode="w") as f:
            f.write("## Introduction\n\nUpdated from file.\n\n## Details\n\nFile-based update.\n\n"
                    "- File item 1\n- File item 2\n\n## Status\n\nFile update done.\n\n"
                    "## Cleanup Note\n\nThis page should be automatically deleted after testing.")
            tmp = f.name
        try:
            md_content = Path(tmp).read_text(encoding="utf-8")
            ver = await self._latest_version(page_id)
            page = await self.client.get_page(page_id, body_format="storage")
            new_body = markdown_to_storage(md_content)
            updated = await self.client.update_page(
                page_id, title=page.get("title", ""), body=new_body,
                version_number=ver + 1, body_format="storage",
                version_message="E2E test: update_page from file")
            self._check("update_from_file works", updated.get("version", {}).get("number") == ver + 1)

            # Verify the content was written
            verify = await self.client.get_page(page_id, body_format="storage")
            body = verify.get("body", {}).get("storage", {}).get("value", "")
            self._check("file content is in page", "File-based update" in storage_to_markdown(body))
        finally:
            Path(tmp).unlink(missing_ok=True)

    async def _test_update_section(self, page_id: str):
        print("✏️  Update section (surgical)")
        ver = await self._latest_version(page_id)
        page = await self.client.get_page(page_id, body_format="storage")
        body = page.get("body", {}).get("storage", {}).get("value", "")

        new_body = replace_section(body, "Status", "<p>Section surgically updated!</p>")
        updated = await self.client.update_page(
            page_id, title=page.get("title", ""), body=new_body,
            version_number=ver + 1, body_format="storage",
            version_message="E2E test: update_section")

        # Verify only Status changed
        verify = await self.client.get_page(page_id, body_format="storage")
        verify_body = verify.get("body", {}).get("storage", {}).get("value", "")
        status_content = get_section_content(verify_body, "Status")
        details_content = get_section_content(verify_body, "Details")
        self._check("update_section changes target", "surgically updated" in (status_content or "").lower())
        self._check("update_section preserves other sections", details_content is not None)

    async def _test_update_section_from_file(self, page_id: str):
        print("✏️  Update section (from file)")
        with tempfile.NamedTemporaryFile(suffix=".md", delete=False, mode="w") as f:
            f.write("Section content loaded from a local file.\n\n- Works great\n- No LLM context waste")
            tmp = f.name
        try:
            md_content = Path(tmp).read_text(encoding="utf-8")
            ver = await self._latest_version(page_id)
            page = await self.client.get_page(page_id, body_format="storage")
            body = page.get("body", {}).get("storage", {}).get("value", "")

            new_body = replace_section(body, "Status", md_content, content_format="markdown")
            updated = await self.client.update_page(
                page_id, title=page.get("title", ""), body=new_body,
                version_number=ver + 1, body_format="storage",
                version_message="E2E test: update_section from file")

            verify = await self.client.get_page(page_id, body_format="storage")
            verify_body = verify.get("body", {}).get("storage", {}).get("value", "")
            self._check("section_from_file works", "loaded from a local file" in storage_to_markdown(verify_body))
        finally:
            Path(tmp).unlink(missing_ok=True)

    async def _test_append_to_section(self, page_id: str):
        print("➕ Append to section")
        ver = await self._latest_version(page_id)
        page = await self.client.get_page(page_id, body_format="storage")
        body = page.get("body", {}).get("storage", {}).get("value", "")

        new_body = append_to_section(body, "Details", "<p>Appended E2E test item.</p>")
        updated = await self.client.update_page(
            page_id, title=page.get("title", ""), body=new_body,
            version_number=ver + 1, body_format="storage",
            version_message="E2E test: append_to_section")

        verify = await self.client.get_page(page_id, body_format="storage")
        verify_body = verify.get("body", {}).get("storage", {}).get("value", "")
        details = get_section_content(verify_body, "Details")
        self._check("append adds content", "Appended E2E test item" in (details or ""))

    async def _test_find_replace(self, page_id: str):
        print("🔄 Find and replace")
        ver = await self._latest_version(page_id)
        page = await self.client.get_page(page_id, body_format="storage")
        body = page.get("body", {}).get("storage", {}).get("value", "")

        new_body = find_and_replace(body, "Appended E2E test item", "REPLACED E2E item")
        updated = await self.client.update_page(
            page_id, title=page.get("title", ""), body=new_body,
            version_number=ver + 1, body_format="storage",
            version_message="E2E test: find_replace")

        verify = await self.client.get_page(page_id, body_format="storage")
        verify_body = verify.get("body", {}).get("storage", {}).get("value", "")
        self._check("find_replace works", "REPLACED E2E item" in verify_body)

    async def _test_upload_attachment(self, page_id: str):
        print("📎 Upload attachment (binary)")
        content = b"Hello from E2E test! This is a test attachment."
        result = await self.client.upload_attachment(
            page_id, "e2e-test.txt", content, content_type="text/plain")
        results = result.get("results", [result])
        self._check("upload_attachment succeeds", len(results) > 0)

    async def _test_upload_attachment_from_file(self, page_id: str):
        print("📎 Upload attachment (from file)")
        with tempfile.NamedTemporaryFile(suffix=".txt", delete=False, mode="w") as f:
            f.write("File-based upload test content.")
            tmp = f.name
        try:
            content = Path(tmp).read_bytes()
            result = await self.client.upload_attachment(
                page_id, "e2e-file-upload.txt", content, content_type="text/plain")
            results = result.get("results", [result])
            self._check("upload_from_file succeeds", len(results) > 0)
        finally:
            Path(tmp).unlink(missing_ok=True)

    async def _test_list_attachments(self, page_id: str):
        print("📎 List attachments")
        data = await self.client.get_attachments(page_id)
        attachments = data.get("results", [])
        self._check("list_attachments returns results", len(attachments) >= 2)
        titles = [a.get("title", "") for a in attachments]
        self._check("e2e-test.txt in attachments", "e2e-test.txt" in titles)

    async def _test_download_attachment(self, page_id: str):
        print("📥 Download attachment (in context)")
        data = await self.client.get_attachments(page_id)
        target = next((a for a in data.get("results", []) if a.get("title") == "e2e-test.txt"), None)
        if not target:
            self._check("download_attachment", False, "attachment not found")
            return
        dl = target.get("_links", {}).get("download", "")
        if dl.startswith("/download/"):
            dl = f"/wiki{dl}"
        content = await self.client.download_attachment(dl)
        self._check("download_attachment gets content", len(content) > 0)
        self._check("download content matches", b"Hello from E2E test" in content)

    async def _test_download_attachment_to_file(self, page_id: str):
        print("📥 Download attachment (to file)")
        with tempfile.NamedTemporaryFile(suffix=".txt", delete=False) as f:
            tmp = f.name
        try:
            data = await self.client.get_attachments(page_id)
            target = next((a for a in data.get("results", []) if a.get("title") == "e2e-test.txt"), None)
            if not target:
                self._check("download_to_file", False, "attachment not found")
                return
            dl = target.get("_links", {}).get("download", "")
            if dl.startswith("/download/"):
                dl = f"/wiki{dl}"
            content = await self.client.download_attachment(dl)
            Path(tmp).write_bytes(content)
            self._check("download_to_file saves content", Path(tmp).stat().st_size > 0)
        finally:
            Path(tmp).unlink(missing_ok=True)

    async def _test_list_page_images(self, page_id: str):
        print("🖼️  List page images")
        page = await self.client.get_page(page_id, body_format="storage")
        body = page.get("body", {}).get("storage", {}).get("value", "")
        images = extract_images(body)
        self._check("list_page_images runs", isinstance(images, list))

    async def _test_add_label(self, page_id: str):
        print("🏷️  Add label")
        try:
            await self.client.add_label(page_id, "e2e-test-label")
            self._check("add_label succeeds", True)
        except Exception as e:
            self._check("add_label succeeds", False, str(e))

    async def _test_get_labels(self, page_id: str):
        print("🏷️  Get labels")
        data = await self.client.get_labels(page_id)
        labels = [l.get("name", "") for l in data.get("results", [])]
        self._check("get_labels returns results", "e2e-test-label" in labels)

    async def _cleanup(self):
        print("\n🧹 Cleaning up test pages...")
        for page_id in reversed(self.created_page_ids):
            try:
                await self.client._delete(f"/wiki/rest/api/content/{page_id}")
                print(f"  Deleted page {page_id}")
            except Exception as e:
                print(f"  ⚠️  Failed to delete page {page_id}: {e}")


async def main():
    runner = E2ETestRunner()
    success = await runner.run_all()
    sys.exit(0 if success else 1)


if __name__ == "__main__":
    asyncio.run(main())
