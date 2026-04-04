#!/usr/bin/env python3
"""Bootstrap vnstock MCP server in GoClaw via HTTP API (idempotent)."""

from __future__ import annotations

import json
import os
import sys
import time
import urllib.error
import urllib.request
from typing import Any


def _bool_env(name: str, default: bool) -> bool:
    value = os.getenv(name)
    if value is None:
        return default
    return value.strip().lower() in {"1", "true", "yes", "on"}


def _first_owner_id(raw: str | None) -> str:
    if not raw:
        return "system"
    for part in raw.split(","):
        user_id = part.strip()
        if user_id:
            return user_id
    return "system"


def _request_json(
    method: str,
    url: str,
    headers: dict[str, str],
    body: dict[str, Any] | None = None,
    timeout: float = 10.0,
) -> dict[str, Any]:
    data = None
    req_headers = dict(headers)
    if body is not None:
        data = json.dumps(body).encode("utf-8")
        req_headers["Content-Type"] = "application/json"
    req = urllib.request.Request(url=url, data=data, method=method, headers=req_headers)
    with urllib.request.urlopen(req, timeout=timeout) as resp:
        raw = resp.read().decode("utf-8")
    if not raw.strip():
        return {}
    parsed = json.loads(raw)
    if isinstance(parsed, dict):
        return parsed
    return {"data": parsed}


def _wait_healthy(base_url: str, timeout_sec: int, headers: dict[str, str]) -> bool:
    deadline = time.time() + timeout_sec
    health_url = f"{base_url}/health"
    while time.time() < deadline:
        req = urllib.request.Request(url=health_url, method="GET", headers=headers)
        try:
            with urllib.request.urlopen(req, timeout=3.0):
                return True
        except Exception:
            time.sleep(1.0)
    return False


def main() -> int:
    if not _bool_env("ENABLE_VNSTOCK_MCP", False):
        print("vnstock bootstrap skipped: ENABLE_VNSTOCK_MCP is false")
        return 0
    if not _bool_env("GOCLAW_VNSTOCK_AUTO_BOOTSTRAP", True):
        print("vnstock bootstrap skipped: GOCLAW_VNSTOCK_AUTO_BOOTSTRAP is false")
        return 0

    api_key = os.getenv("VNSTOCK_API_KEY", "").strip()
    if not api_key:
        print("vnstock bootstrap skipped: VNSTOCK_API_KEY is empty")
        return 0

    host = os.getenv("GOCLAW_HOST", "127.0.0.1")
    port = os.getenv("GOCLAW_PORT", "18790")
    base_url = f"http://{host}:{port}"

    headers: dict[str, str] = {"Accept": "application/json"}
    gateway_token = os.getenv("GOCLAW_GATEWAY_TOKEN", "").strip()
    if gateway_token:
        headers["Authorization"] = f"Bearer {gateway_token}"
    user_id = os.getenv("GOCLAW_MCP_BOOTSTRAP_USER_ID", "").strip() or _first_owner_id(
        os.getenv("GOCLAW_OWNER_IDS")
    )
    headers["X-GoClaw-User-Id"] = user_id

    if not _wait_healthy(base_url, timeout_sec=120, headers=headers):
        print("vnstock bootstrap warning: GoClaw health check timeout", file=sys.stderr)
        return 0

    create_payload = {
        "name": "vnstock",
        "display_name": "vnstock",
        "transport": "stdio",
        "command": "/opt/vnstock-mcp/bin/python",
        "args": ["-m", "vnstock_agent.server"],
        "env": {
            "VNSTOCK_API_KEY": api_key,
            "FASTMCP_SHOW_SERVER_BANNER": "false",
            "FASTMCP_LOG_LEVEL": "ERROR",
        },
        "tool_prefix": "vnstock",
        "timeout_sec": 60,
        "enabled": True,
    }
    update_payload = {
        "transport": create_payload["transport"],
        "command": create_payload["command"],
        "args": create_payload["args"],
        "env": create_payload["env"],
        "tool_prefix": create_payload["tool_prefix"],
        "timeout_sec": create_payload["timeout_sec"],
        "enabled": create_payload["enabled"],
    }

    servers_resp = _request_json("GET", f"{base_url}/v1/mcp/servers", headers=headers)
    servers = servers_resp.get("servers", [])
    if not isinstance(servers, list):
        servers = []
    existing = next((s for s in servers if isinstance(s, dict) and s.get("name") == "vnstock"), None)

    try:
        if existing and existing.get("id"):
            server_id = str(existing["id"])
            _request_json("PUT", f"{base_url}/v1/mcp/servers/{server_id}", headers=headers, body=update_payload)
            print(f"vnstock bootstrap: updated MCP server id={server_id}")
        else:
            created = _request_json("POST", f"{base_url}/v1/mcp/servers", headers=headers, body=create_payload)
            server_id = created.get("id", "unknown")
            print(f"vnstock bootstrap: created MCP server id={server_id}")

        if server_id and server_id != "unknown":
            grant_payload = {"user_id": user_id}
            _request_json(
                "POST",
                f"{base_url}/v1/mcp/servers/{server_id}/grants/user",
                headers=headers,
                body=grant_payload,
            )
            print(f"vnstock bootstrap: granted user access user_id={user_id}")
    except urllib.error.HTTPError as e:
        body = e.read().decode("utf-8", errors="replace")
        print(f"vnstock bootstrap warning: HTTP {e.code} {body}", file=sys.stderr)
    except Exception as e:  # pragma: no cover - defensive startup hook
        print(f"vnstock bootstrap warning: {e}", file=sys.stderr)

    return 0


if __name__ == "__main__":
    raise SystemExit(main())
