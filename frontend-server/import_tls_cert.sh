#!/usr/bin/env bash
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
# shellcheck disable=SC1091
source "${SCRIPT_DIR}/lib/common.sh"
# shellcheck disable=SC1091
source "${SCRIPT_DIR}/lib/tls_common.sh"

INSTALL_DIR="/opt/autoglm-frontend"
CERT_DIR="${INSTALL_DIR}/certs"
CONFIG_FILE="${INSTALL_DIR}/config.ini"
SERVICE_GROUP="autoglm"
DEST_CERT="${CERT_DIR}/server.crt"
DEST_KEY="${CERT_DIR}/server.key"
SERVICES=(autoglm-post autoglm-multipart)

patch_config() {
    local cert="$1"
    local key="$2"
    python3 - "$CONFIG_FILE" "$cert" "$key" <<'PYEOF'
import configparser
import sys

path, cert, key = sys.argv[1], sys.argv[2], sys.argv[3]
cfg = configparser.ConfigParser()
cfg.read(path, encoding='utf-8')
if not cfg.has_section('tls'):
    cfg.add_section('tls')
cfg.set('tls', 'enabled', 'true')
cfg.set('tls', 'cert_file', cert)
cfg.set('tls', 'key_file', key)
cfg.set('tls', 'min_tls_version', 'TLSv1.2')
with open(path, 'w', encoding='utf-8') as handle:
    cfg.write(handle)
print(f'[INFO]  Patched {path}: tls.enabled=true, cert={cert}, key={key}')
PYEOF
}

restart_component_services() {
    restart_services_if_present "${SERVICES[@]}"
}

main() {
    local cert_search_dir="${1:-}"
    local key_search_dir="${2:-${cert_search_dir}}"
    case "$cert_search_dir" in
        -h|--help|'')
            usage_import_cert_pair
            [[ -n "$cert_search_dir" ]] || exit 1
            exit 0
            ;;
    esac
    import_matching_cert_pair "$cert_search_dir" "$key_search_dir" "$CERT_DIR" "$DEST_CERT" "$DEST_KEY" "$SERVICE_GROUP" patch_config restart_component_services
}

main "$@"
