#!/usr/bin/env python3
"""browser-mcp: stdio MCP server exposing the agentos system browser as one tool.

    browse(url, mode="text"|"links"|"screenshot")

Plain JSON-RPC 2.0 over stdio (newline-delimited), no SDK. Each call shells out
to `agentos-browse`, so visits are logged to visits.jsonl exactly as from the CLI.

Register for Claude Code with:
    claude mcp add --scope user agent-browser -- python3 ~/resident/browser/browser-mcp.py
"""
import base64
import json
import os
import subprocess
import sys

CLI = os.path.join(os.path.dirname(os.path.abspath(__file__)), "agentos-browse")
MODES = ("text", "links", "screenshot")

TOOLS = [{
    "name": "browse",
    "description": (
        "Open a URL in the machine's system browser (headless Chrome, persistent "
        "profile with saved logins, CDP on 127.0.0.1:9222) and return the page's "
        "main text (default), its links, or a screenshot. Every visit is logged "
        "to ~/agentos/browser/visits.jsonl."
    ),
    "inputSchema": {
        "type": "object",
        "properties": {
            "url": {"type": "string", "description": "Absolute URL (https:// assumed if no scheme)."},
            "mode": {"type": "string", "enum": list(MODES), "default": "text",
                     "description": "text = title, final URL and main text (<=8000 chars); "
                                    "links = up to 50 'text -> href' lines; screenshot = PNG."},
        },
        "required": ["url"],
    },
}]


def run_browse(url: str, mode: str) -> dict:
    if mode not in MODES:
        return {"content": [{"type": "text", "text": f"unknown mode {mode!r}; use one of {MODES}"}], "isError": True}
    args = [CLI, url] + ([] if mode == "text" else [f"--{mode}"])
    try:
        p = subprocess.run(args, capture_output=True, text=True, timeout=90)
    except subprocess.TimeoutExpired:
        return {"content": [{"type": "text", "text": f"agentos-browse timed out on {url}"}], "isError": True}
    if p.returncode != 0:
        return {"content": [{"type": "text", "text": (p.stderr or p.stdout).strip() or f"exit {p.returncode}"}], "isError": True}
    out = p.stdout.strip()
    if mode == "screenshot" and os.path.isfile(out):
        with open(out, "rb") as f:
            data = base64.b64encode(f.read()).decode()
        return {"content": [{"type": "text", "text": f"screenshot saved: {out}"},
                            {"type": "image", "data": data, "mimeType": "image/png"}]}
    return {"content": [{"type": "text", "text": out}]}


def reply(id_, result=None, error=None):
    msg = {"jsonrpc": "2.0", "id": id_}
    if error is not None:
        msg["error"] = error
    else:
        msg["result"] = result
    sys.stdout.write(json.dumps(msg) + "\n")
    sys.stdout.flush()


def main():
    for line in sys.stdin:
        line = line.strip()
        if not line:
            continue
        try:
            req = json.loads(line)
        except json.JSONDecodeError:
            reply(None, error={"code": -32700, "message": "parse error"})
            continue
        method, id_, params = req.get("method"), req.get("id"), req.get("params") or {}
        if id_ is None:              # notification (e.g. notifications/initialized): no reply
            continue
        if method == "initialize":
            reply(id_, {
                "protocolVersion": params.get("protocolVersion", "2025-06-18"),
                "capabilities": {"tools": {}},
                "serverInfo": {"name": "agent-browser", "version": "0.1.0"},
            })
        elif method == "ping":
            reply(id_, {})
        elif method == "tools/list":
            reply(id_, {"tools": TOOLS})
        elif method == "tools/call":
            name, a = params.get("name"), params.get("arguments") or {}
            if name != "browse":
                reply(id_, error={"code": -32602, "message": f"unknown tool {name!r}"})
                continue
            url = a.get("url")
            if not url:
                reply(id_, {"content": [{"type": "text", "text": "url is required"}], "isError": True})
                continue
            reply(id_, run_browse(url, a.get("mode") or "text"))
        else:
            reply(id_, error={"code": -32601, "message": f"method not found: {method}"})


if __name__ == "__main__":
    main()
