"""Verify that an installed ``datamining-skill`` answers MCP requests over stdio.

Spawns ``<command> mcp --allow-dir <workspace>``, performs the legacy ``initialize``
handshake and the modern ``server/discover`` probe, lists the tools, profiles a tiny
generated CSV and mines it. Exits non-zero (with the reason on stderr) on any mismatch.

Usage::

    python scripts/mcp_smoke_test.py                      # uses the `datamining-skill` command
    python scripts/mcp_smoke_test.py python -m datamining_skill.cli

The workspace is created under ``./.scratch`` and removed afterwards.
"""

from __future__ import annotations

import json
import shutil
import subprocess
import sys
import uuid
from pathlib import Path
from typing import Any

MODERN_META = {
    "io.modelcontextprotocol/protocolVersion": "2026-07-28",
    "io.modelcontextprotocol/clientCapabilities": {},
}


def fail(message: str) -> None:
    print(f"FAIL: {message}", file=sys.stderr)
    raise SystemExit(1)


def expect(condition: bool, message: str) -> None:
    if not condition:
        fail(message)
    print(f"  ok  {message}")


def main() -> int:
    command = sys.argv[1:] or ["datamining-skill"]
    workspace = Path(".scratch") / f"mcp-smoke-{uuid.uuid4().hex[:8]}"
    workspace.mkdir(parents=True)
    try:
        (workspace / "contacts.csv").write_text(
            "id,note\n" + "".join(f"{i},mail emp{i}@internal.corp.test now\n" for i in range(500)),
            encoding="utf-8",
            newline="\n",
        )
        messages: list[dict[str, Any]] = [
            {"jsonrpc": "2.0", "id": 1, "method": "initialize",
             "params": {"protocolVersion": "2025-06-18", "capabilities": {},
                        "clientInfo": {"name": "smoke-test", "version": "1"}}},
            {"jsonrpc": "2.0", "method": "notifications/initialized"},
            {"jsonrpc": "2.0", "id": 2, "method": "tools/list"},
            {"jsonrpc": "2.0", "id": 3, "method": "server/discover", "params": {"_meta": MODERN_META}},
            {"jsonrpc": "2.0", "id": 4, "method": "tools/call",
             "params": {"name": "profile_dataset", "arguments": {"path": "contacts.csv"}}},
            {"jsonrpc": "2.0", "id": 5, "method": "tools/call",
             "params": {"name": "mine_dataset",
                        "arguments": {"path": "contacts.csv", "output_path": "found.csv"},
                        "_meta": {"progressToken": "smoke"}}},
        ]
        result = subprocess.run(
            [*command, "mcp", "--allow-dir", str(workspace)],
            input="\n".join(json.dumps(m) for m in messages) + "\n",
            capture_output=True,
            text=True,
            encoding="utf-8",
            check=False,
            timeout=120,
        )
        if result.returncode != 0:
            fail(f"server exited with {result.returncode}: {result.stderr.strip()[:500]}")
        try:
            replies = [json.loads(line) for line in result.stdout.splitlines()]
        except ValueError:
            fail("stdout contained something that is not JSON (the protocol channel must stay clean)")
        by_id = {r["id"]: r for r in replies if "id" in r}

        expect(by_id[1]["result"]["serverInfo"]["name"] == "datamining-skill", "initialize handshake")
        names = [t["name"] for t in by_id[2]["result"]["tools"]]
        expect(names == ["profile_dataset", "mine_dataset"], f"tools/list -> {names}")
        expect("2026-07-28" in by_id[3]["result"]["supportedVersions"], "server/discover (modern era)")
        expect(by_id[4]["result"]["structuredContent"]["records"]["count"] == 500, "profile_dataset")
        expect(by_id[5]["result"]["structuredContent"]["records_written"] == 500, "mine_dataset")
        expect(any(r.get("method") == "notifications/progress" for r in replies), "progress streamed")
        found = (workspace / "found.csv").read_text(encoding="utf-8").splitlines()
        expect(found == ["email", *[f"emp{i}@internal.corp.test" for i in range(500)]], "result file is exact")
        print("MCP smoke test passed")
        return 0
    finally:
        shutil.rmtree(workspace, ignore_errors=True)
        try:
            workspace.parent.rmdir()
        except OSError:
            pass


if __name__ == "__main__":
    sys.exit(main())
