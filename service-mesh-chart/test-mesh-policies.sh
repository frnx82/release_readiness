#!/usr/bin/env bash
# =============================================================================
# Service Mesh Security — Verification Script
# =============================================================================
# Tests all 4 security principles from the Istio Service Mesh Guide.
# Run this AFTER deploying the service-mesh-chart to your namespace.
#
# Usage:
#   chmod +x test-mesh-policies.sh
#   ./test-mesh-policies.sh <namespace> [<app-pod-name>]
#
# Example:
#   ./test-mesh-policies.sh demo-dev rest-api
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
# PHASE 0: Pre-Requisites
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
if echo "$CONTAINERS" | grep -q "istio-proxy"; then
  pass "istio-proxy sidecar found in pod '$APP_POD'"
else
  fail "istio-proxy NOT found in pod '$APP_POD' (containers: $CONTAINERS)"
fi

echo -e "\n${BOLD}  Check: istio-init init container ran${NC}"
INIT_CONTAINERS=$(kubectl get pod "$APP_POD" -n "$NAMESPACE" -o jsonpath='{.spec.initContainers[*].name}' 2>/dev/null)
if echo "$INIT_CONTAINERS" | grep -q "istio-init"; then
  pass "istio-init container found (iptables rules configured)"
else
  warn "istio-init NOT found — may use CNI-based interception instead"
fi

# =============================================================================
# PRINCIPLE 1: Tenant Isolation
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
  HTTP_CODE=$(kubectl exec "$APP_POD" -n "$NAMESPACE" -c "$APP_CONTAINER" -- \
    curl -s -o /dev/null -w "%{http_code}" "http://${TARGET_SVC}:8080/health" --connect-timeout 5 2>/dev/null || echo "000")
  if [ "$HTTP_CODE" = "200" ] || [ "$HTTP_CODE" = "404" ] || [ "$HTTP_CODE" = "503" ]; then
    pass "Same-namespace call to $TARGET_SVC returned HTTP $HTTP_CODE (connection allowed)"
  elif [ "$HTTP_CODE" = "403" ]; then
    fail "Same-namespace call to $TARGET_SVC returned 403 — allow-same-namespace may be missing"
  else
    warn "Same-namespace call to $TARGET_SVC returned HTTP $HTTP_CODE"
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

echo -e "\n${BOLD}  Check: SPIFFE identity certificate loaded${NC}"
SPIFFE=$(kubectl exec "$APP_POD" -n "$NAMESPACE" -c istio-proxy -- \
  curl -s localhost:15000/certs 2>/dev/null | grep -o 'spiffe://[^"]*' | head -1 || echo "")
if [ -n "$SPIFFE" ]; then
  pass "SPIFFE identity: $SPIFFE"
else
  warn "Could not retrieve SPIFFE identity from Envoy admin API"
fi

echo -e "\n${BOLD}  Check: TLS handshake stats (proof of mTLS traffic)${NC}"
SSL_HANDSHAKE=$(kubectl exec "$APP_POD" -n "$NAMESPACE" -c istio-proxy -- \
  curl -s localhost:15000/stats 2>/dev/null | grep "ssl.handshake" | head -1 || echo "")
if [ -n "$SSL_HANDSHAKE" ]; then
  HS_COUNT=$(echo "$SSL_HANDSHAKE" | grep -o '[0-9]*$' || echo "0")
  if [ "$HS_COUNT" -gt 0 ]; then
    pass "TLS handshakes: $HS_COUNT (mTLS is active)"
  else
    warn "TLS handshake count is 0 — mTLS may not be negotiating yet"
  fi
else
  warn "Could not retrieve SSL stats from Envoy"
fi

echo -e "\n${BOLD}  Check: Envoy server header in service response${NC}"
if [ -n "$TARGET_SVC" ]; then
  SERVER_HEADER=$(kubectl exec "$APP_POD" -n "$NAMESPACE" -c "$APP_CONTAINER" -- \
    curl -sI "http://${TARGET_SVC}:8080/health" --connect-timeout 5 2>/dev/null | grep -i "^server:" || echo "")
  if echo "$SERVER_HEADER" | grep -qi "envoy"; then
    pass "Response contains 'server: envoy' — traffic is flowing through the mesh"
  elif [ -n "$SERVER_HEADER" ]; then
    warn "Server header is '$SERVER_HEADER' — expected 'envoy'"
  else
    warn "Could not read server header"
  fi
fi

# =============================================================================
# PRINCIPLE 3: CIDP / OIDC Token Validation
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
  # Try to reach it from the pod
  JWKS_CODE=$(kubectl exec "$APP_POD" -n "$NAMESPACE" -c istio-proxy -- \
    curl -s -o /dev/null -w "%{http_code}" "$JWKS_URI" --connect-timeout 5 2>/dev/null || echo "000")
  if [ "$JWKS_CODE" = "200" ]; then
    pass "JWKS endpoint reachable (HTTP 200)"
  else
    warn "JWKS endpoint returned HTTP $JWKS_CODE (may need a ServiceEntry for external access)"
  fi
else
  warn "No JWKS URI found"
fi

echo -e "\n${BOLD}  Test: Call without JWT token (should be rejected if enforced)${NC}"
# This tests if the AuthorizationPolicy enforces JWT for user-facing endpoints
if [ -n "$TARGET_SVC" ]; then
  NO_TOKEN_CODE=$(kubectl exec "$APP_POD" -n "$NAMESPACE" -c "$APP_CONTAINER" -- \
    curl -s -o /dev/null -w "%{http_code}" "http://${TARGET_SVC}:8080/api/v1/test" --connect-timeout 5 2>/dev/null || echo "000")
  if [ "$NO_TOKEN_CODE" = "401" ] || [ "$NO_TOKEN_CODE" = "403" ]; then
    pass "Request without JWT correctly rejected (HTTP $NO_TOKEN_CODE)"
  elif [ "$NO_TOKEN_CODE" = "200" ] || [ "$NO_TOKEN_CODE" = "404" ]; then
    info "Request without JWT returned HTTP $NO_TOKEN_CODE — internal calls use mTLS, not JWT (expected)"
  else
    warn "Request without JWT returned HTTP $NO_TOKEN_CODE"
  fi
fi

# =============================================================================
# PRINCIPLE 4: Service-to-Service Allow-Lists
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
