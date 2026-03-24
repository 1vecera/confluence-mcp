"""Confluence MCP server — fast, surgical Confluence operations."""

from __future__ import annotations

import json
import logging
import os
import sys
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from typing import Any

from mcp.server.fastmcp import FastMCP

from .client import ConfluenceClient
from .content import (
    append_to_section as _content_append_to_section,
    extract_images,
    find_and_replace,
    get_section_content,
    get_sections,
    markdown_to_storage,
    replace_section,
    rewrite_image_to_attachment,
    storage_to_markdown,
)

logger = logging.getLogger("confluence-mcp")


# ------------------------------------------------------------------
# Lifespan — create / close the HTTP client
# ------------------------------------------------------------------


@asynccontextmanager
async def app_lifespan(server: FastMCP) -> AsyncIterator[dict]:
    """Initialize Confluence client from env vars."""
    url = os.environ.get("CONFLUENCE_URL", "")
    username = os.environ.get("CONFLUENCE_USERNAME", "")
    token = os.environ.get("CONFLUENCE_API_TOKEN", "")

    if not all([url, username, token]):
        missing = [k for k, v in {
            "CONFLUENCE_URL": url,
            "CONFLUENCE_USERNAME": username,
            "CONFLUENCE_API_TOKEN": token,
        }.items() if not v]
        logger.error(f"Missing env vars: {', '.join(missing)}")
        print(f"ERROR: Missing required env vars: {', '.join(missing)}", file=sys.stderr)
        yield {"client": None}
        return

    client = ConfluenceClient(url, username, token)
    logger.info(f"Connected to Confluence at {url}")
    try:
        yield {"client": client}
    finally:
        await client.close()


mcp = FastMCP(
    "Confluence MCP",
    lifespan=app_lifespan,
)


def _get_client(ctx) -> ConfluenceClient:
    """Extract client from lifespan context."""
    client = ctx.request_context.lifespan_context.get("client")
    if client is None:
        raise RuntimeError(
            "Confluence client not initialized. Check CONFLUENCE_URL, "
            "CONFLUENCE_USERNAME, CONFLUENCE_API_TOKEN env vars."
        )
    return client


# ==================================================================
# TOOLS — Reading
# ==================================================================


@mcp.tool()
async def get_page(
    ctx: Any,
    page_id: str,
    include_body: bool = True,
    body_format: str = "markdown",
) -> str:
    """Get a Confluence page by ID.

    Args:
        page_id: The numeric page ID.
        include_body: Whether to include page content (default: True).
        body_format: Return body as "markdown", "storage", or "view" (default: markdown).

    Returns:
        JSON with page metadata and optionally the body content.
    """
    client = _get_client(ctx)
    api_format = "storage" if body_format in ("markdown", "storage") else body_format
    page = await client.get_page(page_id, body_format=api_format)

    result: dict[str, Any] = {
        "id": page["id"],
        "title": page.get("title", ""),
        "status": page.get("status", ""),
        "spaceId": page.get("spaceId", ""),
        "version": page.get("version", {}).get("number"),
        "createdAt": page.get("version", {}).get("createdAt", ""),
    }

    if include_body:
        body_value = page.get("body", {}).get(api_format, {}).get("value", "")
        if body_format == "markdown":
            result["body"] = storage_to_markdown(body_value)
            result["body_format"] = "markdown"
        else:
            result["body"] = body_value
            result["body_format"] = api_format

    return json.dumps(result, indent=2, ensure_ascii=False)


