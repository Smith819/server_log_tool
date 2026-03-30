#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
AutoGLM multipart server — stdlib-only HTTP server.
Receives image links from an Android Auto.js app via multipart/form-data.
"""

import configparser
import email.parser
import email.policy
import io
import json
import logging
import logging.handlers
import mimetypes
import os
import posixpath
import re
import signal
import ssl
import sys
import threading
import urllib.parse
import urllib.request
from datetime import datetime, timezone
from http.server import BaseHTTPRequestHandler, HTTPServer

# ---------------------------------------------------------------------------
# Configuration
# ---------------------------------------------------------------------------

_SCRIPT_DIR = os.path.dirname(os.path.abspath(__file__))
_CONFIG_PATH = os.path.join(_SCRIPT_DIR, "config.ini")

_cfg = configparser.ConfigParser()
_cfg.read(_CONFIG_PATH, encoding="utf-8")

PORT = _cfg.getint("server", "multipart_port", fallback=39283)
_RAW_UPLOAD_DIR = _cfg.get("server", "upload_dir", fallback="./uploads")
_RAW_LOG_DIR = _cfg.get("server", "log_dir", fallback="./logs")
DOWNLOAD_TIMEOUT = _cfg.getint("server", "download_timeout", fallback=60)
MAX_IMAGE_SIZE = _cfg.getint("server", "max_image_size", fallback=52428800)  # 50 MB

# TLS — leave blank to run plain HTTP
TLS_CERT = _cfg.get("server", "tls_cert", fallback="").strip()
TLS_KEY  = _cfg.get("server", "tls_key",  fallback="").strip()

# Resolve relative paths relative to the script directory
UPLOAD_DIR = os.path.normpath(
    os.path.join(_SCRIPT_DIR, _RAW_UPLOAD_DIR)
    if not os.path.isabs(_RAW_UPLOAD_DIR)
    else _RAW_UPLOAD_DIR
)
LOG_DIR = os.path.normpath(
    os.path.join(_SCRIPT_DIR, _RAW_LOG_DIR)
    if not os.path.isabs(_RAW_LOG_DIR)
    else _RAW_LOG_DIR
)

os.makedirs(UPLOAD_DIR, exist_ok=True)
os.makedirs(LOG_DIR, exist_ok=True)

# ---------------------------------------------------------------------------
# Logging
# ---------------------------------------------------------------------------

_LOG_FILE = os.path.join(LOG_DIR, "server_multipart.log")

logger = logging.getLogger("autoglm_multipart")
logger.setLevel(logging.DEBUG)

_fmt = logging.Formatter(
    "%(asctime)s [%(levelname)s] %(threadName)s — %(message)s",
    datefmt="%Y-%m-%dT%H:%M:%S",
)

_fh = logging.handlers.RotatingFileHandler(
    _LOG_FILE,
    maxBytes=10 * 1024 * 1024,  # 10 MB
    backupCount=5,
    encoding="utf-8",
)
_fh.setFormatter(_fmt)
logger.addHandler(_fh)

_sh = logging.StreamHandler(sys.stdout)
_sh.setFormatter(_fmt)
logger.addHandler(_sh)

# ---------------------------------------------------------------------------
# URL / filename validation
# ---------------------------------------------------------------------------

_ALLOWED_EXTENSIONS = frozenset({".jpg", ".jpeg", ".png", ".gif", ".webp", ".bmp"})


def _validate_url(url: str):
    """
    Validate image URL and extract a clean filename.
    Returns (filename, error_message). error_message is None on success.
    """
    url = url.strip()
    if not url.startswith(("http://", "https://")):
        return None, "URL must start with http:// or https://"

    parsed = urllib.parse.urlparse(url)
    # Strip query / fragment from path before extracting the filename
    path_only = parsed.path
    filename = posixpath.basename(path_only)
    if not filename:
        return None, "URL path must contain a non-empty filename segment"

    _, ext = posixpath.splitext(filename)
    if ext.lower() not in _ALLOWED_EXTENSIONS:
        return None, (
            f"Filename extension '{ext}' is not allowed. "
            f"Allowed: {', '.join(sorted(_ALLOWED_EXTENSIONS))}"
        )

    return filename, None


# ---------------------------------------------------------------------------
# Multipart parsing (stdlib only)
# ---------------------------------------------------------------------------

def _parse_multipart(content_type: str, body: bytes) -> dict:
    """
    Parse a multipart/form-data body.
    Returns a dict mapping field name -> bytes value.
    Uses email.parser for robustness with UTF-8 filenames.
    """
    # email.parser expects the full MIME message with headers
    # We reconstruct a minimal MIME message
    full_message = (
        f"Content-Type: {content_type}\r\n\r\n"
    ).encode("utf-8") + body

    msg = email.parser.BytesParser().parsebytes(full_message)

    fields = {}
    if msg.is_multipart():
        for part in msg.get_payload():
            # part is a Message object
            disposition = part.get("Content-Disposition", "")
            # Extract field name
            name_match = re.search(r'name="([^"]*)"|name=([^;\s]+)', disposition)
            if not name_match:
                continue
            name = name_match.group(1) if name_match.group(1) is not None else name_match.group(2)
            payload = part.get_payload(decode=True)
            if payload is None:
                # get_payload may return string for text parts without decode
                raw = part.get_payload()
                if isinstance(raw, str):
                    payload = raw.encode("utf-8")
                else:
                    payload = b""
            fields[name] = payload
    return fields


# ---------------------------------------------------------------------------
# Background download worker
# ---------------------------------------------------------------------------

_iso_now = lambda: datetime.now(timezone.utc).isoformat(timespec="seconds")


def _download_image(url: str, filename: str) -> None:
    """
    Download *url* to UPLOAD_DIR/<filename> and write log files.
    Runs in a background thread.
    """
    basename, _ = os.path.splitext(filename)
    save_path = os.path.join(UPLOAD_DIR, filename)
    pre_log_path = os.path.join(UPLOAD_DIR, f"{basename}.log")
    dl_log_path = os.path.join(UPLOAD_DIR, f"{basename}_download.log")

    # Pre-run log
    try:
        with open(pre_log_path, "w", encoding="utf-8") as f:
            f.write(
                f"FILE_URL={url}\n"
                f"TIME={_iso_now()}\n"
                f"STATUS=received\n"
            )
    except OSError as exc:
        logger.warning("Could not write pre-run log %s: %s", pre_log_path, exc)

    # Download
    logger.info("Downloading %s -> %s", url, save_path)
    try:
        req = urllib.request.Request(
            url,
            headers={"User-Agent": "AutoGLM-Server/1.0"},
        )
        with urllib.request.urlopen(req, timeout=DOWNLOAD_TIMEOUT) as resp:
            data = resp.read(MAX_IMAGE_SIZE + 1)
        if len(data) > MAX_IMAGE_SIZE:
            raise ValueError(
                f"Image exceeds max allowed size of {MAX_IMAGE_SIZE} bytes"
            )
        with open(save_path, "wb") as f:
            f.write(data)
        dl_status = "success"
        logger.info("Download complete: %s (%d bytes)", filename, len(data))
    except Exception as exc:  # noqa: BLE001
        dl_status = f"failed: {exc}"
        logger.error("Download failed for %s: %s", url, exc)
        save_path = ""

    # Download result log
    try:
        with open(dl_log_path, "w", encoding="utf-8") as f:
            f.write(
                f"FILE_URL={url}\n"
                f"DOWNLOAD_TIME={_iso_now()}\n"
                f"DOWNLOAD_STATUS={dl_status}\n"
                f"SAVED_AS={save_path}\n"
            )
    except OSError as exc:
        logger.warning("Could not write download log %s: %s", dl_log_path, exc)


# ---------------------------------------------------------------------------
# HTTP request handler
# ---------------------------------------------------------------------------

class MultipartHandler(BaseHTTPRequestHandler):
    server_version = "AutoGLM-Multipart/1.0"
    protocol_version = "HTTP/1.1"

    # ------------------------------------------------------------------
    # Routing
    # ------------------------------------------------------------------

    def do_POST(self):
        parsed_path = urllib.parse.urlparse(self.path)
        if parsed_path.path == "/":
            self._handle_upload()
        else:
            self._send_json(404, {"status": "error", "message": "Not found"})

    def do_GET(self):
        parsed_path = urllib.parse.urlparse(self.path)
        filename = posixpath.basename(parsed_path.path.lstrip("/"))
        if not filename:
            self._send_json(400, {"status": "error", "message": "No filename specified"})
            return
        self._serve_file(filename)

    # ------------------------------------------------------------------
    # Upload handler
    # ------------------------------------------------------------------

    def _handle_upload(self):
        content_type = self.headers.get("Content-Type", "")
        if "multipart/form-data" not in content_type:
            self._send_json(
                400,
                {"status": "error", "message": "Expected multipart/form-data"},
            )
            return

        length_str = self.headers.get("Content-Length")
        if length_str is None:
            self._send_json(411, {"status": "error", "message": "Content-Length required"})
            return

        try:
            length = int(length_str)
        except ValueError:
            self._send_json(400, {"status": "error", "message": "Invalid Content-Length"})
            return

        body = self.rfile.read(length)

        try:
            fields = _parse_multipart(content_type, body)
        except Exception as exc:  # noqa: BLE001
            logger.error("Multipart parse error: %s", exc)
            self._send_json(400, {"status": "error", "message": f"Multipart parse error: {exc}"})
            return

        # Extract the 'file' field which contains the image URL as text
        file_value = fields.get("file")
        if file_value is None:
            self._send_json(400, {"status": "error", "message": "Missing 'file' field"})
            return

        if isinstance(file_value, bytes):
            url = file_value.decode("utf-8", errors="replace").strip()
        else:
            url = str(file_value).strip()

        upload_type = fields.get("type", b"").decode("utf-8", errors="replace")
        name_hint = fields.get("name", b"").decode("utf-8", errors="replace")

        logger.info(
            "Upload received — type=%s, name=%s, url=%s",
            upload_type,
            name_hint,
            url,
        )

        # Validate URL
        filename, err = _validate_url(url)
        if err:
            logger.warning("Invalid URL '%s': %s", url, err)
            self._send_json(400, {"status": "error", "message": err})
            return

        # Respond immediately
        self._send_json(200, {"status": "ok", "received": url})

        # Background download
        t = threading.Thread(
            target=_download_image,
            args=(url, filename),
            name=f"Download-{filename}",
            daemon=True,
        )
        t.start()

    # ------------------------------------------------------------------
    # Static file server (GET)
    # ------------------------------------------------------------------

    def _serve_file(self, filename: str):
        # Prevent directory traversal
        safe_filename = os.path.basename(filename)
        file_path = os.path.join(UPLOAD_DIR, safe_filename)

        if not os.path.isfile(file_path):
            self._send_json(404, {"status": "error", "message": f"File not found: {safe_filename}"})
            return

        mime_type, _ = mimetypes.guess_type(file_path)
        if mime_type is None:
            mime_type = "application/octet-stream"

        try:
            file_size = os.path.getsize(file_path)
            self.send_response(200)
            self.send_header("Content-Type", mime_type)
            self.send_header("Content-Length", str(file_size))
            self.end_headers()
            with open(file_path, "rb") as f:
                while True:
                    chunk = f.read(65536)
                    if not chunk:
                        break
                    self.wfile.write(chunk)
        except OSError as exc:
            logger.error("Error serving file %s: %s", file_path, exc)

    # ------------------------------------------------------------------
    # Helpers
    # ------------------------------------------------------------------

    def _send_json(self, code: int, payload: dict):
        body = json.dumps(payload, ensure_ascii=False).encode("utf-8")
        self.send_response(code)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def log_message(self, fmt, *args):  # silence default BaseHTTPRequestHandler stderr
        logger.debug("%s - - %s", self.client_address[0], fmt % args)


# ---------------------------------------------------------------------------
# Server lifecycle
# ---------------------------------------------------------------------------

_server = None
_shutdown_event = threading.Event()


def run_server():
    global _server
    server_address = ("", PORT)
    _server = HTTPServer(server_address, MultipartHandler)

    if TLS_CERT and TLS_KEY:
        ctx = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
        ctx.load_cert_chain(certfile=TLS_CERT, keyfile=TLS_KEY)
        _server.socket = ctx.wrap_socket(_server.socket, server_side=True)
        proto = "HTTPS"
    else:
        proto = "HTTP"

    logger.info("AutoGLM multipart server listening on %s port %d", proto, PORT)
    logger.info("Upload directory : %s", UPLOAD_DIR)
    logger.info("Log directory    : %s", LOG_DIR)
    _server.serve_forever()


def _shutdown_handler(signum, frame):
    sig_name = (
        signal.Signals(signum).name if hasattr(signal, "Signals") else str(signum)
    )
    logger.info("Received signal %s — shutting down gracefully...", sig_name)
    if _server is not None:
        t = threading.Thread(target=_server.shutdown, daemon=True)
        t.start()
    _shutdown_event.set()


# ---------------------------------------------------------------------------
# Entry point
# ---------------------------------------------------------------------------

if __name__ == "__main__":
    signal.signal(signal.SIGTERM, _shutdown_handler)
    signal.signal(signal.SIGINT, _shutdown_handler)

    server_thread = threading.Thread(
        target=run_server,
        name="HTTPServer",
        daemon=True,
    )
    server_thread.start()

    # Block main thread until shutdown signal
    try:
        _shutdown_event.wait()
    except KeyboardInterrupt:
        _shutdown_handler(signal.SIGINT, None)

    server_thread.join(timeout=10)
    logger.info("Server stopped.")
