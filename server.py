#!/usr/bin/env python3
"""Read-only HTTP service for sanitized Claude and Codex usage snapshots."""

from __future__ import annotations

import argparse
import json
import os
from dataclasses import dataclass
from datetime import datetime, timezone
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any, Mapping
from urllib.parse import urlsplit

from service_auth import AuthError, AuthPolicy, check_read_auth, is_loopback, readiness

from model import SCHEMA_VERSION, UsageStore, utc_iso

SERVICE_NAME = "solo-empire-ai-usage-api"


@dataclass(frozen=True)
class Response:
    status: int
    payload: dict[str, Any]


class AiUsageApi:
    def __init__(
        self,
        state_dir: str | Path | None = None,
        *,
        environment: str | None = None,
        token: str | None = None,
        auth_policy: AuthPolicy | None = None,
        allowed_origins: tuple[str, ...] | None = None,
    ) -> None:
        self.store = UsageStore(state_dir)
        self.environment = (
            (environment or os.environ.get("AI_USAGE_API_ENV", "development"))
            .strip()
            .lower()
        )
        self.token = (
            token
            if token is not None
            else os.environ.get("AI_USAGE_API_TOKEN", "").strip()
        )
        self.auth_policy = auth_policy or AuthPolicy.from_env(
            "AI_USAGE_API_", "ai-usage-api", "ai-usage:read"
        )
        configured_origins = os.environ.get(
            "AI_USAGE_API_CORS_ORIGINS", "http://127.0.0.1:3600,http://localhost:3600"
        )
        self.allowed_origins = (
            allowed_origins
            if allowed_origins is not None
            else tuple(
                value.strip()
                for value in configured_origins.split(",")
                if value.strip()
            )
        )

    def handle(
        self, method: str, target: str, *, headers: Mapping[str, str] | None = None
    ) -> Response:
        request_headers = {
            str(key).lower(): str(value) for key, value in (headers or {}).items()
        }
        try:
            path = urlsplit(target).path.rstrip("/") or "/"
            if method == "OPTIONS":
                return Response(204, {})
            if method == "GET" and path == "/health":
                return Response(
                    200,
                    {
                        "status": "ok",
                        "service": SERVICE_NAME,
                        "schema_version": SCHEMA_VERSION,
                        "timestamp": utc_iso(datetime.now(timezone.utc)),
                    },
                )
            if method == "GET" and path == "/ready":
                status, payload = readiness(
                    self.auth_policy, self.environment, self.store
                )
                return Response(status, payload)
            if path == "/api/v1/ai-usage":
                check_read_auth(
                    self.auth_policy,
                    request_headers,
                    environment=self.environment,
                    token=self.token,
                )
                if method != "GET":
                    return Response(
                        405,
                        {
                            "error": {
                                "code": "method_not_allowed",
                                "message": "ข้อมูลการใช้ AI อ่านได้อย่างเดียว",
                            }
                        },
                    )
                return Response(200, self.store.snapshot())
            return Response(
                404, {"error": {"code": "not_found", "message": "ไม่พบเส้นทางที่ร้องขอ"}}
            )
        except AuthError as exc:
            return Response(
                exc.status, {"error": {"code": exc.code, "message": exc.message}}
            )
        except Exception:
            return Response(
                500,
                {
                    "error": {
                        "code": "internal_error",
                        "message": "ไม่สามารถอ่านข้อมูลการใช้ AI ได้",
                    }
                },
            )


class AiUsageRequestHandler(BaseHTTPRequestHandler):
    api = AiUsageApi()

    def log_message(self, format: str, *args: Any) -> None:  # noqa: A002
        return

    def _dispatch(self) -> None:
        origin = self.headers.get("Origin")
        if origin and origin not in self.api.allowed_origins:
            self._write(
                Response(
                    403,
                    {
                        "error": {
                            "code": "origin_not_allowed",
                            "message": "ไม่อนุญาต origin นี้",
                        }
                    },
                ),
                None,
            )
            return
        self._write(
            self.api.handle(
                self.command, self.path, headers=dict(self.headers.items())
            ),
            origin,
        )

    def _write(self, response: Response, origin: str | None) -> None:
        encoded = (
            b""
            if response.status == 204
            else json.dumps(
                response.payload,
                ensure_ascii=False,
                allow_nan=False,
                separators=(",", ":"),
            ).encode("utf-8")
        )
        self.send_response(response.status)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Cache-Control", "no-store")
        self.send_header("X-Content-Type-Options", "nosniff")
        self.send_header("Content-Length", str(len(encoded)))
        if origin and origin in self.api.allowed_origins:
            self.send_header("Access-Control-Allow-Origin", origin)
            self.send_header("Vary", "Origin")
        if self.command == "OPTIONS":
            self.send_header("Access-Control-Allow-Methods", "GET, OPTIONS")
            self.send_header("Access-Control-Allow-Headers", "Accept, Authorization")
        self.end_headers()
        if encoded:
            self.wfile.write(encoded)

    do_GET = _dispatch
    do_OPTIONS = _dispatch
    do_POST = _dispatch
    do_PUT = _dispatch
    do_PATCH = _dispatch
    do_DELETE = _dispatch


def main() -> None:
    parser = argparse.ArgumentParser(description="Solo Empire AI usage API")
    parser.add_argument(
        "--host", default=os.environ.get("AI_USAGE_API_HOST", "127.0.0.1")
    )
    parser.add_argument(
        "--port",
        type=int,
        default=int(
            os.environ.get("AI_USAGE_API_PORT", os.environ.get("PORT", "3520"))
        ),
    )
    parser.add_argument(
        "--state-dir",
        default=None,
        help="sanitized snapshots; defaults to AI_USAGE_STATE_DIR",
    )
    args = parser.parse_args()
    if not 1 <= args.port <= 65535:
        parser.error("--port must be between 1 and 65535")
    AiUsageRequestHandler.api = AiUsageApi(args.state_dir)
    if not is_loopback(args.host):
        AiUsageRequestHandler.api.environment = "production"
        if not AiUsageRequestHandler.api.auth_policy.ready:
            parser.error(
                "a non-loopback --host requires valid AI_USAGE_API_AUTH_* configuration"
            )
    server = ThreadingHTTPServer((args.host, args.port), AiUsageRequestHandler)
    print(f"{SERVICE_NAME} running on http://{args.host}:{args.port}", flush=True)
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        server.server_close()


if __name__ == "__main__":
    main()
