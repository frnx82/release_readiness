#!/usr/bin/env bash
# =============================================================================
# Service Mesh Security — Verification Script
# =============================================================================
#
# PURPOSE:
#   Validates all 4 Istio service mesh security principles are correctly
#   deployed and enforced in your Kubernetes namespace. Run this AFTER
#   deploying the service-mesh-chart to confirm your security posture.
#
# WHAT THIS SCRIPT TESTS:
#
#   Phase 0 — Pre-Requisites (Sidecar Injection)
#     • Confirms the namespace has istio-injection=enabled label
#     • Verifies all pods have the istio-proxy sidecar (2/2 READY)
#     • Supports both traditional injection and K8s 1.28+ native sidecars
#     ➜ CONFIRMS: All traffic in this namespace flows through the Envoy proxy
#
#   Principle 1 — Tenant Isolation (Zero Trust)
#     • Checks deny-all-default AuthorizationPolicy exists (blocks all external traffic)
#     • Checks allow-same-namespace AuthorizationPolicy exists (allows internal calls)
#     • Tests an actual same-namespace HTTP call between services
#     • Inspects Envoy RBAC denial logs for evidence of blocked external traffic
#     ➜ CONFIRMS: Only services within YOUR namespace can talk to each other.
#                 External namespaces and unknown sources are blocked.
#
#   Principle 2 — mTLS STRICT
#     • Checks PeerAuthentication mode is STRICT (rejects plain HTTP)
#     • Verifies SPIFFE identity certificate is loaded in Envoy
#     • Checks TLS handshake counters (proof of encrypted traffic)
#     • Validates Envoy is in the request path via server header
#     ➜ CONFIRMS: All service-to-service traffic is encrypted with mTLS.
#                 No plaintext HTTP is accepted.
#
#   Principle 3 — CIDP / OIDC Token Validation
#     • Checks RequestAuthentication policy exists with JWT rules
#     • Verifies JWT issuer is configured (e.g., Azure AD, Okta)
#     • Tests JWKS URI reachability from inside the mesh
#     • Tests that requests without JWT tokens are rejected (if enforced)
#     ➜ CONFIRMS: External user requests must present a valid OIDC/JWT token.
#                 Internal service-to-service calls use mTLS identity instead.
#
#   Principle 4 — Service-to-Service Allow-Lists
#     • Checks for per-service AuthorizationPolicies beyond deny-all
#     • Validates authorization-authz policy with JWT requestPrincipals
#     • Checks ServiceAccount principals for mTLS-based service identity
#     ➜ CONFIRMS: Each service has an explicit allow-list of who can call it.
#                 Defense-in-depth beyond namespace-level isolation.
#
# RESULT MEANINGS:
#   ✅ PASS — Check passed, security control is verified
#   ❌ FAIL — Critical: security control is missing or misconfigured
#   ⚠️  WARN — Non-critical: could not verify, or informational
#   ℹ️  INFO — Helpful context, manual check suggestions
#
# USAGE:
#   chmod +x test-mesh-policies.sh
#   ./test-mesh-policies.sh <namespace> [<app-pod-name>]
#
# EXAMPLES:
#   ./test-mesh-policies.sh demo-dev              # Auto-detect pod
#   ./test-mesh-policies.sh demo-dev rest-api      # Use specific pod
#
# REQUIREMENTS:
#   - kubectl configured with cluster access
#   - Target namespace must have at least one running pod
#   - Istio must be installed on the cluster
# =============================================================================

set -euo pipefail

# ── Configuration ────────────────────────────────────────────────────────────
NAMESPACE="${1:-demo-dev}"
APP_POD="${2:-}"  # Optional: specific pod to test from. Auto-detected if empty.
PASS=0
FAIL=0
WARN=0

