#!/usr/bin/env bash
# setup_tls.sh — Generate or install TLS certificates for autoglm-server
# Supports:
#   1. Self-signed certificate (no domain needed, works on LAN)
#   2. Let's Encrypt via certbot (requires a public domain + port 80)
#
# Usage:
#   sudo bash setup_tls.sh self-signed [DOMAIN_OR_IP]
#   sudo bash setup_tls.sh letsencrypt DOMAIN EMAIL

set -euo pipefail

RED='\033[0;31m'
GREEN='\033[0;32m'
YELLOW='\033[1;33m'
NC='\033[0m'

info()  { echo -e "${GREEN}[INFO]${NC}  $*"; }
warn()  { echo -e "${YELLOW}[WARN]${NC}  $*"; }
error() { echo -e "${RED}[ERROR]${NC} $*" >&2; }

INSTALL_DIR="/opt/autoglm-server"
CERT_DIR="${INSTALL_DIR}/certs"
CONFIG_FILE="${INSTALL_DIR}/config.ini"

if [[ $EUID -ne 0 ]]; then
    error "This script must be run as root (use sudo)."
    exit 1
fi

MODE="${1:-}"

if [[ -z "$MODE" ]]; then
    echo "Usage:"
    echo "  sudo bash setup_tls.sh self-signed [DOMAIN_OR_IP]"
    echo "  sudo bash setup_tls.sh letsencrypt DOMAIN EMAIL"
    exit 1
fi

mkdir -p "${CERT_DIR}"
chown root:autoglm "${CERT_DIR}" 2>/dev/null || true
chmod 750 "${CERT_DIR}"

# ─── Helper: patch config.ini ─────────────────────────────────────────────────
patch_config() {
    local cert="$1" key="$2"
    if [[ ! -f "$CONFIG_FILE" ]]; then
        warn "config.ini not found at ${CONFIG_FILE}, skipping auto-patch."
        return
    fi
    # Enable TLS and set paths using Python (avoids sed quoting issues)
    python3 - "$CONFIG_FILE" "$cert" "$key" <<'PYEOF'
import sys, configparser
path, cert, key = sys.argv[1], sys.argv[2], sys.argv[3]
cfg = configparser.ConfigParser()
cfg.read(path)
if not cfg.has_section('tls'):
    cfg.add_section('tls')
if not cfg.has_section('server'):
    cfg.add_section('server')
cfg.set('tls', 'enabled', 'true')
cfg.set('tls', 'cert_file', cert)
cfg.set('tls', 'key_file', key)
cfg.set('tls', 'min_tls_version', 'TLSv1.2')
cfg.set('server', 'tls_cert', cert)
cfg.set('server', 'tls_key', key)
with open(path, 'w') as f:
    cfg.write(f)
print(f'[INFO]  Patched {path}: enabled=true, cert={cert}, key={key}')
PYEOF
}

# ─── Mode 1: Self-signed ──────────────────────────────────────────────────────
if [[ "$MODE" == "self-signed" ]]; then
    DOMAIN_OR_IP="${2:-$(hostname -I | awk '{print $1}')}"
    CERT_FILE="${CERT_DIR}/server.crt"
    KEY_FILE="${CERT_DIR}/server.key"

    info "Generating self-signed certificate for: ${DOMAIN_OR_IP}"

    # Build SAN extension
    if [[ "$DOMAIN_OR_IP" =~ ^[0-9]+\.[0-9]+\.[0-9]+\.[0-9]+$ ]]; then
        SAN="IP:${DOMAIN_OR_IP}"
    else
        SAN="DNS:${DOMAIN_OR_IP}"
    fi

    openssl req -x509 -newkey rsa:4096 -sha256 -days 3650 -nodes \
        -keyout "${KEY_FILE}" \
        -out "${CERT_FILE}" \
        -subj "/CN=${DOMAIN_OR_IP}" \
        -addext "subjectAltName=${SAN}"

    chmod 640 "${CERT_FILE}" "${KEY_FILE}"
    chown root:autoglm "${CERT_FILE}" "${KEY_FILE}" 2>/dev/null || true

    info "Certificate : ${CERT_FILE}"
    info "Private key : ${KEY_FILE}"
    info "Valid for   : 3650 days (10 years)"

    patch_config "${CERT_FILE}" "${KEY_FILE}"

    info "Restarting services..."
    systemctl restart autoglm-post autoglm-multipart 2>/dev/null || warn "Services not installed yet; restart manually after install.sh"

    echo
    warn "Self-signed cert: Android clients must trust this CA or skip verification."
    warn "For production use, prefer Let's Encrypt (option 2)."
    info "Done. Both servers now listen on HTTPS."

# ─── Mode 2: Let's Encrypt ────────────────────────────────────────────────────
elif [[ "$MODE" == "letsencrypt" ]]; then
    DOMAIN="${2:-}"
    EMAIL="${3:-}"

    if [[ -z "$DOMAIN" || -z "$EMAIL" ]]; then
        error "Usage: sudo bash setup_tls.sh letsencrypt DOMAIN EMAIL"
        exit 1
    fi

    info "Installing certbot..."
    if command -v apt-get &>/dev/null; then
        apt-get install -y certbot
    elif command -v dnf &>/dev/null; then
        dnf install -y certbot
    elif command -v yum &>/dev/null; then
        yum install -y certbot
    else
        error "Cannot detect package manager. Install certbot manually."
        exit 1
    fi

    info "Obtaining certificate for ${DOMAIN}..."
    # Temporarily stop services so certbot can use port 80 standalone
    systemctl stop autoglm-post autoglm-multipart 2>/dev/null || true
    certbot certonly --standalone --non-interactive --agree-tos \
        -m "${EMAIL}" -d "${DOMAIN}"
    systemctl start autoglm-post autoglm-multipart 2>/dev/null || true

    CERT_FILE="/etc/letsencrypt/live/${DOMAIN}/fullchain.pem"
    KEY_FILE="/etc/letsencrypt/live/${DOMAIN}/privkey.pem"

    # Give autoglm user read access to private key
    chmod 640 "${KEY_FILE}" 2>/dev/null || true
    chown root:autoglm "${KEY_FILE}" 2>/dev/null || true

    patch_config "${CERT_FILE}" "${KEY_FILE}"

    # Install renewal hook
    HOOK_FILE="/etc/letsencrypt/renewal-hooks/deploy/autoglm-restart.sh"
    cat > "${HOOK_FILE}" <<'HOOK'
#!/usr/bin/env bash
systemctl restart autoglm-post autoglm-multipart
HOOK
    chmod +x "${HOOK_FILE}"
    info "Renewal hook installed: ${HOOK_FILE}"

    info "Restarting services..."
    systemctl restart autoglm-post autoglm-multipart 2>/dev/null || warn "Services not installed yet; restart manually."

    info "Done. Certificate: ${CERT_FILE}"
    info "Auto-renewal is handled by certbot.timer (systemd) or cron."

else
    error "Unknown mode '${MODE}'. Use: self-signed | letsencrypt"
    exit 1
fi
