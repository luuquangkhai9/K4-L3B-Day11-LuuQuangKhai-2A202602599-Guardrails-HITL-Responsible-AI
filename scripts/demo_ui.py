"""Local read-only lab dashboard with an optional live Blue request.

Run from the repo root: python scripts/demo_ui.py
The server binds to 127.0.0.1 and never serves .env or protected files.
"""
from __future__ import annotations

import argparse
import asyncio
import json
import sys
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
SRC = ROOT / "src"
UI = ROOT / "demo"
if str(SRC) not in sys.path:
    sys.path.insert(0, str(SRC))

from guardrails.input_guardrails import detect_injection, topic_filter
from guardrails.output_guardrails import content_filter


def read_json(name: str) -> dict:
    path = ROOT / "outputs" / name
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}


def clean_row(row: dict) -> dict:
    """Keep useful evidence while hiding synthetic credentials in the UI."""
    return {
        "id": row.get("id"),
        "category": row.get("category", ""),
        "input": row.get("input", ""),
        "response_preview": content_filter(row.get("response_preview") or "")["redacted"],
        "leaked": bool(row.get("leaked")),
        "blocked": bool(row.get("blocked")),
        "layer": row.get("layer"),
        "blocked_at": row.get("blocked_at", ""),
    }


def overview() -> dict:
    results = read_json("results.json")
    attacks = read_json("attack_results.json")
    metrics = read_json("metrics.json")
    grade = read_json("grade_report.json")
    return {
        "ready": bool(results and attacks),
        "results": {
            "safe_queries": [clean_row(x) for x in results.get("safe_queries", [])],
            "attack_queries": [clean_row(x) for x in results.get("attack_queries", [])],
            "edge_cases": [clean_row(x) for x in results.get("edge_cases", [])],
            "rate_limit": results.get("rate_limit", {}),
        },
        "attacks": {
            "red": [clean_row(x) for x in attacks.get("unsafe_attacks", [])],
            "advance": [clean_row(x) for x in attacks.get("guards_attacks", [])],
            "provider": attacks.get("llm_provider", "—"),
            "model": attacks.get("llm_model", "—"),
        },
        "metrics": metrics,
        "grade": {
            "technical_failure": grade.get("technical_failure"),
            "schema_ok": grade.get("results_schema", {}).get("ok"),
            "packaging_ok": grade.get("packaging", {}).get("ok"),
            "public_tests": grade.get("public_tests", {}).get("stdout", "").strip(),
        },
    }


class Handler(BaseHTTPRequestHandler):
    def send_bytes(self, data: bytes, content_type: str, status: int = 200) -> None:
        self.send_response(status)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(data)))
        self.send_header("Cache-Control", "no-store")
        self.send_header("X-Content-Type-Options", "nosniff")
        self.end_headers()
        self.wfile.write(data)

    def send_json(self, payload: dict, status: int = 200) -> None:
        self.send_bytes(json.dumps(payload, ensure_ascii=False).encode("utf-8"), "application/json; charset=utf-8", status)

    def do_GET(self) -> None:
        routes = {
            "/": ("index.html", "text/html; charset=utf-8"),
            "/app.css": ("app.css", "text/css; charset=utf-8"),
            "/app.js": ("app.js", "text/javascript; charset=utf-8"),
        }
        if self.path == "/api/overview":
            return self.send_json(overview())
        if self.path not in routes:
            return self.send_json({"error": "Not found"}, 404)
        name, content_type = routes[self.path]
        self.send_bytes((UI / name).read_bytes(), content_type)

    def do_POST(self) -> None:
        if self.path != "/api/playground":
            return self.send_json({"error": "Not found"}, 404)
        try:
            size = int(self.headers.get("Content-Length", "0"))
            if not 0 < size <= 8192:
                return self.send_json({"error": "Request must be 1–8192 bytes"}, 400)
            body = json.loads(self.rfile.read(size))
            prompt = body.get("prompt", "")
            mode = body.get("mode", "check")
            if not isinstance(prompt, str) or len(prompt) > 4000 or mode not in {"check", "live"}:
                return self.send_json({"error": "Invalid prompt or mode"}, 400)
            injection = detect_injection(prompt)
            topic = topic_filter(prompt)
            result = {
                "injection": injection,
                "topic": topic,
                "allowed": injection == "ALLOW" and topic == "ALLOW",
                "mode": mode,
            }
            if mode == "live" and result["allowed"]:
                from agents.agent import create_blue_agent
                from assignment.pipeline import build_production_plugins
                from core.utils import chat_with_agent

                agent, runner = create_blue_agent(build_production_plugins())
                reply, _ = asyncio.run(chat_with_agent(agent, runner, prompt))
                result["response"] = content_filter(reply)["redacted"]
            return self.send_json(result)
        except (ValueError, json.JSONDecodeError):
            return self.send_json({"error": "Invalid JSON request"}, 400)
        except Exception as exc:
            # Provider errors can include account metadata; keep them server-side.
            print(f"Live Blue request failed: {type(exc).__name__}: {exc}", file=sys.stderr)
            return self.send_json({"error": "Blue API unavailable. Check terminal and retry."}, 502)


def main() -> None:
    parser = argparse.ArgumentParser(description="VinBank Guardrails local demo")
    parser.add_argument("--port", type=int, default=8765)
    args = parser.parse_args()
    server = ThreadingHTTPServer(("127.0.0.1", args.port), Handler)
    print(f"Demo UI: http://127.0.0.1:{args.port}")
    print("Press Ctrl+C to stop.")
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        server.server_close()


if __name__ == "__main__":
    main()
