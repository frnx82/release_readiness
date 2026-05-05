# ServiceAccount Security Guide — Per-Service Identity & Authorization

## Why Per-Service ServiceAccounts Matter

In an Istio service mesh, every pod's **mTLS identity** is derived from its Kubernetes ServiceAccount. This identity is the foundation for all authorization policy enforcement.

### The Identity Chain

```
Kubernetes ServiceAccount
    │
    ▼
Istio/istiod issues SPIFFE certificate
    │  e.g., spiffe://cluster.local/ns/uat/sa/rest-api
    │
    ▼
Envoy sidecar presents certificate during mTLS handshake
    │
    ▼
Target pod's Envoy checks AuthorizationPolicy "principals" field
    │
    ▼
✅ ALLOW or ❌ DENY based on policy match
```

### Without Dedicated ServiceAccounts

If all pods use the Kubernetes `default` ServiceAccount:

```
rest-api              → spiffe://cluster.local/ns/uat/sa/default
rest-api-v2           → spiffe://cluster.local/ns/uat/sa/default
billing-service       → spiffe://cluster.local/ns/uat/sa/default
notification-service  → spiffe://cluster.local/ns/uat/sa/default
user-service          → spiffe://cluster.local/ns/uat/sa/default
```

**Problem**: All 5 services have the **same identity**. Istio cannot distinguish who is calling who. Your authorization policies can only say "allow anything from this namespace" — which is Principle 1, not Principle 4.

### With Dedicated ServiceAccounts

```
rest-api              → spiffe://cluster.local/ns/uat/sa/rest-api
rest-api-v2           → spiffe://cluster.local/ns/uat/sa/rest-api-v2
billing-service       → spiffe://cluster.local/ns/uat/sa/billing-service
notification-service  → spiffe://cluster.local/ns/uat/sa/notification-service
user-service          → spiffe://cluster.local/ns/uat/sa/user-service
```

**Result**: Each service has a **unique cryptographic identity**. Authorization policies can now enforce per-service allow-lists.

---

## Security Benefits

### 1. Principle of Least Privilege

Each service can only call the services it needs:

```
rest-api         → can call: billing-service, user-service
billing-service  → can call: notification-service
user-service     → can call: notification-service
notification-service → can call: (nothing — leaf service)
```

If `notification-service` is compromised, it **cannot** call `billing-service` or `rest-api` because its SA (`sa/notification-service`) is not in their allow-lists.

### 2. Blast Radius Containment

| Scenario | With `sa/*` wildcard | With per-service SAs |
|----------|---------------------|---------------------|
| `notification-service` compromised | Attacker can reach ALL services | Attacker can reach NOTHING |
| `rest-api` compromised | Attacker can reach ALL services | Attacker can only reach billing + user |
| Rogue pod deployed with unknown SA | Full access to everything | Blocked by all policies (403) |

### 3. Audit Trail

With dedicated SAs, Envoy access logs show exactly which service made each call:

```
# With dedicated SAs — clear attribution
[2026-05-05T12:00:00Z] source.principal="cluster.local/ns/uat/sa/rest-api"
                       destination.service="billing-service"

# With default SA — no attribution
[2026-05-05T12:00:00Z] source.principal="cluster.local/ns/uat/sa/default"
                       destination.service="billing-service"
                       # Who called? Could be any of 20 services.
```

### 4. Compliance & Regulatory Requirements

Many compliance frameworks (SOC 2, PCI-DSS, HIPAA) require:
- **Unique service identities** — each workload must be individually identifiable
- **Least privilege access** — services must not have more access than required
- **Audit capability** — all service-to-service calls must be attributable

Per-service SAs satisfy all three requirements automatically through the Istio mTLS identity system.

---

## How Authorization Policies Work with ServiceAccounts

### Layer 1: Namespace Boundary (Principle 1)

```yaml
# deny-all-default.yaml — Blocks ALL traffic
apiVersion: security.istio.io/v1beta1
kind: AuthorizationPolicy
metadata:
  name: deny-all-default
spec: {}   # Empty spec = deny everything

# allow-same-namespace.yaml — Opens namespace boundary
# Uses sa/* wildcard because this layer only cares about
# "is the caller from this namespace?"
apiVersion: security.istio.io/v1beta1
kind: AuthorizationPolicy
metadata:
  name: allow-same-namespace
spec:
  action: ALLOW
  rules:
    - from:
        - source:
            principals:
              - "cluster.local/ns/uat/sa/*"  # ← Wildcard OK here
```

### Layer 2: Per-Service Allow-Lists (Principle 4)

