#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
AutoGLM OCR backend service.
Runs the OCR sync worker and exposes an HTTP/HTTPS control API in one process.
"""

from __future__ import annotations

import http.server
import json
import mimetypes
import os
import signal
import threading
import time
import urllib.parse
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from ocr_sync_service import OCRSyncService, SyncError, StateStore, iso_now, load_config, setup_logging
from tls_context import build_server_ssl_context


class ThreadedHTTPServer(http.server.ThreadingHTTPServer):
    allow_reuse_address = True
    daemon_threads = True


class SyncCoordinator:
    def __init__(self, service: OCRSyncService, logger):
        self.service = service
        self.logger = logger
        self.config = service.config
        self.stop_event = threading.Event()
        self.wake_event = threading.Event()
        self.worker_thread: threading.Thread | None = None
        self.api_server: ThreadedHTTPServer | None = None
        self.api_thread: threading.Thread | None = None
        self.status_lock = threading.Lock()
        self.state_store = StateStore(self.config.state_file)
        self.status: dict[str, Any] = {
            "service_started_at": iso_now(),
            "sync_enabled": self.config.enabled,
            "sync_in_progress": False,
            "last_cycle_started_at": None,
            "last_cycle_completed_at": None,
            "last_cycle_status": "idle" if self.config.enabled else "disabled",
            "last_error": None,
            "last_trigger": None,
        }

    def start(self) -> None:
        self.worker_thread = threading.Thread(
            target=self._sync_loop,
            name="OCRSyncLoop",
            daemon=True,
        )
        self.worker_thread.start()

        if self.config.api_enabled:
            handler = self._make_handler()
            self.api_server = ThreadedHTTPServer((self.config.api_listen_host, self.config.api_port), handler)
            self.api_server.is_tls = False
            if self.config.tls_enabled:
                context = build_server_ssl_context(
                    tls_enabled=True,
                    cert_file=self.config.tls_cert_file,
                    key_file=self.config.tls_key_file,
                    min_tls_version=self.config.tls_min_tls_version,
                )
                if context is None:
                    raise SyncError("TLS is enabled but backend API SSL context could not be created")
                self.api_server.socket = context.wrap_socket(self.api_server.socket, server_side=True)
                self.api_server.is_tls = True
            self.api_thread = threading.Thread(
                target=self.api_server.serve_forever,
                name="OCRBackendAPI",
                daemon=True,
            )
            self.api_thread.start()

    def stop(self) -> None:
        self.stop_event.set()
        self.wake_event.set()
        if self.api_server is not None:
            threading.Thread(target=self.api_server.shutdown, daemon=True).start()

    def join(self) -> None:
        if self.worker_thread is not None:
            self.worker_thread.join(timeout=10)
        if self.api_thread is not None:
            self.api_thread.join(timeout=10)

    def request_sync(self, source: str, reason: str, payload: dict[str, Any] | None = None) -> dict[str, Any]:
        trigger = {
            "requested_at": iso_now(),
            "source": source,
            "reason": reason,
            "payload": payload or {},
        }
        with self.status_lock:
            self.status["last_trigger"] = trigger
        if not self.config.enabled:
            return {"accepted": False, "message": "OCR sync is disabled in config.ini"}
        self.wake_event.set()
        return {"accepted": True, "message": "Sync trigger accepted", "trigger": trigger}

    def build_health(self, base_url: str) -> dict[str, Any]:
        with self.status_lock:
            status = dict(self.status)
        return {
            "status": "ok",
            "generated_at": iso_now(),
            "service": "autoglm-ocr-backend",
            "api_base_url": base_url,
            "frontend_base_url": self.config.frontend_base_url,
            "sync_enabled": self.config.enabled,
            "tls_enabled": self.config.tls_enabled,
            "runtime": status,
        }

    def build_status(self, base_url: str) -> dict[str, Any]:
        with self.status_lock:
            status = dict(self.status)
        state = self.state_store.load()
        return {
            "generated_at": iso_now(),
            "service": "autoglm-ocr-backend",
            "api_base_url": base_url,
            "frontend_base_url": self.config.frontend_base_url,
            "sync_enabled": self.config.enabled,
            "api_enabled": self.config.api_enabled,
            "tls_enabled": self.config.tls_enabled,
            "manifest_url": f"{base_url}/manifest.json" if base_url else "manifest.json",
            "state_url": f"{base_url}/state.json" if base_url else "state.json",
            "runtime": status,
            "counts": {
                "images": len(state.get("images", {})),
                "edited_json": len(state.get("edited_json", {})),
            },
            "images": self._recent_records(state.get("images", {}), self.config.api_status_limit),
            "edited_json": self._recent_records(state.get("edited_json", {}), self.config.api_status_limit),
        }

    def build_manifest(self, base_url: str) -> dict[str, Any]:
        files: list[dict[str, Any]] = []
        for path in sorted(
            self.service.outputs_dir.rglob("*"),
            key=lambda item: item.stat().st_mtime,
            reverse=True,
        ):
            if not path.is_file():
                continue
            relative_path = path.relative_to(self.service.outputs_dir).as_posix()
            url = (
                f"{base_url}/artifacts/{urllib.parse.quote(relative_path, safe='/')}"
                if base_url
                else f"artifacts/{relative_path}"
            )
            files.append(
                {
                    "path": relative_path,
                    "name": path.name,
                    "size": path.stat().st_size,
                    "mtime": datetime.fromtimestamp(path.stat().st_mtime, timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
                    "url": url,
                }
            )
        return {
            "generated_at": iso_now(),
            "count": len(files),
            "files": files,
        }

    def load_state(self) -> dict[str, Any]:
        return self.state_store.load()

    def resolve_artifact(self, relative_path: str) -> Path | None:
        target = (self.service.outputs_dir / relative_path).resolve()
        try:
            target.relative_to(self.service.outputs_dir.resolve())
        except ValueError:
            return None
        if not target.is_file():
            return None
        return target

    def _recent_records(self, records: dict[str, Any], limit: int) -> list[dict[str, Any]]:
        normalized: list[dict[str, Any]] = []
        for name, payload in records.items():
            item = {"id": name}
            if isinstance(payload, dict):
                item.update(payload)
            normalized.append(item)
        normalized.sort(
            key=lambda item: (
                str(item.get("last_seen_at") or ""),
                str(item.get("ocr_uploaded_at") or item.get("log_uploaded_at") or ""),
            ),
            reverse=True,
        )
        return normalized[:limit]

    def _mark_cycle_start(self) -> None:
        with self.status_lock:
            self.status["sync_in_progress"] = True
            self.status["last_cycle_started_at"] = iso_now()
            self.status["last_cycle_status"] = "running"
            self.status["last_error"] = None

    def _mark_cycle_finish(self, *, success: bool, error: str | None = None) -> None:
        with self.status_lock:
            self.status["sync_in_progress"] = False
            self.status["last_cycle_completed_at"] = iso_now()
            self.status["last_cycle_status"] = "ok" if success else "error"
            self.status["last_error"] = error

    def _sync_loop(self) -> None:
        next_run_at = 0.0
        while not self.stop_event.is_set():
            now = time.monotonic()
            should_run = self.config.enabled and now >= next_run_at

            if not should_run:
                triggered = self.wake_event.wait(timeout=1.0)
                if triggered:
                    self.wake_event.clear()
                    if self.config.enabled:
                        next_run_at = 0.0
                continue

            self._mark_cycle_start()
            try:
                self.service.sync_once()
            except Exception as exc:  # noqa: BLE001
                self.logger.exception("Sync cycle failed: %s", exc)
                self._mark_cycle_finish(success=False, error=str(exc))
            else:
                self._mark_cycle_finish(success=True)

            next_run_at = time.monotonic() + self.config.poll_interval_seconds

            if self.wake_event.is_set():
                self.wake_event.clear()
                next_run_at = time.monotonic()

    def _make_handler(self):
        coordinator = self

        class OCRBackendApiHandler(http.server.BaseHTTPRequestHandler):
            def log_message(self, format: str, *args) -> None:  # noqa: A003
                coordinator.logger.debug("%s - - %s", self.client_address[0], format % args)

            def do_GET(self) -> None:  # noqa: N802
                parsed = urllib.parse.urlparse(self.path)
                request_path = urllib.parse.unquote(parsed.path)
                if request_path in {"", "/"}:
                    self._send_json(200, coordinator.build_health(self._public_base_url()))
                    return
                if request_path == "/healthz":
                    self._send_json(200, coordinator.build_health(self._public_base_url()))
                    return
                if request_path == "/status.json":
                    self._send_json(200, coordinator.build_status(self._public_base_url()))
                    return
                if request_path == "/state.json":
                    self._send_json(200, coordinator.load_state())
                    return
                if request_path == "/manifest.json":
                    self._send_json(200, coordinator.build_manifest(self._public_base_url()))
                    return
                if request_path.startswith("/artifacts/"):
                    relative_path = request_path.removeprefix("/artifacts/")
                    target = coordinator.resolve_artifact(relative_path)
                    if target is None:
                        self._send_json(404, {"status": "error", "error": "Artifact not found"})
                        return
                    self._serve_file(target)
                    return
                self._send_json(404, {"status": "error", "error": "Not found"})

            def do_POST(self) -> None:  # noqa: N802
                parsed = urllib.parse.urlparse(self.path)
                if parsed.path not in {"/api/sync-now", "/api/frontend-event"}:
                    self._send_json(404, {"status": "error", "error": "Not found"})
                    return
                if not self._is_authorized():
                    self._send_json(401, {"status": "error", "error": "Unauthorized"})
                    return

                payload = self._parse_json_body()
                reason = str(payload.get("reason") or payload.get("event") or "manual").strip() or "manual"
                source = str(payload.get("source") or "external").strip() or "external"
                result = coordinator.request_sync(source=source, reason=reason, payload=payload)
                status_code = 202 if result.get("accepted") else 409
                self._send_json(status_code, result)

            def _is_authorized(self) -> bool:
                token = coordinator.config.api_auth_token.strip()
                if not token:
                    return True
                header_token = self.headers.get("X-AutoGLM-Token", "").strip()
                if header_token == token:
                    return True
                auth_header = self.headers.get("Authorization", "").strip()
                return auth_header == f"Bearer {token}"

            def _parse_json_body(self) -> dict[str, Any]:
                length = int(self.headers.get("Content-Length", "0") or "0")
                raw = self.rfile.read(length) if length > 0 else b"{}"
                if not raw:
                    return {}
                try:
                    data = json.loads(raw.decode("utf-8", errors="replace"))
                except json.JSONDecodeError:
                    return {}
                return data if isinstance(data, dict) else {}

            def _public_base_url(self) -> str:
                if coordinator.config.api_public_base_url:
                    return coordinator.config.api_public_base_url
                host = self.headers.get("Host", "").strip()
                if not host:
                    return ""
                scheme = "https" if getattr(self.server, "is_tls", False) else "http"
                return f"{scheme}://{host}"

            def _send_json(self, code: int, obj: dict[str, Any]) -> None:
                body = json.dumps(obj, ensure_ascii=False).encode("utf-8")
                self.send_response(code)
                self.send_header("Content-Type", "application/json; charset=utf-8")
                self.send_header("Content-Length", str(len(body)))
                self.end_headers()
                self.wfile.write(body)

            def _serve_file(self, target: Path) -> None:
                body = target.read_bytes()
                content_type = mimetypes.guess_type(target.name)[0] or "application/octet-stream"
                self.send_response(200)
                self.send_header("Content-Type", content_type)
                self.send_header("Content-Length", str(len(body)))
                self.end_headers()
                self.wfile.write(body)

        return OCRBackendApiHandler


def main() -> None:
    config = load_config()
    logger = setup_logging(config.log_dir)
    logger.info("OCR backend service starting")
    logger.info("Frontend base URL : %s", config.frontend_base_url)
    logger.info("API listen        : %s:%s", config.api_listen_host, config.api_port)
    if config.api_public_base_url:
        logger.info("API public URL    : %s", config.api_public_base_url)
    logger.info("API TLS enabled   : %s", config.tls_enabled)

    service = OCRSyncService(config, logger)
    coordinator = SyncCoordinator(service, logger)
    coordinator.start()

    def _shutdown(signum: int, frame: Any) -> None:
        del frame
        logger.info("Signal %s received, shutting down...", signum)
        coordinator.stop()

    signal.signal(signal.SIGTERM, _shutdown)
    signal.signal(signal.SIGINT, _shutdown)

    try:
        while not coordinator.stop_event.wait(1.0):
            continue
    finally:
        coordinator.join()
        logger.info("OCR backend service stopped")


if __name__ == "__main__":
    main()
