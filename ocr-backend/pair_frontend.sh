#!/usr/bin/env bash
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
# shellcheck disable=SC1091
source "${SCRIPT_DIR}/lib/common.sh"

ENV_FILE="/etc/autoglm-ocr/ocr-backend.env"
EXAMPLE_ENV="${SCRIPT_DIR}/ocr-backend.env.example"
SERVICE_NAME="autoglm-ocr-sync"

usage() {
    cat <<'EOF'
Usage:
  sudo bash pair_frontend.sh FRONTEND_HOST_OR_URL [--insecure]
  sudo bash pair_frontend.sh [--insecure]

Examples:
  sudo bash pair_frontend.sh 192.168.1.10
  sudo bash pair_frontend.sh front.example.com
  sudo bash pair_frontend.sh https://front.example.com:39283
  sudo bash pair_frontend.sh https://front.example.com:39283 --insecure

Notes:
  - The backend only needs one frontend base URL.
  - This script probes the frontend manifest endpoint and prefers port 39283,
    then falls back to 39282 when you provide only a host or IP.
  - On success it writes /etc/autoglm-ocr/ocr-backend.env, enables OCR sync,
    and restarts autoglm-ocr-sync if the service is installed.
EOF
}

ensure_env_file() {
    if [[ -f "$ENV_FILE" ]]; then
        return 0
    fi
    mkdir -p "$(dirname "$ENV_FILE")"
    if [[ -f "$EXAMPLE_ENV" ]]; then
        install -m 640 -o root -g root "$EXAMPLE_ENV" "$ENV_FILE"
    else
        : > "$ENV_FILE"
        chmod 640 "$ENV_FILE"
    fi
}

probe_frontend() {
    local frontend_input="$1"
    local verify_ssl="$2"

    python3 - "$frontend_input" "$verify_ssl" <<'PYEOF'
from __future__ import annotations

import json
import ssl
import sys
import urllib.error
import urllib.parse
import urllib.request


def candidate_urls(value: str) -> list[str]:
    text = value.strip().rstrip("/")
    if not text:
        return []

    if "://" in text:
        parsed = urllib.parse.urlsplit(text)
        path = parsed.path.rstrip("/")
        if path.endswith("/manifest.json"):
            path = path[: -len("/manifest.json")]
        elif path == "manifest.json":
            path = ""
        normalized = urllib.parse.urlunsplit(
            (parsed.scheme, parsed.netloc, path, "", "")
        ).rstrip("/")
        return [normalized]

    host = text
    return [
        f"https://{host}:39283",
        f"http://{host}:39283",
        f"https://{host}:39282",
        f"http://{host}:39282",
    ]


def unique(values: list[str]) -> list[str]:
    seen: set[str] = set()
    result: list[str] = []
    for item in values:
        if item not in seen:
            seen.add(item)
            result.append(item)
    return result


frontend_input = sys.argv[1]
verify_ssl = sys.argv[2].strip().lower() == "true"
context = None if verify_ssl else ssl._create_unverified_context()
errors: list[str] = []

for base_url in unique(candidate_urls(frontend_input)):
    manifest_url = f"{base_url}/manifest.json"
    request = urllib.request.Request(
        manifest_url,
        headers={"User-Agent": "AutoGLM-OCR-Pair/1.0"},
        method="GET",
    )
    try:
        with urllib.request.urlopen(request, timeout=8, context=context) as response:
            body = response.read().decode("utf-8", errors="replace")
    except ssl.SSLCertVerificationError as exc:
        errors.append(f"{manifest_url} -> TLS verify failed: {exc}")
        continue
    except urllib.error.URLError as exc:
        errors.append(f"{manifest_url} -> {exc}")
        continue
    except Exception as exc:  # noqa: BLE001
        errors.append(f"{manifest_url} -> {exc}")
        continue

    try:
        payload = json.loads(body)
    except json.JSONDecodeError as exc:
        errors.append(f"{manifest_url} -> invalid JSON: {exc}")
        continue

    if not isinstance(payload, dict) or not isinstance(payload.get("files"), list):
        errors.append(f"{manifest_url} -> unexpected manifest format")
        continue

    print(base_url)
    sys.exit(0)

for item in errors:
    print(item, file=sys.stderr)
sys.exit(1)
PYEOF
}

patch_env_file() {
    local base_url="$1"
    local verify_ssl="$2"

    python3 - "$ENV_FILE" "$base_url" "$verify_ssl" <<'PYEOF'
from __future__ import annotations

import re
import sys
from pathlib import Path

path = Path(sys.argv[1])
base_url = sys.argv[2]
verify_ssl = sys.argv[3].strip().lower()

updates = {
    "AUTOGLM_FRONTEND_BASE_URL": base_url,
    "AUTOGLM_OCR_SYNC_ENABLED": "true",
    "AUTOGLM_OCR_VERIFY_SSL": "true" if verify_ssl == "true" else "false",
}

existing = path.read_text(encoding="utf-8") if path.exists() else ""
lines = existing.splitlines()
out: list[str] = []
seen: set[str] = set()
pattern = re.compile(r"^([A-Za-z_][A-Za-z0-9_]*)=.*$")

for line in lines:
    match = pattern.match(line)
    if not match:
        out.append(line)
        continue

    key = match.group(1)
    if key not in updates:
        out.append(line)
        continue

    if key in seen:
        continue
    out.append(f"{key}={updates[key]}")
    seen.add(key)

for key, value in updates.items():
    if key not in seen:
        out.append(f"{key}={value}")

path.write_text("\n".join(out).rstrip("\n") + "\n", encoding="utf-8")
PYEOF
}

main() {
    require_root
    ensure_python3

    local verify_ssl="true"
    local frontend_input=""

    while [[ $# -gt 0 ]]; do
        case "$1" in
            --insecure)
                verify_ssl="false"
                shift
                ;;
            -h|--help)
                usage
                exit 0
                ;;
            *)
                if [[ -n "$frontend_input" ]]; then
                    die "Unexpected extra argument: $1"
                fi
                frontend_input="$1"
                shift
                ;;
        esac
    done

    if [[ -z "$frontend_input" ]]; then
        read -rp "Frontend host or base URL: " frontend_input
    fi
    [[ -n "${frontend_input// }" ]] || die "Frontend host or URL is required."

    ensure_env_file

    info "Probing frontend manifest from: ${frontend_input}"
    local selected_base_url
    if ! selected_base_url="$(probe_frontend "$frontend_input" "$verify_ssl")"; then
        if [[ "$verify_ssl" == "true" ]]; then
            die "Could not reach a valid frontend manifest. If the frontend uses a self-signed HTTPS certificate, rerun with --insecure."
        fi
        die "Could not reach a valid frontend manifest from the provided address."
    fi

    patch_env_file "$selected_base_url" "$verify_ssl"
    restart_services_if_present "$SERVICE_NAME"

    info "Frontend pairing completed."
    info "Selected frontend base URL : ${selected_base_url}"
    info "AUTOGLM_OCR_SYNC_ENABLED   : true"
    info "AUTOGLM_OCR_VERIFY_SSL     : ${verify_ssl}"
    info "Env file updated           : ${ENV_FILE}"
}

main "$@"
