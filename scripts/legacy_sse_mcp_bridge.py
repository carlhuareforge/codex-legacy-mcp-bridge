#!/usr/bin/env python3
"""Bridge legacy SSE-style MCP servers into a local stdio MCP server."""

from __future__ import annotations

import argparse
import importlib.util
import json
import logging
import queue
import ssl
import sys
import threading
import traceback
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path
from typing import Any


LOGGER = logging.getLogger("legacy_sse_mcp_bridge")


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Bridge a legacy SSE MCP server over stdio.")
    parser.add_argument("--config", required=True, help="Path to bridge config JSON.")
    return parser.parse_args()


def load_config(path: str) -> dict[str, Any]:
    with Path(path).open("r", encoding="utf-8") as handle:
        payload = json.load(handle)
    payload.setdefault("connect_timeout_sec", 30)
    payload.setdefault("read_timeout_sec", 300)
    payload.setdefault("request_timeout_sec", 120)
    payload.setdefault("cached_initialize_result", None)
    payload.setdefault("cached_tools", None)
    return payload


def build_ssl_context() -> ssl.SSLContext:
    """Build a validating SSL context that works on stock macOS Python installs."""
    certifi_spec = importlib.util.find_spec("certifi")
    if certifi_spec is not None:
        import certifi  # type: ignore

        return ssl.create_default_context(cafile=certifi.where())

    for path in ("/etc/ssl/cert.pem", "/private/etc/ssl/cert.pem"):
        candidate = Path(path)
        if candidate.exists():
            return ssl.create_default_context(cafile=str(candidate))

    return ssl.create_default_context()


class StdioJsonRpcWriter:
    def __init__(self) -> None:
        self._lock = threading.Lock()

    def send(self, payload: dict[str, Any]) -> None:
        encoded = json.dumps(payload, ensure_ascii=False, separators=(",", ":")).encode("utf-8")
        message = f"Content-Length: {len(encoded)}\r\n\r\n".encode("ascii") + encoded
        with self._lock:
            sys.stdout.buffer.write(message)
            sys.stdout.buffer.flush()


def read_stdio_message() -> dict[str, Any] | None:
    headers: dict[str, str] = {}
    while True:
        line = sys.stdin.buffer.readline()
        if not line:
            return None
        if line in (b"\r\n", b"\n"):
            break
        decoded = line.decode("ascii").strip()
        if ":" not in decoded:
            continue
        key, value = decoded.split(":", 1)
        headers[key.lower().strip()] = value.strip()
    length_text = headers.get("content-length")
    if not length_text:
        raise ValueError("Missing Content-Length header")
    body = sys.stdin.buffer.read(int(length_text))
    return json.loads(body.decode("utf-8"))


def make_error_response(message: dict[str, Any], error_message: str, code: int = -32000) -> dict[str, Any]:
    return {
        "jsonrpc": "2.0",
        "id": message.get("id"),
        "error": {"code": code, "message": error_message},
    }


