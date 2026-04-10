#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
AutoGLM multipart server — stdlib-only HTTP server.
Receives image links from an Android Auto.js app via multipart/form-data.
"""

import configparser
import email.parser
import json
import logging
import logging.handlers
import mimetypes
import os
import posixpath
import re
import signal
import shutil
import ssl
import sys
import threading
import urllib.error
import urllib.parse
import urllib.request
from datetime import datetime, timezone
from http.server import BaseHTTPRequestHandler, HTTPServer


def _env_str(name: str, fallback: str) -> str:
    value = os.getenv(name)
    if value is None:
        return fallback
    stripped = value.strip()
    return stripped if stripped else fallback


def _env_bool(name: str, fallback: bool) -> bool:
    value = os.getenv(name)
    if value is None or not value.strip():
        return fallback
    return value.strip().lower() in {"1", "true", "yes", "on"}


def _env_int(name: str, fallback: int, minimum: int = 0) -> int:
    value = os.getenv(name)
    if value is None or not value.strip():
        return fallback
    try:
        parsed = int(value.strip())
    except ValueError:
        return fallback
    return max(minimum, parsed)

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
PUBLIC_BASE_URL = _cfg.get("server", "public_base_url", fallback="").strip().rstrip("/")
MANIFEST_NAME = _cfg.get("server", "manifest_name", fallback="manifest.json").strip() or "manifest.json"
_HAS_BACKEND_SECTION = _cfg.has_section("backend")


def _bget(key: str, fallback: str) -> str:
    return _cfg.get("backend", key, fallback=fallback) if _HAS_BACKEND_SECTION else fallback


def _bgetboolean(key: str, fallback: bool) -> bool:
    return _cfg.getboolean("backend", key, fallback=fallback) if _HAS_BACKEND_SECTION else fallback

BACKEND_BASE_URL = _env_str("AUTOGLM_BACKEND_BASE_URL", _bget("base_url", "").strip()).rstrip("/")
BACKEND_NOTIFY_URL = _env_str("AUTOGLM_BACKEND_NOTIFY_URL", _bget("notify_url", "").strip()).rstrip("/")
if not BACKEND_NOTIFY_URL and BACKEND_BASE_URL:
    BACKEND_NOTIFY_URL = f"{BACKEND_BASE_URL}/api/sync-now"
_BACKEND_ENABLED_FALLBACK = _bgetboolean("enabled", False) or bool(BACKEND_NOTIFY_URL)
BACKEND_NOTIFY_ENABLED = _env_bool(
    "AUTOGLM_BACKEND_NOTIFY_ENABLED",
    _BACKEND_ENABLED_FALLBACK,
) and bool(BACKEND_NOTIFY_URL)
BACKEND_REQUEST_TIMEOUT = _env_int(
    "AUTOGLM_BACKEND_REQUEST_TIMEOUT_SECONDS",
    int(_bget("request_timeout_seconds", "10")),
    minimum=1,
)
BACKEND_VERIFY_SSL = _env_bool(
    "AUTOGLM_BACKEND_VERIFY_SSL",
    _bgetboolean("verify_ssl", True),
)
BACKEND_AUTH_TOKEN = _env_str("AUTOGLM_BACKEND_AUTH_TOKEN", _bget("auth_token", ""))

# TLS
_HAS_TLS_SECTION = _cfg.has_section("tls")
_LEGACY_TLS_CERT = _cfg.get("server", "tls_cert", fallback="").strip()
_LEGACY_TLS_KEY = _cfg.get("server", "tls_key", fallback="").strip()

if _HAS_TLS_SECTION:
    TLS_ENABLED = _cfg.getboolean("tls", "enabled", fallback=False)
    TLS_CERT = _cfg.get("tls", "cert_file", fallback=_LEGACY_TLS_CERT).strip()
    TLS_KEY = _cfg.get("tls", "key_file", fallback=_LEGACY_TLS_KEY).strip()
    TLS_MIN_VERSION = _cfg.get("tls", "min_tls_version", fallback="TLSv1.2").strip()
else:
    TLS_ENABLED = bool(_LEGACY_TLS_CERT and _LEGACY_TLS_KEY)
    TLS_CERT = _LEGACY_TLS_CERT
    TLS_KEY = _LEGACY_TLS_KEY
    TLS_MIN_VERSION = "TLSv1.2"

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
CHUNK_ROOT_DIR = os.path.join(LOG_DIR, ".multipart_chunks")
CHUNK_COMPLETE_DIR = os.path.join(CHUNK_ROOT_DIR, ".complete")
os.makedirs(CHUNK_ROOT_DIR, exist_ok=True)
os.makedirs(CHUNK_COMPLETE_DIR, exist_ok=True)

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


def _extract_url_from_text(text: str) -> str:
    """
    Extract a usable image URL from plain text.
    Accepts:
    - bare URL bodies
    - FILE_URL=<url> lines
    - log-file style bodies where the first non-empty line is the URL
    """
    text = str(text or "").replace("\r", "")
    if not text:
        return ""

    def _extract_first_http_url(raw: str) -> str:
        raw = str(raw or "").strip()
        if not raw:
            return ""
        if raw.startswith(("http://", "https://")):
            return raw
        m = re.search(r"(https?://[^\s'\"<>]+)", raw)
        return m.group(1) if m else ""

    for raw_line in text.splitlines():
        line = raw_line.strip()
        if not line:
            continue
        if line.upper().startswith("FILE_URL="):
            from_line = _extract_first_http_url(line.split("=", 1)[1].strip())
            if from_line:
                return from_line
        from_line = _extract_first_http_url(line)
        if from_line:
            return from_line

    bare = text.strip()
    from_bare = _extract_first_http_url(bare)
    if from_bare:
        return from_bare
    return ""


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


def _field_bytes_to_text(value) -> str:
    if value is None:
        return ""
    if isinstance(value, bytes):
        return value.decode("utf-8", errors="replace")
    return str(value)


def _extract_url_from_multipart_fields(fields: dict) -> str:
    """
    Extract a usable URL from the uploaded multipart file field.
    The client uploads a temporary text/log file whose contents contain either:
    - FILE_URL=<url>
    - or the raw URL on the first non-empty line
    """
    url = _extract_url_from_text(_field_bytes_to_text(fields.get("file")))
    if url:
        return url

    # Compatibility fallback for clients that may send the link in a text field.
    for key in ("file_url", "FILE_URL", "url", "content", "text"):
        url = _extract_url_from_text(_field_bytes_to_text(fields.get(key)))
        if url:
            return url
    return ""


_chunk_lock = threading.Lock()


def _safe_upload_id(upload_id: str) -> str:
    upload_id = str(upload_id or "").strip()
    if not upload_id:
        return ""
    if not re.fullmatch(r"[A-Za-z0-9._-]{1,128}", upload_id):
        return ""
    return upload_id


def _parse_chunk_int(fields: dict, key: str):
    raw = _field_bytes_to_text(fields.get(key)).strip()
    if raw == "":
        return None
    try:
        return int(raw)
    except ValueError:
        return None


def _get_chunk_meta(fields: dict):
    upload_id = _safe_upload_id(_field_bytes_to_text(fields.get("upload_id")))
    if not upload_id:
        return None

    chunk_index = _parse_chunk_int(fields, "chunk_index")
    chunk_count = _parse_chunk_int(fields, "chunk_count")
    if chunk_index is None or chunk_count is None:
        raise ValueError("chunk_index and chunk_count are required when upload_id is provided")
    if chunk_index < 0 or chunk_count <= 0 or chunk_index >= chunk_count:
        raise ValueError("Invalid chunk_index/chunk_count")

    return {
        "upload_id": upload_id,
        "chunk_index": chunk_index,
        "chunk_count": chunk_count,
    }


def _cleanup_chunk_dirs(max_age_seconds: int = 24 * 3600):
    now = datetime.now(timezone.utc).timestamp()
    for root in (CHUNK_ROOT_DIR, CHUNK_COMPLETE_DIR):
        try:
            entries = os.listdir(root)
        except OSError:
            continue
        for name in entries:
            path = os.path.join(root, name)
            if not os.path.isdir(path):
                continue
            try:
                age = now - os.path.getmtime(path)
            except OSError:
                continue
            if age <= max_age_seconds:
                continue
            try:
                shutil.rmtree(path)
            except OSError:
                logger.warning("Could not remove stale chunk directory %s", path)


def _write_chunk_meta(upload_dir: str, chunk_count: int, name_hint: str):
    meta_path = os.path.join(upload_dir, "meta.json")
    meta = {
        "chunk_count": chunk_count,
        "name": name_hint,
        "updated_at": _iso_now(),
    }
    with open(meta_path, "w", encoding="utf-8") as handle:
        json.dump(meta, handle, ensure_ascii=False)


def _load_chunk_meta(upload_dir: str):
    meta_path = os.path.join(upload_dir, "meta.json")
    if not os.path.isfile(meta_path):
        return None
    with open(meta_path, "r", encoding="utf-8") as handle:
        return json.load(handle)


def _get_completed_chunk_response(upload_id: str):
    marker_dir = os.path.join(CHUNK_COMPLETE_DIR, upload_id)
    marker_path = os.path.join(marker_dir, "response.json")
    if not os.path.isfile(marker_path):
        return None
    with open(marker_path, "r", encoding="utf-8") as handle:
        return json.load(handle)


def _write_completed_chunk_response(upload_id: str, response_payload: dict):
    marker_dir = os.path.join(CHUNK_COMPLETE_DIR, upload_id)
    os.makedirs(marker_dir, exist_ok=True)
    marker_path = os.path.join(marker_dir, "response.json")
    with open(marker_path, "w", encoding="utf-8") as handle:
        json.dump(response_payload, handle, ensure_ascii=False)


def _store_chunk_and_maybe_assemble(file_bytes: bytes, chunk_meta: dict, name_hint: str):
    upload_id = chunk_meta["upload_id"]
    chunk_index = chunk_meta["chunk_index"]
    chunk_count = chunk_meta["chunk_count"]

    completed = _get_completed_chunk_response(upload_id)
    if completed is not None:
        return {"completed": True, "response": completed}

    upload_dir = os.path.join(CHUNK_ROOT_DIR, upload_id)
    os.makedirs(upload_dir, exist_ok=True)

    with _chunk_lock:
        meta = _load_chunk_meta(upload_dir)
        if meta is not None and int(meta.get("chunk_count", -1)) != chunk_count:
            raise ValueError("chunk_count does not match previous chunks for this upload_id")

        _write_chunk_meta(upload_dir, chunk_count, name_hint)

        chunk_path = os.path.join(upload_dir, f"{chunk_index:08d}.part")
        with open(chunk_path, "wb") as handle:
            handle.write(file_bytes)

        missing = []
        for idx in range(chunk_count):
            candidate = os.path.join(upload_dir, f"{idx:08d}.part")
            if not os.path.isfile(candidate):
                missing.append(idx)

        if missing:
            return {
                "completed": False,
                "response": {
                    "status": "partial",
                    "upload_id": upload_id,
                    "received_chunk": chunk_index,
                    "chunk_count": chunk_count,
                    "next_chunk": missing[0],
                },
            }

        assembled = bytearray()
        for idx in range(chunk_count):
            candidate = os.path.join(upload_dir, f"{idx:08d}.part")
            with open(candidate, "rb") as handle:
                assembled.extend(handle.read())

        try:
            shutil.rmtree(upload_dir)
        except OSError:
            logger.warning("Could not clean chunk directory %s", upload_dir)

    return {
        "completed": True,
        "upload_text": assembled.decode("utf-8", errors="replace"),
        "upload_id": upload_id,
    }


def _build_public_base_url(handler) -> str:
    if PUBLIC_BASE_URL:
        return PUBLIC_BASE_URL.rstrip("/")
    host = handler.headers.get("Host", "").strip()
    if not host:
        return ""
    scheme = "https" if TLS_ENABLED else "http"
    return f"{scheme}://{host}"


def _build_manifest(base_url: str) -> dict:
    files = []
    for name in sorted(
        os.listdir(UPLOAD_DIR),
        key=lambda item: os.path.getmtime(os.path.join(UPLOAD_DIR, item)),
        reverse=True,
    ):
        path = os.path.join(UPLOAD_DIR, name)
        if not os.path.isfile(path):
            continue
        files.append(
            {
                "name": name,
                "size": os.path.getsize(path),
                "mtime": datetime.fromtimestamp(os.path.getmtime(path), timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
                "url": f"{base_url}/{urllib.parse.quote(name)}" if base_url else name,
            }
        )
    manifest = {
        "generated_at": _iso_now(),
        "count": len(files),
        "files": files,
    }
    if BACKEND_BASE_URL or BACKEND_NOTIFY_URL:
        manifest["backend"] = {
            "configured": bool(BACKEND_NOTIFY_ENABLED),
            "base_url": BACKEND_BASE_URL,
            "notify_url": BACKEND_NOTIFY_URL,
        }
    return manifest


def _notify_backend_sync(payload: dict) -> None:
    if not BACKEND_NOTIFY_ENABLED or not BACKEND_NOTIFY_URL:
        return

    body = json.dumps(payload, ensure_ascii=False).encode("utf-8")
    request = urllib.request.Request(
        BACKEND_NOTIFY_URL,
        data=body,
        headers={
            "Content-Type": "application/json; charset=utf-8",
            "User-Agent": "AutoGLM-Frontend/1.0",
        },
        method="POST",
    )
    if BACKEND_AUTH_TOKEN:
        request.add_header("X-AutoGLM-Token", BACKEND_AUTH_TOKEN)

    context = None if BACKEND_VERIFY_SSL else ssl._create_unverified_context()
    try:
        with urllib.request.urlopen(
            request,
            timeout=BACKEND_REQUEST_TIMEOUT,
            context=context,
        ) as response:
            response.read()
        logger.info("Triggered OCR backend sync: %s", BACKEND_NOTIFY_URL)
    except urllib.error.URLError as exc:
        logger.warning("Failed to notify OCR backend %s: %s", BACKEND_NOTIFY_URL, exc)


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
        _notify_backend_sync(
            {
                "source": "frontend-multipart",
                "event": "download_ready",
                "reason": f"{filename} downloaded",
                "filename": filename,
                "file_url": url,
                "download_log_name": os.path.basename(dl_log_path),
            }
        )
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
        request_path = parsed_path.path.lstrip("/")
        if request_path == MANIFEST_NAME:
            self._send_json(200, _build_manifest(_build_public_base_url(self)))
            return
        filename = posixpath.basename(request_path)
        if not filename:
            self._send_json(400, {"status": "error", "message": "No filename specified"})
            return
        self._serve_file(filename)

    # ------------------------------------------------------------------
    # Upload handler
    # ------------------------------------------------------------------

    def _handle_upload(self):
        content_type = self.headers.get("Content-Type", "")

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

        is_multipart = "multipart/form-data" in content_type.lower()
        if not is_multipart:
            logger.warning(
                "Rejected non-multipart upload on multipart port (Content-Type=%s)",
                content_type or "<empty>",
            )
            self._send_json(
                400,
                {
                    "status": "error",
                    "message": (
                        "Expected multipart/form-data on port 39283. "
                        "Use port 39282 for form/text/json POST."
                    ),
                },
            )
            return

        try:
            fields = _parse_multipart(content_type, body)
        except Exception as exc:  # noqa: BLE001
            logger.error("Multipart parse error: %s", exc)
            self._send_json(400, {"status": "error", "message": f"Multipart parse error: {exc}"})
            return

        upload_type = _field_bytes_to_text(fields.get("type")).strip()
        name_hint = _field_bytes_to_text(fields.get("name")).strip()
        file_bytes = fields.get("file")

        try:
            chunk_meta = _get_chunk_meta(fields)
        except ValueError as exc:
            self._send_json(400, {"status": "error", "message": str(exc)})
            return

        if chunk_meta is not None:
            if not file_bytes:
                self._send_json(400, {"status": "error", "message": "Missing 'file' field for chunk upload"})
                return
            try:
                chunk_result = _store_chunk_and_maybe_assemble(file_bytes, chunk_meta, name_hint)
            except ValueError as exc:
                self._send_json(400, {"status": "error", "message": str(exc)})
                return

            if "response" in chunk_result and not chunk_result["completed"]:
                self._send_json(202, chunk_result["response"])
                return

            if "response" in chunk_result and chunk_result["completed"]:
                self._send_json(200, chunk_result["response"])
                return

            url = _extract_url_from_text(chunk_result.get("upload_text", ""))
            if not url:
                self._send_json(
                    400,
                    {
                        "status": "error",
                        "message": "Could not extract FILE_URL from assembled chunk upload",
                    },
                )
                return
            completed_upload_id = chunk_result.get("upload_id", "")
        else:
            url = _extract_url_from_multipart_fields(fields)
            if not url:
                self._send_json(
                    400,
                    {
                        "status": "error",
                        "message": "Could not extract FILE_URL from uploaded multipart log file",
                    },
                )
                return
            completed_upload_id = ""

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
        response_payload = {"status": "ok", "received": url}
        if completed_upload_id:
            response_payload["upload_id"] = completed_upload_id
            _write_completed_chunk_response(completed_upload_id, response_payload)
        self._send_json(200, response_payload)

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
    _cleanup_chunk_dirs()
    server_address = ("", PORT)
    _server = HTTPServer(server_address, MultipartHandler)

    if TLS_ENABLED:
        ctx = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
        ctx.minimum_version = (
            ssl.TLSVersion.TLSv1_3 if TLS_MIN_VERSION == "TLSv1.3" else ssl.TLSVersion.TLSv1_2
        )
        ctx.load_cert_chain(certfile=TLS_CERT, keyfile=TLS_KEY)
        _server.socket = ctx.wrap_socket(_server.socket, server_side=True)
        proto = "HTTPS"
    else:
        proto = "HTTP"

    logger.info("AutoGLM multipart server listening on %s port %d", proto, PORT)
    logger.info("Upload directory : %s", UPLOAD_DIR)
    logger.info("Log directory    : %s", LOG_DIR)
    logger.info("Manifest         : %s", MANIFEST_NAME)
    if PUBLIC_BASE_URL:
        logger.info("Public URL       : %s", PUBLIC_BASE_URL)
    if BACKEND_NOTIFY_ENABLED:
        logger.info("Backend notify URL: %s", BACKEND_NOTIFY_URL)
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
