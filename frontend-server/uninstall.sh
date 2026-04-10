#!/usr/bin/env bash
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
# shellcheck disable=SC1091
source "${SCRIPT_DIR}/lib/common.sh"

INSTALL_DIR="/opt/autoglm-frontend"
SERVICE_USER="autoglm-frontend"
SERVICES=(autoglm-post autoglm-multipart)
PURGE_DATA=0
PURGE_USER=0

usage() {
    cat <<'EOF'
Usage:
  sudo bash uninstall.sh [--purge-data] [--purge-user]

Options:
  --purge-data   Remove /opt/autoglm-frontend
  --purge-user   Remove the autoglm-frontend service user
EOF
}

while [[ $# -gt 0 ]]; do
    case "$1" in
        --purge-data) PURGE_DATA=1 ;;
        --purge-user) PURGE_USER=1 ;;
        -h|--help) usage; exit 0 ;;
        *) die "Unknown option: $1" ;;
    esac
    shift
done

main() {
    require_root
    local svc
    for svc in "${SERVICES[@]}"; do
        stop_disable_service "$svc"
        remove_service_file "$svc"
    done
    reload_systemd
    remove_directory_if_requested "$INSTALL_DIR" "$PURGE_DATA"
    remove_user_if_requested "$SERVICE_USER" "$PURGE_USER"
    info "Frontend uninstall completed."
}

main
