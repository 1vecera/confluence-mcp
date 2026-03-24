"""Confluence MCP server — fast, surgical Confluence operations.

Key design: every tool that handles content supports file-based I/O via
optional file_path parameters. This avoids forcing large content through
the LLM context window. Content goes directly disk ↔ Confluence API.
"""

from __future__ import annotations

import base64
import json
import logging
import mimetypes
import os
import re
import sys
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from pathlib import Path
from typing import Any

from mcp.server.fastmcp import Context, FastMCP

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

IMAGE_EXTENSIONS = {".png", ".jpg", ".jpeg", ".gif", ".svg", ".webp", ".bmp", ".ico"}
_CONFLUENCE_IMAGE_URL_RE = re.compile(
    r"(?:https?://[^/]+)?/wiki/download/(?:thumbnails|attachments)/(\d+)/([^?\s)]+)(?:\?[^\s)]*)?",
)


def _sanitize_filename(title: str) -> str:
    """Make a page title safe for use as a filename."""
    sanitized = re.sub(r'[<>:"/\\|?*]', "", title)
    sanitized = re.sub(r"\s+", "_", sanitized).strip("_.")
    return sanitized[:200] if len(sanitized) > 200 else sanitized


def _rewrite_image_urls_to_local(content: str, page_id: str) -> str:
    """Replace Confluence image URLs with local pictures/ paths."""
    from urllib.parse import unquote

    def _replace(match: re.Match) -> str:
        matched_page_id = match.group(1)
        raw_filename = match.group(2)
        filename = unquote(raw_filename)
        return f"pictures/{matched_page_id}_{filename}"

    return _CONFLUENCE_IMAGE_URL_RE.sub(_replace, content)


async def _download_page_images(
    client: ConfluenceClient, page_id: str, pictures_dir: Path
) -> int:
    """Download all image attachments for a page to pictures_dir.

    Returns count of images downloaded.
    """
    pictures_dir.mkdir(parents=True, exist_ok=True)
    try:
        data = await client.get_attachments(page_id)
    except Exception:
        return 0

    count = 0
    for att in data.get("results", []):
        filename = att.get("title", "")
        media_type = att.get("extensions", {}).get("mediaType", "")
        ext = Path(filename).suffix.lower()

        if not (media_type.startswith("image/") or ext in IMAGE_EXTENSIONS):
            continue

        download_path = att.get("_links", {}).get("download", "")
        if not download_path:
            continue

        if download_path.startswith("/download/"):
            download_path = f"/wiki{download_path}"

        try:
            image_bytes = await client.download_attachment(download_path)
            local_name = f"{page_id}_{filename}"
            (pictures_dir / local_name).write_bytes(image_bytes)
            count += 1
        except Exception:
            continue

    return count


# ------------------------------------------------------------------
# Lifespan
# ------------------------------------------------------------------


