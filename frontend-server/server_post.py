#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
AutoGLM image-link receiver server.
Receives image links from an Android Auto.js app, downloads the images,
and serves files from the upload directory.
No third-party dependencies — stdlib only.
"""

import configparser
import http.server
import io
import json
import logging
import logging.handlers
import os
import re
import signal
import ssl
import sys
import threading
import urllib.error
import urllib.parse
import urllib.request
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

# ---------------------------------------------------------------------------
# Configuration
# ---------------------------------------------------------------------------

SCRIPT_DIR = Path(__file__).resolve().parent
CONFIG_FILE = SCRIPT_DIR / "config.ini"

DEFAULTS = {
    "post_port": "39282",
    "upload_dir": str(SCRIPT_DIR / "uploads"),
    "log_dir": str(SCRIPT_DIR / "logs"),
    "download_timeout": "30",
    "max_image_size": "52428800",
    "public_base_url": "",
    "manifest_name": "manifest.json",
}

TLS_DEFAULTS = {
    "enabled": "false",
    "cert_file": str(SCRIPT_DIR / "certs" / "server.crt"),
    "key_file": str(SCRIPT_DIR / "certs" / "server.key"),
    "min_tls_version": "TLSv1.2",
}

BACKEND_DEFAULTS = {
    "enabled": "false",
    "base_url": "",
    "notify_url": "",
    "request_timeout_seconds": "10",
    "verify_ssl": "true",
    "auth_token": "",
}


def env_str(name: str, fallback: str) -> str:
    value = os.getenv(name)
    if value is None:
        return fallback
    stripped = value.strip()
    return stripped if stripped else fallback


def env_bool(name: str, fallback: bool) -> bool:
    value = os.getenv(name)
    if value is None or not value.strip():
        return fallback
    return value.strip().lower() in {"1", "true", "yes", "on"}


def env_int(name: str, fallback: int, minimum: int = 0) -> int:
    value = os.getenv(name)
    if value is None or not value.strip():
        return fallback
    try:
        parsed = int(value.strip())
    except ValueError:
        return fallback
    return max(minimum, parsed)


def load_config():
    cfg = configparser.ConfigParser()
    cfg.read(str(CONFIG_FILE), encoding="utf-8")

    section = "server"
    if not cfg.has_section(section):
        cfg.add_section(section)

    def get(key):
        return cfg.get(section, key, fallback=DEFAULTS[key])

    port = int(get("post_port"))
    upload_dir = Path(get("upload_dir"))
    log_dir = Path(get("log_dir"))
    public_base_url = get("public_base_url").strip().rstrip("/")
    manifest_name = get("manifest_name").strip() or DEFAULTS["manifest_name"]

    # TLS config
    tls_section = "tls"
    def tget(key):
        return cfg.get(tls_section, key, fallback=TLS_DEFAULTS[key]) if cfg.has_section(tls_section) else TLS_DEFAULTS[key]

    tls = {
        "enabled": tget("enabled").strip().lower() == "true",
        "cert_file": tget("cert_file"),
        "key_file": tget("key_file"),
        "min_tls_version": tget("min_tls_version"),
    }

    backend_section = "backend"

    def bget(key):
        return cfg.get(backend_section, key, fallback=BACKEND_DEFAULTS[key]) if cfg.has_section(backend_section) else BACKEND_DEFAULTS[key]

    backend_base_url = env_str("AUTOGLM_BACKEND_BASE_URL", bget("base_url")).rstrip("/")
    backend_notify_url = env_str("AUTOGLM_BACKEND_NOTIFY_URL", bget("notify_url")).rstrip("/")
    if not backend_notify_url and backend_base_url:
        backend_notify_url = f"{backend_base_url}/api/sync-now"

    backend_enabled_fallback = (
        bget("enabled").strip().lower() == "true" or bool(backend_notify_url)
    )

    backend = {
        "enabled": env_bool("AUTOGLM_BACKEND_NOTIFY_ENABLED", backend_enabled_fallback) and bool(backend_notify_url),
        "base_url": backend_base_url,
        "notify_url": backend_notify_url,
        "request_timeout_seconds": env_int(
            "AUTOGLM_BACKEND_REQUEST_TIMEOUT_SECONDS",
            int(bget("request_timeout_seconds")),
            minimum=1,
        ),
        "verify_ssl": env_bool("AUTOGLM_BACKEND_VERIFY_SSL", bget("verify_ssl").strip().lower() == "true"),
        "auth_token": env_str("AUTOGLM_BACKEND_AUTH_TOKEN", bget("auth_token")),
    }
    return port, upload_dir, log_dir, tls, public_base_url, manifest_name, backend


# ---------------------------------------------------------------------------
# Logging setup
# ---------------------------------------------------------------------------

def setup_logging(log_dir: Path) -> logging.Logger:
    log_dir.mkdir(parents=True, exist_ok=True)
    log_file = log_dir / "server_post.log"

    logger = logging.getLogger("server_post")
    logger.setLevel(logging.DEBUG)

    fmt = logging.Formatter(
        "%(asctime)s [%(levelname)s] %(message)s",
        datefmt="%Y-%m-%dT%H:%M:%S",
    )

    # Rotating file handler: 10 MB, 5 backups
    fh = logging.handlers.RotatingFileHandler(
        str(log_file),
        maxBytes=10 * 1024 * 1024,
        backupCount=5,
        encoding="utf-8",
    )
    fh.setFormatter(fmt)
    logger.addHandler(fh)

    # Stdout handler
    sh = logging.StreamHandler(sys.stdout)
    sh.setFormatter(fmt)
    logger.addHandler(sh)

    return logger


# ---------------------------------------------------------------------------
# Validation helpers
# ---------------------------------------------------------------------------

ALLOWED_EXTENSIONS = {".jpg", ".jpeg", ".png", ".gif", ".webp", ".bmp"}


def extract_first_http_url(text: str) -> str:
    text = str(text or "").strip()
    if not text:
        return ""
    if text.startswith("http://") or text.startswith("https://"):
        return text
    m = re.search(r"(https?://[^\s'\"<>]+)", text)
    return m.group(1) if m else ""


def validate_image_url(url: str):
    """
    Returns (filename, error_message).
    filename is the last path segment (before query string), URL-decoded.
    error_message is None on success.
    """
    url = extract_first_http_url(url)
    if not url:
        return None, "Missing image URL"
    if not (url.startswith("http://") or url.startswith("https://")):
        return None, "URL must start with http:// or https://"

    parsed = urllib.parse.urlparse(url)
    path = parsed.path  # e.g. /images/photo.jpg
    # URL-decode path segment to support Chinese characters
    path_decoded = urllib.parse.unquote(path, encoding="utf-8")
    filename = path_decoded.rstrip("/").rsplit("/", 1)[-1]

    if not filename:
        return None, "Could not extract filename from URL path"

    ext = Path(filename).suffix.lower()
    if ext not in ALLOWED_EXTENSIONS:
        return None, (
            f"File extension '{ext}' not allowed. "
            f"Must be one of: {', '.join(sorted(ALLOWED_EXTENSIONS))}"
        )

    return filename, None


# ---------------------------------------------------------------------------
# File write helpers
# ---------------------------------------------------------------------------

_write_lock = threading.Lock()


def safe_write(path: Path, content: str):
    """Thread-safe UTF-8 file write."""
    with _write_lock:
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(content, encoding="utf-8")


def iso_now() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


# ---------------------------------------------------------------------------
# Background download worker
# ---------------------------------------------------------------------------

def notify_backend_sync(backend: dict[str, Any], logger: logging.Logger, payload: dict[str, Any]) -> None:
    if not backend.get("enabled") or not backend.get("notify_url"):
        return

    body = json.dumps(payload, ensure_ascii=False).encode("utf-8")
    request = urllib.request.Request(
        backend["notify_url"],
        data=body,
        headers={
            "Content-Type": "application/json; charset=utf-8",
            "User-Agent": "AutoGLM-Frontend/1.0",
        },
        method="POST",
    )
    if backend.get("auth_token"):
        request.add_header("X-AutoGLM-Token", backend["auth_token"])

    context = None if backend.get("verify_ssl", True) else ssl._create_unverified_context()
    try:
        with urllib.request.urlopen(
            request,
            timeout=backend["request_timeout_seconds"],
            context=context,
        ) as response:
            response.read()
        logger.info("Triggered OCR backend sync: %s", backend["notify_url"])
    except urllib.error.URLError as exc:
        logger.warning("Failed to notify OCR backend %s: %s", backend["notify_url"], exc)


def download_image(url: str, filename: str, upload_dir: Path, logger: logging.Logger, backend: dict[str, Any]):
    """
    Downloads the image and writes the two log files.
    Runs in a daemon thread.
    """
    stem = Path(filename).stem
    dest_path = upload_dir / filename
    pre_log_path = upload_dir / f"{stem}.log"
    dl_log_path = upload_dir / f"{stem}_download.log"

    # Pre-run log (shown in showServerLogActionFloaty)
    pre_log_content = (
        f"FILE_URL={url}\n"
        f"TIME={iso_now()}\n"
        f"STATUS=received"
    )
    safe_write(pre_log_path, pre_log_content)
    logger.info("Written pre-run log: %s", pre_log_path)

    # Download
    try:
        logger.info("Downloading image: %s -> %s", url, dest_path)
        upload_dir.mkdir(parents=True, exist_ok=True)
        req = urllib.request.Request(
            url,
            headers={"User-Agent": "AutoGLM-Server/1.0"},
        )
        with urllib.request.urlopen(req, timeout=60) as resp:
            data = resp.read()
        with _write_lock:
            dest_path.write_bytes(data)

        dl_log_content = (
            f"FILE_URL={url}\n"
            f"DOWNLOAD_TIME={iso_now()}\n"
            f"DOWNLOAD_STATUS=success\n"
            f"SAVED_AS={dest_path}"
        )
        logger.info("Download success: %s (%d bytes)", dest_path, len(data))
        notify_backend_sync(
            backend,
            logger,
            {
                "source": "frontend-post",
                "event": "download_ready",
                "reason": f"{filename} downloaded",
                "filename": filename,
                "file_url": url,
                "download_log_name": dl_log_path.name,
            },
        )

    except Exception as exc:  # noqa: BLE001
        dl_log_content = (
            f"FILE_URL={url}\n"
            f"DOWNLOAD_TIME={iso_now()}\n"
            f"DOWNLOAD_STATUS=failed\n"
            f"ERROR={exc}"
        )
        logger.error("Download failed for %s: %s", url, exc)

    safe_write(dl_log_path, dl_log_content)
    logger.info("Written download log: %s", dl_log_path)


# ---------------------------------------------------------------------------
# Request parsing
# ---------------------------------------------------------------------------

def parse_file_url(handler) -> str | None:
    """
    Parse the image URL from the request body.
    Supports form-encoded, text/plain, and JSON bodies.
    Returns the URL string or None if not found.
    """
    content_length = int(handler.headers.get("Content-Length", 0))
    raw_body = handler.rfile.read(content_length) if content_length > 0 else b""
    content_type = handler.headers.get("Content-Type", "").split(";")[0].strip().lower()

    if content_type == "application/x-www-form-urlencoded":
        params = urllib.parse.parse_qs(
            raw_body.decode("utf-8", errors="replace"),
            keep_blank_values=True,
        )
        values = params.get("file_url") or params.get("FILE_URL")
        return values[0].strip() if values else None

    if content_type == "application/json":
        try:
            obj = json.loads(raw_body.decode("utf-8", errors="replace"))
            return str(obj.get("file_url") or obj.get("FILE_URL") or "").strip() or None
        except (json.JSONDecodeError, AttributeError):
            return None

    text = raw_body.decode("utf-8", errors="replace")
    for line in text.splitlines():
        line = line.strip()
        if not line:
            continue
        if line.upper().startswith("FILE_URL="):
            return line.split("=", 1)[1].strip() or None
        if line.startswith("http://") or line.startswith("https://"):
            return line

    # Compatibility fallback: some clients may send URL-encoded bodies
    # without explicitly setting form Content-Type.
    if "=" in text:
        params = urllib.parse.parse_qs(text, keep_blank_values=True)
        values = params.get("file_url") or params.get("FILE_URL")
        if values:
            parsed = values[0].strip()
            if parsed:
                return parsed.splitlines()[0].strip() or None

    bare = text.strip()
    if bare.startswith("http://") or bare.startswith("https://"):
        return bare.splitlines()[0].strip() or None
    return None


def build_public_base_url(handler, configured_base_url: str) -> str:
    if configured_base_url:
        return configured_base_url.rstrip("/")
    host = handler.headers.get("Host", "").strip()
    if not host:
        return ""
    scheme = "https" if getattr(handler.server, "is_tls", False) else "http"
    return f"{scheme}://{host}"


def build_manifest(upload_dir: Path, base_url: str, backend: dict[str, Any]) -> dict[str, Any]:
    files: list[dict[str, Any]] = []
    for path in sorted(upload_dir.iterdir(), key=lambda item: item.stat().st_mtime, reverse=True):
        if not path.is_file():
            continue
        files.append(
            {
                "name": path.name,
                "size": path.stat().st_size,
                "mtime": datetime.fromtimestamp(path.stat().st_mtime, timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
                "url": f"{base_url}/{urllib.parse.quote(path.name)}" if base_url else path.name,
            }
        )
    manifest = {
        "generated_at": iso_now(),
        "count": len(files),
        "files": files,
    }
    if backend.get("base_url") or backend.get("notify_url"):
        manifest["backend"] = {
            "configured": bool(backend.get("enabled")),
            "base_url": backend.get("base_url") or "",
            "notify_url": backend.get("notify_url") or "",
        }
    return manifest


# ---------------------------------------------------------------------------
# HTTP handler
# ---------------------------------------------------------------------------

class ImageLinkHandler(http.server.BaseHTTPRequestHandler):
    # Injected by server setup
    upload_dir: Path
    logger: logging.Logger
    public_base_url: str
    manifest_name: str
    backend: dict[str, Any]

    # Suppress default request log (we do our own)
    def log_message(self, format, *args):  # noqa: A002
        pass

    def _send_json(self, code: int, obj: dict):
        body = json.dumps(obj, ensure_ascii=False).encode("utf-8")
        self.send_response(code)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def do_POST(self):  # noqa: N802
        client = self.client_address[0]
        self.logger.info("POST %s from %s", self.path, client)

        file_url = parse_file_url(self)
        self.logger.debug("Parsed file_url=%r", file_url)

        filename, err = validate_image_url(file_url or "")
        if err:
            self.logger.warning("Invalid request from %s: %s (url=%r)", client, err, file_url)
            self._send_json(400, {"status": "error", "error": err})
            return

        # Respond immediately
        self._send_json(200, {"status": "ok", "received": file_url})
        self.logger.info("Accepted link for %s, spawning download thread", filename)

        # Background download
        t = threading.Thread(
            target=download_image,
            args=(file_url, filename, self.upload_dir, self.logger, self.backend),
            daemon=True,
        )
        t.start()

    def do_GET(self):  # noqa: N802
        client = self.client_address[0]
        raw_path = self.path.lstrip("/").split("?")[0]
        if raw_path == self.manifest_name:
            base_url = build_public_base_url(self, self.public_base_url)
            manifest = build_manifest(self.upload_dir, base_url, self.backend)
            self.logger.info("GET /%s from %s", self.manifest_name, client)
            self._send_json(200, manifest)
            return

        filename = urllib.parse.unquote(raw_path, encoding="utf-8")
        self.logger.info("GET /%s from %s", filename, client)

        if not filename:
            self._send_json(400, {"status": "error", "error": "No filename specified"})
            return

        target = (self.upload_dir / filename).resolve()
        try:
            target.relative_to(self.upload_dir.resolve())
        except ValueError:
            self.logger.warning("Path traversal attempt: %s", filename)
            self._send_json(403, {"status": "error", "error": "Forbidden"})
            return

        if not target.exists() or not target.is_file():
            self.logger.warning("File not found: %s", target)
            self._send_json(404, {"status": "error", "error": "File not found"})
            return

        data = target.read_bytes()
        ext = target.suffix.lower()
        mime_map = {
            ".jpg": "image/jpeg", ".jpeg": "image/jpeg",
            ".png": "image/png", ".gif": "image/gif",
            ".webp": "image/webp", ".bmp": "image/bmp",
            ".log": "text/plain; charset=utf-8",
            ".txt": "text/plain; charset=utf-8",
            ".json": "application/json; charset=utf-8",
        }
        mime = mime_map.get(ext, "application/octet-stream")

        self.send_response(200)
        self.send_header("Content-Type", mime)
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)
        self.logger.info("Served %s (%d bytes)", target, len(data))


def make_handler(upload_dir: Path, logger: logging.Logger, public_base_url: str, manifest_name: str, backend: dict[str, Any]):
    """Return a handler class with upload_dir and logger bound."""

    class _Handler(ImageLinkHandler):
        pass

    _Handler.upload_dir = upload_dir
    _Handler.logger = logger
    _Handler.public_base_url = public_base_url
    _Handler.manifest_name = manifest_name
    _Handler.backend = backend
    return _Handler


# ---------------------------------------------------------------------------
# Server lifecycle
# ---------------------------------------------------------------------------

class ThreadedHTTPServer(http.server.ThreadingHTTPServer):
    """HTTP server that handles each request in its own thread."""
    # Allow quick restarts
    allow_reuse_address = True
    daemon_threads = True


def wrap_tls(server: ThreadedHTTPServer, tls: dict, logger: logging.Logger):
    """Wrap server socket with SSL/TLS if enabled."""
    cert = tls["cert_file"]
    key = tls["key_file"]
    min_ver = tls["min_tls_version"]
    if not os.path.isfile(cert):
        raise FileNotFoundError(f"TLS cert not found: {cert}")
    if not os.path.isfile(key):
        raise FileNotFoundError(f"TLS key not found: {key}")
    ctx = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
    ctx.minimum_version = (
        ssl.TLSVersion.TLSv1_3 if min_ver == "TLSv1.3" else ssl.TLSVersion.TLSv1_2
    )
    ctx.load_cert_chain(certfile=cert, keyfile=key)
    server.socket = ctx.wrap_socket(server.socket, server_side=True)
    logger.info("TLS enabled (min=%s, cert=%s)", min_ver, cert)


def run():
    port, upload_dir, log_dir, tls, public_base_url, manifest_name, backend = load_config()
    logger = setup_logging(log_dir)

    upload_dir.mkdir(parents=True, exist_ok=True)

    handler_class = make_handler(upload_dir, logger, public_base_url, manifest_name, backend)
    server = ThreadedHTTPServer(("", port), handler_class)
    server.is_tls = False

    scheme = "http"
    if tls["enabled"]:
        wrap_tls(server, tls, logger)
        scheme = "https"
        server.is_tls = True

    logger.info(
        "AutoGLM POST server starting on %s://0.0.0.0:%d", scheme, port
    )
    logger.info("Upload dir : %s", upload_dir.resolve())
    logger.info("Log dir    : %s", log_dir.resolve())
    logger.info("Manifest   : %s", manifest_name)
    if public_base_url:
        logger.info("Public URL : %s", public_base_url)
    if backend.get("enabled"):
        logger.info("Backend notify URL : %s", backend.get("notify_url"))

    def _shutdown(signum, frame):
        logger.info("Signal %d received, shutting down...", signum)
        threading.Thread(target=server.shutdown, daemon=True).start()

    signal.signal(signal.SIGTERM, _shutdown)
    signal.signal(signal.SIGINT, _shutdown)

    try:
        server.serve_forever()
    finally:
        logger.info("Server stopped.")


if __name__ == "__main__":
    run()
