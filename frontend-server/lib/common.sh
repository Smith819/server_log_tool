#!/usr/bin/env bash
set -euo pipefail

RED='\033[0;31m'
GREEN='\033[0;32m'
YELLOW='\033[1;33m'
NC='\033[0m'

_COMMON_OS_LOADED=0
_COMMON_APT_UPDATED=0

info()  { echo -e "${GREEN}[INFO]${NC}  $*"; }
warn()  { echo -e "${YELLOW}[WARN]${NC}  $*"; }
error() { echo -e "${RED}[ERROR]${NC} $*" >&2; }

die() {
    error "$*"
    exit 1
}

require_root() {
    if [[ ${EUID:-$(id -u)} -ne 0 ]]; then
        die "This script must be run as root (use sudo)."
    fi
}

load_os_release() {
    if [[ $_COMMON_OS_LOADED -eq 1 ]]; then
        return 0
    fi
    if [[ -f /etc/os-release ]]; then
        # shellcheck disable=SC1091
        source /etc/os-release
        OS_ID="${ID:-unknown}"
        OS_ID_LIKE="${ID_LIKE:-}"
    else
        OS_ID="unknown"
        OS_ID_LIKE=""
    fi
    _COMMON_OS_LOADED=1
}

is_debian_based() {
    load_os_release
    [[ "$OS_ID" == "ubuntu" || "$OS_ID" == "debian" || "$OS_ID_LIKE" == *debian* || "$OS_ID_LIKE" == *ubuntu* ]]
}

is_rhel_based() {
    load_os_release
    [[ "$OS_ID" == "centos" || "$OS_ID" == "rhel" || "$OS_ID" == "fedora" || "$OS_ID" == "rocky" || "$OS_ID" == "almalinux" || "$OS_ID_LIKE" == *rhel* || "$OS_ID_LIKE" == *fedora* ]]
}

