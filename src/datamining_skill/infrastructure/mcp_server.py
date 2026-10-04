"""Stdio MCP server speaking newline-delimited JSON-RPC 2.0, standard library only.

Serves both protocol generations: legacy clients (2024-11-05 to 2025-11-25) negotiate with
``initialize``; modern clients (2026-07-28) are stateless, send their version and capabilities
in ``params._meta`` and use ``server/discover`` (an unsupported version gets -32022).
Only protocol messages go to stdout; logs and stray ``print`` output go to stderr.

Unsupported by design: JSON-RPC batches, server-to-client requests and mid-call cancellation
(requests run one at a time). Killing the process is safe because mining resumes from its last
checkpoint. See docs/architecture.md, "MCP server".
"""

from __future__ import annotations

import io
import json
import logging
import sys
from collections.abc import Callable
from dataclasses import dataclass
from typing import Any, TextIO

from datamining_skill.infrastructure.mcp_tools import (
    MiningTools,
    ToolOutcome,
    UnknownToolError,
)

MODERN_VERSION = "2026-07-28"
LEGACY_VERSIONS = ("2025-11-25", "2025-06-18", "2025-03-26", "2024-11-05")
SUPPORTED_VERSIONS = (MODERN_VERSION, *LEGACY_VERSIONS)
# Structured tool results exist from revision 2025-06-18 onwards.
_STRUCTURED_CONTENT_FROM = "2025-06-18"

META_PROTOCOL_VERSION = "io.modelcontextprotocol/protocolVersion"
META_CLIENT_CAPABILITIES = "io.modelcontextprotocol/clientCapabilities"
META_SERVER_INFO = "io.modelcontextprotocol/serverInfo"

PARSE_ERROR = -32700
INVALID_REQUEST = -32600
METHOD_NOT_FOUND = -32601
INVALID_PARAMS = -32602
INTERNAL_ERROR = -32603
UNSUPPORTED_PROTOCOL_VERSION = -32022

DEFAULT_MAX_MESSAGE_CHARS = 1_048_576
_TOO_LARGE = object()

Send = Callable[[dict[str, Any]], None]


class JsonRpcError(Exception):
    """A protocol-level failure, rendered as a JSON-RPC error response."""

    def __init__(self, code: int, message: str, data: Any = None) -> None:
        super().__init__(message)
        self.code = code
        self.message = message
        self.data = data


@dataclass(frozen=True, slots=True)
class ServerIdentity:
    """Name, display title and version reported to clients."""

    name: str = "datamining-skill"
    title: str = "DataMining Skill"
    version: str = "0.0.0"

    def to_dict(self) -> dict[str, str]:
        return {"name": self.name, "title": self.title, "version": self.version}


INSTRUCTIONS = (
    "Local-only data mining. Use profile_dataset to inspect a large CSV, JSONL or log file, "
    "then mine_dataset to extract data from it into a CSV or JSONL file. Files must be inside "
    "the directories this server was started with. Interrupted runs resume automatically when "
    "mine_dataset is called again with the same arguments."
)


