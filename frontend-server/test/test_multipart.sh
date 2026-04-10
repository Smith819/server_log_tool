#!/usr/bin/env bash
# Test script for server_multipart.py (Method 2: multipart/form-data)
# Usage: ./test/test_multipart.sh [server_ip] [port] [http|https]

set -euo pipefail

HOST="${1:-127.0.0.1}"
PORT="${2:-39283}"
SCHEME="${3:-http}"
BASE_URL="${SCHEME}://${HOST}:${PORT}"

# curl flags: add -k for https (skip cert verification for self-signed)
CURL_FLAGS=""
[[ "$SCHEME" == "https" ]] && CURL_FLAGS="-k"

TEST_IMAGE_URL="https://www.w3.org/WAI/WCAG21/Techniques/pdf/img/table-word.jpg"
BASENAME="table-word"

RED="\033[0;31m"
GREEN="\033[0;32m"
YELLOW="\033[1;33m"
NC="\033[0m"

pass() { echo -e "${GREEN}[PASS]${NC} $1"; }
fail() { echo -e "${RED}[FAIL]${NC} $1"; EXIT_CODE=1; }
info() { echo -e "${YELLOW}[INFO]${NC} $1"; }

EXIT_CODE=0

echo "========================================"
echo " AutoGLM server_multipart.py Test Suite"
echo " Target: ${BASE_URL}"
echo "========================================"
echo

# --- Health check ---
info "Checking server is reachable..."
HTTP_CODE=$(curl -s $CURL_FLAGS -o /dev/null -w "%{http_code}" --max-time 5 "${BASE_URL}/" || echo "000")
if [ "$HTTP_CODE" = "000" ]; then
    fail "Server not reachable at ${BASE_URL} — is server_multipart.py running?"
    exit 1
fi
info "Server reachable (HTTP ${HTTP_CODE})"

sleep 0.5

# --- Test 1: Multipart POST (link as file content, matching tryUploadLinkAsLogFile) ---
echo
info "Test 1: Multipart POST — file field contains image URL (type=link_log)"
TMPFILE=$(mktemp /tmp/autoglm_test_XXXXXX.log)
echo -n "${TEST_IMAGE_URL}" > "$TMPFILE"
CURL_TMPFILE="$TMPFILE"
if command -v cygpath >/dev/null 2>&1; then
    CURL_TMPFILE=$(cygpath -w "$TMPFILE")
fi

RESP=$(curl -s $CURL_FLAGS -w "\n%{http_code}" -X POST "${BASE_URL}/" \
    -F "file=@${CURL_TMPFILE};type=text/plain;filename=${BASENAME}.log" \
    -F "type=link_log" \
    -F "name=${BASENAME}.log")
HTTP_CODE=$(echo "$RESP" | tail -1)
BODY=$(echo "$RESP" | head -n -1)
rm -f "$TMPFILE"

if [ "$HTTP_CODE" = "200" ]; then
    pass "Multipart POST returned HTTP 200"
    echo "       Response: $BODY"
else
    fail "Multipart POST returned HTTP $HTTP_CODE. Body: $BODY"
fi

sleep 3

# --- Test 2: Reject non-image URL ---
echo
info "Test 2: Multipart POST with non-image URL (expect 400)"
TMPFILE2=$(mktemp /tmp/autoglm_test_XXXXXX.log)
echo -n "http://example.com/document.pdf" > "$TMPFILE2"
CURL_TMPFILE2="$TMPFILE2"
if command -v cygpath >/dev/null 2>&1; then
    CURL_TMPFILE2=$(cygpath -w "$TMPFILE2")
fi

RESP=$(curl -s $CURL_FLAGS -w "\n%{http_code}" -X POST "${BASE_URL}/" \
    -F "file=@${CURL_TMPFILE2};type=text/plain;filename=bad.log" \
    -F "type=link_log" \
    -F "name=bad.log")
HTTP_CODE=$(echo "$RESP" | tail -1)
BODY=$(echo "$RESP" | head -n -1)
rm -f "$TMPFILE2"

if [ "$HTTP_CODE" = "400" ]; then
    pass "Non-image URL correctly rejected with HTTP 400"
else
    fail "Expected 400, got $HTTP_CODE. Body: $BODY"
fi

# --- Test 3: Check log files served by GET ---
echo
info "Test 3: Wait for download and check log files served by GET"
sleep 5

LOG_URL="${BASE_URL}/${BASENAME}.log"
DLOG_URL="${BASE_URL}/${BASENAME}_download.log"

LOG_CODE=$(curl -s $CURL_FLAGS -o /dev/null -w "%{http_code}" "${LOG_URL}")
if [ "$LOG_CODE" = "200" ]; then
    pass "${BASENAME}.log served (HTTP 200)"
    echo "       Content: $(curl -s $CURL_FLAGS ${LOG_URL})"
else
    fail "${BASENAME}.log not found (HTTP ${LOG_CODE})"
fi

DLOG_CODE=$(curl -s $CURL_FLAGS -o /dev/null -w "%{http_code}" "${DLOG_URL}")
if [ "$DLOG_CODE" = "200" ]; then
    pass "${BASENAME}_download.log served (HTTP 200)"
    echo "       Content: $(curl -s $CURL_FLAGS ${DLOG_URL})"
else
    fail "${BASENAME}_download.log not found (HTTP ${DLOG_CODE})"
fi

# --- Test 4: Chinese filename URL ---
echo
info "Test 4: Chinese filename in URL (UTF-8 compatibility)"
CN_URL="https://www.example.com/images/%E6%B5%8B%E8%AF%95%E5%9B%BE%E7%89%87.jpg"
TMPFILE3=$(mktemp /tmp/autoglm_test_XXXXXX.log)
echo -n "${CN_URL}" > "$TMPFILE3"
CURL_TMPFILE3="$TMPFILE3"
if command -v cygpath >/dev/null 2>&1; then
    CURL_TMPFILE3=$(cygpath -w "$TMPFILE3")
fi

RESP=$(curl -s $CURL_FLAGS -w "\n%{http_code}" -X POST "${BASE_URL}/" \
    -F "file=@${CURL_TMPFILE3};type=text/plain;filename=cn_test.log" \
    -F "type=link_log" \
    -F "name=cn_test.log")
HTTP_CODE=$(echo "$RESP" | tail -1)
BODY=$(echo "$RESP" | head -n -1)
rm -f "$TMPFILE3"

if [ "$HTTP_CODE" = "200" ] || [ "$HTTP_CODE" = "400" ]; then
    pass "Chinese URL handled (HTTP ${HTTP_CODE}) — ${BODY}"
else
    fail "Unexpected response ${HTTP_CODE} for Chinese URL"
fi

echo
echo "========================================"
if [ "$EXIT_CODE" = "0" ]; then
    echo -e "${GREEN}All tests passed!${NC}"
else
    echo -e "${RED}Some tests failed. Check server logs with: journalctl -u autoglm-multipart -f${NC}"
fi
exit $EXIT_CODE
