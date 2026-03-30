#!/usr/bin/env bash
# Import an existing certificate/key pair into /opt/autoglm-server/certs
# and convert it into a Python ssl.load_cert_chain compatible PEM pair.

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
SERVICE_GROUP="autoglm"
DEST_CERT="${CERT_DIR}/server.crt"
DEST_KEY="${CERT_DIR}/server.key"

KEY_PROMPTED=0
KEY_PASSPHRASE="${TLS_KEY_PASSPHRASE:-}"

usage() {
    cat <<'EOF'
Usage:
  sudo bash import_tls_cert.sh CERT_DIR [KEY_DIR]

Examples:
  sudo bash import_tls_cert.sh /root/my-cert-bundle
  sudo TLS_KEY_PASSPHRASE='secret' bash import_tls_cert.sh /root/cert-dir /root/key-dir

Behavior:
  - Recursively searches for .cer/.crt/.cert/.pem certificate files
  - Recursively searches for .key/.pem/.p8/.pk8 private key files
  - Tries PEM, DER, and PKCS#7 certificate formats
  - Detects encrypted private keys and converts them into an unencrypted PEM
  - Verifies the cert/key match and that Python ssl can load them
  - Copies the result to /opt/autoglm-server/certs
  - Updates /opt/autoglm-server/config.ini
EOF
}

require_root() {
    if [[ $EUID -ne 0 ]]; then
        error "This script must be run as root (use sudo)."
        exit 1
    fi
}

require_command() {
    if ! command -v "$1" >/dev/null 2>&1; then
        error "Missing required command: $1"
        exit 1
    fi
}

collect_candidates() {
    local mode="$1"
    local search_dir="$2"
    local -n result_ref="$3"
    local -a patterns=()

    if [[ "$mode" == "cert" ]]; then
        patterns=('*.cer' '*.crt' '*.cert' '*.pem')
    else
        patterns=('*.key' '*.pem' '*.p8' '*.pk8')
    fi

    result_ref=()
    while IFS= read -r -d '' file; do
        result_ref+=("$file")
    done < <(
        find "$search_dir" -type f \
            \( -iname "${patterns[0]}" -o -iname "${patterns[1]}" -o -iname "${patterns[2]}" -o -iname "${patterns[3]}" \) \
            -print0 2>/dev/null
    )
}

sort_candidates() {
    local mode="$1"
    shift
    local entry score lower
    for entry in "$@"; do
        lower="$(basename "$entry" | tr '[:upper:]' '[:lower:]')"
        score=0
        if [[ "$mode" == "cert" ]]; then
            [[ "$lower" == *fullchain* ]] && score=$((score + 50))
            [[ "$lower" == *server* ]] && score=$((score + 20))
            [[ "$lower" == *cert* || "$lower" == *.crt || "$lower" == *.cer ]] && score=$((score + 10))
            [[ "$lower" == *chain* ]] && score=$((score + 5))
        else
            [[ "$lower" == *privkey* ]] && score=$((score + 50))
            [[ "$lower" == *private* ]] && score=$((score + 30))
            [[ "$lower" == *server* ]] && score=$((score + 20))
            [[ "$lower" == *.key || "$lower" == *.pk8 || "$lower" == *.p8 ]] && score=$((score + 10))
        fi
        printf '%04d\t%s\n' "$score" "$entry"
    done | sort -r | cut -f2-
}

looks_like_encrypted_key() {
    local src="$1"
    if grep -q "BEGIN ENCRYPTED PRIVATE KEY" "$src" 2>/dev/null; then
        return 0
    fi
    if grep -q "Proc-Type: 4,ENCRYPTED" "$src" 2>/dev/null; then
        return 0
    fi
    return 1
}

