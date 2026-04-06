# Codex Legacy MCP Bridge Plugin

Bridge legacy SSE-based MCP servers into a local stdio MCP server that Codex can use like any other MCP.

This plugin exists for servers that still use the older pattern:

1. `GET /sse/`
2. wait for `event: endpoint`
3. `POST /messages/?session_id=...`

Current Codex MCP `url` entries expect streamable HTTP, so older SSE endpoints need a small compatibility shim.

## What this plugin does

- Packages a generic local bridge script
- Installs a stable Codex MCP entry in `~/.codex/config.toml`
- Registers the plugin in `~/.agents/plugins/marketplace.json`
- Creates a stable plugin path under `~/plugins/codex-legacy-mcp-bridge`

After install, Codex sees the bridged server as a normal stdio MCP server.

## Install

Clone the repo anywhere, then run:

```bash
python3 install.py \
  --upstream-url https://ava.reforge.vc/sse/ \
  --server-id x_research_tools
```

That command:

- creates or refreshes `~/plugins/codex-legacy-mcp-bridge`
- updates `~/.agents/plugins/marketplace.json`
- writes an instance config under `~/.codex/legacy-mcp-bridge/`
- adds a managed MCP block to `~/.codex/config.toml`

Restart Codex after install.

## Uninstall

```bash
python3 uninstall.py --server-id x_research_tools
```

This removes the managed MCP block and plugin marketplace entry. The repo itself is left in place.

## Test

Run the bridge integration test against the live upstream:

```bash
python3 -m unittest tests.test_bridge_integration -v
```

## Why this exists

`https://ava.reforge.vc/sse/` is a real legacy SSE MCP endpoint. It responds to:

- `GET /sse/` with `text/event-stream`
- `event: endpoint` containing a session-specific `/messages/?session_id=...` URL

But it does not accept direct streamable HTTP `POST initialize` requests at `/sse/`, which is why Codex fails against it directly.

