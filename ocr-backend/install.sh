#!/usr/bin/env bash
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
# shellcheck disable=SC1091
source "${SCRIPT_DIR}/lib/common.sh"

INSTALL_DIR="/opt/autoglm-ocr-backend"
SERVICE_USER="autoglm-ocr"
SERVICE_GROUP="autoglm-ocr"
SERVICES=(autoglm-ocr-sync)

main() {
    require_root
    load_os_release
    info "Detected OS: ${OS_ID}"
    ensure_python3
    ensure_system_user "$SERVICE_USER"

    ensure_directory "$INSTALL_DIR" root root 755
    ensure_directory "$INSTALL_DIR/logs" "$SERVICE_USER" "$SERVICE_GROUP" 755
    ensure_directory "$INSTALL_DIR/ocr_sync_work" "$SERVICE_USER" "$SERVICE_GROUP" 755
    ensure_directory "$INSTALL_DIR/runtime_cache" "$SERVICE_USER" "$SERVICE_GROUP" 755
    ensure_directory "$INSTALL_DIR/lib" root root 755
    ensure_directory "/etc/autoglm-ocr" root root 755

    install_managed_file "${SCRIPT_DIR}/ocr_backend_server.py" "${INSTALL_DIR}/ocr_backend_server.py" root root 644
    install_managed_file "${SCRIPT_DIR}/ocr_sync_service.py" "${INSTALL_DIR}/ocr_sync_service.py" root root 644
    install_managed_file "${SCRIPT_DIR}/ocr_result_upload.py" "${INSTALL_DIR}/ocr_result_upload.py" root root 644
    install_managed_file "${SCRIPT_DIR}/tls_context.py" "${INSTALL_DIR}/tls_context.py" root root 644
    install_managed_file "${SCRIPT_DIR}/config.ini" "${INSTALL_DIR}/config.ini" root "$SERVICE_GROUP" 640
    install_managed_file "${SCRIPT_DIR}/setup_tls.sh" "${INSTALL_DIR}/setup_tls.sh" root root 755
    install_managed_file "${SCRIPT_DIR}/import_tls_cert.sh" "${INSTALL_DIR}/import_tls_cert.sh" root root 755
    install_managed_file "${SCRIPT_DIR}/ocr-backend.env.example" "${INSTALL_DIR}/ocr-backend.env.example" root root 644
    install_managed_file "${SCRIPT_DIR}/lib/common.sh" "${INSTALL_DIR}/lib/common.sh" root root 644
    install_managed_file "${SCRIPT_DIR}/lib/tls_common.sh" "${INSTALL_DIR}/lib/tls_common.sh" root root 644
    if [[ ! -f /etc/autoglm-ocr/ocr-backend.env ]]; then
        install_managed_file "${SCRIPT_DIR}/ocr-backend.env.example" "/etc/autoglm-ocr/ocr-backend.env" root root 640
    fi

    install_service_file "${SCRIPT_DIR}/services/autoglm-ocr-sync.service" "autoglm-ocr-sync"
    reload_systemd
    enable_now_services "${SERVICES[@]}"
    open_firewall_ports 39384

    info "OCR backend installed successfully."
    info "Install dir : ${INSTALL_DIR}"
    info "Config file : ${INSTALL_DIR}/config.ini"
    info "Env file    : /etc/autoglm-ocr/ocr-backend.env"
    info "TLS scripts : ${INSTALL_DIR}/setup_tls.sh ${INSTALL_DIR}/import_tls_cert.sh"
    info "Services    : ${SERVICES[*]}"
}

main "$@"