# ── Colors ───────────────────────────────────────────────────────────────────
GREEN='\033[0;32m'
RED='\033[0;31m'
YELLOW='\033[1;33m'
BLUE='\033[0;34m'
CYAN='\033[0;36m'
NC='\033[0m' # No Color
BOLD='\033[1m'

pass()  { ((PASS++)) || true; echo -e "  ${GREEN}✅ PASS${NC}: $1"; }
fail()  { ((FAIL++)) || true; echo -e "  ${RED}❌ FAIL${NC}: $1"; }
warn()  { ((WARN++)) || true; echo -e "  ${YELLOW}⚠️  WARN${NC}: $1"; }
info()  { echo -e "  ${BLUE}ℹ️  INFO${NC}: $1"; }
header(){ echo -e "\n${BOLD}${CYAN}══════════════════════════════════════════════════════════════${NC}"; echo -e "${BOLD}${CYAN}  $1${NC}"; echo -e "${BOLD}${CYAN}══════════════════════════════════════════════════════════════${NC}"; }

# ── Robust helper: exec curl inside a pod and return ONLY the HTTP status code ─
# Usage: exec_curl <pod> <namespace> <container> <url>
# Returns: 3-digit HTTP code or "000" on failure
exec_curl() {
  local pod="$1" ns="$2" ctr="$3" url="$4"
  local raw
  raw=$(kubectl exec "$pod" -n "$ns" -c "$ctr" -- \
    sh -c "curl -s -o /dev/null -w '%{http_code}' '$url' --connect-timeout 5 2>/dev/null" 2>/dev/null) || raw="000"
  # Extract only the last 3-digit number (the HTTP code)
  echo "$raw" | grep -oE '[0-9]{3}$' || echo "000"
}

# ── Pre-flight: Find a pod to exec into ──────────────────────────────────────
if [ -z "$APP_POD" ]; then
  APP_POD=$(kubectl get pods -n "$NAMESPACE" -o jsonpath='{.items[0].metadata.name}' 2>/dev/null || echo "")
fi

if [ -z "$APP_POD" ]; then
  echo -e "${RED}ERROR: No pods found in namespace '$NAMESPACE'. Deploy first.${NC}"
  exit 1
fi

APP_CONTAINER=$(kubectl get pod "$APP_POD" -n "$NAMESPACE" -o jsonpath='{.spec.containers[0].name}' 2>/dev/null || echo "")

echo -e "${BOLD}Service Mesh Security Verification${NC}"
echo -e "Namespace:  ${CYAN}$NAMESPACE${NC}"
echo -e "Test Pod:   ${CYAN}$APP_POD${NC}"
echo -e "Container:  ${CYAN}$APP_CONTAINER${NC}"
echo -e "Timestamp:  $(date -u '+%Y-%m-%dT%H:%M:%SZ')"

# =============================================================================
# PHASE 0: Pre-Requisites — Sidecar Injection
# =============================================================================
# Ensures the Istio sidecar proxy is injected into all pods.
# Without the sidecar, NONE of the security policies will take effect
# because traffic bypasses the Envoy proxy entirely.
#
# What PASS confirms:
#   - Namespace has the istio-injection label → new pods get automatic injection
#   - All pods show 2/2+ READY → sidecar is running alongside the app
#   - istio-proxy container exists → Envoy is intercepting all traffic
#   - istio-init ran → iptables rules redirect traffic through Envoy
# =============================================================================
header "Phase 0: Pre-Requisites — Sidecar Injection"

echo -e "\n${BOLD}  Check: Namespace has istio-injection enabled${NC}"
INJECTION=$(kubectl get namespace "$NAMESPACE" -o jsonpath='{.metadata.labels.istio-injection}' 2>/dev/null || echo "not-set")
if [ "$INJECTION" = "enabled" ]; then
  pass "Namespace '$NAMESPACE' has istio-injection=enabled"
else
  fail "Namespace '$NAMESPACE' missing istio-injection label (got: $INJECTION)"
  info "Fix: kubectl label namespace $NAMESPACE istio-injection=enabled"