class LegacySSESession:
    def __init__(self, config: dict[str, Any], writer: StdioJsonRpcWriter) -> None:
        self.upstream_url = config["upstream_url"]
        self.connect_timeout_sec = float(config["connect_timeout_sec"])
        self.read_timeout_sec = float(config["read_timeout_sec"])
        self.request_timeout_sec = float(config["request_timeout_sec"])
        self.writer = writer
        self.ssl_context = build_ssl_context()
        self.cached_initialize_result = config.get("cached_initialize_result")
        self.cached_tools = config.get("cached_tools")

        self._lock = threading.Lock()
        self._endpoint_ready = threading.Condition(self._lock)
        self._response = None
        self._reader_thread: threading.Thread | None = None
        self._messages_url: str | None = None
        self._reader_error: str | None = None
        self._closed = False
        self._initialize_request: dict[str, Any] | None = None
        self._initialized_notification: dict[str, Any] | None = None
        self._suppressed_response_ids: set[Any] = set()

    def remember_handshake(self, message: dict[str, Any]) -> None:
        method = message.get("method")
        if method == "initialize":
            self._initialize_request = message
        elif method == "notifications/initialized":
            self._initialized_notification = message

    def make_initialize_response(self, request: dict[str, Any]) -> dict[str, Any]:
        cached = dict(self.cached_initialize_result or {})
        requested_version = (
            request.get("params", {}).get("protocolVersion") or cached.get("protocolVersion") or "2025-06-18"
        )
        cached["protocolVersion"] = requested_version
        cached["capabilities"] = {"tools": {"listChanged": True}}
        if "serverInfo" not in cached:
            cached["serverInfo"] = {"name": "Legacy SSE MCP Bridge", "version": "0.1.0"}
        return {"jsonrpc": "2.0", "id": request.get("id"), "result": cached}

    def make_tools_list_response(self, request: dict[str, Any]) -> dict[str, Any] | None:
        if self.cached_tools is None:
            return None
        return {"jsonrpc": "2.0", "id": request.get("id"), "result": {"tools": self.cached_tools}}

    @staticmethod
    def make_empty_response(request: dict[str, Any]) -> dict[str, Any] | None:
        method = request.get("method")
        request_id = request.get("id")
        if request_id is None:
            return None
        if method == "ping":
            return {"jsonrpc": "2.0", "id": request_id, "result": {}}
        if method == "prompts/list":
            return {"jsonrpc": "2.0", "id": request_id, "result": {"prompts": []}}
        if method == "resources/list":
            return {"jsonrpc": "2.0", "id": request_id, "result": {"resources": []}}
        if method == "resources/templates/list":
            return {"jsonrpc": "2.0", "id": request_id, "result": {"resourceTemplates": []}}
        return None

    def close(self) -> None:
        with self._lock:
            self._closed = True
            try:
                if self._response is not None:
                    self._response.close()
            finally:
                self._response = None
                self._endpoint_ready.notify_all()

    def _dispatch_sse_event(self, event: str | None, data: str) -> None:
        if event == "endpoint":
            with self._endpoint_ready:
                self._messages_url = urllib.parse.urljoin(self.upstream_url, data)
                self._endpoint_ready.notify_all()
            LOGGER.info("Discovered legacy MCP message endpoint: %s", self._messages_url)
            return

        if event == "message":
            try:
                payload = json.loads(data)
            except json.JSONDecodeError:
                LOGGER.warning("Skipping non-JSON SSE message: %s", data[:400])
                return

            response_id = payload.get("id")
            with self._lock:
                if response_id in self._suppressed_response_ids:
                    self._suppressed_response_ids.remove(response_id)
                    LOGGER.debug("Suppressed replayed response id=%s", response_id)
                    return
            self.writer.send(payload)
            return

        LOGGER.debug("Ignoring SSE event %r", event)

    def _reader_loop(self) -> None:
        assert self._response is not None
        event_name: str | None = None
        data_lines: list[str] = []
        try:
            while not self._closed:
                raw_line = self._response.readline()
                if raw_line == b"":
                    raise RuntimeError("Legacy SSE upstream closed the connection")
                line = raw_line.decode("utf-8").rstrip("\r\n")
                if line == "":
                    if event_name is not None or data_lines:
                        self._dispatch_sse_event(event_name, "\n".join(data_lines))
                    event_name = None
                    data_lines = []
                    continue
                if line.startswith("event:"):
                    event_name = line.split(":", 1)[1].strip()
                elif line.startswith("data:"):
                    data_lines.append(line.split(":", 1)[1].lstrip())
        except Exception as exc:  # noqa: BLE001
            if not self._closed:
                self._reader_error = f"{type(exc).__name__}: {exc}"
                LOGGER.warning("Legacy SSE reader stopped: %s", self._reader_error)
                with self._endpoint_ready:
                    self._messages_url = None
                    self._endpoint_ready.notify_all()
        finally:
            with self._lock:
                if self._response is not None:
                    try:
                        self._response.close()
                    except Exception:  # noqa: BLE001
                        pass
                self._response = None

    def _open_connection(self) -> None:
        request = urllib.request.Request(
            self.upstream_url,
            headers={"Accept": "text/event-stream"},
            method="GET",
        )
        LOGGER.info("Opening legacy SSE MCP session: %s", self.upstream_url)
        response = urllib.request.urlopen(  # noqa: S310
            request,
            timeout=self.read_timeout_sec,
            context=self.ssl_context,
        )
        content_type = response.headers.get("Content-Type", "")
        if "text/event-stream" not in content_type:
            response.close()
            raise RuntimeError(f"Unexpected content type from legacy SSE server: {content_type}")
        with self._lock:
            self._response = response
            self._messages_url = None
            self._reader_error = None
            self._reader_thread = threading.Thread(target=self._reader_loop, daemon=True)
            self._reader_thread.start()

    def ensure_connection(self) -> bool:
        with self._lock:
            if self._closed:
                raise RuntimeError("Bridge session is already closed")
            needs_open = self._response is None or self._messages_url is None

        if needs_open:
            self._open_connection()

        with self._endpoint_ready:
            if self._messages_url is None:
                self._endpoint_ready.wait(timeout=self.connect_timeout_sec)
            if self._messages_url is None:
                raise RuntimeError(
                    self._reader_error or "Timed out waiting for legacy SSE endpoint discovery"
                )
        return needs_open

    def _post_json(self, payload: dict[str, Any], suppress_response: bool = False) -> None:
        if suppress_response and "id" in payload:
            with self._lock:
                self._suppressed_response_ids.add(payload["id"])
        body = json.dumps(payload).encode("utf-8")
        request = urllib.request.Request(
            self._messages_url,
            data=body,
            headers={
                "Content-Type": "application/json",
                "Accept": "application/json, text/event-stream",
            },
            method="POST",
        )
        with urllib.request.urlopen(  # noqa: S310
            request,
            timeout=self.request_timeout_sec,
            context=self.ssl_context,
        ) as response:
            if response.status not in {200, 202, 204}:
                raise RuntimeError(f"Legacy SSE upstream returned HTTP {response.status}")

    def _replay_handshake_if_needed(self, current_message: dict[str, Any]) -> None:
        method = current_message.get("method")
        if method == "initialize":
            return
        if self._initialize_request is None:
            raise RuntimeError("Received non-initialize request before initialize handshake")
        LOGGER.info("Replaying initialize handshake for reconnected legacy SSE session")
        self._post_json(self._initialize_request, suppress_response=True)
        if self._initialized_notification is not None:
            self._post_json(self._initialized_notification, suppress_response=False)

    def send(self, message: dict[str, Any]) -> None:
        reopened = self.ensure_connection()
        if reopened:
            self._replay_handshake_if_needed(message)
        self._post_json(message, suppress_response=False)