looks_like_private_key() {
    local src="$1"
    local lower

    lower="$(basename "$src" | tr '[:upper:]' '[:lower:]')"
    if [[ "$lower" == *.key || "$lower" == *.pk8 || "$lower" == *.p8 ]]; then
        return 0
    fi
    if grep -Eq "BEGIN (RSA |EC |DSA |ENCRYPTED )?PRIVATE KEY" "$src" 2>/dev/null; then
        return 0
    fi
    return 1
}

prompt_for_passphrase() {
    if [[ -n "$KEY_PASSPHRASE" ]]; then
        return 0
    fi
    if [[ -t 0 ]]; then
        read -rsp "Private key passphrase: " KEY_PASSPHRASE
        echo
        KEY_PROMPTED=1
        return 0
    fi
    return 1
}

convert_cert_to_pem() {
    local src="$1"
    local out="$2"
    local tmp="${out}.tmp"

    rm -f "$tmp"
    if openssl x509 -in "$src" -inform PEM -out "$tmp" >/dev/null 2>&1; then
        mv "$tmp" "$out"
        return 0
    fi
    if openssl x509 -in "$src" -inform DER -out "$tmp" >/dev/null 2>&1; then
        mv "$tmp" "$out"
        return 0
    fi
    if openssl pkcs7 -print_certs -in "$src" -inform PEM 2>/dev/null | awk '/BEGIN CERTIFICATE/,/END CERTIFICATE/' >"$tmp"; then
        if grep -q "BEGIN CERTIFICATE" "$tmp" 2>/dev/null; then
            mv "$tmp" "$out"
            return 0
        fi
    fi
    if openssl pkcs7 -print_certs -in "$src" -inform DER 2>/dev/null | awk '/BEGIN CERTIFICATE/,/END CERTIFICATE/' >"$tmp"; then
        if grep -q "BEGIN CERTIFICATE" "$tmp" 2>/dev/null; then
            mv "$tmp" "$out"
            return 0
        fi
    fi
    rm -f "$tmp"
    return 1
}

convert_key_to_pem() {
    local src="$1"
    local out="$2"
    local mode tmp

    KEY_STATUS=""
    tmp="${out}.tmp"
    rm -f "$tmp"

    for mode in PEM DER; do
        if openssl pkey -in "$src" -inform "$mode" -passin pass: -out "$tmp" -outform PEM >/dev/null 2>&1; then
            KEY_STATUS="Imported unencrypted ${mode} private key."
            mv "$tmp" "$out"
            return 0
        fi
    done

    if ! looks_like_private_key "$src"; then
        rm -f "$tmp"
        return 1
    fi

    if looks_like_encrypted_key "$src"; then
        info "Encrypted private key detected: $src"
    fi

    if [[ -n "$KEY_PASSPHRASE" ]] || prompt_for_passphrase; then
        export TLS_KEY_PASSPHRASE="$KEY_PASSPHRASE"
        for mode in PEM DER; do
            if openssl pkey -in "$src" -inform "$mode" -passin env:TLS_KEY_PASSPHRASE -out "$tmp" -outform PEM >/dev/null 2>&1; then
                KEY_STATUS="Decrypted ${mode} private key into unencrypted PEM."
                mv "$tmp" "$out"
                return 0
            fi
        done
        if [[ $KEY_PROMPTED -eq 1 ]]; then
            warn "The provided private key passphrase did not work for: $src"
        fi
    fi

    rm -f "$tmp"
    return 1
}

pubkey_fingerprint_from_cert() {
    local cert="$1"
    openssl x509 -in "$cert" -pubkey -noout 2>/dev/null \
        | openssl pkey -pubin -outform PEM 2>/dev/null \
        | openssl dgst -sha256 2>/dev/null \
        | awk '{print $2}'
}

pubkey_fingerprint_from_key() {
    local key="$1"
    openssl pkey -in "$key" -pubout -outform PEM 2>/dev/null \
        | openssl dgst -sha256 2>/dev/null \
        | awk '{print $2}'
}