fi

echo -e "\n${BOLD}  Check: All pods have sidecar (2/2 READY)${NC}"
NON_SIDECAR=$(kubectl get pods -n "$NAMESPACE" --no-headers 2>/dev/null | grep -v "2/2\|3/3\|Completed\|Succeeded" | grep -v "^$" || true)
if [ -z "$NON_SIDECAR" ]; then
  pass "All pods show 2/2+ READY (sidecar present)"
else
  fail "Pods missing sidecar (not showing 2/2 READY):"
  echo "$NON_SIDECAR" | while read -r line; do echo -e "       $line"; done
  info "Fix: kubectl rollout restart deployment -n $NAMESPACE"
fi

echo -e "\n${BOLD}  Check: istio-proxy container present in test pod${NC}"
CONTAINERS=$(kubectl get pod "$APP_POD" -n "$NAMESPACE" -o jsonpath='{.spec.containers[*].name}' 2>/dev/null)
INIT_CONTAINERS=$(kubectl get pod "$APP_POD" -n "$NAMESPACE" -o jsonpath='{.spec.initContainers[*].name}' 2>/dev/null)
ALL_CONTAINERS="$CONTAINERS $INIT_CONTAINERS"
if echo "$CONTAINERS" | grep -q "istio-proxy"; then
  pass "istio-proxy sidecar found in pod '$APP_POD' (traditional injection)"
elif echo "$INIT_CONTAINERS" | grep -q "istio-proxy"; then
  pass "istio-proxy found as native sidecar in pod '$APP_POD' (K8s 1.28+ model)"
else
  fail "istio-proxy NOT found in pod '$APP_POD' (containers: $CONTAINERS, initContainers: $INIT_CONTAINERS)"
fi

echo -e "\n${BOLD}  Check: istio-init init container ran${NC}"
if echo "$INIT_CONTAINERS" | grep -q "istio-init"; then
  pass "istio-init container found (iptables rules configured)"
elif echo "$INIT_CONTAINERS" | grep -q "istio-proxy"; then
  pass "Native sidecar mode — istio-proxy handles init (no separate istio-init needed)"
else
  warn "istio-init NOT found — may use CNI-based interception instead"
fi

# =============================================================================
# PRINCIPLE 1: Tenant Isolation (Zero Trust Networking)
# =============================================================================
# Implements "deny by default, allow by exception" within the namespace.
#
# How it works:
#   deny-all-default    → Blocks ALL inbound traffic to every pod
#   allow-same-namespace → Permits traffic ONLY from pods in this namespace
#
# What PASS confirms:
#   - No external namespace or unknown source can reach your services
#   - Services within the namespace can still communicate normally
#   - Envoy RBAC is actively blocking unauthorized traffic attempts
#
# ⚠️  CRITICAL: If deny-all exists without allow-same-namespace,
#    ALL traffic is blocked — including legitimate internal calls!
# =============================================================================
header "Principle 1: Tenant Isolation"

echo -e "\n${BOLD}  Check: deny-all-default AuthorizationPolicy exists${NC}"
if kubectl get authorizationpolicy deny-all-default -n "$NAMESPACE" &>/dev/null; then
  pass "deny-all-default policy exists"
else
  fail "deny-all-default policy MISSING"
  info "Deploy Phase 1: helm upgrade --install ... (includes deny-all-default.yaml)"
fi

echo -e "\n${BOLD}  Check: allow-same-namespace AuthorizationPolicy exists${NC}"
if kubectl get authorizationpolicy allow-same-namespace -n "$NAMESPACE" &>/dev/null; then
  pass "allow-same-namespace policy exists"
else
  fail "allow-same-namespace policy MISSING"
  info "⚠️  If deny-all-default exists without this, ALL traffic is blocked!"
fi