def configure_logging(config: dict[str, Any]) -> None:
    level_name = str(config.get("log_level", "INFO")).upper()
    logging.basicConfig(
        level=getattr(logging, level_name, logging.INFO),
        stream=sys.stderr,
        format="[legacy-sse-mcp-bridge] %(levelname)s %(message)s",
    )


def main() -> int:
    args = parse_args()
    config = load_config(args.config)
    configure_logging(config)
    writer = StdioJsonRpcWriter()
    session = LegacySSESession(config, writer)

    try:
        while True:
            message = read_stdio_message()
            if message is None:
                LOGGER.info("stdin closed; shutting down bridge")
                break

            session.remember_handshake(message)
            method = message.get("method")
            if method == "exit":
                LOGGER.info("Received exit notification")
                break
            if method == "initialize":
                writer.send(session.make_initialize_response(message))
                continue
            if method == "tools/list":
                cached_response = session.make_tools_list_response(message)
                if cached_response is not None:
                    writer.send(cached_response)
                    continue
            empty_response = session.make_empty_response(message)
            if empty_response is not None:
                writer.send(empty_response)
                continue

            try:
                session.send(message)
            except urllib.error.HTTPError as exc:
                body = exc.read().decode("utf-8", errors="replace")
                error_message = f"Legacy SSE upstream HTTP {exc.code}: {body[:500]}"
                LOGGER.error(error_message)
                if "id" in message:
                    writer.send(make_error_response(message, error_message))
            except Exception as exc:  # noqa: BLE001
                error_message = f"{type(exc).__name__}: {exc}"
                LOGGER.error("Bridge request failed: %s", error_message)
                LOGGER.debug(traceback.format_exc())
                if "id" in message:
                    writer.send(make_error_response(message, error_message))
    finally:
        session.close()

    return 0


if __name__ == "__main__":
    raise SystemExit(main())
