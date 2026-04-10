from __future__ import annotations

import configparser
import json
import mimetypes
import os
import re
import time
import urllib.error
import urllib.parse
import urllib.request
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from tls_context import build_client_ssl_context, load_tls_settings

SCRIPT_DIR = Path(__file__).resolve().parent
CONFIG_FILE = SCRIPT_DIR / "config.ini"
CACHE_TTL_SECONDS = 3600
USER_AGENT = (
    "Mozilla/5.0 (Linux; Android 13; MEIZU 20 Pro) "
    "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/144.0.0.0 Mobile Safari/537.36"
)
CHALLENGE_PATTERN = re.compile(
    r'a=toNumbers\("([0-9a-fA-F]+)"\),b=toNumbers\("([0-9a-fA-F]+)"\),c=toNumbers\("([0-9a-fA-F]+)"\)'
)

SBOX = [
    0x63,0x7C,0x77,0x7B,0xF2,0x6B,0x6F,0xC5,0x30,0x01,0x67,0x2B,0xFE,0xD7,0xAB,0x76,
    0xCA,0x82,0xC9,0x7D,0xFA,0x59,0x47,0xF0,0xAD,0xD4,0xA2,0xAF,0x9C,0xA4,0x72,0xC0,
    0xB7,0xFD,0x93,0x26,0x36,0x3F,0xF7,0xCC,0x34,0xA5,0xE5,0xF1,0x71,0xD8,0x31,0x15,
    0x04,0xC7,0x23,0xC3,0x18,0x96,0x05,0x9A,0x07,0x12,0x80,0xE2,0xEB,0x27,0xB2,0x75,
    0x09,0x83,0x2C,0x1A,0x1B,0x6E,0x5A,0xA0,0x52,0x3B,0xD6,0xB3,0x29,0xE3,0x2F,0x84,
    0x53,0xD1,0x00,0xED,0x20,0xFC,0xB1,0x5B,0x6A,0xCB,0xBE,0x39,0x4A,0x4C,0x58,0xCF,
    0xD0,0xEF,0xAA,0xFB,0x43,0x4D,0x33,0x85,0x45,0xF9,0x02,0x7F,0x50,0x3C,0x9F,0xA8,
    0x51,0xA3,0x40,0x8F,0x92,0x9D,0x38,0xF5,0xBC,0xB6,0xDA,0x21,0x10,0xFF,0xF3,0xD2,
    0xCD,0x0C,0x13,0xEC,0x5F,0x97,0x44,0x17,0xC4,0xA7,0x7E,0x3D,0x64,0x5D,0x19,0x73,
    0x60,0x81,0x4F,0xDC,0x22,0x2A,0x90,0x88,0x46,0xEE,0xB8,0x14,0xDE,0x5E,0x0B,0xDB,
    0xE0,0x32,0x3A,0x0A,0x49,0x06,0x24,0x5C,0xC2,0xD3,0xAC,0x62,0x91,0x95,0xE4,0x79,
    0xE7,0xC8,0x37,0x6D,0x8D,0xD5,0x4E,0xA9,0x6C,0x56,0xF4,0xEA,0x65,0x7A,0xAE,0x08,
    0xBA,0x78,0x25,0x2E,0x1C,0xA6,0xB4,0xC6,0xE8,0xDD,0x74,0x1F,0x4B,0xBD,0x8B,0x8A,
    0x70,0x3E,0xB5,0x66,0x48,0x03,0xF6,0x0E,0x61,0x35,0x57,0xB9,0x86,0xC1,0x1D,0x9E,
    0xE1,0xF8,0x98,0x11,0x69,0xD9,0x8E,0x94,0x9B,0x1E,0x87,0xE9,0xCE,0x55,0x28,0xDF,
    0x8C,0xA1,0x89,0x0D,0xBF,0xE6,0x42,0x68,0x41,0x99,0x2D,0x0F,0xB0,0x54,0xBB,0x16,
]
RSBOX = [
    0x52,0x09,0x6A,0xD5,0x30,0x36,0xA5,0x38,0xBF,0x40,0xA3,0x9E,0x81,0xF3,0xD7,0xFB,
    0x7C,0xE3,0x39,0x82,0x9B,0x2F,0xFF,0x87,0x34,0x8E,0x43,0x44,0xC4,0xDE,0xE9,0xCB,
    0x54,0x7B,0x94,0x32,0xA6,0xC2,0x23,0x3D,0xEE,0x4C,0x95,0x0B,0x42,0xFA,0xC3,0x4E,
    0x08,0x2E,0xA1,0x66,0x28,0xD9,0x24,0xB2,0x76,0x5B,0xA2,0x49,0x6D,0x8B,0xD1,0x25,
    0x72,0xF8,0xF6,0x64,0x86,0x68,0x98,0x16,0xD4,0xA4,0x5C,0xCC,0x5D,0x65,0xB6,0x92,
    0x6C,0x70,0x48,0x50,0xFD,0xED,0xB9,0xDA,0x5E,0x15,0x46,0x57,0xA7,0x8D,0x9D,0x84,
    0x90,0xD8,0xAB,0x00,0x8C,0xBC,0xD3,0x0A,0xF7,0xE4,0x58,0x05,0xB8,0xB3,0x45,0x06,
    0xD0,0x2C,0x1E,0x8F,0xCA,0x3F,0x0F,0x02,0xC1,0xAF,0xBD,0x03,0x01,0x13,0x8A,0x6B,
    0x3A,0x91,0x11,0x41,0x4F,0x67,0xDC,0xEA,0x97,0xF2,0xCF,0xCE,0xF0,0xB4,0xE6,0x73,
    0x96,0xAC,0x74,0x22,0xE7,0xAD,0x35,0x85,0xE2,0xF9,0x37,0xE8,0x1C,0x75,0xDF,0x6E,
    0x47,0xF1,0x1A,0x71,0x1D,0x29,0xC5,0x89,0x6F,0xB7,0x62,0x0E,0xAA,0x18,0xBE,0x1B,
    0xFC,0x56,0x3E,0x4B,0xC6,0xD2,0x79,0x20,0x9A,0xDB,0xC0,0xFE,0x78,0xCD,0x5A,0xF4,
    0x1F,0xDD,0xA8,0x33,0x88,0x07,0xC7,0x31,0xB1,0x12,0x10,0x59,0x27,0x80,0xEC,0x5F,
    0x60,0x51,0x7F,0xA9,0x19,0xB5,0x4A,0x0D,0x2D,0xE5,0x7A,0x9F,0x93,0xC9,0x9C,0xEF,
    0xA0,0xE0,0x3B,0x4D,0xAE,0x2A,0xF5,0xB0,0xC8,0xEB,0xBB,0x3C,0x83,0x53,0x99,0x61,
    0x17,0x2B,0x04,0x7E,0xBA,0x77,0xD6,0x26,0xE1,0x69,0x14,0x63,0x55,0x21,0x0C,0x7D,
]
RCON = [0x00,0x01,0x02,0x04,0x08,0x10,0x20,0x40,0x80,0x1B,0x36]