@mcp.tool()
async def get_page_tree(
    ctx: Any,
    page_id: str,
    include_body: bool = False,
) -> str:
    """Get a page and all its descendants as a flat list.

    Useful for downloading an entire documentation tree at once.

    Args:
        page_id: The root page ID.
        include_body: If True, fetch each page's body in markdown (slower).
    """
    client = _get_client(ctx)

    # Get root page
    root = await client.get_page(page_id, body_format="storage")
    root_entry: dict[str, Any] = {
        "id": root["id"],
        "title": root.get("title", ""),
        "depth": 0,
    }
    if include_body:
        body_val = root.get("body", {}).get("storage", {}).get("value", "")
        root_entry["body"] = storage_to_markdown(body_val)

    # Get descendants
    children = await client.get_page_tree(page_id)
    entries = [root_entry]

    for child in children:
        entry: dict[str, Any] = {
            "id": child["id"],
            "title": child.get("title", ""),
            "depth": child.get("_depth", 1),
        }
        if include_body:
            child_page = await client.get_page(child["id"], body_format="storage")
            body_val = child_page.get("body", {}).get("storage", {}).get("value", "")
            entry["body"] = storage_to_markdown(body_val)
        entries.append(entry)

    return json.dumps({"root_id": page_id, "page_count": len(entries), "pages": entries}, indent=2, ensure_ascii=False)


@mcp.tool()
async def get_page_sections(
    ctx: Any,
    page_id: str,
) -> str:
    """List all sections (headings) of a page with their content.

    Useful for understanding page structure before making surgical edits.

    Args:
        page_id: The page ID.

    Returns:
        JSON list of sections with heading, level, and content.
    """
    client = _get_client(ctx)
    page = await client.get_page(page_id, body_format="storage")
    body = page.get("body", {}).get("storage", {}).get("value", "")
    sections = get_sections(body)
    # Convert content to markdown for readability
    for s in sections:
        s["content_markdown"] = storage_to_markdown(s["content"])
    return json.dumps(sections, indent=2, ensure_ascii=False)


@mcp.tool()
async def get_section(
    ctx: Any,
    page_id: str,
    heading: str,
    body_format: str = "markdown",
) -> str:
    """Get content of a specific section by heading name.

    Args:
        page_id: The page ID.
        heading: The exact heading text to find.
        body_format: "markdown" or "storage" (default: markdown).

    Returns:
        The section content, or error if not found.
    """
    client = _get_client(ctx)
    page = await client.get_page(page_id, body_format="storage")
    body = page.get("body", {}).get("storage", {}).get("value", "")
    content = get_section_content(body, heading)

    if content is None:
        sections = get_sections(body)
        available = [s["heading"] for s in sections if s["heading"]]
        return json.dumps({
            "error": f"Section '{heading}' not found",
            "available_sections": available,
        }, indent=2, ensure_ascii=False)

    if body_format == "markdown":
        content = storage_to_markdown(content)

    return json.dumps({
        "page_id": page_id,
        "heading": heading,
        "content": content,
        "format": body_format,
    }, indent=2, ensure_ascii=False)


@mcp.tool()
async def search_pages(
    ctx: Any,
    query: str,
    limit: int = 10,
) -> str:
    """Search Confluence pages using CQL or simple text.

    Args:
        query: CQL query (e.g., 'space=DEV AND title~"docs"') or simple text.
        limit: Max results (1-50, default 10).

    Returns:
        JSON list of matching pages.
    """
    client = _get_client(ctx)
    # Auto-wrap simple queries in CQL
    if not any(op in query for op in ["=", "~", ">", "<", " AND ", " OR "]):
        query = f'siteSearch ~ "{query}"'
    data = await client.search(query, limit=min(limit, 50))
    results = data.get("results", [])
    pages = []
    for r in results:
        content = r.get("content", r)
        pages.append({
            "id": content.get("id", ""),
            "title": content.get("title", ""),
            "type": content.get("type", ""),
            "space": content.get("space", {}).get("key", "") if isinstance(content.get("space"), dict) else "",
            "url": content.get("_links", {}).get("webui", ""),
        })
    return json.dumps(pages, indent=2, ensure_ascii=False)


# ==================================================================
# TOOLS — Writing (surgical updates)
# ==================================================================


