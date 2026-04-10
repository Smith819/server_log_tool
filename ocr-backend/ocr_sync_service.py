#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
AutoGLM OCR sync service.
Polls the frontend manifest, downloads new images / edited JSON files,
runs OCR locally, renders final task logs, and uploads results back to the
original file host.
"""

from __future__ import annotations

import configparser
import json
import logging
import logging.handlers
import os
import shutil
import signal
import subprocess
import sys
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from ocr_result_upload import UploadError, upload_result_file
from tls_context import build_client_ssl_context, load_tls_settings

SCRIPT_DIR = Path(__file__).resolve().parent
CONFIG_FILE = SCRIPT_DIR / "config.ini"
IMAGE_EXTENSIONS = {".jpg", ".jpeg", ".png", ".gif", ".webp", ".bmp"}
EDITED_JSON_SUFFIX = "_download_new.json"
DEFAULT_MODEL = "LoRA/Qwen/Qwen2.5-32B-Instruct"


@dataclass
class SyncConfig:
    enabled: bool
    api_enabled: bool
    api_listen_host: str
    api_port: int
    api_public_base_url: str
    api_auth_token: str
    api_status_limit: int
    frontend_base_url: str
    manifest_name: str
    poll_interval_seconds: int
    request_timeout_seconds: int
    verify_ssl: bool
    tls_enabled: bool
    tls_cert_file: Path | None
    tls_key_file: Path | None
    tls_min_tls_version: str
    work_dir: Path
    runtime_cache_dir: Path
    state_file: Path
    log_dir: Path
    ocr_project_dir: Path
    ocr_python_executable: str
    amap_api_key: str
    siliconflow_api_key: str
    siliconflow_model: str
    skip_llm: bool
    save_slices: bool


class SyncError(RuntimeError):
    pass


_stop_event = threading.Event()


def iso_now() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


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


def setup_logging(log_dir: Path) -> logging.Logger:
    log_dir.mkdir(parents=True, exist_ok=True)
    log_file = log_dir / "ocr_sync_service.log"

    logger = logging.getLogger("ocr_sync_service")
    logger.setLevel(logging.DEBUG)
    logger.handlers.clear()

    fmt = logging.Formatter(
        "%(asctime)s [%(levelname)s] %(message)s",
        datefmt="%Y-%m-%dT%H:%M:%S",
    )

    fh = logging.handlers.RotatingFileHandler(
        str(log_file),
        maxBytes=10 * 1024 * 1024,
        backupCount=5,
        encoding="utf-8",
    )
    fh.setFormatter(fmt)
    logger.addHandler(fh)

    sh = logging.StreamHandler(sys.stdout)
    sh.setFormatter(fmt)
    logger.addHandler(sh)
    return logger


def load_config(config_file: Path = CONFIG_FILE) -> SyncConfig:
    cfg = configparser.ConfigParser()
    cfg.read(str(config_file), encoding="utf-8")

    frontend_base_url = _env_str(
        "AUTOGLM_FRONTEND_BASE_URL",
        cfg.get("ocr_sync", "frontend_base_url", fallback="").strip(),
    ).rstrip("/")
    manifest_name = cfg.get("server", "manifest_name", fallback="manifest.json").strip() or "manifest.json"
    work_dir = Path(cfg.get("ocr_sync", "work_dir", fallback=str(SCRIPT_DIR / "ocr_sync_work")))
    runtime_cache_dir = Path(cfg.get("ocr_sync", "runtime_cache_dir", fallback=str(SCRIPT_DIR / "runtime_cache")))
    state_file_value = cfg.get("ocr_sync", "state_file", fallback=str(runtime_cache_dir / "ocr_sync_state.json"))
    log_dir = Path(cfg.get("server", "log_dir", fallback=str(SCRIPT_DIR / "logs")))
    tls_enabled, tls_cert_file, tls_key_file, tls_min_tls_version = load_tls_settings(cfg)
    api_public_base_url = _env_str(
        "AUTOGLM_OCR_API_PUBLIC_BASE_URL",
        cfg.get("api", "public_base_url", fallback="").strip(),
    ).rstrip("/")

    return SyncConfig(
        enabled=_env_bool("AUTOGLM_OCR_SYNC_ENABLED", cfg.getboolean("ocr_sync", "enabled", fallback=False)),
        api_enabled=_env_bool("AUTOGLM_OCR_API_ENABLED", cfg.getboolean("api", "enabled", fallback=True)),
        api_listen_host=_env_str("AUTOGLM_OCR_API_HOST", cfg.get("api", "listen_host", fallback="0.0.0.0")),
        api_port=_env_int("AUTOGLM_OCR_API_PORT", cfg.getint("api", "port", fallback=39384), minimum=1),
        api_public_base_url=api_public_base_url,
        api_auth_token=_env_str("AUTOGLM_OCR_API_AUTH_TOKEN", cfg.get("api", "auth_token", fallback="")),
        api_status_limit=_env_int("AUTOGLM_OCR_API_STATUS_LIMIT", cfg.getint("api", "status_limit", fallback=50), minimum=1),
        frontend_base_url=frontend_base_url,
        manifest_name=manifest_name,
        poll_interval_seconds=_env_int("AUTOGLM_OCR_POLL_INTERVAL_SECONDS", max(5, cfg.getint("ocr_sync", "poll_interval_seconds", fallback=30)), minimum=5),
        request_timeout_seconds=_env_int("AUTOGLM_OCR_REQUEST_TIMEOUT_SECONDS", max(5, cfg.getint("ocr_sync", "request_timeout_seconds", fallback=120)), minimum=5),
        verify_ssl=_env_bool("AUTOGLM_OCR_VERIFY_SSL", cfg.getboolean("ocr_sync", "verify_ssl", fallback=True)),
        tls_enabled=tls_enabled,
        tls_cert_file=tls_cert_file,
        tls_key_file=tls_key_file,
        tls_min_tls_version=tls_min_tls_version,
        work_dir=work_dir,
        runtime_cache_dir=runtime_cache_dir,
        state_file=Path(state_file_value),
        log_dir=log_dir,
        ocr_project_dir=Path(cfg.get("ocr_sync", "ocr_project_dir", fallback="")).expanduser(),
        ocr_python_executable=cfg.get("ocr_sync", "ocr_python_executable", fallback=sys.executable).strip() or sys.executable,
        amap_api_key=cfg.get("ocr_sync", "amap_api_key", fallback=os.getenv("AMAP_API_KEY", "")).strip(),
        siliconflow_api_key=cfg.get("ocr_sync", "siliconflow_api_key", fallback=os.getenv("SILICONFLOW_API_KEY", "")).strip(),
        siliconflow_model=cfg.get("ocr_sync", "siliconflow_model", fallback=os.getenv("SILICONFLOW_MODEL", DEFAULT_MODEL)).strip() or DEFAULT_MODEL,
        skip_llm=cfg.getboolean("ocr_sync", "skip_llm", fallback=False),
        save_slices=cfg.getboolean("ocr_sync", "save_slices", fallback=False),
    )


class StateStore:
    def __init__(self, path: Path):
        self.path = path

    def load(self) -> dict[str, Any]:
        if not self.path.exists():
            return {"images": {}, "edited_json": {}}
        try:
            data = json.loads(self.path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            return {"images": {}, "edited_json": {}}
        if not isinstance(data, dict):
            return {"images": {}, "edited_json": {}}
        data.setdefault("images", {})
        data.setdefault("edited_json", {})
        return data

    def save(self, data: dict[str, Any]) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.path.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")


class FrontendClient:
    def __init__(self, config: SyncConfig):
        self.config = config

    def manifest_url(self) -> str:
        if not self.config.frontend_base_url:
            raise SyncError("[ocr_sync] frontend_base_url is required")
        return f"{self.config.frontend_base_url}/{urllib.parse.quote(self.config.manifest_name)}"

    def _open(self, url: str):
        req = urllib.request.Request(url, headers={"User-Agent": "AutoGLM-OCR-Sync/1.0"})
        context = build_client_ssl_context(
            verify_ssl=self.config.verify_ssl,
            tls_enabled=self.config.tls_enabled,
            cert_file=self.config.tls_cert_file,
            key_file=self.config.tls_key_file,
            min_tls_version=self.config.tls_min_tls_version,
        )
        return urllib.request.urlopen(req, timeout=self.config.request_timeout_seconds, context=context)

    def fetch_manifest(self) -> dict[str, Any]:
        with self._open(self.manifest_url()) as response:
            raw = response.read().decode("utf-8", errors="replace")
        data = json.loads(raw)
        if not isinstance(data, dict) or not isinstance(data.get("files"), list):
            raise SyncError("Frontend manifest response is invalid")
        return data

    def download_file(self, url: str, destination: Path) -> Path:
        destination.parent.mkdir(parents=True, exist_ok=True)
        with self._open(url) as response:
            data = response.read()
        destination.write_bytes(data)
        return destination


class OCRRenderer:
    def __init__(self, config: SyncConfig):
        self.config = config
        self._render_func = None

    def _load_render_func(self):
        if self._render_func is not None:
            return self._render_func
        project_dir = self.config.ocr_project_dir.resolve()
        if not project_dir.is_dir():
            raise SyncError(f"OCR project directory not found: {project_dir}")
        project_dir_str = str(project_dir)
        if project_dir_str not in sys.path:
            sys.path.insert(0, project_dir_str)
        from home_pic_processor.render import render_autoglm_task_log

        self._render_func = render_autoglm_task_log
        return self._render_func

    def render_log_from_json(self, data: dict[str, Any]) -> str:
        render_func = self._load_render_func()
        return render_func(data)


class OCRSyncService:
    def __init__(self, config: SyncConfig, logger: logging.Logger):
        self.config = config
        self.logger = logger
        self.frontend = FrontendClient(config)
        self.renderer = OCRRenderer(config)
        self.state_store = StateStore(config.state_file)

        self.images_dir = config.work_dir / "images"
        self.logs_dir = config.work_dir / "download_logs"
        self.edited_json_dir = config.work_dir / "edited_json"
        self.outputs_dir = config.work_dir / "outputs"
        self.tmp_root_dir = config.work_dir / "tmp"

        for path in (
            self.images_dir,
            self.logs_dir,
            self.edited_json_dir,
            self.outputs_dir,
            self.tmp_root_dir,
            self.config.runtime_cache_dir,
        ):
            path.mkdir(parents=True, exist_ok=True)

    def run_forever(self) -> None:
        if not self.config.enabled:
            raise SyncError("[ocr_sync] enabled=false; refusing to start sync loop")
        while not _stop_event.is_set():
            try:
                self.sync_once()
            except Exception as exc:  # noqa: BLE001
                self.logger.exception("Sync cycle failed: %s", exc)
            _stop_event.wait(self.config.poll_interval_seconds)

    def sync_once(self) -> None:
        manifest = self.frontend.fetch_manifest()
        files = manifest.get("files") or []
        state = self.state_store.load()
        self.logger.info("Fetched manifest with %d files", len(files))

        images = [item for item in files if self._is_image_entry(item)]
        edited_json_files = [item for item in files if self._is_edited_json_entry(item)]

        for item in images:
            self._sync_image(item, state)
        for item in edited_json_files:
            self._sync_edited_json(item, state)

        self.state_store.save(state)

    def _is_image_entry(self, item: dict[str, Any]) -> bool:
        name = str(item.get("name") or "")
        return Path(name).suffix.lower() in IMAGE_EXTENSIONS

    def _is_edited_json_entry(self, item: dict[str, Any]) -> bool:
        name = str(item.get("name") or "")
        return name.endswith(EDITED_JSON_SUFFIX)

    def _file_url(self, item: dict[str, Any]) -> str:
        url = str(item.get("url") or "").strip()
        if not url:
            raise SyncError(f"Manifest entry missing url: {item}")
        return url

    def _stem_for_image(self, image_name: str) -> str:
        return Path(image_name).stem

    def _original_file_url_from_log(self, log_path: Path) -> str:
        text = log_path.read_text(encoding="utf-8", errors="replace")
        for line in text.splitlines():
            line = line.strip()
            if line.startswith("FILE_URL="):
                return line.split("=", 1)[1].strip()
        raise SyncError(f"FILE_URL not found in {log_path}")

    def _recover_image_record_for_edited_json(self, image_stem: str, state: dict[str, Any]) -> dict[str, Any] | None:
        record = state["images"].setdefault(image_stem, {})
        original_file_url = str(record.get("original_file_url") or "").strip()
        if original_file_url:
            return record

        download_log_name = f"{image_stem}_download.log"
        download_log_url = f"{self.config.frontend_base_url}/{urllib.parse.quote(download_log_name)}"
        download_log_path = self.logs_dir / download_log_name
        if not download_log_path.exists():
            try:
                self.frontend.download_file(download_log_url, download_log_path)
                self.logger.info("Downloaded source log for edited JSON recovery: %s", download_log_path)
            except Exception as exc:  # noqa: BLE001
                self.logger.warning("Failed to download source log for %s: %s", image_stem, exc)

        if not download_log_path.exists():
            return None

        try:
            original_file_url = self._original_file_url_from_log(download_log_path)
        except Exception as exc:  # noqa: BLE001
            self.logger.warning("Failed to recover original_file_url from %s: %s", download_log_path, exc)
            return None

        record["original_file_url"] = original_file_url
        record["download_log_url"] = download_log_url
        record["last_seen_at"] = iso_now()
        return record

    def _extract_uploaded_file_url(self, upload_result: dict[str, Any]) -> str:
        return str(
            upload_result.get("fileUrl")
            or upload_result.get("downUrl")
            or upload_result.get("viewUrl")
            or upload_result.get("filePageUrl")
            or ""
        ).strip()

    def _upload_result_or_none(self, file_path: str | Path, original_file_url: str, context: str) -> dict[str, Any] | None:
        try:
            result = upload_result_file(file_path, original_file_url)
        except UploadError as exc:
            self.logger.warning("Upload skipped for %s: %s", context, exc)
            return None
        except Exception as exc:  # noqa: BLE001
            self.logger.warning("Upload failed for %s: %s", context, exc)
            return None

        if not result or not result.get("ok"):
            self.logger.warning("Upload did not succeed for %s: %s", context, (result or {}).get("msg") or result)
            return None
        return result

    def _sync_image(self, item: dict[str, Any], state: dict[str, Any]) -> None:
        image_name = str(item.get("name") or "")
        stem = self._stem_for_image(image_name)
        record = state["images"].setdefault(stem, {})
        record.setdefault("image_name", image_name)
        record.setdefault("image_url", self._file_url(item))
        record["last_seen_at"] = iso_now()
        record["manifest_mtime"] = item.get("mtime")

        if record.get("ocr_uploaded"):
            return

        image_path = self.images_dir / image_name
        if not image_path.exists():
            self.frontend.download_file(self._file_url(item), image_path)
            self.logger.info("Downloaded image: %s", image_path)

        download_log_name = f"{stem}_download.log"
        download_log_url = f"{self.config.frontend_base_url}/{urllib.parse.quote(download_log_name)}"
        download_log_path = self.logs_dir / download_log_name
        if not download_log_path.exists():
            self.frontend.download_file(download_log_url, download_log_path)
            self.logger.info("Downloaded source log: %s", download_log_path)

        original_file_url = record.get("original_file_url") or self._original_file_url_from_log(download_log_path)
        record["original_file_url"] = original_file_url
        record["download_log_url"] = download_log_url

        outputs = self._run_ocr_for_image(image_path, stem)
        json_upload = self._upload_result_or_none(outputs["json"], original_file_url, f"{image_name} JSON")
        log_upload = self._upload_result_or_none(outputs["log"], original_file_url, f"{image_name} log")

        record["json_file"] = outputs["json"]
        record["log_file"] = outputs["log"]
        record["json_file_url"] = self._extract_uploaded_file_url(json_upload or {})
        record["log_file_url"] = self._extract_uploaded_file_url(log_upload or {})
        if json_upload and log_upload:
            record["ocr_uploaded"] = True
            record["ocr_uploaded_at"] = iso_now()
            self.logger.info("Uploaded OCR outputs for %s", image_name)
        else:
            record["ocr_uploaded"] = False
            record.pop("ocr_uploaded_at", None)
            self.logger.warning("OCR outputs not fully uploaded for %s; will retry next cycle", image_name)

    def _run_ocr_for_image(self, image_path: Path, stem: str) -> dict[str, str]:
        image_tmp_dir = self.tmp_root_dir / stem / "input"
        image_tmp_dir.mkdir(parents=True, exist_ok=True)
        temp_image_path = image_tmp_dir / image_path.name
        if temp_image_path.resolve() != image_path.resolve():
            shutil.copy2(image_path, temp_image_path)

        output_dir = self.outputs_dir / stem
        output_dir.mkdir(parents=True, exist_ok=True)

        command = [
            self.config.ocr_python_executable,
            "-m",
            "home_pic_processor",
            "--input-dir",
            str(image_tmp_dir),
            "--output-dir",
            str(output_dir),
            "--export-autoglm",
            "--flat-output",
            "--model",
            self.config.siliconflow_model,
        ]
        if self.config.amap_api_key:
            command.extend(["--amap-api-key", self.config.amap_api_key])
        if self.config.siliconflow_api_key:
            command.extend(["--siliconflow-api-key", self.config.siliconflow_api_key])
        if self.config.skip_llm:
            command.append("--skip-llm")
        if not self.config.save_slices:
            command.append("--no-slices")

        env = os.environ.copy()
        env.setdefault("PYTHONUTF8", "1")
        if self.config.amap_api_key:
            env["AMAP_API_KEY"] = self.config.amap_api_key
        if self.config.siliconflow_api_key:
            env["SILICONFLOW_API_KEY"] = self.config.siliconflow_api_key
        env["SILICONFLOW_MODEL"] = self.config.siliconflow_model

        self.logger.info("Running OCR for %s", image_path.name)
        result = subprocess.run(
            command,
            cwd=str(self.config.ocr_project_dir),
            env=env,
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            check=False,
        )
        if result.returncode != 0:
            raise SyncError(
                f"OCR command failed for {image_path.name}:\nSTDOUT:\n{result.stdout}\nSTDERR:\n{result.stderr}"
            )

        json_path = output_dir / f"{stem}.json"
        log_path = output_dir / f"{stem}_download.log"
        if not json_path.is_file() or not log_path.is_file():
            raise SyncError(f"OCR outputs missing for {image_path.name}: {json_path}, {log_path}")
        return {"json": str(json_path), "log": str(log_path)}

    def _sync_edited_json(self, item: dict[str, Any], state: dict[str, Any]) -> None:
        name = str(item.get("name") or "")
        stem = name[: -len(".json")]
        edited_record = state["edited_json"].setdefault(name, {})
        edited_record["last_seen_at"] = iso_now()
        edited_record["edited_json_url"] = self._file_url(item)
        edited_record["manifest_mtime"] = item.get("mtime")

        if edited_record.get("log_uploaded"):
            return

        image_stem = stem.removesuffix("_download_new")
        image_record = self._recover_image_record_for_edited_json(image_stem, state)
        if not image_record or not image_record.get("original_file_url"):
            self.logger.warning("Skipping edited JSON without original image context: %s", name)
            return

        edited_json_path = self.edited_json_dir / name
        if not edited_json_path.exists():
            self.frontend.download_file(self._file_url(item), edited_json_path)
            self.logger.info("Downloaded edited JSON: %s", edited_json_path)

        data = json.loads(edited_json_path.read_text(encoding="utf-8"))
        rendered_log = self.renderer.render_log_from_json(data)
        final_log_name = f"{stem}_download.log"
        final_log_path = self.outputs_dir / image_stem / final_log_name
        final_log_path.parent.mkdir(parents=True, exist_ok=True)
        final_log_path.write_text(rendered_log, encoding="utf-8")

        upload_result = self._upload_result_or_none(final_log_path, image_record["original_file_url"], f"{name} final log")
        edited_record["log_file"] = str(final_log_path)
        edited_record["log_file_url"] = self._extract_uploaded_file_url(upload_result or {})
        if upload_result:
            edited_record["log_uploaded"] = True
            edited_record["log_uploaded_at"] = iso_now()
            self.logger.info("Uploaded edited JSON log: %s", name)
        else:
            edited_record["log_uploaded"] = False
            edited_record.pop("log_uploaded_at", None)
            self.logger.warning("Edited JSON log upload not completed for %s; will retry next cycle", name)


def _shutdown(signum, frame) -> None:
    del frame
    logging.getLogger("ocr_sync_service").info("Signal %s received, shutting down...", signum)
    _stop_event.set()


def main() -> None:
    config = load_config()
    logger = setup_logging(config.log_dir)
    logger.info("OCR sync service starting")
    logger.info("Frontend base URL : %s", config.frontend_base_url)
    logger.info("Manifest name     : %s", config.manifest_name)
    logger.info("Work dir          : %s", config.work_dir)
    logger.info("State file        : %s", config.state_file)
    logger.info("OCR project dir   : %s", config.ocr_project_dir)

    signal.signal(signal.SIGTERM, _shutdown)
    signal.signal(signal.SIGINT, _shutdown)

    service = OCRSyncService(config, logger)
    try:
        service.run_forever()
    finally:
        logger.info("OCR sync service stopped")


if __name__ == "__main__":
    main()
