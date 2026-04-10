#!/usr/bin/env bash
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
# shellcheck disable=SC1091
source "${SCRIPT_DIR}/lib/common.sh"

INSTALL_DIR="/opt/autoglm-frontend"
SERVICE_USER="autoglm-frontend"
SERVICE_GROUP="autoglm-frontend"
SERVICES=(autoglm-post autoglm-multipart)

main() {
    require_root
    load_os_release
    info "Detected OS: ${OS_ID}"
    ensure_python3
    ensure_system_user "$SERVICE_USER"

    ensure_directory "$INSTALL_DIR" root root 755
    ensure_directory "$INSTALL_DIR/uploads" "$SERVICE_USER" "$SERVICE_GROUP" 755
    ensure_directory "$INSTALL_DIR/logs" "$SERVICE_USER" "$SERVICE_GROUP" 755
    ensure_directory "$INSTALL_DIR/test" root root 755
    ensure_directory "$INSTALL_DIR/lib" root root 755
    ensure_directory "/etc/autoglm-frontend" root root 755

    install_managed_file "${SCRIPT_DIR}/server_post.py" "${INSTALL_DIR}/server_post.py" root root 644
    install_managed_file "${SCRIPT_DIR}/server_multipart.py" "${INSTALL_DIR}/server_multipart.py" root root 644
    install_managed_file "${SCRIPT_DIR}/config.ini" "${INSTALL_DIR}/config.ini" root "$SERVICE_GROUP" 640
    install_managed_file "${SCRIPT_DIR}/setup_tls.sh" "${INSTALL_DIR}/setup_tls.sh" root root 755
    install_managed_file "${SCRIPT_DIR}/import_tls_cert.sh" "${INSTALL_DIR}/import_tls_cert.sh" root root 755
    install_managed_file "${SCRIPT_DIR}/frontend.env.example" "${INSTALL_DIR}/frontend.env.example" root root 644
    install_managed_file "${SCRIPT_DIR}/lib/common.sh" "${INSTALL_DIR}/lib/common.sh" root root 644
    install_managed_file "${SCRIPT_DIR}/lib/tls_common.sh" "${INSTALL_DIR}/lib/tls_common.sh" root root 644
    install_managed_file "${SCRIPT_DIR}/test/test_post.sh" "${INSTALL_DIR}/test/test_post.sh" root root 755
    install_managed_file "${SCRIPT_DIR}/test/test_multipart.sh" "${INSTALL_DIR}/test/test_multipart.sh" root root 755
    if [[ ! -f /etc/autoglm-frontend/frontend.env ]]; then
        install_managed_file "${SCRIPT_DIR}/frontend.env.example" "/etc/autoglm-frontend/frontend.env" root root 640
    fi

    install_service_file "${SCRIPT_DIR}/services/autoglm-post.service" "autoglm-post"
    install_service_file "${SCRIPT_DIR}/services/autoglm-multipart.service" "autoglm-multipart"
    reload_systemd
    enable_now_services "${SERVICES[@]}"
    open_firewall_ports 39282 39283

    info "Frontend installed successfully."
    info "Install dir : ${INSTALL_DIR}"
    info "Config file : ${INSTALL_DIR}/config.ini"
    info "Env file    : /etc/autoglm-frontend/frontend.env"
    info "TLS scripts : ${INSTALL_DIR}/setup_tls.sh ${INSTALL_DIR}/import_tls_cert.sh"
    info "Services    : ${SERVICES[*]}"
}

main "$@"
