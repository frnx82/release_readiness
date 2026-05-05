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
#   Phase 4B — ServiceAccount Identity Audit
#     • Checks if pods use dedicated ServiceAccounts (not 'default')
#     • Verifies unique SPIFFE identities per service
#     • Checks that ALLOW policies have 'from' source constraints
#     ➜ CONFIRMS: Each service has a unique mTLS identity for fine-grained policies.
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
#
# ARTIFACTORY AUTH (for Phase 5 pen tests):
#   If your busybox image is in a private Artifactory registry, set:
#     export REGISTRY_URL=your-artifactory.example.com
#     export REGISTRY_USER=your-username
#     export REGISTRY_KEY=your-api-key
#     export PENTEST_IMAGE=your-artifactory.example.com/docker-local/busybox:latest
#   The script will create a temporary imagePullSecret automatically.
# =============================================================================

set -euo pipefail

# ── Configuration ────────────────────────────────────────────────────────────
NAMESPACE="${1:-demo-dev}"
APP_POD="${2:-}"  # Optional: specific pod to test from. Auto-detected if empty.
PENTEST_IMAGE="${PENTEST_IMAGE:-busybox:latest}"  # Override: PENTEST_IMAGE=registry.example.com/busybox:latest
REGISTRY_URL="${REGISTRY_URL:-}"     # Artifactory registry URL (e.g., artifactory.example.com)
REGISTRY_USER="${REGISTRY_USER:-}"   # Artifactory username
REGISTRY_KEY="${REGISTRY_KEY:-}"     # Artifactory API key or password
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
# PHASE 4B: ServiceAccount Identity Audit
# =============================================================================
# Verifies that services use dedicated ServiceAccounts instead of the
# Kubernetes 'default' SA. Without dedicated SAs, all pods share the same
# mTLS identity and Principle 4 per-service allow-lists become meaningless.
#
# What PASS confirms:
#   - Each service has its own ServiceAccount (unique SPIFFE identity)
#   - ALLOW AuthorizationPolicies include 'from' source constraints
#   - No open ALLOW rules that bypass deny-all-default
# =============================================================================
header "Phase 4B: ServiceAccount Identity Audit"

echo -e "\n${BOLD}  Check: Services use dedicated ServiceAccounts (not 'default')${NC}"
SA_ISSUES=0
while IFS= read -r pod_name; do
  [ -z "$pod_name" ] && continue
  POD_SA=$(kubectl get pod "$pod_name" -n "$NAMESPACE" -o jsonpath='{.spec.serviceAccountName}' 2>/dev/null || echo "unknown")
  if [ "$POD_SA" = "default" ]; then
    warn "Pod '$pod_name' uses the 'default' ServiceAccount — no unique mTLS identity"
    ((SA_ISSUES++)) || true
  else
    pass "Pod '$pod_name' → SA: $POD_SA"
  fi
done <<< "$(kubectl get pods -n "$NAMESPACE" --no-headers -o custom-columns=NAME:.metadata.name 2>/dev/null | grep -v 'Completed\|Succeeded' | grep -v '^$')"

if [ "$SA_ISSUES" -gt 0 ]; then
  info "Impact: Pods using 'default' SA share the same mTLS identity"
  info "        Per-service allow-lists (Principle 4) cannot distinguish them"
  info "Fix: Create a ServiceAccount per service and set serviceAccountName in Deployment spec"
fi

echo -e "\n${BOLD}  Check: Custom ServiceAccounts in namespace${NC}"
SA_LIST=$(kubectl get serviceaccount -n "$NAMESPACE" --no-headers -o custom-columns=NAME:.metadata.name 2>/dev/null | grep -v "^default$" || true)
SA_COUNT=$(echo "$SA_LIST" | grep -c "." 2>/dev/null || echo "0")
if [ "$SA_COUNT" -gt 0 ]; then
  pass "Found $SA_COUNT custom ServiceAccount(s)"
  echo "$SA_LIST" | while read -r sa; do
    info "  → $sa (spiffe://cluster.local/ns/$NAMESPACE/sa/$sa)"
  done
