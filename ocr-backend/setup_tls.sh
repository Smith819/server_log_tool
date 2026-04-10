#!/usr/bin/env bash
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
# shellcheck disable=SC1091
source "${SCRIPT_DIR}/lib/common.sh"
# shellcheck disable=SC1091
source "${SCRIPT_DIR}/lib/tls_common.sh"

INSTALL_DIR="/opt/autoglm-ocr-backend"
CERT_DIR="${INSTALL_DIR}/certs"
CONFIG_FILE="${INSTALL_DIR}/config.ini"
SERVICE_GROUP="autoglm-ocr"
DEST_CERT="${CERT_DIR}/api-server.crt"
DEST_KEY="${CERT_DIR}/api-server.key"
SERVICES=(autoglm-ocr-sync)

usage() {
    cat <<'EOF'
Usage:
  sudo bash setup_tls.sh self-signed [DOMAIN_OR_IP]
  sudo bash setup_tls.sh letsencrypt DOMAIN EMAIL

Notes:
  This script manages the backend API service certificate only.
EOF
}

patch_config() {
    local cert="$1"
    local key="$2"
    python3 - "$CONFIG_FILE" "$cert" "$key" <<'PYEOF'
import configparser
import sys

path, cert, key = sys.argv[1], sys.argv[2], sys.argv[3]
cfg = configparser.ConfigParser()
cfg.read(path, encoding='utf-8')
if not cfg.has_section('api_tls'):
    cfg.add_section('api_tls')
cfg.set('api_tls', 'enabled', 'true')
cfg.set('api_tls', 'cert_file', cert)
cfg.set('api_tls', 'key_file', key)
cfg.set('api_tls', 'min_tls_version', 'TLSv1.2')
with open(path, 'w', encoding='utf-8') as handle:
    cfg.write(handle)
print(f'[INFO]  Patched {path}: api_tls.enabled=true, cert={cert}, key={key}')
PYEOF
}

restart_component_services() {
    restart_services_if_present "${SERVICES[@]}"
}

san_for_identity() {
    local identity="$1"
    if [[ "$identity" =~ ^[0-9]+\.[0-9]+\.[0-9]+\.[0-9]+$ ]]; then
        echo "IP:${identity}"
    else
        echo "DNS:${identity}"
    fi
}

install_letsencrypt_hook() {
    local domain="$1"
    local hook_file="/etc/letsencrypt/renewal-hooks/deploy/autoglm-ocr-backend-sync.sh"
    cat > "$hook_file" <<EOF
#!/usr/bin/env bash
set -euo pipefail
install -m 640 -o root -g ${SERVICE_GROUP} /etc/letsencrypt/live/${domain}/fullchain.pem ${DEST_CERT}
install -m 640 -o root -g ${SERVICE_GROUP} /etc/letsencrypt/live/${domain}/privkey.pem ${DEST_KEY}
systemctl restart ${SERVICES[0]}
EOF
    chmod 755 "$hook_file"
}

main() {
    local mode="${1:-}"
    case "$mode" in
        -h|--help|'')
            usage
            [[ -n "$mode" ]] || exit 1
            ;;
        self-signed)
            require_root
            ensure_python3
            ensure_openssl
            local identity="${2:-$(hostname -I 2>/dev/null | awk '{print $1}')}"
            identity="${identity// /}"
            [[ -n "$identity" ]] || die "Could not infer host IP. Pass DOMAIN_OR_IP explicitly."
            info "Generating OCR backend API certificate for ${identity}..."
            generate_self_signed_pair "$DEST_CERT" "$DEST_KEY" "$SERVICE_GROUP" "$identity" "serverAuth" "$(san_for_identity "$identity")"
            patch_config "$DEST_CERT" "$DEST_KEY"
            restart_component_services
            info "OCR backend API certificate installed at ${DEST_CERT}"
            ;;
        letsencrypt)
            require_root
            ensure_python3
            ensure_openssl
            ensure_certbot
            local domain="${2:-}"
            local email="${3:-}"
            [[ -n "$domain" && -n "$email" ]] || { usage; exit 1; }
            prepare_cert_dir "$CERT_DIR" "$SERVICE_GROUP"
            stop_disable_service "${SERVICES[0]}"
            certbot certonly --standalone --non-interactive --agree-tos -m "$email" -d "$domain"
            install -m 640 -o root -g "$SERVICE_GROUP" "/etc/letsencrypt/live/${domain}/fullchain.pem" "$DEST_CERT"
            install -m 640 -o root -g "$SERVICE_GROUP" "/etc/letsencrypt/live/${domain}/privkey.pem" "$DEST_KEY"
            patch_config "$DEST_CERT" "$DEST_KEY"
            reload_systemd
            enable_now_services "${SERVICES[@]}"
            install_letsencrypt_hook "$domain"
            restart_component_services
            info "OCR backend API Let's Encrypt certificate synced to ${CERT_DIR}"
            ;;
        *)
            die "Unknown mode: ${mode}"
            ;;
    esac
}

main "$@"