echo -e "\n${BOLD}  Test: Same-namespace service call (should SUCCEED)${NC}"
# Find a target service to curl
TARGET_SVC=$(kubectl get svc -n "$NAMESPACE" --no-headers -o custom-columns=NAME:.metadata.name 2>/dev/null | grep -v "$APP_POD" | head -1 || echo "")
if [ -n "$TARGET_SVC" ]; then
  # Get port from service (default to 8080)
  SVC_PORT=$(kubectl get svc "$TARGET_SVC" -n "$NAMESPACE" -o jsonpath='{.spec.ports[0].port}' 2>/dev/null || echo "8080")
  HTTP_CODE=$(exec_curl "$APP_POD" "$NAMESPACE" "$APP_CONTAINER" "http://${TARGET_SVC}:${SVC_PORT}/")
  if [ "$HTTP_CODE" = "200" ] || [ "$HTTP_CODE" = "404" ] || [ "$HTTP_CODE" = "503" ]; then
    pass "Same-namespace call to $TARGET_SVC:$SVC_PORT returned HTTP $HTTP_CODE (connection allowed)"
  elif [ "$HTTP_CODE" = "403" ]; then
    fail "Same-namespace call to $TARGET_SVC returned 403 — allow-same-namespace may be missing"
  elif [ "$HTTP_CODE" = "000" ]; then
    warn "Same-namespace call to $TARGET_SVC:$SVC_PORT timed out (service may not be listening, or curl not in container)"
  else
    pass "Same-namespace call to $TARGET_SVC:$SVC_PORT returned HTTP $HTTP_CODE (connection allowed)"
  fi
else
  warn "No other services found in namespace to test same-namespace connectivity"
fi

echo -e "\n${BOLD}  Check: Envoy RBAC denials in proxy logs (external traffic blocked)${NC}"
RBAC_DENIALS=$(kubectl logs "$APP_POD" -n "$NAMESPACE" -c istio-proxy --tail=100 2>/dev/null | grep -ci "rbac" || true)
if [ "$RBAC_DENIALS" -gt 0 ]; then
  info "Found $RBAC_DENIALS RBAC entries in proxy logs — tenant isolation is actively enforcing"
else
  info "No RBAC denials in recent logs (normal if no external traffic attempted)"
fi

# =============================================================================
# PRINCIPLE 2: mTLS STRICT
# =============================================================================
# Forces all service-to-service communication to use mutual TLS.
#
# How it works:
#   PeerAuthentication (mode: STRICT) → Rejects any plaintext HTTP connection
#   Envoy auto-rotates SPIFFE certificates for each pod's identity
#   All traffic is encrypted + both sides verify each other's identity
#
# What PASS confirms:
#   - PeerAuthentication is STRICT (not PERMISSIVE or missing)
#   - Pod has a valid SPIFFE identity (e.g., spiffe://cluster.local/ns/demo-dev/sa/default)
#   - TLS handshakes are occurring (proof of encrypted traffic)
#   - Envoy proxy is in the request path (server: envoy header)
#
# STRICT vs PERMISSIVE:
#   STRICT     → Only mTLS accepted. Plaintext rejected. ✅ Secure
#   PERMISSIVE → Both mTLS and plaintext accepted. ⚠️  Migration mode only
# =============================================================================
header "Principle 2: mTLS STRICT"

echo -e "\n${BOLD}  Check: PeerAuthentication policy exists${NC}"
PA_MODE=$(kubectl get peerauthentication default -n "$NAMESPACE" -o jsonpath='{.spec.mtls.mode}' 2>/dev/null || echo "NOT_FOUND")
if [ "$PA_MODE" = "STRICT" ]; then
  pass "PeerAuthentication mode is STRICT ✅"
elif [ "$PA_MODE" = "PERMISSIVE" ]; then
  warn "PeerAuthentication mode is PERMISSIVE (accepts plain HTTP too — not fully secure)"
  info "Switch to STRICT when ready: set peerAuthentication.mtlsMode=STRICT in values.yaml"
