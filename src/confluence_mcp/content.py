"""Content manipulation — surgical edits on Confluence storage format.

Confluence pages are stored in XHTML-like "storage format". This module
provides helpers to:
  - Parse storage format into an editable tree
  - Find/replace sections by heading
  - Insert/append/replace content blocks
  - Convert between markdown and storage format
  - Extract and rewrite image references
"""

from __future__ import annotations

import re
from typing import Literal

from bs4 import BeautifulSoup, Tag
from markdownify import markdownify as md


# ------------------------------------------------------------------
# Markdown ↔ Storage format conversion
# ------------------------------------------------------------------

def storage_to_markdown(storage_html: str) -> str:
    """Convert Confluence storage format to markdown."""
    return md(storage_html, heading_style="ATX", bullets="-", strip=["style"])


def markdown_to_storage(markdown_text: str) -> str:
    """Convert markdown to basic Confluence storage format.

    This is a pragmatic converter — handles headings, paragraphs, lists,
    code blocks, bold, italic, links, and images. For complex macros,
    use storage format directly.
    """
    lines = markdown_text.split("\n")
    table_line_indices = _scan_table_lines(lines)
    html_parts: list[str] = []
    in_code_block = False
    code_lang = ""
    code_lines: list[str] = []
    in_list: str | None = None  # "ul" or "ol"
    list_items: list[str] = []
    table_rows: list[str] = []
    blockquote_lines: list[str] = []

    def _flush_list() -> None:
        nonlocal in_list, list_items
        if in_list and list_items:
            tag = in_list
            items = "".join(f"<li>{_inline(item)}</li>" for item in list_items)
            html_parts.append(f"<{tag}>{items}</{tag}>")
        in_list = None
        list_items = []

    def _flush_table() -> None:
        nonlocal table_rows
        if not table_rows:
            return
        parsed = [_parse_table_row(r) for r in table_rows]
        # Detect separator row (all cells match --- pattern)
        sep_idx = None
        for i, cells in enumerate(parsed):
            if all(re.match(r"^:?-{3,}:?$", c.strip()) for c in cells):
                sep_idx = i
                break
        parts = ['<table><tbody>']
        if sep_idx is not None and sep_idx > 0:
            # Rows before separator are header
            parts = ["<table><thead>"]
            for row_cells in parsed[:sep_idx]:
                parts.append("<tr>" + "".join(f"<th>{_inline(c.strip())}</th>" for c in row_cells) + "</tr>")
            parts.append("</thead><tbody>")
            body = parsed[sep_idx + 1:]
        else:
            body = parsed
        for row_cells in body:
            parts.append("<tr>" + "".join(f"<td>{_inline(c.strip())}</td>" for c in row_cells) + "</tr>")
        parts.append("</tbody></table>")
        html_parts.append("".join(parts))
        table_rows = []

    def _flush_blockquote() -> None:
        nonlocal blockquote_lines
        if not blockquote_lines:
            return
        inner = " ".join(blockquote_lines)
        html_parts.append(
            '<ac:structured-macro ac:name="info">'
            f"<ac:rich-text-body><p>{_inline(inner)}</p></ac:rich-text-body>"
            "</ac:structured-macro>"
        )
        blockquote_lines = []

    def _flush_all() -> None:
        _flush_list()
        _flush_table()
        _flush_blockquote()

    for line_idx, line in enumerate(lines):
        # Code block fences
        if line.startswith("```"):
            if in_code_block:
                code = "\n".join(code_lines)
                lang_attr = f' ac:language="{code_lang}"' if code_lang else ""
                html_parts.append(
                    f'<ac:structured-macro ac:name="code">'
                    f"<ac:parameter ac:name=\"language\">{code_lang}</ac:parameter>"
                    f"<ac:plain-text-body><![CDATA[{code}]]></ac:plain-text-body>"
                    f"</ac:structured-macro>"
                )
                in_code_block = False
                code_lines = []
                code_lang = ""
            else:
                _flush_all()
                in_code_block = True
                code_lang = line[3:].strip()
            continue

        if in_code_block:
            code_lines.append(line)
            continue

        stripped = line.strip()

        # Empty line — flush everything
        if not stripped:
            _flush_all()
            continue

        # Table rows: identified by pre-scan (handles both `| a | b |` and `a| b` formats)
        if line_idx in table_line_indices:
            _flush_list()
            _flush_blockquote()
            table_rows.append(stripped)
            continue

        # If we were collecting table rows but this line isn't one, flush
        if table_rows:
            _flush_table()

        # Blockquotes: lines starting with >
        bq_match = re.match(r"^>\s?(.*)", stripped)
        if bq_match:
            _flush_list()
            _flush_table()
            blockquote_lines.append(bq_match.group(1))
            continue

        # If we were collecting blockquote lines but this isn't one, flush
        if blockquote_lines:
            _flush_blockquote()

        # Headings
        heading_match = re.match(r"^(#{1,6})\s+(.+)$", stripped)
        if heading_match:
            _flush_list()
            level = len(heading_match.group(1))
            text = _inline(heading_match.group(2))
            html_parts.append(f"<h{level}>{text}</h{level}>")
            continue

        # Unordered list
        ul_match = re.match(r"^[-*+]\s+(.+)$", stripped)
        if ul_match:
            if in_list != "ul":
                _flush_list()
                in_list = "ul"
            list_items.append(ul_match.group(1))
            continue

        # Ordered list
        ol_match = re.match(r"^\d+\.\s+(.+)$", stripped)
        if ol_match:
            if in_list != "ol":
                _flush_list()
                in_list = "ol"
            list_items.append(ol_match.group(1))
            continue

        # Horizontal rule
        if re.match(r"^[-*_]{3,}$", stripped):
            _flush_list()
            html_parts.append("<hr />")
            continue

        # Regular paragraph
        _flush_list()
        html_parts.append(f"<p>{_inline(stripped)}</p>")

    _flush_all()
    return "\n".join(html_parts)


