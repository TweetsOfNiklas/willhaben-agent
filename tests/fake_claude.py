"""Stand-in for the `claude` CLI: echoes argv/stdin back as structured_output."""
import json
import os
import sys

stdin = sys.stdin.read()
if os.environ.get("FAKE_CLAUDE_ERROR"):
    print(json.dumps({"type": "result", "subtype": "success", "is_error": True,
                      "result": "Failed to authenticate: OAuth session expired"}))
    sys.exit(1)
print(json.dumps({
    "type": "result", "subtype": "success", "is_error": False, "duration_ms": 12, "total_cost_usd": 0.0,
    "modelUsage": {"claude-opus-5-5": {}},
    "structured_output": {"argv": sys.argv[1:], "stdin": stdin, "cwd_files": sorted(os.listdir(".")),
                          "has_token": bool(os.environ.get("CLAUDE_CODE_OAUTH_TOKEN")),
                          "has_api_key": bool(os.environ.get("ANTHROPIC_API_KEY"))},
}))
