#!/usr/bin/env python3
"""Install the Codex legacy MCP bridge plugin on the current machine."""

from __future__ import annotations

import argparse
import json
import importlib.util
import ssl
import sys
import urllib.parse
import urllib.request
from pathlib import Path
from typing import Any


PLUGIN_NAME = "codex-legacy-mcp-bridge"
MARKER_PREFIX = "# BEGIN codex-legacy-mcp-bridge:"


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Install the Codex legacy MCP bridge plugin.")
    parser.add_argument(
        "--upstream-url",
        default="https://ava.reforge.vc/sse/",
        help="Legacy SSE MCP endpoint to bridge.",
    )
    parser.add_argument(
        "--server-id",
        default="x_research_tools",
        help="Codex MCP server id to register in ~/.codex/config.toml.",
    )
    parser.add_argument(
        "--display-name",
        default="X Research Tools",
        help="Human-readable server name stored in the bridge instance config.",
    )
    parser.add_argument(
        "--force",
        action="store_true",
        help="Replace an existing symlink or marketplace entry if needed.",
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


def build_ssl_context() -> ssl.SSLContext:
    certifi_spec = importlib.util.find_spec("certifi")
    if certifi_spec is not None:
        import certifi  # type: ignore

        return ssl.create_default_context(cafile=certifi.where())

    for path in ("/etc/ssl/cert.pem", "/private/etc/ssl/cert.pem"):
        candidate = Path(path)
        if candidate.exists():
            return ssl.create_default_context(cafile=str(candidate))

    return ssl.create_default_context()


def _legacy_probe(upstream_url: str) -> tuple[dict[str, Any], list[dict[str, Any]]]:
    context = build_ssl_context()
    request = urllib.request.Request(
        upstream_url,
        headers={"Accept": "text/event-stream"},
        method="GET",
    )
    response = urllib.request.urlopen(request, timeout=30, context=context)  # noqa: S310
    content_type = response.headers.get("Content-Type", "")
    if "text/event-stream" not in content_type:
        response.close()
        raise RuntimeError(f"Unexpected SSE content type during install probe: {content_type}")

    event_name = None
    data_lines: list[str] = []
    endpoint_path = None
    initialize_result = None
    tools = None

    def read_event() -> tuple[str | None, str]:
        nonlocal event_name, data_lines
        while True:
            raw_line = response.readline()
            if raw_line == b"":
                raise RuntimeError("Legacy SSE upstream closed during install probe")
            line = raw_line.decode("utf-8").rstrip("\r\n")
            if line == "":
                if event_name is not None or data_lines:
                    payload = (event_name, "\n".join(data_lines))
                    event_name = None
                    data_lines = []
                    return payload
                continue
            if line.startswith("event:"):
                event_name = line.split(":", 1)[1].strip()
            elif line.startswith("data:"):
                data_lines.append(line.split(":", 1)[1].lstrip())

    event_name, data = read_event()
    if event_name != "endpoint":
        raise RuntimeError(f"Expected endpoint event during install probe, got {event_name!r}")
    endpoint_path = data
    post_url = urllib.parse.urljoin(upstream_url, endpoint_path)

    def post_json(payload: dict[str, Any]) -> None:
        body = json.dumps(payload).encode("utf-8")
        post_request = urllib.request.Request(
            post_url,
            data=body,
            headers={
                "Content-Type": "application/json",
                "Accept": "application/json, text/event-stream",
            },
            method="POST",
        )
        with urllib.request.urlopen(post_request, timeout=30, context=context) as post_response:  # noqa: S310
            if post_response.status not in {200, 202, 204}:
                raise RuntimeError(f"Install probe got HTTP {post_response.status} from legacy upstream")

    post_json(
        {
            "jsonrpc": "2.0",
            "id": 1,
            "method": "initialize",
            "params": {
                "protocolVersion": "2025-06-18",
                "capabilities": {},
                "clientInfo": {"name": "codex-legacy-mcp-bridge-installer", "version": "0"},
            },
        }
    )
    post_json({"jsonrpc": "2.0", "method": "notifications/initialized", "params": {}})
    post_json({"jsonrpc": "2.0", "id": 2, "method": "tools/list", "params": {}})

    while initialize_result is None or tools is None:
        event_name, data = read_event()
        if event_name != "message":
            continue
        payload = json.loads(data)
        if payload.get("id") == 1:
            initialize_result = payload["result"]
        elif payload.get("id") == 2:
            tools = payload["result"]["tools"]

    response.close()
    initialize_result["capabilities"] = {"tools": {"listChanged": True}}
    return initialize_result, tools


def ensure_plugin_link(repo_root: Path, plugin_path: Path, force: bool) -> None:
    plugin_path.parent.mkdir(parents=True, exist_ok=True)
    if plugin_path.exists() or plugin_path.is_symlink():
        resolved = plugin_path.resolve()
        if resolved == repo_root.resolve():
            return
        if not force:
            raise SystemExit(
                f"Plugin path already exists and points elsewhere: {plugin_path} -> {resolved}. "
                "Re-run with --force to replace it."
            )
        if plugin_path.is_symlink():
            plugin_path.unlink()
        else:
            raise SystemExit(
                f"Refusing to replace non-symlink plugin path automatically: {plugin_path}"
            )
    if repo_root.resolve() != plugin_path.resolve():
        plugin_path.symlink_to(repo_root.resolve(), target_is_directory=True)


def load_json(path: Path, default: Any) -> Any:
    if not path.exists():
        return default
    with path.open("r", encoding="utf-8") as handle:
        return json.load(handle)


def dump_json(path: Path, payload: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", encoding="utf-8") as handle:
        json.dump(payload, handle, indent=2)
        handle.write("\n")


def update_marketplace(plugin_name: str) -> Path:
    marketplace_path = Path.home() / ".agents" / "plugins" / "marketplace.json"
    data = load_json(
        marketplace_path,
        {
            "name": "local-codex-plugins",
            "interface": {"displayName": "Local Codex Plugins"},
            "plugins": [],
        },
    )
    if data.get("name", "").startswith("[TODO:"):
        data["name"] = "local-codex-plugins"
    interface = data.setdefault("interface", {})
    if interface.get("displayName", "").startswith("[TODO:"):
        interface["displayName"] = "Local Codex Plugins"
    plugins = data.setdefault("plugins", [])
    entry = {
        "name": plugin_name,
        "source": {"source": "local", "path": f"./plugins/{plugin_name}"},
        "policy": {"installation": "AVAILABLE", "authentication": "ON_INSTALL"},
        "category": "Productivity",
    }
    replaced = False
    for index, existing in enumerate(plugins):
        if existing.get("name") == plugin_name:
            plugins[index] = entry
            replaced = True
            break
    if not replaced:
        plugins.append(entry)
    dump_json(marketplace_path, data)
    return marketplace_path


def write_instance_config(server_id: str, display_name: str, upstream_url: str) -> Path:
    initialize_result, tools = _legacy_probe(upstream_url)
    config_path = Path.home() / ".codex" / "legacy-mcp-bridge" / f"{server_id}.json"
    dump_json(
        config_path,
        {
            "server_id": server_id,
            "display_name": display_name,
            "upstream_url": upstream_url,
            "connect_timeout_sec": 30,
            "read_timeout_sec": 300,
            "request_timeout_sec": 120,
            "cached_initialize_result": initialize_result,
            "cached_tools": tools,
        },
    )
    return config_path


def update_plugin_mcp_json(plugin_root: Path) -> Path:
    mcp_path = plugin_root / ".mcp.json"
    dump_json(
        mcp_path,
        {
            "mcpServers": {
                "legacy-sse-bridge": {
                    "note": (
                        "This plugin uses install.py to write the authoritative stdio MCP entry "
                        "into ~/.codex/config.toml. The bundled bridge script lives under "
                        "./scripts/legacy_sse_mcp_bridge.py."
                    )
                }
            }
        },
    )
    return mcp_path


def toml_block(server_id: str, script_path: Path, config_path: Path) -> str:
    begin = f"{MARKER_PREFIX}{server_id}"
    end = f"# END codex-legacy-mcp-bridge:{server_id}"
    args = json.dumps([str(script_path), "--config", str(config_path)])
    python_executable = json.dumps(sys.executable)
    lines = [
        begin,
        f"[mcp_servers.{server_id}]",
        f"command = {python_executable}",
        f"args = {args}",
        "startup_timeout_sec = 5",
        "tool_timeout_sec = 120",
        end,
        "",
    ]
    return "\n".join(lines)


def update_codex_config(server_id: str, script_path: Path, config_path: Path) -> Path:
    config_path_toml = Path.home() / ".codex" / "config.toml"
    config_path_toml.parent.mkdir(parents=True, exist_ok=True)
    existing = ""
    if config_path_toml.exists():
        existing = config_path_toml.read_text(encoding="utf-8")
    begin = f"{MARKER_PREFIX}{server_id}"
    end = f"# END codex-legacy-mcp-bridge:{server_id}"
    block = toml_block(server_id, script_path, config_path)

    if begin in existing and end in existing:
        start_index = existing.index(begin)
        end_index = existing.index(end) + len(end)
        updated = existing[:start_index] + block + existing[end_index:]
    else:
        if existing and not existing.endswith("\n"):
            existing += "\n"
        updated = existing + ("\n" if existing else "") + block
    config_path_toml.write_text(updated, encoding="utf-8")
    return config_path_toml


def main() -> None:
    args = parse_args()
    server_id = sanitize_server_id(args.server_id)
    repo_root = Path(__file__).resolve().parent
    stable_plugin_path = Path.home() / "plugins" / PLUGIN_NAME

    ensure_plugin_link(repo_root, stable_plugin_path, args.force)
    marketplace_path = update_marketplace(PLUGIN_NAME)
    instance_config_path = write_instance_config(server_id, args.display_name, args.upstream_url)
    plugin_mcp_path = update_plugin_mcp_json(stable_plugin_path)
    codex_config_path = update_codex_config(
        server_id,
        stable_plugin_path / "scripts" / "legacy_sse_mcp_bridge.py",
        instance_config_path,
    )

    print(f"Installed plugin link: {stable_plugin_path}")
    print(f"Updated marketplace: {marketplace_path}")
    print(f"Wrote instance config: {instance_config_path}")
    print(f"Updated plugin MCP file: {plugin_mcp_path}")
    print(f"Updated Codex config: {codex_config_path}")
    print("Restart Codex to pick up the new MCP server.")


if __name__ == "__main__":
    main()