def _parse_table_row(row: str) -> list[str]:
    """Split a markdown table row into cells, stripping outer pipes."""
    inner = row.strip("|")
    return inner.split("|")


def _is_separator_line(stripped: str) -> bool:
    """Return True if line is a markdown table separator (---|---|---)."""
    if "|" not in stripped:
        return False
    cells = stripped.strip("|").split("|")
    return bool(cells) and all(re.match(r"^\s*:?-{3,}:?\s*$", c) for c in cells)


def _scan_table_lines(lines: list[str]) -> set[int]:
    """Pre-scan: return set of line indices that belong to a markdown table.

    A line is a table row if it contains `|` and is either:
      - a separator row (---|---|---), or
      - adjacent (no blank line between) to a separator row, or
      - part of a contiguous run of lines with leading AND trailing pipes
        (legacy `| A | B |` format without a separator).
    """
    n = len(lines)
    in_code = False
    has_pipe = [False] * n
    is_sep = [False] * n
    is_blank = [False] * n
    has_outer_pipes = [False] * n

    for i, line in enumerate(lines):
        if line.startswith("```"):
            in_code = not in_code
            continue
        if in_code:
            continue
        stripped = line.strip()
        if not stripped:
            is_blank[i] = True
            continue
        has_pipe[i] = "|" in stripped
        is_sep[i] = _is_separator_line(stripped)
        has_outer_pipes[i] = stripped.startswith("|") and stripped.endswith("|")

    table_lines: set[int] = set()

    # Expand outward from each separator while neighbouring lines have pipes
    for i, sep in enumerate(is_sep):
        if not sep:
            continue
        table_lines.add(i)
        j = i - 1
        while j >= 0 and has_pipe[j] and not is_blank[j]:
            table_lines.add(j)
            j -= 1
        j = i + 1
        while j < n and has_pipe[j] and not is_blank[j]:
            table_lines.add(j)
            j += 1

    # Contiguous runs of outer-pipe rows (handles header-less tables)
    i = 0
    while i < n:
        if has_outer_pipes[i]:
            j = i
            while j < n and has_outer_pipes[j]:
                table_lines.add(j)
                j += 1
            i = j
        else:
            i += 1

    return table_lines


