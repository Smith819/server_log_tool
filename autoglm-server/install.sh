#!/usr/bin/env bash
set -euo pipefail

# ─── Colors ───────────────────────────────────────────────────────────────────
RED='\033[0;31m'
GREEN='\033[0;32m'
YELLOW='\033[1;33m'
NC='\033[0m' # No Color

info()    { echo -e "${GREEN}[INFO]${NC}  $*"; }
warn()    { echo -e "${YELLOW}[WARN]${NC}  $*"; }
error()   { echo -e "${RED}[ERROR]${NC} $*" >&2; }

# ─── 1. Check root ────────────────────────────────────────────────────────────
if [[ $EUID -ne 0 ]]; then
    error "This script must be run as root (use sudo)."
    exit 1
fi

INSTALL_DIR="/opt/autoglm-server"
SERVICE_USER="autoglm"
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"

# ─── 2. Detect OS ─────────────────────────────────────────────────────────────
if [[ -f /etc/os-release ]]; then
    # shellcheck disable=SC1091
    source /etc/os-release
    OS_ID="${ID:-unknown}"
    OS_ID_LIKE="${ID_LIKE:-}"
else
    OS_ID="unknown"
    OS_ID_LIKE=""
fi

is_debian_based() {
    [[ "$OS_ID" == "ubuntu" || "$OS_ID" == "debian" || "$OS_ID_LIKE" == *debian* || "$OS_ID_LIKE" == *ubuntu* ]]
}

is_rhel_based() {
    [[ "$OS_ID" == "centos" || "$OS_ID" == "rhel" || "$OS_ID" == "fedora" || \
       "$OS_ID" == "rocky" || "$OS_ID" == "almalinux" || \
       "$OS_ID_LIKE" == *rhel* || "$OS_ID_LIKE" == *fedora* ]]
}

info "Detected OS: ${OS_ID}"

# ─── 3. Install python3 if not present ────────────────────────────────────────
if ! command -v python3 &>/dev/null; then
    info "python3 not found. Installing..."
    if is_debian_based; then
        apt-get update -qq
        apt-get install -y python3
    elif is_rhel_based; then
        if command -v dnf &>/dev/null; then
            dnf install -y python3
        else
            yum install -y python3
        fi
    else
        error "Unsupported OS '${OS_ID}'. Please install python3 manually and re-run."
        exit 1
    fi
else
    info "python3 is already installed: $(python3 --version)"
fi

# ─── 4. Create system user ────────────────────────────────────────────────────
if ! id -u "$SERVICE_USER" &>/dev/null; then
    info "Creating system user '${SERVICE_USER}'..."
    useradd -r -s /bin/false "$SERVICE_USER"
else
    info "User '${SERVICE_USER}' already exists, skipping."
fi

# ─── 5. Create install directory ──────────────────────────────────────────────
info "Creating ${INSTALL_DIR}..."
mkdir -p "${INSTALL_DIR}"

# ─── 6. Copy server files ─────────────────────────────────────────────────────
for f in server_post.py server_multipart.py config.ini; do
    if [[ -f "${SCRIPT_DIR}/${f}" ]]; then
        info "Copying ${f} -> ${INSTALL_DIR}/${f}"
        cp "${SCRIPT_DIR}/${f}" "${INSTALL_DIR}/${f}"
    else
        warn "${f} not found in ${SCRIPT_DIR}, skipping."
    fi
done

# ─── 7. Create uploads and logs directories ───────────────────────────────────
info "Creating uploads and logs directories..."
mkdir -p "${INSTALL_DIR}/uploads"
mkdir -p "${INSTALL_DIR}/logs"

# ─── 8. Set ownership ─────────────────────────────────────────────────────────
info "Setting ownership of ${INSTALL_DIR} to ${SERVICE_USER}..."
chown -R "${SERVICE_USER}:${SERVICE_USER}" "${INSTALL_DIR}"