class McpServer:
    """JSON-RPC 2.0 / MCP handler: ``serve`` runs over any pair of text streams, ``serve_stdio`` over the process's."""

    def __init__(
        self,
        tools: MiningTools,
        *,
        identity: ServerIdentity | None = None,
        logger: logging.Logger | None = None,
        max_message_chars: int = DEFAULT_MAX_MESSAGE_CHARS,
    ) -> None:
        self._tools = tools
        self._identity = identity or ServerIdentity()
        self._logger = logger or logging.getLogger(__name__)
        self._max_chars = max_message_chars
        self._legacy_version: str | None = None  # set by `initialize`

    def serve(self, stdin: TextIO, stdout: TextIO) -> None:
        """Process messages until ``stdin`` reaches end of file."""

        def send(message: dict[str, Any]) -> None:
            # ASCII-only output is independent of the platform encoding; compact separators
            # keep every message on one line
            stdout.write(json.dumps(message, ensure_ascii=True, separators=(",", ":")) + "\n")
            stdout.flush()

        while True:
            line = self._read_line(stdin)
            if line is None:
                return
            if line is _TOO_LARGE:
                send(_error_response(None, INVALID_REQUEST, "Message too large"))
                continue
            assert isinstance(line, str)
            if line.strip():
                self.handle_line(line, send)

    def handle_line(self, line: str, send: Send) -> None:
        """Handle one raw message, sending any response or notifications."""
        try:
            message = json.loads(line.lstrip("﻿"))  # tolerate a stray UTF-8 BOM
        except (ValueError, RecursionError):
            send(_error_response(None, PARSE_ERROR, "Parse error"))
            return
        if isinstance(message, list):
            send(_error_response(None, INVALID_REQUEST, "Batch requests are not supported"))
            return
        if not isinstance(message, dict):
            send(_error_response(None, INVALID_REQUEST, "Invalid Request"))
            return
        if "method" not in message:
            if "result" in message or "error" in message:
                return  # a response to something we never asked; ignore it
            send(_error_response(_valid_id(message), INVALID_REQUEST, "Invalid Request"))
            return

        has_id = "id" in message
        request_id = message.get("id")
        if has_id and (isinstance(request_id, bool) or not isinstance(request_id, (str, int))):
            send(_error_response(None, INVALID_REQUEST, "Invalid Request: id must be a string or integer"))
            return
        method = message.get("method")
        params = message.get("params", {})
        if message.get("jsonrpc") != "2.0" or not isinstance(method, str):
            if has_id:
                send(_error_response(request_id, INVALID_REQUEST, "Invalid Request"))
            return
        if params is None:
            params = {}
        if not isinstance(params, dict):
            if has_id:
                send(_error_response(request_id, INVALID_PARAMS, "params must be an object"))
            return

        if not has_id:
            self._notification(method, params)
            return
        try:
            response = self._request(method, params, send)
        except JsonRpcError as exc:
            send(_error_response(request_id, exc.code, exc.message, exc.data))
        except Exception:  # noqa: BLE001 - a handler bug must not kill the loop
            self._logger.exception("unhandled error in %s", method)
            send(_error_response(request_id, INTERNAL_ERROR, "Internal error"))
        else:
            send({"jsonrpc": "2.0", "id": request_id, "result": response})

    def _read_line(self, stream: TextIO) -> str | object | None:
        """Read one line, size-bounded so a hostile peer cannot exhaust memory."""
        line = stream.readline(self._max_chars + 1)
        if line == "":
            return None
        if len(line) > self._max_chars and not line.endswith("\n"):
            while True:  # discard the rest of the oversized message piecewise
                rest = stream.readline(65_536)
                if rest == "" or rest.endswith("\n"):
                    break
            return _TOO_LARGE
        return line

    def _notification(self, method: str, params: dict[str, Any]) -> None:
        if method == "notifications/cancelled":
            self._logger.info("cancellation requested for a call that has already finished")
        # notifications/initialized and unknown ones need no action or reply

    def _request(self, method: str, params: dict[str, Any], send: Send) -> dict[str, Any]:
        meta = params.get("_meta")
        if meta is not None and not isinstance(meta, dict):
            raise JsonRpcError(INVALID_PARAMS, "_meta must be an object")
        modern = isinstance(meta, dict) and META_PROTOCOL_VERSION in meta

        if method == "initialize":
            return self._initialize(params)
        if method == "server/discover":
            self._require_modern(meta)
            return self._decorate(self._discover_result(), modern=True)
        if method == "ping" and not modern:
            return {}
        if method not in ("tools/list", "tools/call"):
            raise JsonRpcError(METHOD_NOT_FOUND, f"Method not found: {method}")

        if modern:
            self._require_modern(meta)
        elif self._legacy_version is None:
            raise JsonRpcError(
                INVALID_PARAMS,
                f"missing '_meta' field '{META_PROTOCOL_VERSION}' (or send 'initialize' first)",
            )

        if method == "tools/list":
            listing: dict[str, Any] = {"tools": [tool.to_dict() for tool in self._tools.definitions()]}
            if modern:
                # static for the process lifetime; "private" keeps shared caches out
                listing.update({"ttlMs": 300_000, "cacheScope": "private"})
            return self._decorate(listing, modern=modern)
        return self._decorate(self._call_tool(params, meta, send, modern=modern), modern=modern)

    def _initialize(self, params: dict[str, Any]) -> dict[str, Any]:
        requested = params.get("protocolVersion")
        if not isinstance(requested, str):
            raise JsonRpcError(INVALID_PARAMS, "protocolVersion must be a string")
        # echo a supported revision, else propose the newest legacy one
        negotiated = requested if requested in LEGACY_VERSIONS else LEGACY_VERSIONS[0]
        self._legacy_version = negotiated
        return {
            "protocolVersion": negotiated,
            "capabilities": {"tools": {"listChanged": False}},
            "serverInfo": self._identity.to_dict(),
            "instructions": INSTRUCTIONS,
        }

    def _require_modern(self, meta: Any) -> None:
        """Validate the per-request ``_meta`` of a modern request."""
        if not isinstance(meta, dict) or META_PROTOCOL_VERSION not in meta:
            raise JsonRpcError(INVALID_PARAMS, f"missing required _meta field '{META_PROTOCOL_VERSION}'")
        version = meta[META_PROTOCOL_VERSION]
        if not isinstance(version, str):
            raise JsonRpcError(INVALID_PARAMS, f"'{META_PROTOCOL_VERSION}' must be a string")
        if version != MODERN_VERSION:
            raise JsonRpcError(
                UNSUPPORTED_PROTOCOL_VERSION,
                "Unsupported protocol version",
                {"supported": list(SUPPORTED_VERSIONS), "requested": version},
            )
        if not isinstance(meta.get(META_CLIENT_CAPABILITIES), dict):
            raise JsonRpcError(
                INVALID_PARAMS, f"missing required _meta field '{META_CLIENT_CAPABILITIES}'"
            )

    def _discover_result(self) -> dict[str, Any]:
        return {
            "supportedVersions": list(SUPPORTED_VERSIONS),
            "capabilities": {"tools": {}},
            "instructions": INSTRUCTIONS,
            "ttlMs": 300_000,
            "cacheScope": "private",
        }

    def _decorate(self, result: dict[str, Any], *, modern: bool) -> dict[str, Any]:
        """Add the fields only the modern revision defines."""
        if not modern:
            return result
        meta = dict(result.get("_meta", {}))
        meta[META_SERVER_INFO] = {"name": self._identity.name, "version": self._identity.version}
        return {"resultType": "complete", **result, "_meta": meta}

    def _call_tool(
        self, params: dict[str, Any], meta: Any, send: Send, *, modern: bool
    ) -> dict[str, Any]:
        name = params.get("name")
        arguments = params.get("arguments", {})
        if not isinstance(name, str):
            raise JsonRpcError(INVALID_PARAMS, "name must be a string")
        if arguments is None:
            arguments = {}
        if not isinstance(arguments, dict):
            raise JsonRpcError(INVALID_PARAMS, "arguments must be an object")

        token = meta.get("progressToken") if isinstance(meta, dict) else None
        if isinstance(token, bool) or not isinstance(token, (str, int)):
            token = None
        last_progress = -1

        def progress(done: int, total: int, message: str) -> None:
            nonlocal last_progress
            if token is None or done <= last_progress:
                return  # progress must strictly increase
            last_progress = done
            send(
                {
                    "jsonrpc": "2.0",
                    "method": "notifications/progress",
                    "params": {
                        "progressToken": token,
                        "progress": done,
                        "total": total,
                        "message": message,
                    },
                }
            )

        try:
            outcome = self._tools.call(name, arguments, progress)
        except UnknownToolError as exc:
            raise JsonRpcError(INVALID_PARAMS, str(exc)) from exc
        structured_ok = modern or (
            self._legacy_version is not None and self._legacy_version >= _STRUCTURED_CONTENT_FROM
        )
        return _tool_result(outcome, structured=structured_ok)