def _inline(text: str) -> str:
    """Convert inline markdown elements to HTML."""
    # Images: ![alt](url)
    text = re.sub(r"!\[([^\]]*)\]\(([^)]+)\)", r'<ac:image><ri:url ri:value="\2" /></ac:image>', text)
    # Links: [text](url)
    text = re.sub(r"\[([^\]]+)\]\(([^)]+)\)", r'<a href="\2">\1</a>', text)
    # Bold: **text**
    text = re.sub(r"\*\*(.+?)\*\*", r"<strong>\1</strong>", text)
    # Italic: *text*
    text = re.sub(r"\*(.+?)\*", r"<em>\1</em>", text)
    # Inline code: `code`
    text = re.sub(r"`([^`]+)`", r"<code>\1</code>", text)
    return text


# ------------------------------------------------------------------
# Section-based surgical editing
# ------------------------------------------------------------------

def get_sections(storage_html: str) -> list[dict]:
    """Parse storage format and return sections split by headings.

    Returns a list of dicts:
        {"heading": "Section Title", "level": 2, "content": "<p>...</p>", "index": 0}

    The first section (index 0) may have heading=None if content precedes
    the first heading.
    """
    soup = BeautifulSoup(storage_html, "html.parser")
    sections: list[dict] = []
    current: dict = {"heading": None, "level": 0, "content_parts": [], "index": 0}

    for element in soup.children:
        if isinstance(element, Tag) and re.match(r"^h[1-6]$", element.name):
            # Save previous section
            if current["content_parts"] or current["heading"] is not None:
                current["content"] = "".join(str(p) for p in current["content_parts"])
                del current["content_parts"]
                sections.append(current)
            level = int(element.name[1])
            current = {
                "heading": element.get_text(strip=True),
                "level": level,
                "content_parts": [],
                "index": len(sections),
            }
        else:
            current["content_parts"].append(element)

    # Final section
    current["content"] = "".join(str(p) for p in current["content_parts"])
    del current["content_parts"]
    sections.append(current)

    return sections


def get_section_content(storage_html: str, heading: str) -> str | None:
    """Get the content of a specific section by heading name.

    Returns the HTML content between the heading and the next heading
    of equal or higher level, or None if not found.
    """
    sections = get_sections(storage_html)
    for section in sections:
        if section["heading"] and section["heading"].lower() == heading.lower():
            return section["content"]
    return None


def replace_section(
    storage_html: str,
    heading: str,
    new_content: str,
    *,
    content_format: Literal["storage", "markdown"] = "storage",
) -> str:
    """Replace content of a section identified by heading.

    Keeps the heading itself, replaces everything between it and the
    next heading of equal/higher level.
    """
    if content_format == "markdown":
        new_content = markdown_to_storage(new_content)

    soup = BeautifulSoup(storage_html, "html.parser")
    heading_tag = None

    # Find the heading
    for tag in soup.find_all(re.compile(r"^h[1-6]$")):
        if tag.get_text(strip=True).lower() == heading.lower():
            heading_tag = tag
            break

    if heading_tag is None:
        raise ValueError(f"Section '{heading}' not found in page")

    heading_level = int(heading_tag.name[1])

    # Collect elements to remove (between this heading and next same/higher level)
    elements_to_remove = []
    sibling = heading_tag.next_sibling
    while sibling:
        if isinstance(sibling, Tag) and re.match(r"^h[1-6]$", sibling.name):
            sib_level = int(sibling.name[1])
            if sib_level <= heading_level:
                break
        next_sib = sibling.next_sibling
        elements_to_remove.append(sibling)
        sibling = next_sib

    # Remove old content
    for elem in elements_to_remove:
        elem.extract()

    # Insert new content after heading
    new_soup = BeautifulSoup(new_content, "html.parser")
    insert_after = heading_tag
    for child in list(new_soup.children):
        insert_after.insert_after(child)
        insert_after = child

    return str(soup)