@mcp.tool()
async def update_page(
    ctx: Any,
    page_id: str,
    body: str,
    title: str | None = None,
    body_format: str = "markdown",
    version_message: str | None = None,
) -> str:
    """Update an entire page's content.

    Args:
        page_id: The page ID.
        body: The new page body.
        title: New title (optional, keeps current if not provided).
        body_format: "markdown" or "storage" (default: markdown).
        version_message: Optional commit message for this version.

    Returns:
        JSON with the updated page info.
    """
    client = _get_client(ctx)

    # Fetch current page to get version number and current title
    current = await client.get_page(page_id, body_format="storage")
    current_version = current.get("version", {}).get("number", 1)
    current_title = current.get("title", "")

    if body_format == "markdown":
        body = markdown_to_storage(body)

    updated = await client.update_page(
        page_id,
        title=title or current_title,
        body=body,
        version_number=current_version + 1,
        body_format="storage",
        version_message=version_message,
    )

    return json.dumps({
        "success": True,
        "id": updated["id"],
        "title": updated.get("title", ""),
        "version": updated.get("version", {}).get("number"),
    }, indent=2, ensure_ascii=False)


@mcp.tool()
async def update_section(
    ctx: Any,
    page_id: str,
    heading: str,
    new_content: str,
    content_format: str = "markdown",
    version_message: str | None = None,
) -> str:
    """Surgically replace the content of a specific section by heading.

    Only the section under the specified heading is changed; the rest of
    the page is untouched. This is the key tool for surgical edits.

    Args:
        page_id: The page ID.
        heading: The heading text identifying the section.
        new_content: The replacement content for the section body.
        content_format: "markdown" or "storage" (default: markdown).
        version_message: Optional commit message.

    Returns:
        JSON with updated page info or error.
    """
    client = _get_client(ctx)
    current = await client.get_page(page_id, body_format="storage")
    current_body = current.get("body", {}).get("storage", {}).get("value", "")
    current_version = current.get("version", {}).get("number", 1)
    current_title = current.get("title", "")

    fmt = "markdown" if content_format == "markdown" else "storage"
    new_body = replace_section(current_body, heading, new_content, content_format=fmt)

    updated = await client.update_page(
        page_id,
        title=current_title,
        body=new_body,
        version_number=current_version + 1,
        body_format="storage",
        version_message=version_message or f"Updated section: {heading}",
    )

    return json.dumps({
        "success": True,
        "id": updated["id"],
        "title": updated.get("title", ""),
        "version": updated.get("version", {}).get("number"),
        "updated_section": heading,
    }, indent=2, ensure_ascii=False)


@mcp.tool()
async def append_to_section(
    ctx: Any,
    page_id: str,
    heading: str,
    content: str,
    content_format: str = "markdown",
    version_message: str | None = None,
) -> str:
    """Append content to the end of a specific section.

    Useful for adding items to a list, notes, or log entries without
    replacing the entire section.

    Args:
        page_id: The page ID.
        heading: The heading text identifying the section.
        content: Content to append.
        content_format: "markdown" or "storage".
        version_message: Optional commit message.
    """
    client = _get_client(ctx)
    current = await client.get_page(page_id, body_format="storage")
    current_body = current.get("body", {}).get("storage", {}).get("value", "")
    current_version = current.get("version", {}).get("number", 1)
    current_title = current.get("title", "")

    fmt = "markdown" if content_format == "markdown" else "storage"
    new_body = _content_append_to_section(current_body, heading, content, content_format=fmt)

    updated = await client.update_page(
        page_id,
        title=current_title,
        body=new_body,
        version_number=current_version + 1,
        body_format="storage",
        version_message=version_message or f"Appended to section: {heading}",
    )

    return json.dumps({
        "success": True,
        "id": updated["id"],
        "version": updated.get("version", {}).get("number"),
        "appended_to": heading,
    }, indent=2, ensure_ascii=False)