@asynccontextmanager
async def app_lifespan(server: FastMCP) -> AsyncIterator[dict]:
    url = os.environ.get("CONFLUENCE_URL", "")
    username = os.environ.get("CONFLUENCE_USERNAME", "")
    token = os.environ.get("CONFLUENCE_API_TOKEN", "")

    if not all([url, username, token]):
        missing = [k for k, v in {
            "CONFLUENCE_URL": url, "CONFLUENCE_USERNAME": username,
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


mcp = FastMCP("Confluence MCP", lifespan=app_lifespan)


def _get_client(ctx: Context) -> ConfluenceClient:
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
    ctx: Context,
    page_id: str,
    include_body: bool = True,
    body_format: str = "markdown",
    output_file: str | None = None,
    include_images: bool = True,
) -> str:
    """Get a Confluence page by ID.

    Args:
        page_id: The numeric page ID.
        include_body: Whether to include page content (default: True).
        body_format: Return body as "markdown", "storage", or "view" (default: markdown).
        output_file: If provided, write the page body to this file path instead
                     of returning it in the response. Keeps the LLM context clean.
        include_images: If True and output_file is set, download images to a
                        pictures/ subdirectory and rewrite URLs to local paths.
    """
    client = _get_client(ctx)
    api_format = "storage" if body_format in ("markdown", "storage") else body_format
    page = await client.get_page(page_id, body_format=api_format)

    result: dict[str, Any] = {
        "id": page["id"], "title": page.get("title", ""),
        "status": page.get("status", ""), "spaceId": page.get("spaceId", ""),
        "version": page.get("version", {}).get("number"),
        "createdAt": page.get("version", {}).get("createdAt", ""),
    }

    if include_body:
        body_value = page.get("body", {}).get(api_format, {}).get("value", "")
        body_text = storage_to_markdown(body_value) if body_format == "markdown" else body_value

        if output_file:
            p = Path(output_file).expanduser()
            p.parent.mkdir(parents=True, exist_ok=True)

            # Download images and rewrite URLs
            img_count = 0
            if include_images and body_format == "markdown":
                pictures_dir = p.parent / "pictures"
                img_count = await _download_page_images(client, page_id, pictures_dir)
                body_text = _rewrite_image_urls_to_local(body_text, page_id)

            p.write_text(body_text, encoding="utf-8")
            result["body_written_to"] = str(p)
            result["body_format"] = body_format
            if img_count:
                result["images_downloaded"] = img_count
        else:
            result["body"] = body_text
            result["body_format"] = body_format

    return json.dumps(result, indent=2, ensure_ascii=False)


@mcp.tool()
async def get_page_tree(
    ctx: Context,
    page_id: str,
    include_body: bool = False,
    output_dir: str | None = None,
    include_images: bool = True,
) -> str:
    """Get a page and all its descendants as a flat list.

    When output_dir is set, saves each page as a .md file and downloads all
    images to a pictures/ subdirectory — same as the fetch-confluence skill.

    Args:
        page_id: The root page ID.
        include_body: If True, fetch each page's body in markdown (slower).
        output_dir: If provided, write each page as a separate .md file to this
                    directory. Files are named by depth and title.
        include_images: If True and output_dir is set, download all image
                        attachments to pictures/ and rewrite URLs in markdown.
    """
    client = _get_client(ctx)

    root = await client.get_page(page_id, body_format="storage")
    root_entry: dict[str, Any] = {"id": root["id"], "title": root.get("title", ""), "depth": 0}

    children = await client.get_page_tree(page_id)
    all_pages = [root_entry] + [
        {"id": c["id"], "title": c.get("title", ""), "depth": c.get("_depth", 1)}
        for c in children
    ]

    out_dir = Path(output_dir).expanduser() if output_dir else None
    if out_dir:
        out_dir.mkdir(parents=True, exist_ok=True)

    total_images = 0
    for i, entry in enumerate(all_pages):
        if include_body or out_dir:
            pg = await client.get_page(entry["id"], body_format="storage")
            body_val = pg.get("body", {}).get("storage", {}).get("value", "")
            md_body = storage_to_markdown(body_val)

            if out_dir:
                # Download images for this page
                if include_images:
                    pictures_dir = out_dir / "pictures"
                    img_count = await _download_page_images(client, entry["id"], pictures_dir)
                    md_body = _rewrite_image_urls_to_local(md_body, entry["id"])
                    total_images += img_count

                fname = f"{i:03d}_{_sanitize_filename(entry['title'])}.md"
                fpath = out_dir / fname
                header = f"# {entry['title']}\n\n**Page ID:** {entry['id']}  \n**Depth:** {entry['depth']}\n\n---\n\n"
                fpath.write_text(header + md_body, encoding="utf-8")
                entry["file"] = str(fpath)
            elif include_body:
                entry["body"] = md_body

    result: dict[str, Any] = {"root_id": page_id, "page_count": len(all_pages), "pages": all_pages}
    if out_dir:
        result["output_dir"] = str(out_dir)
        if total_images:
            result["images_downloaded"] = total_images
    return json.dumps(result, indent=2, ensure_ascii=False)


@mcp.tool()
async def get_page_sections(ctx: Context, page_id: str) -> str:
    """List all sections (headings) of a page with their content.

    Args:
        page_id: The page ID.
    """
    client = _get_client(ctx)
    page = await client.get_page(page_id, body_format="storage")
    body = page.get("body", {}).get("storage", {}).get("value", "")
    sections = get_sections(body)
    for s in sections:
        s["content_markdown"] = storage_to_markdown(s["content"])
    return json.dumps(sections, indent=2, ensure_ascii=False)


@mcp.tool()
async def get_section(
    ctx: Context,
    page_id: str,
    heading: str,
    body_format: str = "markdown",
    output_file: str | None = None,
) -> str:
    """Get content of a specific section by heading name.

    Args:
        page_id: The page ID.
        heading: The exact heading text to find.
        body_format: "markdown" or "storage" (default: markdown).
        output_file: If provided, write section content to this file.
    """
    client = _get_client(ctx)
    page = await client.get_page(page_id, body_format="storage")
    body = page.get("body", {}).get("storage", {}).get("value", "")
    content = get_section_content(body, heading)

    if content is None:
        available = [s["heading"] for s in get_sections(body) if s["heading"]]
        return json.dumps({"error": f"Section '{heading}' not found", "available_sections": available}, indent=2, ensure_ascii=False)

    if body_format == "markdown":
        content = storage_to_markdown(content)

    if output_file:
        p = Path(output_file).expanduser()
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text(content, encoding="utf-8")
        return json.dumps({"page_id": page_id, "heading": heading, "format": body_format,
                           "written_to": str(p)}, indent=2, ensure_ascii=False)

    return json.dumps({"page_id": page_id, "heading": heading, "content": content,
                       "format": body_format}, indent=2, ensure_ascii=False)


@mcp.tool()
async def search_pages(ctx: Context, query: str, limit: int = 10) -> str:
    """Search Confluence pages using CQL or simple text.

    Args:
        query: CQL query or simple text.
        limit: Max results (1-50, default 10).
    """
    client = _get_client(ctx)
    if not any(op in query for op in ["=", "~", ">", "<", " AND ", " OR "]):
        query = f'siteSearch ~ "{query}"'
    data = await client.search(query, limit=min(limit, 50))
    pages = []
    for r in data.get("results", []):
        c = r.get("content", r)
        pages.append({"id": c.get("id", ""), "title": c.get("title", ""), "type": c.get("type", ""),
                       "space": c.get("space", {}).get("key", "") if isinstance(c.get("space"), dict) else "",
                       "url": c.get("_links", {}).get("webui", "")})
    return json.dumps(pages, indent=2, ensure_ascii=False)


# ==================================================================
# TOOLS — Writing (surgical updates)
# ==================================================================


@mcp.tool()
async def update_page(
    ctx: Context,
    page_id: str,
    body: str | None = None,
    title: str | None = None,
    body_format: str = "markdown",
    version_message: str | None = None,
    input_file: str | None = None,
) -> str:
    """Update an entire page's content.

    Content can come from the body parameter OR from a file via input_file.
    Using input_file avoids sending large content through the LLM.

    Args:
        page_id: The page ID.
        body: The new page body (ignored if input_file is provided).
        title: New title (optional, keeps current if not provided).
        body_format: "markdown" or "storage" (default: markdown).
        version_message: Optional commit message for this version.
        input_file: Path to a file whose content will be used as the page body.
                    The file format is determined by body_format.
    """
    client = _get_client(ctx)
    current = await client.get_page(page_id, body_format="storage")

    if input_file:
        p = Path(input_file).expanduser()
        if not p.exists():
            return json.dumps({"error": f"File not found: {p}"}, indent=2)
        body = p.read_text(encoding="utf-8")
    elif body is None:
        return json.dumps({"error": "Either body or input_file must be provided"}, indent=2)

    if body_format == "markdown":
        body = markdown_to_storage(body)

    updated = await client.update_page(
        page_id, title=title or current.get("title", ""), body=body,
        version_number=current.get("version", {}).get("number", 1) + 1,
        body_format="storage", version_message=version_message)
    return json.dumps({"success": True, "id": updated["id"], "title": updated.get("title", ""),
                        "version": updated.get("version", {}).get("number")}, indent=2, ensure_ascii=False)


@mcp.tool()
async def update_section(
    ctx: Context,
    page_id: str,
    heading: str,
    new_content: str | None = None,
    content_format: str = "markdown",
    version_message: str | None = None,
    input_file: str | None = None,
) -> str:
    """Surgically replace the content of a specific section by heading.

    Only the section under the specified heading is changed; the rest of
    the page is untouched.

    Args:
        page_id: The page ID.
        heading: The heading text identifying the section.
        new_content: The replacement content (ignored if input_file is provided).
        content_format: "markdown" or "storage" (default: markdown).
        version_message: Optional commit message.
        input_file: Path to a file whose content replaces the section.
    """
    client = _get_client(ctx)
    current = await client.get_page(page_id, body_format="storage")
    current_body = current.get("body", {}).get("storage", {}).get("value", "")

    if input_file:
        p = Path(input_file).expanduser()
        if not p.exists():
            return json.dumps({"error": f"File not found: {p}"}, indent=2)
        new_content = p.read_text(encoding="utf-8")
    elif new_content is None:
        return json.dumps({"error": "Either new_content or input_file must be provided"}, indent=2)

    fmt = "markdown" if content_format == "markdown" else "storage"
    new_body = replace_section(current_body, heading, new_content, content_format=fmt)

    updated = await client.update_page(
        page_id, title=current.get("title", ""), body=new_body,
        version_number=current.get("version", {}).get("number", 1) + 1,
        body_format="storage", version_message=version_message or f"Updated section: {heading}")
    return json.dumps({"success": True, "id": updated["id"], "title": updated.get("title", ""),
                        "version": updated.get("version", {}).get("number"),
                        "updated_section": heading}, indent=2, ensure_ascii=False)


@mcp.tool()
async def append_to_section(
    ctx: Context,
    page_id: str,
    heading: str,
    content: str | None = None,
    content_format: str = "markdown",
    version_message: str | None = None,
    input_file: str | None = None,
) -> str:
    """Append content to the end of a specific section.

    Args:
        page_id: The page ID.
        heading: The heading text identifying the section.
        content: Content to append (ignored if input_file is provided).
        content_format: "markdown" or "storage".
        version_message: Optional commit message.
        input_file: Path to a file whose content will be appended.
    """
    client = _get_client(ctx)
    current = await client.get_page(page_id, body_format="storage")
    current_body = current.get("body", {}).get("storage", {}).get("value", "")

    if input_file:
        p = Path(input_file).expanduser()
        if not p.exists():
            return json.dumps({"error": f"File not found: {p}"}, indent=2)
        content = p.read_text(encoding="utf-8")
    elif content is None:
        return json.dumps({"error": "Either content or input_file must be provided"}, indent=2)

    fmt = "markdown" if content_format == "markdown" else "storage"
    new_body = _content_append_to_section(current_body, heading, content, content_format=fmt)

    updated = await client.update_page(
        page_id, title=current.get("title", ""), body=new_body,
        version_number=current.get("version", {}).get("number", 1) + 1,
        body_format="storage", version_message=version_message or f"Appended to section: {heading}")
    return json.dumps({"success": True, "id": updated["id"],
                        "version": updated.get("version", {}).get("number"),
                        "appended_to": heading}, indent=2, ensure_ascii=False)


@mcp.tool()
async def find_replace_in_page(
    ctx: Context,
    page_id: str,
    find_text: str,
    replace_text: str,
    version_message: str | None = None,
) -> str:
    """Find and replace text within a page, preserving all HTML structure.

    Args:
        page_id: The page ID.
        find_text: Text to search for.
        replace_text: Text to replace it with.
        version_message: Optional commit message.
    """
    client = _get_client(ctx)
    current = await client.get_page(page_id, body_format="storage")
    current_body = current.get("body", {}).get("storage", {}).get("value", "")
    if find_text not in current_body:
        return json.dumps({"error": f"Text '{find_text}' not found in page"}, indent=2)
    new_body = find_and_replace(current_body, find_text, replace_text)
    updated = await client.update_page(
        page_id, title=current.get("title", ""), body=new_body,
        version_number=current.get("version", {}).get("number", 1) + 1,
        body_format="storage", version_message=version_message or f"Find/replace: '{find_text}' -> '{replace_text}'")
    return json.dumps({"success": True, "id": updated["id"],
                        "version": updated.get("version", {}).get("number")}, indent=2, ensure_ascii=False)


@mcp.tool()
async def create_page(
    ctx: Context,
    space_id: str,
    title: str,
    body: str | None = None,
    parent_id: str | None = None,
    body_format: str = "markdown",
    input_file: str | None = None,
) -> str:
    """Create a new Confluence page.

    Args:
        space_id: The space ID (numeric).
        title: Page title.
        body: Page content (ignored if input_file is provided).
        parent_id: Optional parent page ID.
        body_format: "markdown" or "storage" (default: markdown).
        input_file: Path to a file whose content becomes the page body.
    """
    client = _get_client(ctx)

    if input_file:
        p = Path(input_file).expanduser()
        if not p.exists():
            return json.dumps({"error": f"File not found: {p}"}, indent=2)
        body = p.read_text(encoding="utf-8")
    elif body is None:
        return json.dumps({"error": "Either body or input_file must be provided"}, indent=2)

    if body_format == "markdown":
        body = markdown_to_storage(body)
    created = await client.create_page(space_id, title=title, body=body, parent_id=parent_id)
    return json.dumps({"success": True, "id": created["id"], "title": created.get("title", ""),
                        "version": created.get("version", {}).get("number")}, indent=2, ensure_ascii=False)


# ==================================================================
# TOOLS — Attachments & Images
# ==================================================================


@mcp.tool()
async def list_attachments(ctx: Context, page_id: str) -> str:
    """List all attachments on a page.

    Args:
        page_id: The page ID.
    """
    client = _get_client(ctx)
    data = await client.get_attachments(page_id)
    result = [{"id": a.get("id", ""), "title": a.get("title", ""),
               "mediaType": a.get("extensions", {}).get("mediaType", ""),
               "fileSize": a.get("extensions", {}).get("fileSize", 0),
               "download": a.get("_links", {}).get("download", "")} for a in data.get("results", [])]
    return json.dumps(result, indent=2, ensure_ascii=False)


@mcp.tool()
async def download_attachment(
    ctx: Context,
    page_id: str,
    filename: str,
    output_file: str | None = None,
) -> str:
    """Download an attachment from a page by filename.

    Args:
        page_id: The page ID.
        filename: The attachment filename.
        output_file: If provided, save the attachment to this file path
                     instead of returning base64 in the response.
    """
    client = _get_client(ctx)
    data = await client.get_attachments(page_id)
    target = next((a for a in data.get("results", []) if a.get("title") == filename), None)
    if not target:
        available = [a.get("title", "") for a in data.get("results", [])]
        return json.dumps({"error": f"Attachment '{filename}' not found", "available": available}, indent=2, ensure_ascii=False)

    dl = target.get("_links", {}).get("download", "")
    if dl.startswith("/download/"):
        dl = f"/wiki{dl}"
    content_bytes = await client.download_attachment(dl)

    if output_file:
        p = Path(output_file).expanduser()
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_bytes(content_bytes)
        return json.dumps({"filename": filename, "size": len(content_bytes),
                           "saved_to": str(p)}, indent=2, ensure_ascii=False)

    return json.dumps({"filename": filename,
                        "mediaType": target.get("extensions", {}).get("mediaType", ""),
                        "size": len(content_bytes),
                        "content_base64": base64.b64encode(content_bytes).decode()}, indent=2, ensure_ascii=False)


@mcp.tool()
async def upload_attachment(
    ctx: Context,
    page_id: str,
    filename: str | None = None,
    content_base64: str | None = None,
    content_type: str | None = None,
    comment: str | None = None,
    input_file: str | None = None,
) -> str:
    """Upload a file as an attachment to a page.

    Can upload from a local file (input_file) or from base64 content.

    Args:
        page_id: The page ID.
        filename: Name for the attachment (auto-detected from input_file if not given).
        content_base64: File content as base64 (ignored if input_file is provided).
        content_type: MIME type (auto-detected from filename if not given).
        comment: Optional comment for the attachment.
        input_file: Path to a local file to upload directly.
    """
    client = _get_client(ctx)

    if input_file:
        p = Path(input_file).expanduser()
        if not p.exists():
            return json.dumps({"error": f"File not found: {p}"}, indent=2)
        content = p.read_bytes()
        filename = filename or p.name
        content_type = content_type or mimetypes.guess_type(p.name)[0] or "application/octet-stream"
    elif content_base64:
        content = base64.b64decode(content_base64)
        if not filename:
            return json.dumps({"error": "filename is required when using content_base64"}, indent=2)
        content_type = content_type or "application/octet-stream"
    else:
        return json.dumps({"error": "Either input_file or content_base64 must be provided"}, indent=2)

    result = await client.upload_attachment(page_id, filename, content, content_type=content_type, comment=comment)
    results = result.get("results", [result])
    att = results[0] if results else result
    return json.dumps({"success": True, "id": att.get("id", ""), "title": att.get("title", filename)}, indent=2, ensure_ascii=False)


@mcp.tool()
async def upload_image_and_embed(
    ctx: Context,
    page_id: str,
    filename: str | None = None,
    image_base64: str | None = None,
    content_type: str = "image/png",
    replace_url: str | None = None,
    input_file: str | None = None,
) -> str:
    """Upload an image and optionally embed it in the page.

    Can upload from a local file (input_file) or from base64 content.

    Args:
        page_id: The page ID.
        filename: Name for the image file (auto-detected from input_file).
        image_base64: Image content as base64 (ignored if input_file is provided).
        content_type: MIME type (auto-detected from filename if not given).
        replace_url: If provided, replaces this external image URL in the page
                     body with the new attachment reference.
        input_file: Path to a local image file to upload directly.
    """
    client = _get_client(ctx)

    if input_file:
        p = Path(input_file).expanduser()
        if not p.exists():
            return json.dumps({"error": f"File not found: {p}"}, indent=2)
        content = p.read_bytes()
        filename = filename or p.name
        content_type = mimetypes.guess_type(p.name)[0] or content_type
    elif image_base64:
        content = base64.b64decode(image_base64)
        if not filename:
            return json.dumps({"error": "filename is required when using image_base64"}, indent=2)
    else:
        return json.dumps({"error": "Either input_file or image_base64 must be provided"}, indent=2)

    result = await client.upload_attachment(page_id, filename, content, content_type=content_type)

    if replace_url:
        current = await client.get_page(page_id, body_format="storage")
        new_body = rewrite_image_to_attachment(
            current.get("body", {}).get("storage", {}).get("value", ""), replace_url, filename)
        await client.update_page(
            page_id, title=current.get("title", ""), body=new_body,
            version_number=current.get("version", {}).get("number", 1) + 1,
            body_format="storage", version_message=f"Embedded image: {filename}")

    results = result.get("results", [result])
    att = results[0] if results else result
    return json.dumps({"success": True, "attachment_id": att.get("id", ""), "filename": filename,
                        "embedded": replace_url is not None}, indent=2, ensure_ascii=False)


@mcp.tool()
async def list_page_images(ctx: Context, page_id: str) -> str:
    """List all image references in a page.

    Args:
        page_id: The page ID.
    """
    client = _get_client(ctx)
    page = await client.get_page(page_id, body_format="storage")
    return json.dumps(extract_images(page.get("body", {}).get("storage", {}).get("value", "")), indent=2, ensure_ascii=False)


# ==================================================================
# TOOLS — Labels
# ==================================================================


@mcp.tool()
async def get_labels(ctx: Context, page_id: str) -> str:
    """Get labels on a page.

    Args:
        page_id: The page ID.
    """
    client = _get_client(ctx)
    data = await client.get_labels(page_id)
    return json.dumps([{"name": l.get("name", ""), "prefix": l.get("prefix", "")} for l in data.get("results", [])], indent=2, ensure_ascii=False)


@mcp.tool()
async def add_label(ctx: Context, page_id: str, label: str) -> str:
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
    mcp.run(transport="stdio")


if __name__ == "__main__":
    main()