pkg_install() {
    require_root
    local packages=("$@")
    if [[ ${#packages[@]} -eq 0 ]]; then
        return 0
    fi
    if is_debian_based; then
        if [[ $_COMMON_APT_UPDATED -eq 0 ]]; then
            apt-get update -qq
            _COMMON_APT_UPDATED=1
        fi
        DEBIAN_FRONTEND=noninteractive apt-get install -y "${packages[@]}"
    elif is_rhel_based; then
        if command -v dnf >/dev/null 2>&1; then
            dnf install -y "${packages[@]}"
        else
            yum install -y "${packages[@]}"
        fi
    else
        die "Unsupported OS '${OS_ID}'. Please install required packages manually."
    fi
}

ensure_command() {
    local command_name="$1"
    local package_name="${2:-$1}"
    if ! command -v "$command_name" >/dev/null 2>&1; then
        info "Installing missing command '${command_name}' via package '${package_name}'..."
        pkg_install "$package_name"
    fi
}

ensure_python3() {
    ensure_command python3 python3
}

ensure_openssl() {
    ensure_command openssl openssl
}

ensure_certbot() {
    ensure_command certbot certbot
}

ensure_system_user() {
    local user_name="$1"
    if id -u "$user_name" >/dev/null 2>&1; then
        info "User '${user_name}' already exists, skipping."
        return 0
    fi

    local shell_path="/usr/sbin/nologin"
    if [[ ! -x "$shell_path" ]]; then
        shell_path="/sbin/nologin"
    fi
    if [[ ! -x "$shell_path" ]]; then
        shell_path="/bin/false"
    fi

    info "Creating system user '${user_name}'..."
    useradd -r -s "$shell_path" "$user_name"
}

ensure_directory() {
    local dir_path="$1"
    local owner="$2"
    local group="$3"
    local mode="$4"
    mkdir -p "$dir_path"
    chown "$owner:$group" "$dir_path"
    chmod "$mode" "$dir_path"
}

install_managed_file() {
    local src="$1"
    local dst="$2"
    local owner="$3"
    local group="$4"
    local mode="$5"
    install -D -m "$mode" -o "$owner" -g "$group" "$src" "$dst"
}

systemctl_available() {
    command -v systemctl >/dev/null 2>&1
}

reload_systemd() {
    if systemctl_available; then
        systemctl daemon-reload
    else
        warn "systemctl not found; skipped daemon-reload."
    fi
}

install_service_file() {
    local src="$1"
    local unit_name="$2"
    [[ -f "$src" ]] || die "Service file not found: $src"
    install_managed_file "$src" "/etc/systemd/system/${unit_name}.service" root root 644
}

service_known() {
    local unit_name="$1"
    systemctl_available || return 1
    systemctl list-unit-files --type=service --all 2>/dev/null | awk '{print $1}' | grep -Fxq "${unit_name}.service"
}

stop_disable_service() {
    local unit_name="$1"
    if ! systemctl_available || ! service_known "$unit_name"; then
        return 0
    fi
    if systemctl is-active --quiet "$unit_name" 2>/dev/null; then
        info "Stopping ${unit_name}..."
        systemctl stop "$unit_name" || warn "Could not stop ${unit_name}."
    fi
    if systemctl is-enabled --quiet "$unit_name" 2>/dev/null; then
        info "Disabling ${unit_name}..."
        systemctl disable "$unit_name" || warn "Could not disable ${unit_name}."
    fi
}

remove_service_file() {
    local unit_name="$1"
    local service_file="/etc/systemd/system/${unit_name}.service"
    if [[ -f "$service_file" ]]; then
        info "Removing ${service_file}..."
        rm -f "$service_file"
    fi
}

enable_now_services() {
    systemctl_available || die "systemctl not available on this host."
    local units=("$@")
    if [[ ${#units[@]} -gt 0 ]]; then
        info "Enabling and starting: ${units[*]}"
        systemctl enable --now "${units[@]}"
    fi
}

restart_services_if_present() {
    systemctl_available || return 0
    local unit_name
    for unit_name in "$@"; do
        if service_known "$unit_name"; then
            info "Restarting ${unit_name}..."
            systemctl restart "$unit_name" || warn "Could not restart ${unit_name}."
        fi
    done
}

open_firewall_ports() {
    local ports=("$@")
    if [[ ${#ports[@]} -eq 0 ]]; then
        return 0
    fi
    if command -v ufw >/dev/null 2>&1; then
        local port
        for port in "${ports[@]}"; do
            ufw allow "${port}/tcp" >/dev/null 2>&1 || warn "Failed to open ${port}/tcp in ufw."
        done
        return 0
    fi
    if command -v firewall-cmd >/dev/null 2>&1; then
        local port
        for port in "${ports[@]}"; do
            firewall-cmd --permanent --add-port="${port}/tcp" >/dev/null 2>&1 || warn "Failed to open ${port}/tcp in firewalld."
        done
        firewall-cmd --reload >/dev/null 2>&1 || warn "Failed to reload firewalld."
        return 0
    fi
    warn "No supported firewall tool detected; open these TCP ports manually: ${ports[*]}"
}

remove_directory_if_requested() {
    local dir_path="$1"
    local purge_requested="$2"
    if [[ "$purge_requested" != "1" ]]; then
        info "Keeping ${dir_path}. Re-run with --purge-data to remove installed data."
        return 0
    fi
    if [[ -d "$dir_path" ]]; then
        info "Removing ${dir_path}..."
        rm -rf "$dir_path"
    fi
}

remove_user_if_requested() {
    local user_name="$1"
    local purge_requested="$2"
    if [[ "$purge_requested" != "1" ]]; then
        info "Keeping user '${user_name}'. Re-run with --purge-user to remove it."
        return 0
    fi
    if id -u "$user_name" >/dev/null 2>&1; then
        info "Removing user '${user_name}'..."
        userdel "$user_name" || warn "Could not remove user '${user_name}'."
    fi
}
