#!/usr/bin/env bash
set -euo pipefail

LIB_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
# shellcheck disable=SC1091
source "${LIB_DIR}/common.sh"

KEY_PROMPTED=0
KEY_STATUS=""
KEY_PASSPHRASE="${TLS_KEY_PASSPHRASE:-}"

usage_import_cert_pair() {
    cat <<'EOF'
Usage:
  sudo bash import_tls_cert.sh CERT_DIR [KEY_DIR]

Examples:
  sudo bash import_tls_cert.sh /root/my-cert-bundle
  sudo TLS_KEY_PASSPHRASE='secret' bash import_tls_cert.sh /root/cert-dir /root/key-dir
EOF
}

require_group() {
    local group_name="$1"
    if ! getent group "$group_name" >/dev/null 2>&1; then
        die "Group '${group_name}' does not exist. Run install.sh first."
    fi
}

prepare_cert_dir() {
    local cert_dir="$1"
    local group_name="$2"
    mkdir -p "$cert_dir"
    chown root:"$group_name" "$cert_dir"
    chmod 750 "$cert_dir"
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
            [[ "$lower" == *server* || "$lower" == *client* ]] && score=$((score + 20))
            [[ "$lower" == *cert* || "$lower" == *.crt || "$lower" == *.cer ]] && score=$((score + 10))
            [[ "$lower" == *chain* ]] && score=$((score + 5))
        else
            [[ "$lower" == *privkey* ]] && score=$((score + 50))
            [[ "$lower" == *private* ]] && score=$((score + 30))
            [[ "$lower" == *server* || "$lower" == *client* ]] && score=$((score + 20))
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

python_ssl_check_pair() {
    local cert="$1"
    local key="$2"
    python3 - "$cert" "$key" <<'PYEOF'
import ssl
import sys

ctx = ssl.SSLContext(ssl.PROTOCOL_TLS_CLIENT)
ctx.load_cert_chain(sys.argv[1], sys.argv[2])
print('python-ssl-ok')
PYEOF
}

generate_self_signed_pair() {
    local cert_file="$1"
    local key_file="$2"
    local group_name="$3"
    local common_name="$4"
    local usage="$5"
    local san_value="${6:-}"
    local -a openssl_args=()

    prepare_cert_dir "$(dirname "$cert_file")" "$group_name"

    openssl_args=(
        req -x509 -newkey rsa:4096 -sha256 -days 825 -nodes
        -keyout "$key_file"
        -out "$cert_file"
        -subj "/CN=${common_name}"
        -addext "extendedKeyUsage=${usage}"
    )
    if [[ -n "$san_value" ]]; then
        openssl_args+=(-addext "subjectAltName=${san_value}")
    fi

    openssl "${openssl_args[@]}"
    chown root:"$group_name" "$cert_file" "$key_file"
    chmod 640 "$cert_file" "$key_file"
}

import_matching_cert_pair() {
    local cert_search_dir="$1"
    local key_search_dir="$2"
    local cert_dir="$3"
    local dest_cert="$4"
    local dest_key="$5"
    local service_group="$6"
    local patch_callback="$7"
    local restart_callback="$8"

    local -a raw_cert_candidates raw_key_candidates
    local -a cert_candidates key_candidates
    local -a converted_cert_srcs converted_cert_files
    local -a converted_key_srcs converted_key_files converted_key_statuses
    local cert_candidate key_candidate cert_out key_out
    local cert_idx key_idx selected_cert selected_key selected_cert_src selected_key_src tmp_dir

    require_root
    ensure_python3
    ensure_openssl
    require_group "$service_group"

    [[ -n "$cert_search_dir" ]] || { usage_import_cert_pair; exit 1; }
    [[ -d "$cert_search_dir" ]] || die "Certificate search directory not found: $cert_search_dir"
    [[ -d "$key_search_dir" ]] || die "Key search directory not found: $key_search_dir"

    collect_candidates cert "$cert_search_dir" raw_cert_candidates
    collect_candidates key "$key_search_dir" raw_key_candidates

    [[ ${#raw_cert_candidates[@]} -gt 0 ]] || die "No certificate-like files found under: $cert_search_dir"
    [[ ${#raw_key_candidates[@]} -gt 0 ]] || die "No key-like files found under: $key_search_dir"

    mapfile -t cert_candidates < <(sort_candidates cert "${raw_cert_candidates[@]}")
    mapfile -t key_candidates < <(sort_candidates key "${raw_key_candidates[@]}")

    info "Found ${#cert_candidates[@]} certificate candidate(s)."
    info "Found ${#key_candidates[@]} private key candidate(s)."

    tmp_dir="$(mktemp -d)"
    trap "rm -rf -- '$tmp_dir'" EXIT

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

    [[ ${#converted_cert_files[@]} -gt 0 ]] || die "Could not convert any certificate candidate into PEM."
    if [[ ${#converted_key_files[@]} -eq 0 ]]; then
        [[ -n "${TLS_KEY_PASSPHRASE:-}" ]] || warn "If the key is encrypted, rerun with TLS_KEY_PASSPHRASE or use an interactive terminal."
        die "Could not convert any private key candidate into an unencrypted PEM."
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
            if python_ssl_check_pair "${converted_cert_files[$cert_idx]}" "${converted_key_files[$key_idx]}" >/dev/null 2>&1; then
                selected_cert="${converted_cert_files[$cert_idx]}"
                selected_key="${converted_key_files[$key_idx]}"
                selected_cert_src="${converted_cert_srcs[$cert_idx]}"
                selected_key_src="${converted_key_srcs[$key_idx]}"
                break 2
            fi
        done
    done

    [[ -n "$selected_cert" && -n "$selected_key" ]] || die "No certificate/key pair both matched and passed Python ssl verification."

    info "Matched certificate: $selected_cert_src"
    info "Matched key        : $selected_key_src"

    prepare_cert_dir "$cert_dir" "$service_group"
    install -m 640 -o root -g "$service_group" "$selected_cert" "$dest_cert"
    install -m 640 -o root -g "$service_group" "$selected_key" "$dest_key"

    python_ssl_check_pair "$dest_cert" "$dest_key" >/dev/null
    "$patch_callback" "$dest_cert" "$dest_key"
    "$restart_callback"

    info "Certificate deployed to: $dest_cert"
    info "Private key deployed to: $dest_key"
    info "Done. Certificate import completed successfully."
}