# ─── 9. Install systemd service files ─────────────────────────────────────────
for svc in autoglm-post autoglm-multipart; do
    SRC="${SCRIPT_DIR}/services/${svc}.service"
    DST="/etc/systemd/system/${svc}.service"
    if [[ -f "$SRC" ]]; then
        info "Installing ${svc}.service -> ${DST}"
        cp "$SRC" "$DST"
        chmod 644 "$DST"
    else
        error "Service file not found: ${SRC}"
        exit 1
    fi
done

# ─── 10. Reload systemd ───────────────────────────────────────────────────────
info "Reloading systemd daemon..."
systemctl daemon-reload

# ─── 11. Enable services ──────────────────────────────────────────────────────
info "Enabling autoglm-post and autoglm-multipart..."
systemctl enable autoglm-post autoglm-multipart

# ─── 12. Start services ───────────────────────────────────────────────────────
info "Starting autoglm-post and autoglm-multipart..."
systemctl start autoglm-post autoglm-multipart

# ─── 13. Open firewall ports ──────────────────────────────────────────────────
POST_PORT=39282
MULTIPART_PORT=39283

if command -v ufw &>/dev/null; then
    info "ufw detected. Opening ports ${POST_PORT} and ${MULTIPART_PORT}..."
    ufw allow "${POST_PORT}/tcp" || warn "Failed to open port ${POST_PORT} in ufw."
    ufw allow "${MULTIPART_PORT}/tcp" || warn "Failed to open port ${MULTIPART_PORT} in ufw."
elif command -v firewall-cmd &>/dev/null; then
    info "firewalld detected. Opening ports ${POST_PORT} and ${MULTIPART_PORT}..."
    firewall-cmd --permanent --add-port="${POST_PORT}/tcp" || warn "Failed to open port ${POST_PORT} in firewalld."
    firewall-cmd --permanent --add-port="${MULTIPART_PORT}/tcp" || warn "Failed to open port ${MULTIPART_PORT} in firewalld."
    firewall-cmd --reload || warn "Failed to reload firewalld."
else
    warn "No supported firewall (ufw/firewalld) detected. Make sure ports ${POST_PORT} and ${MULTIPART_PORT} are open manually."
fi

# ─── 14. Print success message ────────────────────────────────────────────────
SERVER_IP=$(hostname -I 2>/dev/null | awk '{print $1}' || echo "<server-ip>")

echo ""
echo -e "${GREEN}======================================================${NC}"
echo -e "${GREEN}  AutoGLM Server installed successfully!${NC}"
echo -e "${GREEN}======================================================${NC}"
echo ""
echo -e "  Server IP  : ${YELLOW}${SERVER_IP}${NC}"
echo -e "  POST server (Method 1) port : ${YELLOW}${POST_PORT}${NC}"
echo -e "  Multipart server (Method 2) port : ${YELLOW}${MULTIPART_PORT}${NC}"
echo ""
echo -e "  Config file : ${YELLOW}${INSTALL_DIR}/config.ini${NC}"
echo -e "  Uploads dir : ${YELLOW}${INSTALL_DIR}/uploads/${NC}"
echo -e "  Logs dir    : ${YELLOW}${INSTALL_DIR}/logs/${NC}"
echo ""
echo "  Check service status:"
echo "    systemctl status autoglm-post"
echo "    systemctl status autoglm-multipart"
echo ""
echo "  View logs:"
echo "    journalctl -u autoglm-post -f"
echo "    journalctl -u autoglm-multipart -f"
echo ""
echo -e "${YELLOW}  IMPORTANT: In the AutoGLM app settings, set:${NC}"
echo -e "    SERVER_LOG    -> http://${SERVER_IP}:${POST_PORT}/  (or :${MULTIPART_PORT}/)"
echo -e "    LOG_FILE_URL  -> match the server you are using"
echo -e "  Edit ${INSTALL_DIR}/config.ini to adjust ports or directories."
echo ""