@dataclass
class UploadConfig:
    enabled: bool
    verify_ssl: bool
    client_tls_enabled: bool
    client_tls_cert_file: Path | None
    client_tls_key_file: Path | None
    client_tls_min_tls_version: str
    cloudreve_username: str
    cloudreve_password: str
    cloudreve_upload_path: str
    cloudreve_purchase_ticket: str
    caihong_cookie: str
    caihong_show: int
    caihong_ispwd: int
    caihong_pwd: str
    runtime_cache_dir: Path


class UploadError(RuntimeError):
    pass


class JsonCache:
    def __init__(self, path: Path, ttl_seconds: int = CACHE_TTL_SECONDS):
        self.path = path
        self.ttl_seconds = ttl_seconds

    def _load_all(self) -> dict[str, Any]:
        if not self.path.exists():
            return {}
        try:
            return json.loads(self.path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            return {}

    def _save_all(self, data: dict[str, Any]) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.path.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")

    def get(self, key: str) -> dict[str, Any] | None:
        data = self._load_all()
        item = data.get(key)
        if not isinstance(item, dict):
            return None
        expires_at = float(item.get("expires_at", 0) or 0)
        if expires_at <= time.time():
            data.pop(key, None)
            self._save_all(data)
            return None
        return item

    def put(self, key: str, value: dict[str, Any], ttl_seconds: int | None = None) -> None:
        data = self._load_all()
        ttl = self.ttl_seconds if ttl_seconds is None else ttl_seconds
        value = dict(value)
        value["expires_at"] = time.time() + ttl
        data[key] = value
        self._save_all(data)

    def remove(self, key: str) -> None:
        data = self._load_all()
        if key in data:
            data.pop(key, None)
            self._save_all(data)


def load_config(config_file: Path = CONFIG_FILE) -> UploadConfig:
    cfg = configparser.ConfigParser()
    cfg.read(str(config_file), encoding="utf-8")
    runtime_cache_dir = Path(cfg.get("ocr_sync", "runtime_cache_dir", fallback=str(SCRIPT_DIR / "runtime_cache")))
    client_tls_enabled, client_tls_cert_file, client_tls_key_file, client_tls_min_tls_version = load_tls_settings(cfg, "client_tls")
    return UploadConfig(
        enabled=cfg.getboolean("upload", "enabled", fallback=False),
        verify_ssl=cfg.getboolean("upload", "verify_ssl", fallback=True),
        client_tls_enabled=client_tls_enabled,
        client_tls_cert_file=client_tls_cert_file,
        client_tls_key_file=client_tls_key_file,
        client_tls_min_tls_version=client_tls_min_tls_version,
        cloudreve_username=cfg.get("upload", "cloudreve_username", fallback="").strip(),
        cloudreve_password=cfg.get("upload", "cloudreve_password", fallback="").strip(),
        cloudreve_upload_path=cfg.get("upload", "cloudreve_upload_path", fallback="my").strip() or "my",
        cloudreve_purchase_ticket=cfg.get("upload", "cloudreve_purchase_ticket", fallback="").strip(),
        caihong_cookie=cfg.get("upload", "caihong_cookie", fallback="").strip(),
        caihong_show=cfg.getint("upload", "caihong_show", fallback=1),
        caihong_ispwd=cfg.getint("upload", "caihong_ispwd", fallback=0),
        caihong_pwd=cfg.get("upload", "caihong_pwd", fallback="").strip(),
        runtime_cache_dir=runtime_cache_dir,
    )


def detect_provider(original_file_url: str) -> str:
    url = str(original_file_url or "").lower()
    if "/api/v4/" in url:
        return "cloudreve"
    if "down.php" in url:
        return "caihong"
    raise UploadError(f"Unsupported upload target for URL: {original_file_url}")


def upload_result_file(file_path: str | Path, original_file_url: str, config: UploadConfig | None = None) -> dict[str, Any]:
    cfg = config or load_config()
    if not cfg.enabled:
        return {"ok": False, "provider": None, "msg": "Upload disabled in config.ini"}
    provider = detect_provider(original_file_url)
    if provider == "cloudreve":
        return upload_to_cloudreve(Path(file_path), original_file_url, cfg)
    if provider == "caihong":
        return upload_to_caihong(Path(file_path), original_file_url, cfg)
    raise UploadError(f"Unsupported provider: {provider}")


def _ssl_context(*, verify_ssl: bool, tls_enabled: bool, tls_cert_file: Path | None, tls_key_file: Path | None, tls_min_tls_version: str):
    return build_client_ssl_context(
        verify_ssl=verify_ssl,
        tls_enabled=tls_enabled,
        cert_file=tls_cert_file,
        key_file=tls_key_file,
        min_tls_version=tls_min_tls_version,
    )


def _read_body(response) -> tuple[int, dict[str, str], bytes, str]:
    body = response.read()
    headers = {k: v for k, v in response.headers.items()}
    text = body.decode("utf-8", errors="replace")
    return response.getcode(), headers, body, text


def _request(
    method: str,
    url: str,
    *,
    body: bytes | None = None,
    headers: dict[str, str] | None = None,
    verify_ssl: bool = True,
    tls_enabled: bool = False,
    tls_cert_file: Path | None = None,
    tls_key_file: Path | None = None,
    tls_min_tls_version: str = "TLSv1.2",
) -> tuple[int, dict[str, str], bytes, str]:
    req = urllib.request.Request(url, data=body, method=method)
    for key, value in (headers or {}).items():
        req.add_header(key, value)
    try:
        with urllib.request.urlopen(
            req,
            context=_ssl_context(
                verify_ssl=verify_ssl,
                tls_enabled=tls_enabled,
                tls_cert_file=tls_cert_file,
                tls_key_file=tls_key_file,
                tls_min_tls_version=tls_min_tls_version,
            ),
            timeout=120,
        ) as response:
            return _read_body(response)
    except urllib.error.HTTPError as exc:
        return _read_body(exc)


def _json_request(
    method: str,
    url: str,
    payload: dict[str, Any] | None,
    *,
    headers: dict[str, str] | None = None,
    verify_ssl: bool = True,
    tls_enabled: bool = False,
    tls_cert_file: Path | None = None,
    tls_key_file: Path | None = None,
    tls_min_tls_version: str = "TLSv1.2",
) -> tuple[int, dict[str, str], dict[str, Any] | None, str]:
    encoded = None if payload is None else json.dumps(payload, ensure_ascii=False).encode("utf-8")
    request_headers = {
        "Accept": "application/json",
        "Content-Type": "application/json",
        "User-Agent": USER_AGENT,
    }
    request_headers.update(headers or {})
    status, response_headers, _, text = _request(
        method,
        url,
        body=encoded,
        headers=request_headers,
        verify_ssl=verify_ssl,
        tls_enabled=tls_enabled,
        tls_cert_file=tls_cert_file,
        tls_key_file=tls_key_file,
        tls_min_tls_version=tls_min_tls_version,
    )
    try:
        return status, response_headers, json.loads(text) if text else None, text
    except json.JSONDecodeError:
        return status, response_headers, None, text


def _cache_key(base_url: str, username: str) -> str:
    return f"{base_url.lower()}::{username.lower()}"


def _cloudreve_token_cache(config: UploadConfig) -> JsonCache:
    return JsonCache(config.runtime_cache_dir / "cloudreve_token_cache.json")


def _caihong_cache(config: UploadConfig) -> JsonCache:
    return JsonCache(config.runtime_cache_dir / "caihong_session_cache.json")


def _cloudreve_base_url(original_file_url: str) -> str:
    parsed = urllib.parse.urlsplit(original_file_url)
    return f"{parsed.scheme}://{parsed.netloc}"


def _get_token_from_cache(base_url: str, config: UploadConfig) -> str | None:
    cache = _cloudreve_token_cache(config)
    cached = cache.get(_cache_key(base_url, config.cloudreve_username))
    if cached and cached.get("token"):
        return str(cached["token"])
    return None


def _put_token_cache(base_url: str, token: str, config: UploadConfig) -> None:
    _cloudreve_token_cache(config).put(
        _cache_key(base_url, config.cloudreve_username),
        {"token": token},
        ttl_seconds=CACHE_TTL_SECONDS,
    )


def _remove_token_cache(base_url: str, config: UploadConfig) -> None:
    _cloudreve_token_cache(config).remove(_cache_key(base_url, config.cloudreve_username))


def _get_cloudreve_token(base_url: str, config: UploadConfig, force_refresh: bool = False) -> str:
    if not force_refresh:
        token = _get_token_from_cache(base_url, config)
        if token:
            return token
    token = _cloudreve_login(base_url, config)
    _put_token_cache(base_url, token, config)
    return token


def _cloudreve_login(base_url: str, config: UploadConfig) -> str:
    if not config.cloudreve_username or not config.cloudreve_password:
        raise UploadError("Cloudreve credentials are missing in [upload]")
    status, _, data, raw = _json_request(
        "POST",
        f"{base_url}/api/v4/session/token",
        {"email": config.cloudreve_username, "password": config.cloudreve_password},
        verify_ssl=config.verify_ssl,
        tls_enabled=config.client_tls_enabled,
        tls_cert_file=config.client_tls_cert_file,
        tls_key_file=config.client_tls_key_file,
        tls_min_tls_version=config.client_tls_min_tls_version,
    )
    if status >= 400 or not isinstance(data, dict) or data.get("code") != 0:
        raise UploadError(f"Cloudreve login failed: {data.get('msg') if isinstance(data, dict) else raw}")
    token_data = ((data.get("data") or {}).get("token"))
    access_token = token_data.get("access_token") if isinstance(token_data, dict) else token_data
    if not access_token:
        raise UploadError("Cloudreve login did not return access_token")
    return str(access_token)


def _build_download_result(download_url: str) -> dict[str, Any]:
    parsed = urllib.parse.urlsplit(download_url)
    query_items = [(k, v) for k, v in urllib.parse.parse_qsl(parsed.query, keep_blank_values=True) if k != "download"]
    query_items.append(("download", "1"))
    direct_url = urllib.parse.urlunsplit((parsed.scheme, parsed.netloc, parsed.path, urllib.parse.urlencode(query_items), parsed.fragment))
    filename = urllib.parse.unquote(Path(parsed.path).name)
    return {
        "url": direct_url,
        "filename": filename,
    }


def _cloudreve_get_download_url(base_url: str, token: str, uri: str, config: UploadConfig) -> dict[str, Any]:
    headers = {"Authorization": f"Bearer {token}"}
    if config.cloudreve_purchase_ticket:
        headers["X-Cr-Purchase-Ticket"] = config.cloudreve_purchase_ticket
    status, response_headers, data, raw = _json_request(
        "POST",
        f"{base_url}/api/v4/file/url",
        {"uris": [uri]},
        headers=headers,
        verify_ssl=config.verify_ssl,
        tls_enabled=config.client_tls_enabled,
        tls_cert_file=config.client_tls_cert_file,
        tls_key_file=config.client_tls_key_file,
        tls_min_tls_version=config.client_tls_min_tls_version,
    )
    if status < 400 and isinstance(data, dict) and data.get("code") == 0:
        payload = data.get("data")
        if isinstance(payload, str):
            return _build_download_result(payload)
        if isinstance(payload, list) and payload:
            first = payload[0]
            if isinstance(first, str):
                return _build_download_result(first)
            if isinstance(first, dict) and isinstance(first.get("url"), str):
                return _build_download_result(first["url"])
        if isinstance(payload, dict):
            urls = payload.get("urls")
            if isinstance(urls, list) and urls:
                first_url = urls[0]
                if isinstance(first_url, str):
                    return _build_download_result(first_url)
                if isinstance(first_url, dict) and isinstance(first_url.get("url"), str):
                    return _build_download_result(first_url["url"])
            for key in ("url", "download_url", "source_url"):
                value = payload.get(key)
                if isinstance(value, str):
                    return _build_download_result(value)
    location = response_headers.get("Location")
    if location:
        return _build_download_result(location)
    raise UploadError(f"Cloudreve get file URL failed: {data.get('msg') if isinstance(data, dict) else raw}")


def _guess_target_uri(file_path: Path, upload_path: str) -> str:
    safe_dir = upload_path.strip("/") or "my"
    return f"cloudreve://{safe_dir}/{urllib.parse.quote(file_path.name)}"


def _cloudreve_create_session(base_url: str, token: str, file_path: Path, config: UploadConfig) -> dict[str, Any]:
    headers = {"Authorization": f"Bearer {token}"}
    if config.cloudreve_purchase_ticket:
        headers["X-Cr-Purchase-Ticket"] = config.cloudreve_purchase_ticket
    payload = {
        "uri": _guess_target_uri(file_path, config.cloudreve_upload_path),
        "size": file_path.stat().st_size,
        "last_modified": int(file_path.stat().st_mtime * 1000),
        "mime_type": mimetypes.guess_type(file_path.name)[0] or "application/octet-stream",
        "encryption_supported": ["aes-256-ctr"],
    }
    status, _, data, raw = _json_request(
        "PUT",
        f"{base_url}/api/v4/file/upload",
        payload,
        headers=headers,
        verify_ssl=config.verify_ssl,
        tls_enabled=config.client_tls_enabled,
        tls_cert_file=config.client_tls_cert_file,
        tls_key_file=config.client_tls_key_file,
        tls_min_tls_version=config.client_tls_min_tls_version,
    )
    if status >= 400 and not isinstance(data, dict):
        raise UploadError(f"Cloudreve create session failed: {raw}")
    if isinstance(data, dict) and data.get("code") == 40004 and data.get("msg") == "Object existed":
        download_result = _cloudreve_get_download_url(base_url, token, payload["uri"], config)
        return {
            "duplicate": True,
            "uri": payload["uri"],
            "download_result": download_result,
            "raw": data,
        }
    if not isinstance(data, dict) or data.get("code") != 0:
        raise UploadError(f"Cloudreve create session failed: {data.get('msg') if isinstance(data, dict) else raw}")
    session_data = data.get("data") or {}
    return {
        "duplicate": False,
        "uri": str(session_data.get("uri") or payload["uri"]),
        "session_id": session_data.get("sessionID") or session_data.get("sessionId") or session_data.get("session_id") or session_data.get("id"),
        "chunk_size": int(session_data.get("chunk_size") or session_data.get("chunkSize") or file_path.stat().st_size),
        "upload_urls": session_data.get("uploadURLs") or session_data.get("upload_urls") or session_data.get("uploadUrls") or [],
        "raw": data,
    }


def _cloudreve_upload_chunks(base_url: str, token: str, file_path: Path, session: dict[str, Any], config: UploadConfig) -> None:
    session_id = session.get("session_id")
    if not session_id:
        raise UploadError("Cloudreve session did not return session_id")
    chunk_size = int(session.get("chunk_size") or file_path.stat().st_size)
    upload_urls = session.get("upload_urls") or []
    if isinstance(upload_urls, str):
        upload_urls = [upload_urls]
    with file_path.open("rb") as handle:
        index = 0
        while True:
            chunk = handle.read(chunk_size)
            if not chunk:
                return
            if index < len(upload_urls):
                target = upload_urls[index]
            elif len(upload_urls) == 1 and isinstance(upload_urls[0], str) and "{index}" in upload_urls[0]:
                target = upload_urls[0].format(index=index, session_id=session_id)
            else:
                target = f"{base_url}/api/v4/file/upload/{session_id}/{index}"
            headers = {
                "Accept": "*/*",
                "Authorization": f"Bearer {token}",
                "Content-Type": "application/octet-stream",
                "Content-Length": str(len(chunk)),
                "User-Agent": USER_AGENT,
            }
            status, _, _, raw = _request(
                "POST",
                target,
                body=chunk,
                headers=headers,
                verify_ssl=config.verify_ssl,
                tls_enabled=config.client_tls_enabled,
                tls_cert_file=config.client_tls_cert_file,
                tls_key_file=config.client_tls_key_file,
                tls_min_tls_version=config.client_tls_min_tls_version,
            )
            if status >= 400:
                raise UploadError(f"Cloudreve chunk upload failed at chunk {index}: {raw}")
            index += 1


def upload_to_cloudreve(file_path: Path, original_file_url: str, config: UploadConfig) -> dict[str, Any]:
    if not file_path.is_file():
        raise UploadError(f"File not found: {file_path}")
    base_url = _cloudreve_base_url(original_file_url)
    token = _get_cloudreve_token(base_url, config)
    try:
        session = _cloudreve_create_session(base_url, token, file_path, config)
    except UploadError:
        _remove_token_cache(base_url, config)
        token = _get_cloudreve_token(base_url, config, force_refresh=True)
        session = _cloudreve_create_session(base_url, token, file_path, config)
    if session.get("duplicate"):
        download_result = session["download_result"]
        return {
            "ok": True,
            "provider": "cloudreve",
            "downUrl": download_result["url"],
            "viewUrl": download_result["url"],
            "fileUrl": download_result["url"],
            "name": download_result.get("filename") or file_path.name,
            "msg": "File already exists",
            "raw": session.get("raw"),
        }
    _cloudreve_upload_chunks(base_url, token, file_path, session, config)
    time.sleep(1)
    download_result = _cloudreve_get_download_url(base_url, token, str(session["uri"]), config)
    return {
        "ok": True,
        "provider": "cloudreve",
        "downUrl": download_result["url"],
        "viewUrl": download_result["url"],
        "fileUrl": download_result["url"],
        "name": download_result.get("filename") or file_path.name,
        "msg": "File uploaded successfully",
        "raw": session.get("raw"),
    }


def _caihong_base_url(original_file_url: str) -> str:
    parsed = urllib.parse.urlsplit(original_file_url)
    return f"{parsed.scheme}://{parsed.netloc}"


def _encode_multipart(fields: dict[str, str], file_field: str, file_path: Path) -> tuple[bytes, str]:
    boundary = f"----ClaudeCodeBoundary{int(time.time() * 1000)}"
    body = bytearray()

    def add_line(value: str) -> None:
        body.extend(value.encode("utf-8"))
        body.extend(b"\r\n")

    for key, value in fields.items():
        add_line(f"--{boundary}")
        add_line(f'Content-Disposition: form-data; name="{key}"')
        add_line("")
        add_line(str(value))

    add_line(f"--{boundary}")
    add_line(
        f'Content-Disposition: form-data; name="{file_field}"; filename="{file_path.name}"'
    )
    add_line(f"Content-Type: {mimetypes.guess_type(file_path.name)[0] or 'application/octet-stream'}")
    add_line("")
    body.extend(file_path.read_bytes())
    body.extend(b"\r\n")
    add_line(f"--{boundary}--")
    return bytes(body), boundary


def upload_to_caihong(file_path: Path, original_file_url: str, config: UploadConfig) -> dict[str, Any]:
    if not file_path.is_file():
        raise UploadError(f"File not found: {file_path}")
    base_url = _caihong_base_url(original_file_url)
    api_url = f"{base_url}/api.php"
    fields = {
        "show": str(config.caihong_show),
        "ispwd": str(config.caihong_ispwd),
        "pwd": config.caihong_pwd,
        "format": "json",
    }
    body, boundary = _encode_multipart(fields, "file", file_path)
    headers = {
        "Accept": "application/json, text/plain, */*",
        "Content-Type": f"multipart/form-data; boundary={boundary}",
        "Content-Length": str(len(body)),
        "Referer": f"{base_url}/",
        "Origin": base_url,
        "User-Agent": USER_AGENT,
    }
    if config.caihong_cookie:
        headers["Cookie"] = config.caihong_cookie
    status, _, _, text = _request(
        "POST",
        api_url,
        body=body,
        headers=headers,
        verify_ssl=config.verify_ssl,
        tls_enabled=config.client_tls_enabled,
        tls_cert_file=config.client_tls_cert_file,
        tls_key_file=config.client_tls_key_file,
        tls_min_tls_version=config.client_tls_min_tls_version,
    )
    try:
        data = json.loads(text)
    except json.JSONDecodeError as exc:
        raise UploadError(f"Caihong upload returned non-JSON response: {exc}: {text[:400]}") from exc
    if status >= 400 or int(data.get("code", -1)) != 0:
        raise UploadError(f"Caihong upload failed: {data.get('msg') or text}")
    _caihong_cache(config).put(base_url.lower(), {"cookie": config.caihong_cookie, "last_success": time.time()})
    return {
        "ok": True,
        "provider": "caihong",
        "downUrl": data.get("downurl") or data.get("viewurl") or "",
        "viewUrl": data.get("viewurl") or data.get("downurl") or "",
        "fileUrl": data.get("downurl") or data.get("viewurl") or "",
        "name": data.get("name") or file_path.name,
        "msg": data.get("msg") or "File uploaded successfully",
        "raw": data,
    }


def to_numbers(hex_value: str) -> list[int]:
    return [int(hex_value[index:index + 2], 16) for index in range(0, len(hex_value), 2)]


def to_hex(values: list[int]) -> str:
    return "".join(f"{value:02x}" for value in values)


def gmul(a: int, b: int) -> int:
    result = 0
    for _ in range(8):
        if b & 1:
            result ^= a
        high_bit = a & 0x80
        a = (a << 1) & 0xFF
        if high_bit:
            a ^= 0x1B
        b >>= 1
    return result


def key_expansion(key_bytes: list[int]) -> list[list[int]]:
    words: list[list[int]] = []
    for index in range(4):
        start = index * 4
        words.append(key_bytes[start:start + 4])
    for index in range(4, 44):
        temp = words[index - 1][:]
        if index % 4 == 0:
            temp = temp[1:] + temp[:1]
            temp = [SBOX[value] for value in temp]
            temp[0] ^= RCON[index // 4]
        words.append([words[index - 4][offset] ^ temp[offset] for offset in range(4)])
    return words


def add_round_key(state: list[list[int]], words: list[list[int]], round_index: int) -> None:
    for column in range(4):
        state[0][column] ^= words[round_index * 4 + column][0]
        state[1][column] ^= words[round_index * 4 + column][1]
        state[2][column] ^= words[round_index * 4 + column][2]
        state[3][column] ^= words[round_index * 4 + column][3]


def inv_sub_bytes(state: list[list[int]]) -> None:
    for row in range(4):
        for column in range(4):
            state[row][column] = RSBOX[state[row][column]]


def inv_shift_rows(state: list[list[int]]) -> None:
    state[1] = state[1][-1:] + state[1][:-1]
    state[2] = state[2][-2:] + state[2][:-2]
    state[3] = state[3][1:] + state[3][:1]


def inv_mix_columns(state: list[list[int]]) -> None:
    for column in range(4):
        s0, s1, s2, s3 = state[0][column], state[1][column], state[2][column], state[3][column]
        state[0][column] = gmul(s0, 0x0E) ^ gmul(s1, 0x0B) ^ gmul(s2, 0x0D) ^ gmul(s3, 0x09)
        state[1][column] = gmul(s0, 0x09) ^ gmul(s1, 0x0E) ^ gmul(s2, 0x0B) ^ gmul(s3, 0x0D)
        state[2][column] = gmul(s0, 0x0D) ^ gmul(s1, 0x09) ^ gmul(s2, 0x0E) ^ gmul(s3, 0x0B)
        state[3][column] = gmul(s0, 0x0B) ^ gmul(s1, 0x0D) ^ gmul(s2, 0x09) ^ gmul(s3, 0x0E)


def decrypt_block(block: list[int], words: list[list[int]]) -> list[int]:
    state = [[0] * 4 for _ in range(4)]
    for index, value in enumerate(block):
        row = index % 4
        column = index // 4
        state[row][column] = value
    add_round_key(state, words, 10)
    for round_index in range(9, 0, -1):
        inv_shift_rows(state)
        inv_sub_bytes(state)
        add_round_key(state, words, round_index)
        inv_mix_columns(state)
    inv_shift_rows(state)
    inv_sub_bytes(state)
    add_round_key(state, words, 0)
    result: list[int] = []
    for column in range(4):
        for row in range(4):
            result.append(state[row][column])
    return result


def aes_cbc_decrypt(cipher_bytes: list[int], key_bytes: list[int], iv_bytes: list[int]) -> list[int]:
    words = key_expansion(key_bytes)
    iv = iv_bytes[:]
    result: list[int] = []
    for index in range(0, len(cipher_bytes), 16):
        block = cipher_bytes[index:index + 16]
        plain = decrypt_block(block, words)
        for offset in range(16):
            plain[offset] ^= iv[offset]
        iv = block
        result.extend(plain)
    return result


def solve_js_challenge(body_text: str) -> str | None:
    match = CHALLENGE_PATTERN.search(body_text)
    if not match:
        return None
    key_hex, iv_hex, cipher_hex = match.groups()
    return to_hex(aes_cbc_decrypt(to_numbers(cipher_hex), to_numbers(key_hex), to_numbers(iv_hex)))
