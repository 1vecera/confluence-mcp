"""Tests for content manipulation (surgical edits)."""

from confluence_mcp.content import (
    append_to_section,
    extract_images,
    find_and_replace,
    get_section_content,
    get_sections,
    markdown_to_storage,
    replace_section,
    rewrite_image_to_attachment,
    storage_to_markdown,
)


SAMPLE_STORAGE = """<h1>Introduction</h1>
<p>This is the intro paragraph.</p>
<h2>Details</h2>
<p>Some details here.</p>
<ul><li>Item 1</li><li>Item 2</li></ul>
<h2>Summary</h2>
<p>Summary text goes here.</p>
<h1>Appendix</h1>
<p>Appendix content.</p>"""


class TestGetSections:
    def test_basic_sections(self):
        sections = get_sections(SAMPLE_STORAGE)
        headings = [s["heading"] for s in sections]
        assert "Introduction" in headings
        assert "Details" in headings
        assert "Summary" in headings
        assert "Appendix" in headings

    def test_section_levels(self):
        sections = get_sections(SAMPLE_STORAGE)
        by_name = {s["heading"]: s for s in sections}
        assert by_name["Introduction"]["level"] == 1
        assert by_name["Details"]["level"] == 2
        assert by_name["Summary"]["level"] == 2
        assert by_name["Appendix"]["level"] == 1

    def test_content_before_first_heading(self):
        html = "<p>Before heading</p><h1>Title</h1><p>After</p>"
        sections = get_sections(html)
        assert sections[0]["heading"] is None
        assert "Before heading" in sections[0]["content"]


class TestGetSectionContent:
    def test_found(self):
        content = get_section_content(SAMPLE_STORAGE, "Details")
        assert content is not None
        assert "Some details here" in content
        assert "Item 1" in content

    def test_not_found(self):
        content = get_section_content(SAMPLE_STORAGE, "Nonexistent")
        assert content is None

    def test_case_insensitive(self):
        content = get_section_content(SAMPLE_STORAGE, "details")
        assert content is not None


class TestReplaceSection:
    def test_replace_section_content(self):
        new_html = replace_section(
            SAMPLE_STORAGE, "Details", "<p>New details content.</p>"
        )
        assert "New details content" in new_html
        assert "Some details here" not in new_html
        # Other sections unchanged
        assert "Summary text goes here" in new_html
        assert "This is the intro paragraph" in new_html

    def test_replace_with_markdown(self):
        new_html = replace_section(
            SAMPLE_STORAGE,
            "Details",
            "**Bold new content**",
            content_format="markdown",
        )
        assert "<strong>Bold new content</strong>" in new_html

    def test_heading_not_found_raises(self):
        import pytest
        with pytest.raises(ValueError, match="not found"):
            replace_section(SAMPLE_STORAGE, "Nonexistent", "whatever")


class TestAppendToSection:
    def test_append(self):
        new_html = append_to_section(
            SAMPLE_STORAGE, "Details", "<p>Appended item.</p>"
        )
        assert "Appended item" in new_html
        assert "Some details here" in new_html  # Original preserved

    def test_append_markdown(self):
        new_html = append_to_section(
            SAMPLE_STORAGE, "Details", "- New item", content_format="markdown"
        )
        assert "New item" in new_html


class TestFindReplace:
    def test_basic_replace(self):
        result = find_and_replace(SAMPLE_STORAGE, "intro paragraph", "opening text")
        assert "opening text" in result
        assert "intro paragraph" not in result


class TestMarkdownToStorage:
    def test_heading(self):
        result = markdown_to_storage("## Hello World")
        assert "<h2>Hello World</h2>" in result

    def test_paragraph(self):
        result = markdown_to_storage("Some text here")
        assert "<p>Some text here</p>" in result

    def test_bold(self):
        result = markdown_to_storage("**bold text**")
        assert "<strong>bold text</strong>" in result

    def test_code_block(self):
        result = markdown_to_storage("```python\nprint('hello')\n```")
        assert "ac:structured-macro" in result
        assert "print('hello')" in result

    def test_unordered_list(self):
        result = markdown_to_storage("- item 1\n- item 2")
        assert "<ul>" in result
        assert "<li>" in result

    def test_link(self):
        result = markdown_to_storage("[Click here](https://example.com)")
        assert 'href="https://example.com"' in result


class TestStorageToMarkdown:
    def test_heading(self):
        result = storage_to_markdown("<h2>Hello</h2>")
        assert "##" in result
        assert "Hello" in result

    def test_paragraph(self):
        result = storage_to_markdown("<p>Some text</p>")
        assert "Some text" in result.strip()


class TestExtractImages:
    def test_attachment_images(self):
        html = '<ac:image><ri:attachment ri:filename="diagram.png" /></ac:image>'
        images = extract_images(html)
        assert len(images) == 1
        assert images[0]["type"] == "attachment"
        assert images[0]["filename"] == "diagram.png"

    def test_external_images(self):
        html = '<ac:image><ri:url ri:value="https://example.com/img.png" /></ac:image>'
        images = extract_images(html)
        assert len(images) == 1
        assert images[0]["type"] == "external"
        assert images[0]["src"] == "https://example.com/img.png"

    def test_html_img(self):
        html = '<img src="https://example.com/pic.jpg" />'
        images = extract_images(html)
        assert len(images) == 1
        assert images[0]["type"] == "html"


class TestRewriteImageToAttachment:
    def test_rewrite_ri_url(self):
        html = '<ac:image><ri:url ri:value="https://old.com/img.png" /></ac:image>'
        result = rewrite_image_to_attachment(html, "https://old.com/img.png", "new.png")
        assert "ri:attachment" in result
        assert "new.png" in result
        assert "https://old.com" not in result

    def test_rewrite_img_tag(self):
        html = '<img src="https://old.com/pic.jpg" />'
        result = rewrite_image_to_attachment(html, "https://old.com/pic.jpg", "local.jpg")
        assert "ri:attachment" in result
        assert "local.jpg" in result