cert_and_key_match() {
    local cert="$1"
    local key="$2"
    local cert_fp key_fp

    cert_fp="$(pubkey_fingerprint_from_cert "$cert")"
    key_fp="$(pubkey_fingerprint_from_key "$key")"

    [[ -n "$cert_fp" && "$cert_fp" == "$key_fp" ]]
}

python_ssl_check() {
    local cert="$1"
    local key="$2"
    python3 - "$cert" "$key" <<'PYEOF'
import ssl
import sys

ctx = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
ctx.load_cert_chain(sys.argv[1], sys.argv[2])
print("python-ssl-ok")
PYEOF
}

patch_config() {
    local cert="$1"
    local key="$2"
    if [[ ! -f "$CONFIG_FILE" ]]; then
        warn "config.ini not found at ${CONFIG_FILE}, skipping auto-patch."
        return
    fi
    python3 - "$CONFIG_FILE" "$cert" "$key" <<'PYEOF'
import configparser
import sys

path, cert, key = sys.argv[1], sys.argv[2], sys.argv[3]
cfg = configparser.ConfigParser()
cfg.read(path, encoding="utf-8")
if not cfg.has_section("tls"):
    cfg.add_section("tls")
cfg.set("tls", "enabled", "true")
cfg.set("tls", "cert_file", cert)
cfg.set("tls", "key_file", key)
cfg.set("tls", "min_tls_version", "TLSv1.2")
with open(path, "w", encoding="utf-8") as handle:
    cfg.write(handle)
print(f"[INFO]  Patched {path}: enabled=true, cert={cert}, key={key}")
PYEOF
}

restart_services() {
    if command -v systemctl >/dev/null 2>&1; then
        info "Restarting autoglm services..."
        if ! systemctl restart autoglm-post autoglm-multipart 2>/dev/null; then
            warn "Could not restart one or more services automatically. Restart them manually."
        fi
    fi
}