def _tool_result(outcome: ToolOutcome, *, structured: bool) -> dict[str, Any]:
    if outcome.error is not None:
        return {"content": [{"type": "text", "text": outcome.error}], "isError": True}
    assert outcome.data is not None
    tool_result: dict[str, Any] = {
        "content": [{"type": "text", "text": json.dumps(outcome.data, indent=2)}],
        "isError": False,
    }
    if structured:
        tool_result["structuredContent"] = outcome.data
    return tool_result


def _valid_id(message: dict[str, Any]) -> str | int | None:
    request_id = message.get("id")
    if isinstance(request_id, bool) or not isinstance(request_id, (str, int)):
        return None
    return request_id


def _error_response(
    request_id: str | int | None, code: int, message: str, data: Any = None
) -> dict[str, Any]:
    error: dict[str, Any] = {"code": code, "message": message}
    if data is not None:
        error["data"] = data
    return {"jsonrpc": "2.0", "id": request_id, "error": error}


def serve_stdio(server: McpServer) -> None:
    """Run ``server`` on the real stdin/stdout, switched to UTF-8 with ``\\n`` line endings.

    ``sys.stdout`` points at stderr meanwhile, so a stray ``print`` cannot corrupt the protocol.
    """
    protocol_in, protocol_out = sys.stdin, sys.stdout
    if isinstance(protocol_in, io.TextIOWrapper):
        protocol_in.reconfigure(encoding="utf-8", errors="replace", newline="\n")
    if isinstance(protocol_out, io.TextIOWrapper):
        protocol_out.reconfigure(encoding="utf-8", newline="\n", line_buffering=True)
    sys.stdout = sys.stderr
    try:
        server.serve(protocol_in, protocol_out)
    finally:
        sys.stdout = protocol_out
