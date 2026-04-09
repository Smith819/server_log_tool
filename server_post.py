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
import urllib.parse
import urllib.request
from datetime import datetime, timezone
from pathlib import Path

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
}

TLS_DEFAULTS = {
    "enabled": "false",
    "cert_file": str(SCRIPT_DIR / "certs" / "server.crt"),
    "key_file": str(SCRIPT_DIR / "certs" / "server.key"),
    "min_tls_version": "TLSv1.2",
}


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
    return port, upload_dir, log_dir, tls


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


def validate_image_url(url: str):
    """
    Returns (filename, error_message).
    filename is the last path segment (before query string), URL-decoded.
    error_message is None on success.
    """
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

def download_image(url: str, filename: str, upload_dir: Path, logger: logging.Logger):
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

    # text/plain or anything else — look for FILE_URL= line
    text = raw_body.decode("utf-8", errors="replace")
    # Compatibility fallback: some clients may send URL-encoded bodies
    # without explicitly setting form Content-Type.
    if "=" in text:
        params = urllib.parse.parse_qs(text, keep_blank_values=True)
        values = params.get("file_url") or params.get("FILE_URL")
        if values:
            parsed = values[0].strip()
            if parsed:
                return parsed
    for line in text.splitlines():
        line = line.strip()
        if line.upper().startswith("FILE_URL="):
            return line.split("=", 1)[1].strip() or None
    # Fallback: if body is just a bare URL
    bare = text.strip()
    if bare.startswith("http://") or bare.startswith("https://"):
        return bare
    return None


# ---------------------------------------------------------------------------
# HTTP handler
# ---------------------------------------------------------------------------

class ImageLinkHandler(http.server.BaseHTTPRequestHandler):
    # Injected by server setup
    upload_dir: Path
    logger: logging.Logger

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
            args=(file_url, filename, self.upload_dir, self.logger),
            daemon=True,
        )
        t.start()

    def do_GET(self):  # noqa: N802
        client = self.client_address[0]
        # Strip leading slash and query string
        raw_path = self.path.lstrip("/").split("?")[0]
        filename = urllib.parse.unquote(raw_path, encoding="utf-8")
        self.logger.info("GET /%s from %s", filename, client)

        if not filename:
            self._send_json(400, {"status": "error", "error": "No filename specified"})
            return

        # Prevent path traversal
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
        # Guess content type by extension
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


def make_handler(upload_dir: Path, logger: logging.Logger):
    """Return a handler class with upload_dir and logger bound."""

    class _Handler(ImageLinkHandler):
        pass

    _Handler.upload_dir = upload_dir
    _Handler.logger = logger
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
    port, upload_dir, log_dir, tls = load_config()
    logger = setup_logging(log_dir)

    upload_dir.mkdir(parents=True, exist_ok=True)

    handler_class = make_handler(upload_dir, logger)
    server = ThreadedHTTPServer(("", port), handler_class)

    scheme = "http"
    if tls["enabled"]:
        wrap_tls(server, tls, logger)
        scheme = "https"

    logger.info(
        "AutoGLM POST server starting on %s://0.0.0.0:%d", scheme, port
    )
    logger.info("Upload dir : %s", upload_dir.resolve())
    logger.info("Log dir    : %s", log_dir.resolve())

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
