#!/usr/bin/env python3
"""Remove the managed Codex config and marketplace entry for this plugin."""

from __future__ import annotations

import argparse
import json
from pathlib import Path


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Uninstall the Codex legacy MCP bridge plugin.")
    parser.add_argument(
        "--server-id",
        default="x_research_tools",
        help="Codex MCP server id previously installed by install.py.",
    )
    return parser.parse_args()


def sanitize_server_id(server_id: str) -> str:
    cleaned = []
    for char in server_id:
        if char.isalnum() or char in {"_", "-"}:
            cleaned.append(char)
    result = "".join(cleaned).strip("_-")
    if not result:
        raise SystemExit("server-id must contain at least one alphanumeric character")
    return result


def remove_config_block(server_id: str) -> None:
    config_path = Path.home() / ".codex" / "config.toml"
    if not config_path.exists():
        return
    begin = f"# BEGIN codex-legacy-mcp-bridge:{server_id}"
    end = f"# END codex-legacy-mcp-bridge:{server_id}"
    text = config_path.read_text(encoding="utf-8")
    if begin not in text or end not in text:
        return
    start_index = text.index(begin)
    end_index = text.index(end) + len(end)
    updated = text[:start_index] + text[end_index:]
    config_path.write_text(updated.rstrip() + "\n", encoding="utf-8")


def remove_marketplace_entry(plugin_name: str) -> None:
    marketplace_path = Path.home() / ".agents" / "plugins" / "marketplace.json"
    if not marketplace_path.exists():
        return
    data = json.loads(marketplace_path.read_text(encoding="utf-8"))
    plugins = data.get("plugins", [])
    data["plugins"] = [entry for entry in plugins if entry.get("name") != plugin_name]
    marketplace_path.write_text(json.dumps(data, indent=2) + "\n", encoding="utf-8")


def remove_instance_config(server_id: str) -> None:
    config_path = Path.home() / ".codex" / "legacy-mcp-bridge" / f"{server_id}.json"
    if config_path.exists():
        config_path.unlink()


def main() -> None:
    args = parse_args()
    server_id = sanitize_server_id(args.server_id)
    remove_config_block(server_id)
    remove_marketplace_entry("codex-legacy-mcp-bridge")
    remove_instance_config(server_id)
    print("Removed managed Codex MCP entry, marketplace entry, and instance config.")


if __name__ == "__main__":
    main()