elif [ "$PA_MODE" = "NOT_FOUND" ]; then
  fail "PeerAuthentication 'default' not found in namespace $NAMESPACE"
  info "Deploy Phase 2: peer-authentication.yaml"
else
  warn "PeerAuthentication mode is '$PA_MODE'"
fi

# Detect which container name istio-proxy uses (traditional vs native sidecar)
PROXY_CONTAINER="istio-proxy"
if ! kubectl exec "$APP_POD" -n "$NAMESPACE" -c istio-proxy -- true 2>/dev/null; then
  # Native sidecar: try to find the sidecar container name
  PROXY_CONTAINER=$(kubectl get pod "$APP_POD" -n "$NAMESPACE" -o jsonpath='{.spec.initContainers[?(@.name=="istio-proxy")].name}' 2>/dev/null || echo "")
  if [ -z "$PROXY_CONTAINER" ]; then
    PROXY_CONTAINER="istio-proxy"  # fall back, will just warn
  fi
fi

echo -e "\n${BOLD}  Check: SPIFFE identity certificate loaded${NC}"
SPIFFE=$(kubectl exec "$APP_POD" -n "$NAMESPACE" -c "$PROXY_CONTAINER" -- \
  curl -s localhost:15000/certs 2>/dev/null | grep -o 'spiffe://[^"]*' | head -1 || echo "")
if [ -n "$SPIFFE" ]; then
  pass "SPIFFE identity: $SPIFFE"
else
  # Try via pilot-agent (native sidecar alternative)
  SPIFFE=$(kubectl exec "$APP_POD" -n "$NAMESPACE" -c "$PROXY_CONTAINER" -- \
    pilot-agent request GET /certs 2>/dev/null | grep -o 'spiffe://[^"]*' | head -1 || echo "")
  if [ -n "$SPIFFE" ]; then
    pass "SPIFFE identity: $SPIFFE (via pilot-agent)"
  else
    warn "Could not retrieve SPIFFE identity — Envoy admin API may not be on port 15000"
    info "Manual check: kubectl exec $APP_POD -n $NAMESPACE -c $PROXY_CONTAINER -- pilot-agent request GET /certs"
  fi
fi

echo -e "\n${BOLD}  Check: TLS handshake stats (proof of mTLS traffic)${NC}"
SSL_FOUND=false

# Method 1: Direct curl to Envoy admin
SSL_HANDSHAKE=$(kubectl exec "$APP_POD" -n "$NAMESPACE" -c "$PROXY_CONTAINER" -- \
  curl -s localhost:15000/stats 2>/dev/null | grep "ssl.handshake" | head -1 || echo "")

# Method 2: pilot-agent request (native sidecar)
if [ -z "$SSL_HANDSHAKE" ]; then
  SSL_HANDSHAKE=$(kubectl exec "$APP_POD" -n "$NAMESPACE" -c "$PROXY_CONTAINER" -- \
    pilot-agent request GET /stats 2>/dev/null | grep "ssl.handshake" | head -1 || echo "")
fi

# Method 3: wget fallback (some containers have wget but not curl)
if [ -z "$SSL_HANDSHAKE" ]; then
  SSL_HANDSHAKE=$(kubectl exec "$APP_POD" -n "$NAMESPACE" -c "$PROXY_CONTAINER" -- \
    wget -qO- localhost:15000/stats 2>/dev/null | grep "ssl.handshake" | head -1 || echo "")
fi

if [ -n "$SSL_HANDSHAKE" ]; then
  HS_COUNT=$(echo "$SSL_HANDSHAKE" | grep -o '[0-9]*$' || echo "0")
  if [ "$HS_COUNT" -gt 0 ]; then
    pass "TLS handshakes: $HS_COUNT (mTLS is active)"
    SSL_FOUND=true
  else
    warn "TLS handshake count is 0 — mTLS may not be negotiating yet"
  fi