main() {
    local cert_search_dir key_search_dir tmp_dir
    local -a raw_cert_candidates raw_key_candidates
    local -a cert_candidates key_candidates
    local -a converted_cert_srcs converted_cert_files
    local -a converted_key_srcs converted_key_files converted_key_statuses
    local cert_candidate key_candidate cert_out key_out
    local cert_idx key_idx selected_cert selected_key
    local selected_cert_src selected_key_src
    local group_owner

    require_root
    require_command openssl
    require_command python3

    cert_search_dir="${1:-}"
    key_search_dir="${2:-${cert_search_dir}}"

    if [[ -z "$cert_search_dir" ]]; then
        usage
        exit 1
    fi

    if [[ ! -d "$cert_search_dir" ]]; then
        error "Certificate search directory not found: $cert_search_dir"
        exit 1
    fi
    if [[ ! -d "$key_search_dir" ]]; then
        error "Key search directory not found: $key_search_dir"
        exit 1
    fi

    if ! getent group "$SERVICE_GROUP" >/dev/null 2>&1; then
        error "Group '${SERVICE_GROUP}' does not exist. Run install.sh first."
        exit 1
    fi

    collect_candidates cert "$cert_search_dir" raw_cert_candidates
    collect_candidates key "$key_search_dir" raw_key_candidates

    if [[ ${#raw_cert_candidates[@]} -eq 0 ]]; then
        error "No certificate-like files found under: $cert_search_dir"
        exit 1
    fi
    if [[ ${#raw_key_candidates[@]} -eq 0 ]]; then
        error "No key-like files found under: $key_search_dir"
        exit 1
    fi

    mapfile -t cert_candidates < <(sort_candidates cert "${raw_cert_candidates[@]}")
    mapfile -t key_candidates < <(sort_candidates key "${raw_key_candidates[@]}")

    info "Found ${#cert_candidates[@]} certificate candidate(s)."
    info "Found ${#key_candidates[@]} private key candidate(s)."

    tmp_dir="$(mktemp -d)"
    trap 'rm -rf "$tmp_dir"' EXIT

    cert_idx=0
    for cert_candidate in "${cert_candidates[@]}"; do
        cert_out="${tmp_dir}/cert_${cert_idx}.pem"
        if convert_cert_to_pem "$cert_candidate" "$cert_out"; then
            converted_cert_srcs+=("$cert_candidate")
            converted_cert_files+=("$cert_out")
            info "Accepted certificate candidate: $cert_candidate"
            cert_idx=$((cert_idx + 1))
        else
            warn "Skipped non-certificate file: $cert_candidate"
        fi
    done

    key_idx=0
    for key_candidate in "${key_candidates[@]}"; do
        key_out="${tmp_dir}/key_${key_idx}.pem"
        if convert_key_to_pem "$key_candidate" "$key_out"; then
            converted_key_srcs+=("$key_candidate")
            converted_key_files+=("$key_out")
            converted_key_statuses+=("$KEY_STATUS")
            info "Accepted key candidate: $key_candidate"
            info "$KEY_STATUS"
            key_idx=$((key_idx + 1))
        else
            warn "Skipped unusable key file: $key_candidate"
        fi
    done

    if [[ ${#converted_cert_files[@]} -eq 0 ]]; then
        error "Could not convert any certificate candidate into PEM."
        exit 1
    fi
    if [[ ${#converted_key_files[@]} -eq 0 ]]; then
        error "Could not convert any private key candidate into an unencrypted PEM."
        if [[ -z "${TLS_KEY_PASSPHRASE:-}" ]]; then
            warn "If the key is encrypted, rerun with TLS_KEY_PASSPHRASE or use an interactive terminal."
        fi
        exit 1
    fi

    selected_cert=""
    selected_key=""
    selected_cert_src=""
    selected_key_src=""

    for cert_idx in "${!converted_cert_files[@]}"; do
        for key_idx in "${!converted_key_files[@]}"; do
            if ! cert_and_key_match "${converted_cert_files[$cert_idx]}" "${converted_key_files[$key_idx]}"; then
                continue
            fi
            if python_ssl_check "${converted_cert_files[$cert_idx]}" "${converted_key_files[$key_idx]}" >/dev/null 2>&1; then
                selected_cert="${converted_cert_files[$cert_idx]}"
                selected_key="${converted_key_files[$key_idx]}"
                selected_cert_src="${converted_cert_srcs[$cert_idx]}"
                selected_key_src="${converted_key_srcs[$key_idx]}"
                break 2
            fi
        done
    done

    if [[ -z "$selected_cert" || -z "$selected_key" ]]; then
        error "No certificate/key pair both matched and passed Python ssl verification."
        exit 1
    fi

    info "Matched certificate: $selected_cert_src"
    info "Matched key        : $selected_key_src"

    mkdir -p "$CERT_DIR"
    group_owner="root:${SERVICE_GROUP}"
    install -m 640 -o root -g "$SERVICE_GROUP" "$selected_cert" "$DEST_CERT"
    install -m 640 -o root -g "$SERVICE_GROUP" "$selected_key" "$DEST_KEY"
    chown "$group_owner" "$CERT_DIR"
    chmod 750 "$CERT_DIR"

    python_ssl_check "$DEST_CERT" "$DEST_KEY" >/dev/null
    patch_config "$DEST_CERT" "$DEST_KEY"
    restart_services

    info "Certificate deployed to: $DEST_CERT"
    info "Private key deployed to: $DEST_KEY"
    info "TLS directory permissions: $(stat -c '%A %U:%G %n' "$CERT_DIR")"
    info "Certificate permissions  : $(stat -c '%A %U:%G %n' "$DEST_CERT")"
    info "Key permissions          : $(stat -c '%A %U:%G %n' "$DEST_KEY")"
    info "Done. HTTPS is enabled in ${CONFIG_FILE}."
}

main "$@"