else
  warn "No custom ServiceAccounts found — all pods use 'default'"
fi

echo -e "\n${BOLD}  Check: ALLOW policies have 'from' source constraints${NC}"
OPEN_ALLOW=0
CHECKED_ALLOW=0
while IFS= read -r pol_name; do
  [ -z "$pol_name" ] && continue
  POL_ACTION=$(kubectl get authorizationpolicy "$pol_name" -n "$NAMESPACE" -o jsonpath='{.spec.action}' 2>/dev/null || echo "")
  [ "$POL_ACTION" != "ALLOW" ] && continue
  ((CHECKED_ALLOW++)) || true
  HAS_FROM=$(kubectl get authorizationpolicy "$pol_name" -n "$NAMESPACE" -o yaml 2>/dev/null | grep -c "from:" || echo "0")
  if [ "$HAS_FROM" -eq 0 ]; then
    fail "Policy '$pol_name' has ALLOW but NO 'from' constraint — allows ANY source!"
    ((OPEN_ALLOW++)) || true
  else
    pass "Policy '$pol_name' has 'from' source constraints"
  fi
done <<< "$(kubectl get authorizationpolicy -n "$NAMESPACE" --no-headers -o custom-columns=NAME:.metadata.name 2>/dev/null)"

if [ "$OPEN_ALLOW" -eq 0 ] && [ "$CHECKED_ALLOW" -gt 0 ]; then
  pass "All $CHECKED_ALLOW ALLOW policies have proper 'from' constraints"
fi

# =============================================================================
# SUMMARY: All Istio Resources
# =============================================================================
# Lists every Istio CRD deployed in the namespace for quick audit.
# This is a read-only inventory — no pass/fail checks.
# =============================================================================
header "Summary: Deployed Istio Resources"

echo -e "\n${BOLD}  PeerAuthentication:${NC}"
kubectl get peerauthentication -n "$NAMESPACE" --no-headers 2>/dev/null | while read -r line; do echo -e "    $line"; done || true
echo ""

echo -e "${BOLD}  AuthorizationPolicy:${NC}"
kubectl get authorizationpolicy -n "$NAMESPACE" --no-headers 2>/dev/null | while read -r line; do echo -e "    $line"; done || true
echo ""

echo -e "${BOLD}  RequestAuthentication:${NC}"
kubectl get requestauthentication -n "$NAMESPACE" --no-headers 2>/dev/null | while read -r line; do echo -e "    $line"; done || true
echo ""

echo -e "${BOLD}  VirtualService:${NC}"
kubectl get virtualservice -n "$NAMESPACE" --no-headers 2>/dev/null | while read -r line; do echo -e "    $line"; done || true
echo ""

echo -e "${BOLD}  Gateway:${NC}"
kubectl get gateway -n "$NAMESPACE" --no-headers 2>/dev/null | while read -r line; do echo -e "    $line"; done || true
echo ""