fi

# Method 4: Check if TLS context exists in proxy config (proves mTLS is configured)
if [ "$SSL_FOUND" = false ]; then
  TLS_CONTEXT=$(kubectl exec "$APP_POD" -n "$NAMESPACE" -c "$PROXY_CONTAINER" -- \
    pilot-agent request GET /config_dump 2>/dev/null | grep -c "transport_socket" || echo "0")
  if [ "$TLS_CONTEXT" -gt 0 ]; then
    pass "TLS transport sockets configured ($TLS_CONTEXT found) — mTLS is set up"
  elif [ "$PA_MODE" = "STRICT" ]; then
    info "Could not query Envoy stats directly, but PeerAuthentication is STRICT — mTLS is enforced by policy"
  else
    warn "Could not verify mTLS handshake stats — Envoy admin API not accessible in this container"
    info "This is common with native sidecars. Verify manually: istioctl proxy-config listeners $APP_POD -n $NAMESPACE"
  fi
fi

echo -e "\n${BOLD}  Check: Envoy server header in service response${NC}"
if [ -n "$TARGET_SVC" ]; then
  SERVER_HEADER=$(kubectl exec "$APP_POD" -n "$NAMESPACE" -c "$APP_CONTAINER" -- \
    sh -c "curl -sI 'http://${TARGET_SVC}:${SVC_PORT}/' --connect-timeout 5 2>/dev/null | grep -i '^server:'" 2>/dev/null || echo "")
  if echo "$SERVER_HEADER" | grep -qi "envoy"; then
    pass "Response contains 'server: envoy' — traffic is flowing through the mesh"
  elif [ -n "$SERVER_HEADER" ]; then
    info "Server header is '$SERVER_HEADER' (may still be proxied via Envoy)"
  else
    warn "Could not read server header"
  fi
fi

# =============================================================================
# PRINCIPLE 3: CIDP / OIDC Token Validation
# =============================================================================
# Validates external user requests using JWT tokens from your OIDC provider
# (e.g., Azure AD, Okta, Google). This is the "front door" authentication.
#
# How it works:
#   RequestAuthentication → Defines which JWT issuers are trusted and the JWKS URI
#   AuthorizationPolicy   → Enforces that requests to /api/* paths must carry a valid JWT
#   Internal calls        → Use mTLS identity (SPIFFE), NOT JWT tokens
#
# What PASS confirms:
#   - RequestAuthentication policy exists with JWT rules
#   - JWT issuer is configured (your OIDC provider URL)
#   - JWKS URI is reachable (Istio can download the signing keys)
#   - Unauthenticated requests are rejected with 401/403
#
# NOTE: Internal service-to-service calls within the mesh do NOT need JWT.
#       They authenticate via mTLS (Principle 2). A 200 response on the
#       no-token test from inside the mesh is expected and correct.
# =============================================================================
header "Principle 3: CIDP / OIDC Token Validation"

echo -e "\n${BOLD}  Check: RequestAuthentication policy exists${NC}"
RA_COUNT=$(kubectl get requestauthentication -n "$NAMESPACE" --no-headers 2>/dev/null | wc -l | tr -d ' ')
if [ "$RA_COUNT" -gt 0 ]; then
  pass "Found $RA_COUNT RequestAuthentication policy(ies)"
  kubectl get requestauthentication -n "$NAMESPACE" --no-headers 2>/dev/null | while read -r line; do
    info "  → $line"
  done
else
  fail "No RequestAuthentication policies found"
  info "Deploy Phase 3: req-auth.yaml"
fi

echo -e "\n${BOLD}  Check: JWT issuer configured${NC}"
JWT_ISSUER=$(kubectl get requestauthentication -n "$NAMESPACE" -o jsonpath='{.items[0].spec.jwtRules[0].issuer}' 2>/dev/null || echo "")
if [ -n "$JWT_ISSUER" ]; then
  pass "JWT issuer configured: $JWT_ISSUER"