def append_to_section(
    storage_html: str,
    heading: str,
    content_to_append: str,
    *,
    content_format: Literal["storage", "markdown"] = "storage",
) -> str:
    """Append content to the end of a section."""
    if content_format == "markdown":
        content_to_append = markdown_to_storage(content_to_append)

    soup = BeautifulSoup(storage_html, "html.parser")
    heading_tag = None

    for tag in soup.find_all(re.compile(r"^h[1-6]$")):
        if tag.get_text(strip=True).lower() == heading.lower():
            heading_tag = tag
            break

    if heading_tag is None:
        raise ValueError(f"Section '{heading}' not found in page")

    heading_level = int(heading_tag.name[1])

    # Find last element in this section
    last_in_section = heading_tag
    sibling = heading_tag.next_sibling
    while sibling:
        if isinstance(sibling, Tag) and re.match(r"^h[1-6]$", sibling.name):
            sib_level = int(sibling.name[1])
            if sib_level <= heading_level:
                break
        last_in_section = sibling
        sibling = sibling.next_sibling

    # Insert after last element
    new_soup = BeautifulSoup(content_to_append, "html.parser")
    insert_after = last_in_section
    for child in list(new_soup.children):
        insert_after.insert_after(child)
        insert_after = child

    return str(soup)


def find_and_replace(storage_html: str, find_text: str, replace_text: str) -> str:
    """Simple text find-and-replace within storage format, preserving tags."""
    return storage_html.replace(find_text, replace_text)


# ------------------------------------------------------------------
# Image helpers
# ------------------------------------------------------------------

def extract_images(storage_html: str) -> list[dict]:
    """Extract all image references from storage format.

    Returns list of dicts with keys: type, src, filename, attachment_id.
    """
    soup = BeautifulSoup(storage_html, "html.parser")
    images: list[dict] = []

    # Confluence attachment images: <ac:image><ri:attachment ri:filename="..."/></ac:image>
    for ac_img in soup.find_all("ac:image"):
        ri_att = ac_img.find("ri:attachment")
        if ri_att:
            images.append({
                "type": "attachment",
                "filename": ri_att.get("ri:filename", ""),
                "src": None,
            })
        ri_url = ac_img.find("ri:url")
        if ri_url:
            images.append({
                "type": "external",
                "src": ri_url.get("ri:value", ""),
                "filename": None,
            })

    # Standard HTML images
    for img in soup.find_all("img"):
        images.append({
            "type": "html",
            "src": img.get("src", ""),
            "filename": None,
        })

    return images


def rewrite_image_to_attachment(storage_html: str, image_url: str, filename: str) -> str:
    """Replace an external image URL with a Confluence attachment reference."""
    soup = BeautifulSoup(storage_html, "html.parser")

    # Handle ri:url references
    for ri_url in soup.find_all("ri:url"):
        if ri_url.get("ri:value") == image_url:
            parent = ri_url.parent
            if parent and parent.name == "ac:image":
                ri_url.decompose()
                new_att = soup.new_tag("ri:attachment")
                new_att["ri:filename"] = filename
                parent.append(new_att)

    # Handle standard img tags
    for img in soup.find_all("img"):
        if img.get("src") == image_url:
            ac_image = soup.new_tag("ac:image")
            ri_att = soup.new_tag("ri:attachment")
            ri_att["ri:filename"] = filename
            ac_image.append(ri_att)
            img.replace_with(ac_image)

    return str(soup)
