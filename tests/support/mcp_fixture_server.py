"""A protocol peer for timeout, cancellation, pagination and notification tests."""

import json
import sys
import time

behavior = sys.argv[1] if len(sys.argv) > 1 else "normal"
for line in sys.stdin:
    request = json.loads(line)
    if "id" not in request:
        continue
    method = request["method"]
    if method == "initialize":
        result = {
            "protocolVersion": "2024-11-05",
            "capabilities": {"tools": {}},
            "serverInfo": {"name": "fixture", "version": "1"},
        }
    elif method == "tools/list":
        second = request.get("params", {}).get("cursor") == "second"
        result = {
            "tools": [{"name": "second" if second else "first", "inputSchema": {"type": "object"}}]
        }
        if not second:
            result["nextCursor"] = "second"
    else:
        if behavior == "hang":
            time.sleep(60)
        if behavior == "malformed":
            print("not-json", flush=True)
            continue
        print(
            json.dumps(
                {"jsonrpc": "2.0", "method": "notifications/progress", "params": {"progress": 1}}
            ),
            flush=True,
        )
        result = {"content": [{"type": "text", "text": "fixture-ok"}]}
    print(json.dumps({"jsonrpc": "2.0", "id": request["id"], "result": result}), flush=True)