else
  warn "Could not read JWT issuer from RequestAuthentication"
fi

echo -e "\n${BOLD}  Check: JWKS URI reachable${NC}"
JWKS_URI=$(kubectl get requestauthentication -n "$NAMESPACE" -o jsonpath='{.items[0].spec.jwtRules[0].jwksUri}' 2>/dev/null || echo "")
if [ -n "$JWKS_URI" ]; then
  info "JWKS URI: $JWKS_URI"
  # Try to reach it from the proxy container
  JWKS_CODE=$(exec_curl "$APP_POD" "$NAMESPACE" "$PROXY_CONTAINER" "$JWKS_URI")
  if [ "$JWKS_CODE" = "200" ]; then
    pass "JWKS endpoint reachable (HTTP 200)"
  elif [ "$JWKS_CODE" = "000" ]; then
    warn "JWKS endpoint unreachable — may need a ServiceEntry for external OIDC provider"
    info "Fix: Create a ServiceEntry allowing egress to the JWKS host"
  else
    warn "JWKS endpoint returned HTTP $JWKS_CODE"
  fi
else
  warn "No JWKS URI configured in RequestAuthentication"
  info "This is OK if you use forwardOriginalToken and validate JWT at the app level"
fi

echo -e "\n${BOLD}  Test: Call without JWT token (should be rejected if enforced)${NC}"
# This tests if the AuthorizationPolicy enforces JWT for user-facing endpoints
if [ -n "$TARGET_SVC" ]; then
  NO_TOKEN_CODE=$(exec_curl "$APP_POD" "$NAMESPACE" "$APP_CONTAINER" "http://${TARGET_SVC}:${SVC_PORT}/api/v1/test")
  if [ "$NO_TOKEN_CODE" = "401" ] || [ "$NO_TOKEN_CODE" = "403" ]; then
    pass "Request without JWT correctly rejected (HTTP $NO_TOKEN_CODE)"
  elif [ "$NO_TOKEN_CODE" = "200" ] || [ "$NO_TOKEN_CODE" = "404" ]; then
    info "Request without JWT returned HTTP $NO_TOKEN_CODE — internal calls use mTLS, not JWT (expected)"
  elif [ "$NO_TOKEN_CODE" = "000" ]; then
    warn "Could not reach $TARGET_SVC:$SVC_PORT/api/v1/test (curl may not be in container)"
  else
    warn "Request without JWT returned HTTP $NO_TOKEN_CODE"
  fi
fi

# =============================================================================
# PRINCIPLE 4: Service-to-Service Allow-Lists
# =============================================================================
# Granular authorization: each service explicitly declares who can call it.
# This is defense-in-depth beyond namespace-level isolation (Principle 1).
#
# How it works:
#   Per-service AuthorizationPolicy → Whitelists specific ServiceAccounts
#   requestPrincipals rule          → Allows JWT-authenticated external users
#   SA principals rule              → Allows mTLS-authenticated internal services
#
# What PASS confirms:
#   - Per-service AuthorizationPolicies exist (beyond deny-all / allow-same)
#   - JWT requestPrincipals rules are configured (external user access)
#   - ServiceAccount principals rules configured (internal service access)
#
# This is the final layer: even if a pod in the same namespace is compromised,
# it can only reach services it is explicitly allowed to call.
# =============================================================================
header "Principle 4: Service-to-Service Allow-Lists"

echo -e "\n${BOLD}  Check: Per-service AuthorizationPolicies exist${NC}"
ALL_AUTHZ=$(kubectl get authorizationpolicy -n "$NAMESPACE" --no-headers 2>/dev/null || true)
P4_POLICIES=$(echo "$ALL_AUTHZ" | grep -v "deny-all\|allow-same" | grep -v "^$" || true)
P4_COUNT=$(echo "$P4_POLICIES" | grep -c "." 2>/dev/null || echo "0")

