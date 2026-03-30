#!/usr/bin/env bash
set -euo pipefail

# ─── Colors ───────────────────────────────────────────────────────────────────
RED='\033[0;31m'
GREEN='\033[0;32m'
YELLOW='\033[1;33m'
NC='\033[0m'

info()  { echo -e "${GREEN}[INFO]${NC}  $*"; }
warn()  { echo -e "${YELLOW}[WARN]${NC}  $*"; }
error() { echo -e "${RED}[ERROR]${NC} $*" >&2; }

INSTALL_DIR="/opt/autoglm-server"
SERVICE_USER="autoglm"

# Check root
if [[ $EUID -ne 0 ]]; then
    error "This script must be run as root (use sudo)."
    exit 1
fi

echo ""
warn "This will uninstall the AutoGLM server services."
echo ""

# ─── 1. Stop services ─────────────────────────────────────────────────────────
for svc in autoglm-post autoglm-multipart; do
    if systemctl is-active --quiet "$svc" 2>/dev/null; then
        info "Stopping ${svc}..."
        systemctl stop "$svc" || warn "Could not stop ${svc}."
    else
        info "${svc} is not running, skipping stop."
    fi
done

# ─── 2. Disable services ──────────────────────────────────────────────────────
for svc in autoglm-post autoglm-multipart; do
    if systemctl is-enabled --quiet "$svc" 2>/dev/null; then
        info "Disabling ${svc}..."
        systemctl disable "$svc" || warn "Could not disable ${svc}."
    else
        info "${svc} is not enabled, skipping disable."
    fi
done

# ─── 3. Remove service files ──────────────────────────────────────────────────
for svc in autoglm-post autoglm-multipart; do
    SVC_FILE="/etc/systemd/system/${svc}.service"
    if [[ -f "$SVC_FILE" ]]; then
        info "Removing ${SVC_FILE}..."
        rm -f "$SVC_FILE"
    else
        info "${SVC_FILE} not found, skipping."
    fi
done

# ─── 4. Reload systemd ────────────────────────────────────────────────────────
info "Reloading systemd daemon..."
systemctl daemon-reload

# ─── 5. Ask about install directory ───────────────────────────────────────────
echo ""
read -r -p "Delete ${INSTALL_DIR}/ and all its contents? [y/N] " CONFIRM_DELETE
CONFIRM_DELETE="${CONFIRM_DELETE:-N}"

if [[ "$CONFIRM_DELETE" =~ ^[Yy]$ ]]; then
    info "Deleting ${INSTALL_DIR}..."
    rm -rf "${INSTALL_DIR}"
    info "${INSTALL_DIR} removed."
else
    info "Keeping ${INSTALL_DIR}. You can remove it manually later:"
    echo "    rm -rf ${INSTALL_DIR}"
fi

# ─── 6. Remove system user ────────────────────────────────────────────────────
if id -u "$SERVICE_USER" &>/dev/null; then
    info "Removing system user '${SERVICE_USER}'..."
    userdel "$SERVICE_USER" || warn "Could not remove user '${SERVICE_USER}'."
else
    info "User '${SERVICE_USER}' does not exist, skipping."
fi

# ─── 7. Print done ────────────────────────────────────────────────────────────
echo ""
echo -e "${GREEN}======================================================${NC}"
echo -e "${GREEN}  AutoGLM Server uninstalled successfully.${NC}"
echo -e "${GREEN}======================================================${NC}"
echo ""