```yaml
# rest-api-allow-list.yaml — Only specific services can call rest-api
apiVersion: security.istio.io/v1beta1
kind: AuthorizationPolicy
metadata:
  name: rest-api-allow-list
spec:
  selector:
    matchLabels:
      app: rest-api
  action: ALLOW
  rules:
    # Internal service callers (mTLS identity)
    - from:
        - source:
            principals:
              - "cluster.local/ns/uat/sa/ui-backend"      # ← Specific SA
              - "cluster.local/ns/uat/sa/reporting-service" # ← Specific SA
    # External callers (JWT identity)
    - from:
        - source:
            requestPrincipals:
              - "https://cidp.example.gdc.corp/oauth2/*"
```

### How Istio Evaluates (Order of Operations)

```
Incoming request to rest-api
    │
    ▼
1. deny-all-default → DENY (matches everything)
    │
    ▼
2. allow-same-namespace → Is caller from sa/* in this namespace?
    │  YES → provisional ALLOW
    │
    ▼
3. rest-api-allow-list → Is caller sa/ui-backend or sa/reporting-service?
    │  YES → ALLOW ✅
    │  NO  → fall through to deny-all → DENY ❌
```

> **Important**: When multiple ALLOW policies exist, Istio uses a **union** model.
> A request is allowed if it matches ANY ALLOW policy. To restrict further,
> you would remove the `allow-same-namespace` policy and rely solely on
> per-service allow-lists (full zero-trust).

---

## Implementation Guide

### Step 1: Create ServiceAccounts

For each microservice, create a ServiceAccount YAML:

```yaml
# templates/serviceaccounts.yaml
{{- range .Values.services }}
---
apiVersion: v1
kind: ServiceAccount
metadata:
  name: {{ .name }}
  namespace: {{ $.Values.namespace }}
  labels:
    app: {{ .name }}
{{- end }}
```

### Step 2: Assign to Deployments

In each Deployment spec, reference the ServiceAccount:

```yaml
spec:
  template:
    spec:
      serviceAccountName: {{ .name }}   # ← Must match SA name
      containers:
        - name: {{ .name }}
          image: {{ .image }}
```

### Step 3: Define Allow-Lists in values.yaml

```yaml
services:
  - name: rest-api
    allowedCallers:
      - ui-backend
      - reporting-service
      - rest-api-v2

  - name: rest-api-v2
    allowedCallers:
      - ui-backend
      - reporting-service

  - name: billing-service
    allowedCallers:
      - rest-api
      - payment-gateway

  - name: notification-service
    allowedCallers:
      - rest-api
      - billing-service
      - scheduler-service

  - name: user-service
    allowedCallers:
      - rest-api
      - rest-api-v2
      - admin-service
```

### Step 4: Generate Policies from Template

```yaml
# templates/per-service-authz.yaml
{{- range .Values.services }}
---
apiVersion: security.istio.io/v1beta1
kind: AuthorizationPolicy
metadata:
  name: {{ .name }}-allow-list
  namespace: {{ $.Release.Namespace }}
  labels:
    security-principle: "p4-service-allow-list"
    app: {{ .name }}
spec:
  selector:
    matchLabels:
      app: {{ .name }}
  action: ALLOW
  rules:
    # Rule 1: Internal callers (mTLS identity)
    - from:
        - source:
            principals:
              {{- range .allowedCallers }}
              - "cluster.local/ns/{{ $.Values.namespace }}/sa/{{ . }}"
              {{- end }}
    # Rule 2: External callers (JWT identity)
    - from:
        - source:
            requestPrincipals:
              - "{{ $.Values.virtualservice.cidp_oauth2_base_url }}/*"
{{- end }}
```

---

## Testing Methodology

### Test Matrix

The `test-mesh-policies.sh` script validates all 4 security principles plus penetration tests:

| Phase | What It Tests | Expected Result |
|-------|--------------|-----------------|
| **Phase 0** | Sidecar injection | All pods show 2/2 READY |
| **Principle 1** | Tenant isolation (deny-all + allow-same) | Cross-namespace blocked, same-namespace allowed |
| **Principle 2** | mTLS STRICT | SPIFFE identity present, TLS handshakes > 0 |
| **Principle 3** | JWT/OIDC validation | RequestAuthentication exists, JWKS reachable |
| **Principle 4** | Per-service allow-lists | Specific SA principals configured |
| **Phase 4B** | ServiceAccount identity audit | Dedicated SAs, no open ALLOW policies |
| **Phase 5** | Penetration tests (7 attacks) | All attacks denied |

### Penetration Test Details

| Attack | Vector | What It Proves |
|--------|--------|---------------|
| **1** | Cross-namespace call via Service DNS | deny-all blocks external namespaces |
| **2** | Gateway call without JWT | JWT enforcement on external traffic |
| **3** | Fake/invalid JWT token | JWT signature validation works |
| **4** | Plaintext HTTP to Pod IP | mTLS STRICT rejects plaintext |
| **5** | Spoofed Host header | Host-based routing is enforced |
| **6** | Non-mesh pod (no sidecar) | mTLS rejects non-mesh connections |
| **7** | Same-namespace rogue ServiceAccount | Per-service allow-lists enforced |