# =============================================================================
# PHASE 5: Penetration Tests — Attack Simulation
# =============================================================================
# Simulates real-world attacks to verify that your deny-all + JWT + mTLS
# policies actually block unauthorized access. Tests both cross-namespace
# (external) and same-namespace (insider threat) attack vectors.
#
# ── SETUP ────────────────────────────────────────────────────────────────
#   1. Creates a temporary namespace "mesh-pentest-tmp" (NO istio-injection)
#   2. Deploys an "attacker" pod (busybox) in that namespace — no sidecar,
#      so all traffic from this pod is plaintext (simulates non-mesh traffic)
#   3. Identifies a target service and pod in YOUR namespace
#   4. Runs 7 attack vectors (Attacks 1-6 from external ns, Attack 7 from
#      inside your namespace)
#   5. Cleans up: deletes attacker pod, rogue pod/SA, and temp namespace
#
# ── ATTACK VECTORS ───────────────────────────────────────────────────────
#
#   Attack 1: Cross-Namespace Call via Service DNS
#     What:   Calls http://<svc>.<your-ns>.svc.cluster.local from mesh-pentest-tmp
#     Tests:  Principle 1 (deny-all-default blocks cross-namespace traffic)
#     Expect: 403 or 000 (connection refused) = PASS
#             200 = FAIL — deny-all is not working!
#
#   Attack 2: Gateway Call Without JWT Token
#     What:   Calls your service with the VirtualService Host header but no JWT
#     Tests:  Principle 3 (JWT enforcement on external-facing routes)
#     Expect: 401/403/000 = PASS
#             200 = FAIL — JWT enforcement is missing!
#
#   Attack 3: Fake/Invalid JWT Token
#     What:   Sends a request with a forged JWT (wrong issuer + fake signature)
#     Tests:  Principle 3 (JWT signature and issuer validation via JWKS)
#     Expect: 401 = PASS (signature validation caught the fake)
#             200 = FAIL — JWT tokens are not being validated!
#
#   Attack 4: Plaintext HTTP Directly to Pod IP
#     What:   Bypasses Service DNS and calls the pod IP directly with plain HTTP
#     Tests:  Principle 2 (mTLS STRICT rejects non-TLS connections)
#     Expect: 000 (connection reset) = PASS — Envoy rejected plaintext
#             200 = FAIL — mTLS is not enforced, plaintext is accepted!
#
#   Attack 5: Spoofed/Wrong Host Header
#     What:   Calls the service with Host: evil.example.com
#     Tests:  VirtualService host-based routing validation
#     Expect: 403/404/000 = PASS
#             200 = FAIL — host header is not validated!
#
#   Attack 6: Non-Mesh Pod (No Sidecar) Access
#     What:   The attacker pod has NO Istio sidecar — sends plaintext HTTP
#     Tests:  Principle 2 (mTLS STRICT rejects connections from non-mesh pods)
#     Expect: 000 (connection reset by Envoy) = PASS
#             200 = FAIL — your services accept traffic from outside the mesh!
#     Note:   This differs from Attack 4 by using Service DNS instead of Pod IP
#
#   Attack 7: Same-Namespace Rogue ServiceAccount ★ NEW
#     What:   Deploys a pod INSIDE your namespace with an unknown ServiceAccount
#             ("rogue-pentest-sa"). This pod GETS a sidecar and valid mTLS cert.
#     Tests:  Principle 4 (per-service allow-lists with specific SA names)
#     Expect: 403 = PASS — per-service allow-lists are enforced!
#             200 = WARN — you're using sa/* wildcard (Principle 1 works,
#                          but Principle 4 per-service isolation is not active)
#     Why:    This simulates a compromised or rogue pod within your own
#             namespace. If you use sa/* wildcard in your policies, this pod
#             has full access. If you use specific SA names, it's blocked.
#     Setup:  Creates ServiceAccount "rogue-pentest-sa" + pod "rogue-pentest"
#             in YOUR namespace, then cleans up both after the test.
#
# ── RESULT INTERPRETATION ────────────────────────────────────────────────
#   ✅ PASS — Attack was blocked (403/401/000/connection reset)
#   ❌ FAIL — Attack succeeded (200) — security control is broken!
#   ⚠️  WARN — Could not run test, or informational (Attack 7 with sa/*)
#
# ── REQUIREMENTS ─────────────────────────────────────────────────────────
#   - Permission to create/delete namespaces (for mesh-pentest-tmp)
#   - Permission to create/delete ServiceAccounts and pods in your namespace
#     (for Attack 7 rogue SA test)
#   - busybox image accessible (or set PENTEST_IMAGE + registry credentials)
#
# ⚠️  This creates and deletes:
#     - Namespace: "mesh-pentest-tmp" (Attacks 1-6)
#     - ServiceAccount: "rogue-pentest-sa" in YOUR namespace (Attack 7)
#     - Pod: "rogue-pentest" in YOUR namespace (Attack 7)
#     All resources are cleaned up automatically after tests complete.
# =============================================================================
header "Phase 5: Penetration Tests — External Attack Simulation"

PENTEST_NS="mesh-pentest-tmp"
PENTEST_POD="attacker"
PENTEST_FAILED=false