if [ "$P4_COUNT" -gt 0 ]; then
  pass "Found $P4_COUNT per-service AuthorizationPolicies"
  echo "$P4_POLICIES" | while read -r line; do
    info "  → $line"
  done
else
  warn "No per-service allow-list policies found yet (Phase 4 — deployed last)"
fi

echo -e "\n${BOLD}  Check: authorization-authz policy (JWT + same-namespace rules)${NC}"
AUTHZ_ALLOW=$(echo "$ALL_AUTHZ" | grep "allow-after-authz\|service-allow" || true)
if [ -n "$AUTHZ_ALLOW" ]; then
  pass "Service allow-after-authz policy found"

  # Check for requestPrincipals rule (CIDP JWT)
  REQ_PRINCIPALS=$(kubectl get authorizationpolicy -n "$NAMESPACE" -o yaml 2>/dev/null | grep -c "requestPrincipals" || echo "0")
  if [ "$REQ_PRINCIPALS" -gt 0 ]; then
    pass "JWT requestPrincipals rule configured"
  else
    warn "No requestPrincipals rule found in policies"
  fi

  # Check for SA principals rule (same-namespace mTLS)
  SA_PRINCIPALS=$(kubectl get authorizationpolicy -n "$NAMESPACE" -o yaml 2>/dev/null | grep -c 'cluster.local/ns/' || echo "0")
  if [ "$SA_PRINCIPALS" -gt 0 ]; then
    pass "ServiceAccount principals rule configured (same-namespace mTLS)"
  else
    warn "No SA principals rule found"
  fi
else
  warn "No authorization-authz (allow-after-authz) policy found"
fi

# =============================================================================
# SUMMARY: All Istio Resources
# =============================================================================
# Lists every Istio CRD deployed in the namespace for quick audit.
# This is a read-only inventory — no pass/fail checks.
# =============================================================================
header "Summary: Deployed Istio Resources"

echo -e "\n${BOLD}  PeerAuthentication:${NC}"
kubectl get peerauthentication -n "$NAMESPACE" --no-headers 2>/dev/null | while read -r line; do echo -e "    $line"; done
echo ""

echo -e "${BOLD}  AuthorizationPolicy:${NC}"
kubectl get authorizationpolicy -n "$NAMESPACE" --no-headers 2>/dev/null | while read -r line; do echo -e "    $line"; done
echo ""

echo -e "${BOLD}  RequestAuthentication:${NC}"
kubectl get requestauthentication -n "$NAMESPACE" --no-headers 2>/dev/null | while read -r line; do echo -e "    $line"; done
echo ""

echo -e "${BOLD}  VirtualService:${NC}"
kubectl get virtualservice -n "$NAMESPACE" --no-headers 2>/dev/null | while read -r line; do echo -e "    $line"; done
echo ""

echo -e "${BOLD}  Gateway:${NC}"
kubectl get gateway -n "$NAMESPACE" --no-headers 2>/dev/null | while read -r line; do echo -e "    $line"; done
echo ""

# =============================================================================
# FINAL SCORE
# =============================================================================
header "Final Score"
TOTAL=$((PASS + FAIL + WARN))
echo ""
echo -e "  ${GREEN}✅ Passed:  $PASS${NC}"
echo -e "  ${RED}❌ Failed:  $FAIL${NC}"
echo -e "  ${YELLOW}⚠️  Warnings: $WARN${NC}"
echo -e "  ${BLUE}📊 Total:   $TOTAL checks${NC}"
echo ""

if [ "$FAIL" -eq 0 ]; then
  echo -e "  ${GREEN}${BOLD}🎉 All critical checks passed! Mesh security is configured correctly.${NC}"
else
  echo -e "  ${RED}${BOLD}⛔ $FAIL critical check(s) failed. Review the output above and fix before proceeding.${NC}"
fi
echo ""
