from __future__ import annotations

import configparser
import ssl
from pathlib import Path


def load_tls_settings(cfg: configparser.ConfigParser) -> tuple[bool, Path | None, Path | None, str]:
    enabled = cfg.getboolean("tls", "enabled", fallback=False)
    cert_value = cfg.get(
        "tls",
        "cert_file",
        fallback=cfg.get("tls", "client_cert_file", fallback=""),
    ).strip()
    key_value = cfg.get(
        "tls",
        "key_file",
        fallback=cfg.get("tls", "client_key_file", fallback=""),
    ).strip()
    min_tls_version = cfg.get("tls", "min_tls_version", fallback="TLSv1.2").strip() or "TLSv1.2"
    cert_file = Path(cert_value).expanduser() if cert_value else None
    key_file = Path(key_value).expanduser() if key_value else None
    return enabled, cert_file, key_file, min_tls_version


def apply_min_tls_version(context: ssl.SSLContext, min_tls_version: str) -> None:
    context.minimum_version = (
        ssl.TLSVersion.TLSv1_3
        if min_tls_version == "TLSv1.3"
        else ssl.TLSVersion.TLSv1_2
    )


def build_client_ssl_context(
    *,
    verify_ssl: bool,
    tls_enabled: bool,
    cert_file: Path | None,
    key_file: Path | None,
    min_tls_version: str,
) -> ssl.SSLContext | None:
    if not tls_enabled and verify_ssl:
        return None

    context = ssl.create_default_context() if verify_ssl else ssl._create_unverified_context()
    apply_min_tls_version(context, min_tls_version)
    if tls_enabled:
        if cert_file is None or key_file is None:
            raise ValueError("TLS is enabled but cert_file/key_file is incomplete")
        context.load_cert_chain(str(cert_file), str(key_file))
    return context


def build_server_ssl_context(
    *,
    tls_enabled: bool,
    cert_file: Path | None,
    key_file: Path | None,
    min_tls_version: str,
) -> ssl.SSLContext | None:
    if not tls_enabled:
        return None
    if cert_file is None or key_file is None:
        raise ValueError("TLS is enabled but cert_file/key_file is incomplete")

    context = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
    apply_min_tls_version(context, min_tls_version)
    context.load_cert_chain(str(cert_file), str(key_file))
    return context