@mcp.tool()
async def find_replace_in_page(
    ctx: Any,
    page_id: str,
    find_text: str,
    replace_text: str,
    version_message: str | None = None,
) -> str:
    """Find and replace text within a page.

    Simple text replacement preserving all HTML structure.

    Args:
        page_id: The page ID.
        find_text: Text to search for.
        replace_text: Text to replace it with.
        version_message: Optional commit message.
    """
    client = _get_client(ctx)
    current = await client.get_page(page_id, body_format="storage")
    current_body = current.get("body", {}).get("storage", {}).get("value", "")
    current_version = current.get("version", {}).get("number", 1)
    current_title = current.get("title", "")

    if find_text not in current_body:
        return json.dumps({"error": f"Text '{find_text}' not found in page"}, indent=2)

    new_body = find_and_replace(current_body, find_text, replace_text)

    updated = await client.update_page(
        page_id,
        title=current_title,
        body=new_body,
        version_number=current_version + 1,
        body_format="storage",
        version_message=version_message or f"Find/replace: '{find_text}' → '{replace_text}'",
    )

    return json.dumps({
        "success": True,
        "id": updated["id"],
        "version": updated.get("version", {}).get("number"),
    }, indent=2, ensure_ascii=False)


@mcp.tool()
async def create_page(
    ctx: Any,
    space_id: str,
    title: str,
    body: str,
    parent_id: str | None = None,
    body_format: str = "markdown",
) -> str:
    """Create a new Confluence page.

    Args:
        space_id: The space ID (numeric, not the space key).
        title: Page title.
        body: Page content.
        parent_id: Optional parent page ID.
        body_format: "markdown" or "storage" (default: markdown).

    Returns:
        JSON with the created page info.
    """
    client = _get_client(ctx)
    if body_format == "markdown":
        body = markdown_to_storage(body)

    created = await client.create_page(
        space_id, title=title, body=body, parent_id=parent_id
    )

    return json.dumps({
        "success": True,
        "id": created["id"],
        "title": created.get("title", ""),
        "version": created.get("version", {}).get("number"),
    }, indent=2, ensure_ascii=False)


# ==================================================================
# TOOLS — Attachments & Images
# ==================================================================


@mcp.tool()
async def list_attachments(
    ctx: Any,
    page_id: str,
) -> str:
    """List all attachments on a page.

    Args:
        page_id: The page ID.

    Returns:
        JSON list of attachments with id, title, mediaType, size, download link.
    """
    client = _get_client(ctx)
    data = await client.get_attachments(page_id)
    attachments = data.get("results", [])
    result = []
    for att in attachments:
        result.append({
            "id": att.get("id", ""),
            "title": att.get("title", ""),
            "mediaType": att.get("extensions", {}).get("mediaType", ""),
            "fileSize": att.get("extensions", {}).get("fileSize", 0),
            "download": att.get("_links", {}).get("download", ""),
        })
    return json.dumps(result, indent=2, ensure_ascii=False)


@mcp.tool()
async def download_attachment(
    ctx: Any,
    page_id: str,
    filename: str,
) -> str:
    """Download an attachment from a page by filename.

    Returns the raw content as base64-encoded string with metadata.

    Args:
        page_id: The page ID.
        filename: The attachment filename.
    """
    import base64
    client = _get_client(ctx)
    data = await client.get_attachments(page_id)
    attachments = data.get("results", [])

    target = None
    for att in attachments:
        if att.get("title") == filename:
            target = att
            break

    if not target:
        available = [a.get("title", "") for a in attachments]
        return json.dumps({
            "error": f"Attachment '{filename}' not found",
            "available": available,
        }, indent=2, ensure_ascii=False)

    download_path = target.get("_links", {}).get("download", "")
    if download_path.startswith("/download/"):
        download_path = f"/wiki{download_path}"

    content_bytes = await client.download_attachment(download_path)
    return json.dumps({
        "filename": filename,
        "mediaType": target.get("extensions", {}).get("mediaType", ""),
        "size": len(content_bytes),
        "content_base64": base64.b64encode(content_bytes).decode(),
    }, indent=2, ensure_ascii=False)


