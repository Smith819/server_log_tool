#!/usr/bin/env bash
# Test script for server_post.py (Method 1: form/text/json POST)
# Usage: ./test/test_post.sh [server_ip] [port] [http|https]

set -euo pipefail

HOST="${1:-127.0.0.1}"
PORT="${2:-39282}"
SCHEME="${3:-http}"
BASE_URL="${SCHEME}://${HOST}:${PORT}"

# curl flags: add -k for https (skip cert verification for self-signed)
CURL_FLAGS=""
[[ "$SCHEME" == "https" ]] && CURL_FLAGS="-k"

# A real publicly accessible JPEG image
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
echo " AutoGLM server_post.py Test Suite"
echo " Target: ${BASE_URL}"
echo "========================================"
echo

# --- Health check ---
info "Checking server is reachable..."
HTTP_CODE=$(curl -s $CURL_FLAGS -o /dev/null -w "%{http_code}" --max-time 5 "${BASE_URL}/" || echo "000")
if [ "$HTTP_CODE" = "000" ]; then
    fail "Server not reachable at ${BASE_URL} — is server_post.py running?"
    exit 1
fi
info "Server reachable (HTTP ${HTTP_CODE})"

sleep 0.5

# --- Test 1: Form-encoded POST ---
echo
info "Test 1: Form-encoded POST (file_url field)"
NOW_MS=$(date +%s000)
TIME_ISO=$(date -u +%Y-%m-%dT%H:%M:%SZ)
RESP=$(curl -s $CURL_FLAGS -w "\n%{http_code}" -X POST "${BASE_URL}/" \
    --data-urlencode "file_url=${TEST_IMAGE_URL}" \
    -d "time=${NOW_MS}")
HTTP_CODE=$(echo "$RESP" | tail -1)
BODY=$(echo "$RESP" | head -n -1)
if [ "$HTTP_CODE" = "200" ]; then
    pass "Form POST returned HTTP 200"
    echo "       Response: $BODY"
else
    fail "Form POST returned HTTP $HTTP_CODE (expected 200). Body: $BODY"
fi

sleep 2

# --- Test 2: text/plain POST ---
echo
info "Test 2: text/plain POST (FILE_URL= line format)"
PLAIN_BODY="FILE_URL=${TEST_IMAGE_URL}
TIME=$(date -u +%Y-%m-%dT%H:%M:%SZ)"
RESP=$(curl -s $CURL_FLAGS -w "\n%{http_code}" -X POST "${BASE_URL}/" \
    -H "Content-Type: text/plain" \
    --data-binary "$PLAIN_BODY")
HTTP_CODE=$(echo "$RESP" | tail -1)
BODY=$(echo "$RESP" | head -n -1)
if [ "$HTTP_CODE" = "200" ]; then
    pass "text/plain POST returned HTTP 200"
    echo "       Response: $BODY"
else
    fail "text/plain POST returned HTTP $HTTP_CODE. Body: $BODY"
fi

sleep 2

# --- Test 3: JSON POST ---
echo
info "Test 3: JSON POST ({\"file_url\": \"...\"})"
JSON_BODY="{\"file_url\": \"${TEST_IMAGE_URL}\", \"time\": $(date +%s000), \"type\": \"picture_link\"}"
RESP=$(curl -s $CURL_FLAGS -w "\n%{http_code}" -X POST "${BASE_URL}/" \
    -H "Content-Type: application/json" \
    -d "$JSON_BODY")
HTTP_CODE=$(echo "$RESP" | tail -1)
BODY=$(echo "$RESP" | head -n -1)
if [ "$HTTP_CODE" = "200" ]; then
    pass "JSON POST returned HTTP 200"
    echo "       Response: $BODY"
else
    fail "JSON POST returned HTTP $HTTP_CODE. Body: $BODY"
fi

sleep 2

# --- Test 4: Reject non-image URL ---
echo
info "Test 4: Non-image URL should be rejected (expect 400)"
RESP=$(curl -s $CURL_FLAGS -w "\n%{http_code}" -X POST "${BASE_URL}/" \
    --data-urlencode "file_url=http://example.com/document.pdf")
HTTP_CODE=$(echo "$RESP" | tail -1)
if [ "$HTTP_CODE" = "400" ]; then
    pass "Non-image URL correctly rejected with HTTP 400"
else
    fail "Expected 400 for non-image URL, got $HTTP_CODE"
fi

# --- Test 5: Check log files created ---
echo
info "Test 5: Wait for download and check log files served by GET"
sleep 5  # wait for background download

LOG_URL="${BASE_URL}/${BASENAME}.log"
DLOG_URL="${BASE_URL}/${BASENAME}_download.log"

LOG_CODE=$(curl -s $CURL_FLAGS -o /dev/null -w "%{http_code}" "${LOG_URL}")
if [ "$LOG_CODE" = "200" ]; then
    pass "${BASENAME}.log served (HTTP 200)"
    echo "       Content: $(curl -s $CURL_FLAGS ${LOG_URL})"
else
    fail "${BASENAME}.log not found at ${LOG_URL} (HTTP ${LOG_CODE}) — download may still be in progress"
fi

DLOG_CODE=$(curl -s $CURL_FLAGS -o /dev/null -w "%{http_code}" "${DLOG_URL}")
if [ "$DLOG_CODE" = "200" ]; then
    pass "${BASENAME}_download.log served (HTTP 200)"
    echo "       Content: $(curl -s $CURL_FLAGS ${DLOG_URL})"
else
    fail "${BASENAME}_download.log not found at ${DLOG_URL} (HTTP ${DLOG_CODE})"
fi

echo
echo "========================================"
if [ "$EXIT_CODE" = "0" ]; then
    echo -e "${GREEN}All tests passed!${NC}"
else
    echo -e "${RED}Some tests failed. Check server logs with: journalctl -u autoglm-post -f${NC}"
fi
exit $EXIT_CODE
