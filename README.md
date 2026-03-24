# confluence-mcp

Fast, surgical Confluence MCP server for AI agents.

## What makes this different

Unlike generic Atlassian MCP servers, this one is **Confluence-only** and optimized for how AI agents actually work with documentation:

- **Surgical section edits** — Update a single section by heading name without touching the rest of the page
- **Fast page tree download** — Get an entire page hierarchy in one call
- **Smart image handling** — Upload images as attachments and auto-embed them in pages
- **Markdown in/out** — Read and write in markdown; storage format conversion is automatic
- **Find & replace** — Simple text replacement preserving all HTML structure
- **Section-aware reading** — Fetch just the section you need, not the whole page

## Installation

### Via uvx (recommended for MCP clients)

```bash
uvx --from git+https://github.com/1vecera/confluence-mcp confluence-mcp
```

### Via pip

```bash
pip install git+https://github.com/1vecera/confluence-mcp
```

## Configuration

Set these environment variables:

```bash
CONFLUENCE_URL=https://yoursite.atlassian.net
CONFLUENCE_USERNAME=you@company.com
CONFLUENCE_API_TOKEN=your-api-token
```

Get your API token at: https://id.atlassian.com/manage-profile/security/api-tokens

### Claude Code config

Add to `~/.claude.json`:

```json
{
  "mcpServers": {
    "confluence": {
      "command": "uvx",
      "args": ["--from", "git+https://github.com/1vecera/confluence-mcp", "confluence-mcp"],
      "env": {
        "CONFLUENCE_URL": "https://yoursite.atlassian.net",
        "CONFLUENCE_USERNAME": "you@company.com",
        "CONFLUENCE_API_TOKEN": "your-token"
      }
    }
  }
}
```

## Tools

### Reading

| Tool | Description |
|------|-------------|
| `get_page` | Get page content in markdown or storage format |
| `get_page_tree` | Download entire page hierarchy at once |
| `get_page_sections` | List all sections with their content |
| `get_section` | Get a specific section by heading name |
| `search_pages` | Search via CQL or simple text |
| `list_page_images` | List all image references in a page |

### Writing (surgical)

| Tool | Description |
|------|-------------|
| `update_page` | Update entire page content |
| `update_section` | Replace only a specific section — the key surgical edit tool |
| `append_to_section` | Add content to end of a section |
| `find_replace_in_page` | Find and replace text preserving HTML |
| `create_page` | Create a new page |

### Attachments & Images

| Tool | Description |
|------|-------------|
| `list_attachments` | List all attachments on a page |
| `download_attachment` | Download an attachment by filename |
| `upload_attachment` | Upload a file attachment |
| `upload_image_and_embed` | Upload image + optionally rewrite page to embed it |

### Labels

| Tool | Description |
|------|-------------|
| `get_labels` | Get labels on a page |
| `add_label` | Add a label to a page |

## Examples

### Surgical section update

Instead of downloading and re-uploading an entire page:

```
update_section(page_id="123456", heading="Status", new_content="Project is **on track** for Q2 delivery.")
```

### Download entire doc tree

```
get_page_tree(page_id="123456", include_body=True)
```

### Upload and embed an image

```
upload_image_and_embed(page_id="123456", filename="arch.png", image_base64="...", replace_url="https://old-host.com/arch.png")
```

## Development

```bash
uv sync --extra dev
uv run pytest --cov=confluence_mcp --cov-report=term-missing
```

## License

MIT