@mcp.tool()
async def upload_attachment(
    ctx: Any,
    page_id: str,
    filename: str,
    content_base64: str,
    content_type: str = "application/octet-stream",
    comment: str | None = None,
) -> str:
    """Upload a file as an attachment to a page.

    If an attachment with the same filename exists, it is updated.

    Args:
        page_id: The page ID.
        filename: Name for the attachment file.
        content_base64: File content encoded as base64 string.
        content_type: MIME type (default: application/octet-stream).
        comment: Optional comment for the attachment.

    Returns:
        JSON with the uploaded attachment info.
    """
    import base64
    client = _get_client(ctx)
    content = base64.b64decode(content_base64)
    result = await client.upload_attachment(
        page_id, filename, content, content_type=content_type, comment=comment
    )
    results = result.get("results", [result])
    att = results[0] if results else result
    return json.dumps({
        "success": True,
        "id": att.get("id", ""),
        "title": att.get("title", filename),
    }, indent=2, ensure_ascii=False)


@mcp.tool()
async def upload_image_and_embed(
    ctx: Any,
    page_id: str,
    filename: str,
    image_base64: str,
    content_type: str = "image/png",
    replace_url: str | None = None,
) -> str:
    """Upload an image and optionally embed it in the page.

    This is the recommended way to add images to Confluence pages.
    It uploads the image as an attachment, then optionally rewrites
    an external image URL in the page body to reference the attachment.

    Args:
        page_id: The page ID.
        filename: Name for the image file (e.g., "diagram.png").
        image_base64: Image content as base64.
        content_type: MIME type (default: image/png).
        replace_url: If provided, replaces this external image URL in the
                     page body with the new attachment reference.
    """
    import base64
    client = _get_client(ctx)

    # Upload image
    content = base64.b64decode(image_base64)
    result = await client.upload_attachment(
        page_id, filename, content, content_type=content_type
    )

    if replace_url:
        # Rewrite the page body to reference the attachment
        current = await client.get_page(page_id, body_format="storage")
        current_body = current.get("body", {}).get("storage", {}).get("value", "")
        current_version = current.get("version", {}).get("number", 1)
        current_title = current.get("title", "")

        new_body = rewrite_image_to_attachment(current_body, replace_url, filename)

        await client.update_page(
            page_id,
            title=current_title,
            body=new_body,
            version_number=current_version + 1,
            body_format="storage",
            version_message=f"Embedded image: {filename}",
        )

    results = result.get("results", [result])
    att = results[0] if results else result
    return json.dumps({
        "success": True,
        "attachment_id": att.get("id", ""),
        "filename": filename,
        "embedded": replace_url is not None,
    }, indent=2, ensure_ascii=False)


@mcp.tool()
async def list_page_images(
    ctx: Any,
    page_id: str,
) -> str:
    """List all image references in a page.

    Shows both attachment-based and external images.

    Args:
        page_id: The page ID.
    """
    client = _get_client(ctx)
    page = await client.get_page(page_id, body_format="storage")
    body = page.get("body", {}).get("storage", {}).get("value", "")
    images = extract_images(body)
    return json.dumps(images, indent=2, ensure_ascii=False)


# ==================================================================
# TOOLS — Labels
# ==================================================================


@mcp.tool()
async def get_labels(
    ctx: Any,
    page_id: str,
) -> str:
    """Get labels on a page.

    Args:
        page_id: The page ID.
    """
    client = _get_client(ctx)
    data = await client.get_labels(page_id)
    labels = [{"name": l.get("name", ""), "prefix": l.get("prefix", "")} for l in data.get("results", [])]
    return json.dumps(labels, indent=2, ensure_ascii=False)


@mcp.tool()
async def add_label(
    ctx: Any,
    page_id: str,
    label: str,
) -> str:
    """Add a label to a page.

    Args:
        page_id: The page ID.
        label: The label text.
    """
    client = _get_client(ctx)
    await client.add_label(page_id, label)
    return json.dumps({"success": True, "label": label}, indent=2, ensure_ascii=False)


# ==================================================================
# Entry point
# ==================================================================


def main():
    """Run the MCP server via stdio transport."""
    mcp.run(transport="stdio")


if __name__ == "__main__":
    main()
