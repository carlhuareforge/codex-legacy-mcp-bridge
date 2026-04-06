# Legacy SSE MCP Bridge Implementation Plan

> **For Claude:** REQUIRED SUB-SKILL: Use superpowers:executing-plans to implement this plan task-by-task.

**Goal:** Build a reusable Codex plugin that bridges legacy SSE MCP servers into a local stdio MCP server.

**Architecture:** A Python stdio MCP proxy opens a legacy SSE session upstream, discovers the session-specific message endpoint, forwards JSON-RPC requests upstream, and relays upstream `event: message` JSON-RPC responses back to Codex over stdio framing. An installer registers the plugin and writes the documented `mcp_servers.<id>.command` config.

**Tech Stack:** Python 3 standard library, Codex `config.toml`, local plugin marketplace metadata

---

1. Scaffold plugin structure and fill plugin metadata.
2. Implement stdio framing helpers.
3. Implement legacy SSE session discovery and message forwarding.
4. Implement generic proxy behavior and error handling.
5. Implement installer and uninstaller.
6. Run live integration tests against `https://ava.reforge.vc/sse/`.
7. Register local MCP entry and verify discovery.
8. Initialize git and publish the repo.

