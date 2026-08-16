"""Loopback-only offline data source for the enrollment-repair Remote UI."""

from __future__ import annotations

import argparse
import json
from http import HTTPStatus
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path


def _summary() -> dict:
    return {
        "hub": {"app_version": "fixture"},
        "studios": [{
            "id": "image@fixture-agent-a", "machine": "fixture-agent-a",
            "machine_label": "Fixture Agent", "host": "100.64.0.10",
            "modality": "image", "status": "up", "enabled": True, "emoji": "🎨",
        }],
        "resources": {
            "studios": {"image@fixture-agent-a": {}},
            "machines": {"fixture-agent-a": {
                "status": "connected", "enabled": True,
                "hardware_profile": {"id": "fixture", "display_name": "Mac mini M4 · 16 GB"},
            }},
        },
        "control_plane": {"settings": {
            "role": "controller", "site_id": "fixture-site", "site_name": "Fixture Site",
            "controller_id": "fixture-controller", "database_mode": "off",
        }},
        "alerts_active": 0,
    }


def _eligibility(
    *, issuance_enabled: bool = True,
    request_state: str | None = None,
    code: str | None = None,
) -> dict:
    return {"issuance_enabled": issuance_enabled, "machines": [{
        "machine": "fixture-agent-a", "display_label": "Fixture Agent", "host": "100.64.0.10",
        "eligible": True, "code": "eligible", "detail": "Eligible for enrollment repair.",
        "request_state": request_state, "code": code or "eligible",
    }]}


def create_fixture_server(
    frontend_path: Path, *, host: str = "127.0.0.1", port: int = 0,
) -> ThreadingHTTPServer:
    """Return an unstarted local-only HTTP server with deterministic repair state."""
    if host != "127.0.0.1":
        raise ValueError("fixture must bind only to 127.0.0.1")
    frontend = Path(frontend_path).read_bytes()
    state = {"phase": "eligible", "batch_id": "fixture-batch"}

    class FixtureHandler(BaseHTTPRequestHandler):
        def log_message(self, _format: str, *_args: object) -> None:
            return

        def _json(self, payload: dict, status: int = HTTPStatus.OK) -> None:
            encoded = json.dumps(payload).encode()
            self.send_response(status)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(encoded)))
            self.end_headers()
            self.wfile.write(encoded)

        def _batch(self) -> dict:
            phase = state["phase"]
            request_state = {
                "eligible": "queued", "repairing": "dispatched",
                "pending": "confirmation_pending", "complete": "complete",
                "retryable": "retryable", "needs_review": "needs_review",
            }[phase]
            error_code = {
                "retryable": "offline",
                "needs_review": "ambiguous_registry_host",
            }.get(phase)
            return {
                "batch_id": state["batch_id"],
                "state": "complete" if phase in {"complete", "retryable", "needs_review"} else "running",
                "targets": ["fixture-agent-a"],
                "requests": [{
                    "request_id": "fixture-request-a", "target_machine": "fixture-agent-a",
                    "state": request_state, "error_code": error_code,
                    "evidence": {"conflict": "sanitized fixture evidence"} if phase == "needs_review" else {},
                }],
                "rejected": {},
            }

        def do_GET(self) -> None:  # noqa: N802
            if self.path == "/":
                self.send_response(HTTPStatus.OK)
                self.send_header("Content-Type", "text/html; charset=utf-8")
                self.send_header("Content-Length", str(len(frontend)))
                self.end_headers()
                self.wfile.write(frontend)
            elif self.path == "/api/hub/summary":
                self._json(_summary())
            elif self.path == "/api/hub/controller":
                self._json(_summary()["control_plane"])
            elif self.path == "/api/hub/registry/hardware-profiles":
                self._json({"profiles": [{"id": "fixture", "display_name": "Mac mini M4 · 16 GB"}], "assignments": {}})
            elif self.path == "/api/hub/enrollment-repairs/eligibility":
                terminal = {
                    "retryable": ("retryable", "offline"),
                    "needs_review": ("needs_review", "ambiguous_registry_host"),
                }.get(state["phase"], (None, None))
                self._json(_eligibility(
                    issuance_enabled=state["phase"] != "issuance_disabled",
                    request_state=terminal[0], code=terminal[1],
                ))
            elif self.path == f"/api/hub/enrollment-repairs/{state['batch_id']}":
                self._json(self._batch())
            else:
                self._json({"detail": "offline fixture path not found"}, HTTPStatus.NOT_FOUND)

        def do_POST(self) -> None:  # noqa: N802
            size = int(self.headers.get("Content-Length", "0"))
            raw = self.rfile.read(size)
            if self.path == "/api/hub/enrollment-repairs":
                try:
                    payload = json.loads(raw or b"{}")
                except json.JSONDecodeError:
                    self._json({"detail": "invalid JSON"}, HTTPStatus.BAD_REQUEST)
                    return
                if payload != {"machines": ["fixture-agent-a"]}:
                    self._json({"detail": "fixture only accepts the registered stable ID"}, HTTPStatus.BAD_REQUEST)
                    return
                if state["phase"] == "start_failure":
                    self._json({"detail": {"code": "repair_request_rejected"}}, HTTPStatus.SERVICE_UNAVAILABLE)
                    return
                if state["phase"] == "issuance_disabled":
                    self._json({"detail": {"code": "repair_issuance_disabled"}}, HTTPStatus.SERVICE_UNAVAILABLE)
                    return
                state["phase"] = "repairing"
                self._json(self._batch(), HTTPStatus.ACCEPTED)
            elif self.path == "/__fixture/advance":
                next_phase = {"eligible": "repairing", "repairing": "pending", "pending": "complete", "complete": "complete"}
                state["phase"] = next_phase[state["phase"]]
                self._json({"phase": state["phase"], "offline": True})
            elif self.path == "/__fixture/state":
                try:
                    payload = json.loads(raw or b"{}")
                except json.JSONDecodeError:
                    self._json({"detail": "invalid JSON"}, HTTPStatus.BAD_REQUEST)
                    return
                phase = payload.get("phase") if isinstance(payload, dict) else None
                if phase not in {"eligible", "repairing", "pending", "complete", "retryable", "needs_review", "start_failure", "issuance_disabled"}:
                    self._json({"detail": "unsupported fixture phase"}, HTTPStatus.BAD_REQUEST)
                    return
                state["phase"] = phase
                self._json({"phase": phase, "offline": True})
            else:
                self._json({"detail": "offline fixture path not found"}, HTTPStatus.NOT_FOUND)

    return ThreadingHTTPServer((host, port), FixtureHandler)


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--port", type=int, default=0)
    parser.add_argument("--frontend", type=Path, default=Path(__file__).resolve().parents[2] / "frontend" / "index.html")
    args = parser.parse_args()
    server = create_fixture_server(args.frontend, port=args.port)
    host, bound_port = server.server_address[:2]
    print(f"http://{host}:{bound_port} — offline mock; no Hub/fleet connection", flush=True)
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        server.server_close()


if __name__ == "__main__":
    main()