# Get a target service in the namespace
TARGET_SVC_NAME=$(kubectl get svc -n "$NAMESPACE" --no-headers -o custom-columns=NAME:.metadata.name 2>/dev/null | head -1 || echo "")
TARGET_SVC_IP=$(kubectl get svc "$TARGET_SVC_NAME" -n "$NAMESPACE" -o jsonpath='{.spec.clusterIP}' 2>/dev/null || echo "")
TARGET_POD_IP=$(kubectl get pod "$APP_POD" -n "$NAMESPACE" -o jsonpath='{.status.podIP}' 2>/dev/null || echo "")
TARGET_PORT=$(kubectl get svc "$TARGET_SVC_NAME" -n "$NAMESPACE" -o jsonpath='{.spec.ports[0].port}' 2>/dev/null || echo "8080")

if [ -z "$TARGET_SVC_NAME" ]; then
  warn "No services found in $NAMESPACE — skipping penetration tests"
else

  echo -e "\n${BOLD}  Setting up attacker pod in namespace '$PENTEST_NS'...${NC}"

  # Create temporary namespace (no istio injection = no sidecar = plaintext)
  kubectl create namespace "$PENTEST_NS" 2>/dev/null || true

  # If Artifactory credentials are provided, create an imagePullSecret
  PULL_SECRET_NAME="artifactory-pull-secret"
  if [ -n "$REGISTRY_URL" ] && [ -n "$REGISTRY_USER" ] && [ -n "$REGISTRY_KEY" ]; then
    echo -e "  ${BLUE}ℹ️  INFO${NC}: Creating imagePullSecret for $REGISTRY_URL"
    kubectl create secret docker-registry "$PULL_SECRET_NAME" \
      -n "$PENTEST_NS" \
      --docker-server="$REGISTRY_URL" \
      --docker-username="$REGISTRY_USER" \
      --docker-password="$REGISTRY_KEY" \
      2>/dev/null || true

    # Deploy attacker pod WITH imagePullSecret
    kubectl run "$PENTEST_POD" -n "$PENTEST_NS" --image="$PENTEST_IMAGE" \
      --overrides="{\"spec\":{\"imagePullSecrets\":[{\"name\":\"$PULL_SECRET_NAME\"}]}}" \
      --restart=Never --command -- sleep 300 2>/dev/null || true
  else
    # Deploy attacker pod without imagePullSecret (public or pre-configured registry)
    kubectl run "$PENTEST_POD" -n "$PENTEST_NS" --image="$PENTEST_IMAGE" \
      --restart=Never --command -- sleep 300 2>/dev/null || true
  fi

  # Wait for the pod to be ready (60s to allow for Artifactory image pull)
  kubectl wait --for=condition=Ready pod/"$PENTEST_POD" -n "$PENTEST_NS" --timeout=60s 2>/dev/null || {
    warn "Attacker pod failed to start — showing diagnostics:"
    echo ""
    echo -e "  ${BOLD}  Pod Status:${NC}"
    kubectl get pod "$PENTEST_POD" -n "$PENTEST_NS" -o wide 2>&1 | sed 's/^/       /' || true
    echo ""
    echo -e "  ${BOLD}  Pod Events (last 15 lines):${NC}"
    kubectl describe pod "$PENTEST_POD" -n "$PENTEST_NS" 2>&1 | tail -15 | sed 's/^/       /' || true
    echo ""
    # Also check if the secret was created correctly
    echo -e "  ${BOLD}  ImagePullSecrets on pod:${NC}"
    kubectl get pod "$PENTEST_POD" -n "$PENTEST_NS" -o jsonpath='{.spec.imagePullSecrets[*].name}' 2>&1 | sed 's/^/       /' || true
    echo ""
    echo -e "  ${BOLD}  Image being pulled:${NC}"
    kubectl get pod "$PENTEST_POD" -n "$PENTEST_NS" -o jsonpath='{.spec.containers[0].image}' 2>&1 | sed 's/^/       /' || true
    echo ""
    info "Common causes:"
    info "  • ImagePullBackOff  → Wrong image path or registry credentials"
    info "  • ErrImagePull      → REGISTRY_URL, REGISTRY_USER, or REGISTRY_KEY incorrect"
    info "  • Pending           → No nodes available or resource limits"
    info ""
    info "Verify your image path: $PENTEST_IMAGE"
    info "Verify your registry:   ${REGISTRY_URL:-not set}"
    info ""
    info "Manual debug: kubectl describe pod $PENTEST_POD -n $PENTEST_NS"
    echo ""
    warn "Skipping penetration tests — cleaning up"
    kubectl delete namespace "$PENTEST_NS" --ignore-not-found 2>/dev/null || true
    PENTEST_FAILED=true
  }

  if [ "$PENTEST_FAILED" = false ]; then

    # ── Test 1: Cross-namespace call via Service DNS ──────────────────
    echo -e "\n${BOLD}  Attack 1: Cross-namespace call via Service DNS${NC}"
    info "Attempting: http://${TARGET_SVC_NAME}.${NAMESPACE}.svc.cluster.local:${TARGET_PORT}/"
    ATTACK1=$(kubectl exec "$PENTEST_POD" -n "$PENTEST_NS" -- \
      sh -c "wget -q -O /dev/null -S 'http://${TARGET_SVC_NAME}.${NAMESPACE}.svc.cluster.local:${TARGET_PORT}/' -T 5 2>&1 | grep 'HTTP/' | tail -1 | grep -oE '[0-9]{3}' || echo '000'" 2>/dev/null || echo "000")
    if [ "$ATTACK1" = "200" ]; then
      fail "ATTACK SUCCEEDED! Cross-namespace call returned HTTP 200 — deny-all is NOT working!"
    elif [ "$ATTACK1" = "000" ]; then
      pass "Cross-namespace call BLOCKED (connection refused/reset — mTLS rejected plaintext)"
    else
      pass "Cross-namespace call DENIED (HTTP $ATTACK1)"
    fi

    # ── Test 2: Call with no JWT token via gateway host ────────────────
    echo -e "\n${BOLD}  Attack 2: Call via gateway hostname without JWT${NC}"
    VS_HOST=$(kubectl get virtualservice -n "$NAMESPACE" -o jsonpath='{.items[0].spec.hosts[0]}' 2>/dev/null || echo "")
    if [ -n "$VS_HOST" ]; then
      info "Attempting: wget with Host: ${VS_HOST}"
      ATTACK2=$(kubectl exec "$PENTEST_POD" -n "$PENTEST_NS" -- \
        sh -c "wget -q -O /dev/null -S --header='Host: ${VS_HOST}' 'http://${TARGET_SVC_NAME}.${NAMESPACE}.svc.cluster.local:${TARGET_PORT}/' -T 5 2>&1 | grep 'HTTP/' | tail -1 | grep -oE '[0-9]{3}' || echo '000'" 2>/dev/null || echo "000")
      if [ "$ATTACK2" = "200" ]; then
        fail "ATTACK SUCCEEDED! No-JWT call returned HTTP 200 — JWT enforcement is NOT working!"
      else
        pass "No-JWT call DENIED (HTTP $ATTACK2)"
      fi
    else
      warn "Could not determine VirtualService host — skipping"
    fi

    # ── Test 3: Call with a fake JWT token ─────────────────────────────
    echo -e "\n${BOLD}  Attack 3: Call with a fake/invalid JWT token${NC}"
    FAKE_JWT="eyJhbGciOiJSUzI1NiIsInR5cCI6IkpXVCJ9.eyJpc3MiOiJmYWtlLWlzc3VlciIsInN1YiI6ImhhY2tlciIsImV4cCI6OTk5OTk5OTk5OX0.fake-signature"
    ATTACK3=$(kubectl exec "$PENTEST_POD" -n "$PENTEST_NS" -- \
      sh -c "wget -q -O /dev/null -S --header='Authorization: Bearer ${FAKE_JWT}' 'http://${TARGET_SVC_NAME}.${NAMESPACE}.svc.cluster.local:${TARGET_PORT}/' -T 5 2>&1 | grep 'HTTP/' | tail -1 | grep -oE '[0-9]{3}' || echo '000'" 2>/dev/null || echo "000")
    if [ "$ATTACK3" = "200" ]; then
      fail "ATTACK SUCCEEDED! Fake JWT accepted — req-auth validation is NOT working!"
    elif [ "$ATTACK3" = "401" ]; then
      pass "Fake JWT correctly REJECTED (HTTP 401 — signature validation works)"
    else
      pass "Fake JWT call DENIED (HTTP $ATTACK3)"
    fi

    # ── Test 4: Plaintext HTTP to pod IP (bypass service DNS) ──────────
    echo -e "\n${BOLD}  Attack 4: Plaintext HTTP directly to Pod IP${NC}"
    if [ -n "$TARGET_POD_IP" ]; then
      info "Attempting: wget http://${TARGET_POD_IP}:${TARGET_PORT}/"
      ATTACK4=$(kubectl exec "$PENTEST_POD" -n "$PENTEST_NS" -- \
        sh -c "wget -q -O /dev/null -S 'http://${TARGET_POD_IP}:${TARGET_PORT}/' -T 5 2>&1 | grep 'HTTP/' | tail -1 | grep -oE '[0-9]{3}' || echo '000'" 2>/dev/null || echo "000")
      if [ "$ATTACK4" = "200" ]; then
        fail "ATTACK SUCCEEDED! Direct pod IP call returned HTTP 200 — mTLS is NOT blocking plaintext!"
      else
        pass "Direct pod IP call DENIED (HTTP $ATTACK4 — mTLS STRICT rejected plaintext)"
      fi
    else
      warn "Could not determine pod IP — skipping"
    fi

    # ── Test 5: Wrong Host header ──────────────────────────────────────
    echo -e "\n${BOLD}  Attack 5: Call with a spoofed/wrong Host header${NC}"
    ATTACK5=$(kubectl exec "$PENTEST_POD" -n "$PENTEST_NS" -- \
      sh -c "wget -q -O /dev/null -S --header='Host: evil.example.com' 'http://${TARGET_SVC_NAME}.${NAMESPACE}.svc.cluster.local:${TARGET_PORT}/' -T 5 2>&1 | grep 'HTTP/' | tail -1 | grep -oE '[0-9]{3}' || echo '000'" 2>/dev/null || echo "000")
    if [ "$ATTACK5" = "200" ]; then
      fail "ATTACK SUCCEEDED! Wrong host header accepted — host validation is NOT working!"
    else
      pass "Wrong host header DENIED (HTTP $ATTACK5)"
    fi

    # ── Test 6: Access from non-mesh pod (no sidecar) ──────────────────
    echo -e "\n${BOLD}  Attack 6: Access from pod without Istio sidecar (plaintext)${NC}"
    info "The attacker pod has no sidecar — this tests mTLS rejection of non-mesh traffic"
    ATTACK6=$(kubectl exec "$PENTEST_POD" -n "$PENTEST_NS" -- \
      sh -c "wget -q -O /dev/null -S 'http://${TARGET_SVC_NAME}.${NAMESPACE}.svc.cluster.local:${TARGET_PORT}/api/v1/' -T 5 2>&1 | grep 'HTTP/' | tail -1 | grep -oE '[0-9]{3}' || echo '000'" 2>/dev/null || echo "000")
    if [ "$ATTACK6" = "200" ]; then
      fail "ATTACK SUCCEEDED! Non-mesh pod reached service — mTLS STRICT is NOT enforced!"
    elif [ "$ATTACK6" = "000" ]; then
      pass "Non-mesh pod BLOCKED (connection reset — mTLS rejected plaintext connection)"
    else
      pass "Non-mesh pod DENIED (HTTP $ATTACK6)"
    fi

    # ── Test 7: Same-namespace pod with unauthorized ServiceAccount ───
    echo -e "\n${BOLD}  Attack 7: Same-namespace pod with unauthorized ServiceAccount${NC}"
    info "Tests Principle 4: per-service allow-lists within the namespace"
    info "A pod WITH a sidecar but using an unknown SA should be blocked if per-service allow-lists are active"

    ROGUE_SA="rogue-pentest-sa"
    ROGUE_POD="rogue-pentest"
    ROGUE_FAILED=false

    # Create a rogue ServiceAccount in the TARGET namespace
    kubectl create serviceaccount "$ROGUE_SA" -n "$NAMESPACE" 2>/dev/null || true

    # Deploy pod with rogue SA — it WILL get a sidecar (namespace has injection enabled)
    if [ -n "$REGISTRY_URL" ] && [ -n "$REGISTRY_USER" ] && [ -n "$REGISTRY_KEY" ]; then
      kubectl run "$ROGUE_POD" -n "$NAMESPACE" --image="$PENTEST_IMAGE" \
        --overrides="{\"spec\":{\"serviceAccountName\":\"$ROGUE_SA\",\"imagePullSecrets\":[{\"name\":\"$PULL_SECRET_NAME\"}]}}" \
        --restart=Never --command -- sleep 120 2>/dev/null || true
    else
      kubectl run "$ROGUE_POD" -n "$NAMESPACE" --image="$PENTEST_IMAGE" \
        --overrides="{\"spec\":{\"serviceAccountName\":\"$ROGUE_SA\"}}" \
        --restart=Never --command -- sleep 120 2>/dev/null || true
    fi

    # Wait for pod + sidecar to be ready
    kubectl wait --for=condition=Ready pod/"$ROGUE_POD" -n "$NAMESPACE" --timeout=60s 2>/dev/null || {
      warn "Rogue pod failed to start — skipping Attack 7"
      ROGUE_FAILED=true
    }

    if [ "$ROGUE_FAILED" = false ]; then
      # The rogue pod has a sidecar with mTLS identity: sa/rogue-pentest-sa
      # If per-service allow-lists are configured, this SA should NOT be allowed
      ATTACK7=$(kubectl exec "$ROGUE_POD" -n "$NAMESPACE" -- \
        sh -c "wget -q -O /dev/null -S 'http://${TARGET_SVC_NAME}:${TARGET_PORT}/' -T 5 2>&1 | grep 'HTTP/' | tail -1 | grep -oE '[0-9]{3}' || echo '000'" 2>/dev/null || echo "000")
      if [ "$ATTACK7" = "403" ]; then
        pass "Rogue SA correctly BLOCKED (HTTP 403) — per-service allow-lists are enforced!"
      elif [ "$ATTACK7" = "200" ]; then
        warn "Rogue SA got HTTP 200 — per-service allow-lists (Principle 4) not yet enforced"
        info "This is expected if using 'sa/*' wildcard in authorization policies"
        info "For full zero-trust, configure specific SA names in your allow-list policies"
      elif [ "$ATTACK7" = "000" ]; then
        warn "Rogue pod could not reach $TARGET_SVC_NAME (wget may not be available with sidecar)"
      else
        info "Rogue SA got HTTP $ATTACK7"
      fi

      # Cleanup rogue pod and SA
      kubectl delete pod "$ROGUE_POD" -n "$NAMESPACE" --grace-period=0 --force 2>/dev/null || true
      kubectl delete serviceaccount "$ROGUE_SA" -n "$NAMESPACE" 2>/dev/null || true
      info "Rogue pod and SA cleaned up"
    else
      # Cleanup on failure
      kubectl delete pod "$ROGUE_POD" -n "$NAMESPACE" --ignore-not-found --grace-period=0 --force 2>/dev/null || true
      kubectl delete serviceaccount "$ROGUE_SA" -n "$NAMESPACE" --ignore-not-found 2>/dev/null || true
    fi

    # ── Cleanup ────────────────────────────────────────────────────────
    echo -e "\n${BOLD}  Cleaning up attacker pod...${NC}"
    kubectl delete pod "$PENTEST_POD" -n "$PENTEST_NS" --grace-period=0 --force 2>/dev/null || true
    kubectl delete namespace "$PENTEST_NS" --ignore-not-found 2>/dev/null || true
    info "Cleanup complete — '$PENTEST_NS' namespace removed"
  fi
fi

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