### Attack 7 Deep Dive — Rogue ServiceAccount Test

This is the most nuanced test. It deploys a pod **inside your namespace** (with a sidecar) but using an unauthorized ServiceAccount:

```
Your Namespace (uat)
├── rest-api         (sa/rest-api)        ← legitimate
├── billing-service  (sa/billing-service)  ← legitimate
├── rogue-pentest    (sa/rogue-pentest-sa) ← attacker!
```

The rogue pod has a valid mTLS certificate (it's in the mesh), but its SA identity `sa/rogue-pentest-sa` is **not** in any service's allow-list.

| Your Policy Config | Attack 7 Result | Meaning |
|-------------------|-----------------|---------|
| Uses `sa/*` wildcard | ⚠️ WARN (HTTP 200) | Namespace isolation works, but no per-service restriction |
| Uses specific SAs | ✅ PASS (HTTP 403) | Full zero-trust: unknown services are blocked |

### Running the Tests

```bash
# Basic run (auto-detect pod)
./test-mesh-policies.sh uat

# Specify a pod
./test-mesh-policies.sh uat rest-api-pod-abc123

# With private registry for pentest image
export REGISTRY_URL=artifactory.example.com
export REGISTRY_USER=your-username
export REGISTRY_KEY=your-api-key
export PENTEST_IMAGE=artifactory.example.com/docker-local/busybox:latest
./test-mesh-policies.sh uat
```

---

## Security Maturity Levels

### Level 1: Namespace Isolation (Minimum Viable Security)
- ✅ `deny-all-default` + `allow-same-namespace`
- ✅ mTLS STRICT
- ✅ All pods use `sa/default`
- **Protection**: Blocks cross-namespace attacks

### Level 2: External Authentication (Recommended Baseline)
- Everything from Level 1, plus:
- ✅ RequestAuthentication with JWT validation
- ✅ AuthorizationPolicy with `requestPrincipals`
- ✅ ALLOW policies have `from` constraints (no open allow rules)
- **Protection**: Blocks cross-namespace + unauthenticated external access

### Level 3: Per-Service Zero Trust (Production Best Practice)
- Everything from Level 2, plus:
- ✅ Dedicated ServiceAccount per service
- ✅ Per-service AuthorizationPolicies with specific SA names
- ✅ Attack 7 (rogue SA test) passes with 403
- **Protection**: Blocks cross-namespace + unauthenticated + unauthorized internal access

---

## Common Pitfalls

### 1. ALLOW Policy Without `from` Clause

```yaml
# ❌ BAD — Allows ANY source to bypass deny-all
spec:
  action: ALLOW
  rules:
    - to:
        - operation:
            hosts:
              - myservice.uat.svc.cluster.local

# ✅ GOOD — Restricts to specific sources
spec:
  action: ALLOW
  rules:
    - to:
        - operation:
            hosts:
              - myservice.uat.svc.cluster.local
      from:
        - source:
            principals:
              - "cluster.local/ns/uat/sa/authorized-caller"
```

### 2. Using `sa/*` with Per-Service Policies

If `allow-same-namespace` uses `sa/*` AND you have per-service policies, the `sa/*` wildcard takes precedence (Istio ALLOW is a union). For true per-service enforcement, you must either:
- Remove `allow-same-namespace` entirely, OR
- Keep it and accept that per-service policies only add JWT enforcement

### 3. Forgetting Gateway Traffic

The ASM gateway has its own ServiceAccount (typically `sa/asm-gateway` in the `asm-gateway` namespace). If you switch to specific SAs, remember to allow the gateway:

```yaml
principals:
  - "cluster.local/ns/uat/sa/rest-api"          # internal caller
  - "cluster.local/ns/asm-gateway/sa/asm-gateway" # gateway traffic
```

### 4. PeerAuthentication in PERMISSIVE Mode

PERMISSIVE mode accepts both mTLS and plaintext. An attacker without a sidecar can bypass all authorization policies because Istio cannot extract a source principal from plaintext connections. Always use **STRICT** in production.

---

## Quick Reference

| Concept | Resource | Field |
|---------|----------|-------|
| Pod identity | `ServiceAccount` | `metadata.name` |
| SPIFFE URI | Auto-generated | `spiffe://cluster.local/ns/<ns>/sa/<sa>` |
| mTLS enforcement | `PeerAuthentication` | `spec.mtls.mode: STRICT` |
| Internal allow-list | `AuthorizationPolicy` | `spec.rules[].from[].source.principals` |
| External allow-list | `AuthorizationPolicy` | `spec.rules[].from[].source.requestPrincipals` |
| JWT validation | `RequestAuthentication` | `spec.jwtRules[].issuer` |
| Target selection | `AuthorizationPolicy` | `spec.selector.matchLabels` |
