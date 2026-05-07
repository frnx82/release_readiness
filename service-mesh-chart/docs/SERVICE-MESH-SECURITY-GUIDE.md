# Service Mesh Security — A Simple Guide

> **Audience:** Dev Leads & Team  
> **Purpose:** Explain our 4 security principles, what each config file does, and how we verify everything  
> **Time to present:** ~15 minutes

---

## What Is This?

We use **Istio service mesh** to secure our Kubernetes services. Think of it as adding **4 invisible security layers** around every service — without changing any application code.

**Before the mesh:** Any pod in the cluster could talk to any other pod. No encryption. No identity checks.

**After the mesh:** Every service has its own identity, all traffic is encrypted, and only authorized callers are allowed in.

![Service Mesh Overview](docs/mesh-overview-diagram.png)

---

## The 4 Security Principles

### 🛡️ Principle 1: Tenant Isolation — "Lock the Front Door"

**In plain English:** No one from outside our namespace can talk to our services. Period.

**How it works — two YAML files working together:**

| File | What It Does | Analogy |
|------|-------------|---------|
| `deny-all-default.yaml` | Blocks ALL incoming traffic to every pod | Closing all the doors |
| `allow-same-namespace.yaml` | Opens the door ONLY for services within our namespace | Giving keys only to our team |

**The rule:** `deny-all-default` has an **empty spec `{}`** — in Istio, this means "deny everything." Then `allow-same-namespace` says "allow traffic from `cluster.local/ns/OUR-NAMESPACE/sa/*`" — which is any service account in our namespace.

> ⚠️ **Critical:** If you deploy `deny-all-default` WITHOUT `allow-same-namespace`, you will block ALL traffic — including services talking to each other within the namespace. Always deploy them together.

**What gets blocked:**
- ❌ Pod from `other-team-ns` trying to call our `billing-service`
- ❌ Random external traffic hitting our pods
- ✅ Our `payment-gateway` calling our `billing-service` — allowed

---

### 🔒 Principle 2: mTLS STRICT — "Encrypt Everything"

**In plain English:** Every conversation between services is encrypted. Both sides prove their identity with certificates. No plaintext HTTP allowed.

**How it works — one YAML file:**

| File | What It Does | Analogy |
|------|-------------|---------|
| `peer-authentication.yaml` | Sets `mode: STRICT` — rejects any non-mTLS connection | Speaking only in secret code |

**What happens behind the scenes:**
1. Istio gives each pod a **SPIFFE certificate** (like an ID badge): `spiffe://cluster.local/ns/uat/sa/billing-service`
2. When `billing-service` calls `payment-gateway`, both exchange certificates
3. The connection is encrypted with TLS — nobody can sniff the traffic
4. If someone sends plain HTTP (no certificate), the connection is **rejected**

**STRICT vs PERMISSIVE:**
- `STRICT` = Only mTLS accepted. Plain HTTP rejected. ✅ **This is what we use**
- `PERMISSIVE` = Both mTLS and plain HTTP accepted. ⚠️ Only for migration, not secure

---

### 🔑 Principle 3: JWT Validation — "Check the Badge at the Gate"

**In plain English:** When an external user hits our API, they must present a valid JWT token from our OIDC provider (CIDP). No token = no entry.

**How it works — one YAML file:**

| File | What It Does | Analogy |
|------|-------------|---------|
| `req-auth.yaml` | Tells Istio which JWT issuer to trust and where to find the signing keys (JWKS) | Security guard checking employee badges |

**Key settings:**
- `issuer`: The OIDC provider URL (our CIDP)
- `jwksUri`: Where Istio downloads the public keys to verify JWT signatures
- `forwardOriginalToken: true`: Pass the token through to the app (so it can read user email, roles, etc.)
- `selector: auth-type: oidc`: Only applies to services labeled with `auth-type: oidc`

**Important distinction:**
- **External users** → Must have a JWT token (checked by this policy)
- **Internal services** → Use mTLS identity instead (Principle 2) — no JWT needed for service-to-service calls

---

### 📋 Principle 4: Service-to-Service Allow-Lists — "Specific Guest List"

**In plain English:** Even within our namespace, each service has an explicit list of who can call it. Defense-in-depth.

**How it works — two YAML files:**

| File | What It Does | Analogy |
|------|-------------|---------|
| `authorization.yaml` | Controls who can access services labeled `auth-type: oidc` via host matching and namespace-scoped principals | VIP list per room |
| `authorization-authz.yaml` | Two rules: (1) External users with valid JWT, (2) Internal services from same namespace | Two types of approved guests |

**The two rules in `authorization-authz.yaml`:**

```
Rule 1 — External user with valid JWT:
  from:
    source:
      requestPrincipals: "https://cidp.example.com/oauth2/*"
  → User has a valid CIDP token ✅

Rule 2 — Internal service from same namespace:
  from:
    source:
      principals: "cluster.local/ns/uat/sa/*"
  → Service is in our namespace with a valid mTLS cert ✅
```

---

## CIDP Integration — How Authentication Works End-to-End

**CIDP** (Cloud Identity Provider) is our organization's OAuth2/OIDC identity server. It issues JWT tokens that prove who a user or service is. Here's how it connects to the service mesh.

### What Is CIDP?

**CIDP (Cloud Identity Provider)** is our organization's OAuth2/OIDC identity server. It issues JWT tokens that prove who a user or service is.

**Simple analogy:** Think of CIDP like a badge office — users authenticate with CIDP, receive a JWT token (their digital badge), and present that badge to access our services. The mesh checks the badge at the door.

### The Authentication Flow — Step by Step

```
Step 1: User logs in
┌──────────┐                    ┌──────────────┐
│  User /  │ ── credentials ──→ │    CIDP       │
│  Client  │ ←── JWT token ──── │  (OAuth2)     │
└──────────┘                    └──────────────┘
                Token contains:
                • iss: https://cidp.example.gdc.corp/oauth2
                • sub: user@company.com
                • exp: 1716312000 (expiry time)
                • scopes: [api.read, api.write]

Step 2: User makes API call with token
┌──────────┐                    ┌──────────────┐
│  User    │ ── Bearer token ─→ │ ASM Gateway  │
└──────────┘                    └──────┬───────┘
                                       │
Step 3: Istio validates the token       ▼
                                ┌──────────────┐
                                │ req-auth.yaml│ ← RequestAuthentication
                                │              │
                                │ 1. Is issuer  │
                                │    = CIDP? ✅ │
                                │ 2. Download   │
                                │    JWKS keys  │
                                │ 3. Verify     │
                                │    signature ✅│
                                │ 4. Check      │
                                │    expiry   ✅ │
                                └──────┬───────┘
                                       │
Step 4: Authorization check             ▼
                                ┌──────────────┐
                                │ authz.yaml   │ ← AuthorizationPolicy
                                │              │
                                │ requestPrin- │
                                │ cipals match │
                                │ CIDP issuer? │
                                │          ✅  │
                                └──────┬───────┘
                                       │
Step 5: Request reaches service         ▼
                                ┌──────────────┐
                                │ billing-     │
                                │ service      │
                                │              │
                                │ Gets headers:│
                                │ Remote-User: │
                                │ user@co.com  │
                                └──────────────┘
```

### How CIDP Connects to Principles 3 and 4

| Principle | CIDP Role | Config File |
|-----------|----------|-------------|
| **P3: JWT Validation** | Istio trusts CIDP as the JWT issuer, downloads CIDP's public keys (JWKS) to verify token signatures | `req-auth.yaml` |
| **P4: Allow-Lists** | `requestPrincipals` rule checks that the JWT issuer matches our CIDP URL — only tokens from OUR CIDP are accepted | `authorization-authz.yaml` |

### The Three Config Points for CIDP

**1. `values.yaml` — Set the CIDP URL (one place, used everywhere)**
```yaml
virtualservice:
  cidp_oauth2_base_url: "https://cidp.example.gdc.corp/oauth2"
```

**2. `req-auth.yaml` — Tell Istio to trust CIDP tokens**
```yaml
spec:
  jwtRules:
    - issuer: https://cidp.example.gdc.corp/oauth2         # Trust tokens from CIDP
      jwksUri: https://cidp.example.gdc.corp/oauth2/connect/jwk_uri  # Download signing keys
      forwardOriginalToken: true                            # Pass token to the app
      outputClaimToHeaders:
        - header: Remote-User      # Extract email from token
          claim: email             # and put it in this header
```

**3. `authorization-authz.yaml` — Only allow requests with valid CIDP tokens**
```yaml
rules:
  - from:
      - source:
          requestPrincipals:
            - "https://cidp.example.gdc.corp/oauth2/*"   # Must be from our CIDP
```

### What the Application Receives

After Istio validates the CIDP token, your application gets:

| Header | Value | How It Got There |
|--------|-------|-----------------|
| `Authorization` | `Bearer eyJhbG...` | Original token forwarded (`forwardOriginalToken: true`) |
| `Remote-User` | `user@company.com` | Extracted from JWT `email` claim by `outputClaimToHeaders` |

Your app **never needs to validate the JWT itself** — Istio already did it. The app can just read `Remote-User` to know who's calling.

### For External Teams Connecting to Our APIs

External teams that need to call our services must:

1. **Get CIDP credentials** — Request a Client ID + Secret from the IAM team
2. **Call CIDP token endpoint** — Use OAuth2 Client Credentials Grant:
   ```bash
   curl -X POST https://cidp.example.gdc.corp/oauth2/connect/token \
     -d "client_id=THEIR_CLIENT_ID" \
     -d "client_secret=THEIR_SECRET" \
     -d "grant_type=client_credentials" \
     -d "scope=api.read"
   ```
3. **Include token in API calls** — Add `Authorization: Bearer <token>` header
4. **Handle token refresh** — Tokens expire; refresh before expiry

### Internal Service-to-Service Calls — No CIDP Needed

> **Important:** Services calling other services within the mesh do **NOT** use CIDP tokens. They use **mTLS identity** (Principle 2) instead.

| Caller | Authentication Method | Why |
|--------|---------------------|-----|
| External user / browser | CIDP JWT token | User identity via OIDC |
| External team / API client | CIDP JWT token (client credentials) | Service identity via OAuth2 |
| Internal service (same namespace) | mTLS (SPIFFE certificate) | Automatic, no tokens needed |

This is by design — internal services don't need to manage tokens. Istio handles identity via mTLS certificates automatically.

---

## How Traffic Flows

![Traffic Flow Diagram](docs/traffic-flow-diagram.png)

### ✅ Legitimate Request (Left Side)
1. External user sends request with JWT token → hits ASM Gateway
2. `RequestAuthentication` validates the JWT signature and issuer
3. Traffic enters namespace via mTLS → reaches `billing-service`
4. `billing-service` calls `payment-gateway` internally via mTLS (no JWT needed)

### ❌ Blocked Attacks (Right Side)
- **Attack A:** Hacker without credentials → blocked by `deny-all-default` → 403
- **Attack B:** Pod from another namespace → blocked by tenant isolation → Connection Reset
- **Attack C:** Pod without Istio sidecar → blocked by mTLS STRICT → Connection Reset

---

## The Config Files at a Glance

| File | Principle | What It Does | Risk Level |
|------|-----------|-------------|------------|
| `deny-all-default.yaml` | P1 | Blocks all external traffic | 🟢 Low |
| `allow-same-namespace.yaml` | P1 | Allows same-namespace traffic | 🟢 Low |
| `peer-authentication.yaml` | P2 | Enforces mTLS STRICT | 🟡 Medium |
| `req-auth.yaml` | P3 | Validates JWT tokens from CIDP | 🟢 Low |
| `authorization.yaml` | P4 | Host-based + namespace-scoped access | 🟡 Medium |
| `authorization-authz.yaml` | P4 | JWT principals + SA allow rules | 🟡 Medium |
| `values.yaml` | All | Toggle each principle on/off | N/A |

### Toggling Principles (in `values.yaml`)

```yaml
tenantIsolation:
  enabled: true          # Principle 1 on/off

peerAuthentication:
  enabled: true          # Principle 2 on/off
  mtlsMode: STRICT       # STRICT or PERMISSIVE

requestAuthentication:
  enabled: true          # Principle 3 on/off

serviceAllowLists:
  enabled: true          # Principle 4 on/off
```

---

## How We Verify Everything — The Test Script

We have a single script (`test-mesh-policies.sh`) that validates **all 4 principles** are working correctly. Run it after any deployment.

```bash
./test-mesh-policies.sh <namespace> [pod-name]
# Example:
./test-mesh-policies.sh uat
```

![Test Coverage Diagram](docs/test-coverage-diagram.png)

### What the Script Checks — Step by Step

#### Phase 0: Pre-Requisites
> "Is the mesh even running?"

| Check | What It Verifies | If It Fails |
|-------|-----------------|-------------|
| Namespace has `istio-injection=enabled` | New pods get automatic sidecar injection | No policies will work without sidecars |
| All pods show `2/2 READY` | Sidecar is running alongside the app | Traffic bypasses the proxy |
| `istio-proxy` container present | Envoy is intercepting traffic | No encryption, no auth |

#### Phase 1 Checks: Tenant Isolation
> "Is the front door locked?"

| Check | What It Verifies | If It Fails |
|-------|-----------------|-------------|
| `deny-all-default` policy exists | External traffic is blocked by default | Anyone can call your services |
| `allow-same-namespace` policy exists | Internal traffic still works | Everything is broken (all blocked) |
| Same-namespace HTTP call succeeds | Services can still talk to each other | Policy is too restrictive |
| Envoy RBAC denials in logs | External attacks are being blocked | N/A (informational) |

#### Phase 2 Checks: mTLS
> "Is everything encrypted?"

| Check | What It Verifies | If It Fails |
|-------|-----------------|-------------|
| PeerAuthentication mode = `STRICT` | Plain HTTP is rejected | Traffic is unencrypted |
| SPIFFE identity certificate loaded | Pod has a unique identity | Can't verify who's calling |
| TLS handshake count > 0 | Encryption is actually happening | mTLS not active |
| `server: envoy` header present | Traffic flows through the proxy | Proxy is bypassed |

#### Phase 3 Checks: JWT Validation
> "Are we checking badges at the door?"

| Check | What It Verifies | If It Fails |
|-------|-----------------|-------------|
| RequestAuthentication policy exists | JWT rules are deployed | No token validation |
| JWT issuer configured | We know which OIDC provider to trust | Tokens from anyone accepted |
| JWKS URI reachable | Istio can download signing keys | Can't verify token signatures |
| No-token request rejected | Unauthenticated users are blocked | Anyone can access the API |

#### Phase 4 Checks: Allow-Lists
> "Does each service have a guest list?"

| Check | What It Verifies | If It Fails |
|-------|-----------------|-------------|
| Per-service AuthorizationPolicies exist | Beyond deny-all, specific rules per service | Only namespace-level isolation |
| JWT requestPrincipals rule configured | External users checked per-service | No per-service JWT control |
| ServiceAccount principals rule configured | Internal calls checked per-service | No per-service mTLS control |
| Dedicated ServiceAccounts per service | Each service has a unique identity | All pods share same identity |

#### Phase 4B: ServiceAccount Identity Audit
> "Does each service have its own ID badge?"

| Check | What It Verifies | If It Fails |
|-------|-----------------|-------------|
| Pods don't use `default` ServiceAccount | Unique SPIFFE identity per service | Can't distinguish services in allow-lists |
| ALLOW policies have `from` constraints | Rules aren't wide-open | Any source can access ALLOW'd services |

#### Phase 5: Penetration Tests 🔴
> "Can we actually hack ourselves?"

The script deploys an **attacker pod** in a separate namespace (no Istio sidecar) and runs 7 real attack simulations:

| Attack | What It Simulates | Expected Result | Principle Tested |
|--------|------------------|----------------|-----------------|
| 1. Cross-NS call via Service DNS | Pod from another namespace calls your service | **BLOCKED** (403 or connection refused) | P1: Tenant Isolation |
| 2. Gateway call without JWT | External request without authentication | **BLOCKED** (401/403) | P3: JWT Validation |
| 3. Fake/invalid JWT token | Forged token with wrong issuer + fake signature | **REJECTED** (401) | P3: JWT Validation |
| 4. Plaintext HTTP to Pod IP | Bypass service DNS, hit pod directly with plain HTTP | **BLOCKED** (connection reset) | P2: mTLS STRICT |
| 5. Spoofed Host header | Wrong `Host: evil.example.com` | **BLOCKED** (403/404) | VirtualService |
| 6. Non-mesh pod access | Pod without Istio sidecar sends plaintext | **BLOCKED** (connection reset) | P2: mTLS STRICT |
| 7. Rogue ServiceAccount | Pod INSIDE your namespace with unknown SA | **BLOCKED** (403) if per-service lists active | P4: Allow-Lists |

> **Attack 7 is the most interesting** — it tests whether a compromised pod in your OWN namespace can reach other services. If you use `sa/*` wildcard, it gets through (Principle 1 works, but Principle 4 doesn't). If you use specific SA names, it's blocked.

---

## Result Meanings

```
✅ PASS    — Security control is verified and working
❌ FAIL    — Critical! Security control is missing or broken
⚠️ WARN    — Could not verify, or informational finding
ℹ️ INFO    — Helpful context, manual check suggestions
```

### Example Output

```
Service Mesh Security Verification
Namespace:  uat
Test Pod:   billing-service-7d8f9b6c4-abc12

══════════════════════════════════════════════════════
  Phase 0: Pre-Requisites — Sidecar Injection
══════════════════════════════════════════════════════
  ✅ PASS: Namespace 'uat' has istio-injection=enabled
  ✅ PASS: All pods show 2/2+ READY (sidecar present)

══════════════════════════════════════════════════════
  Principle 1: Tenant Isolation
══════════════════════════════════════════════════════
  ✅ PASS: deny-all-default policy exists
  ✅ PASS: allow-same-namespace policy exists
  ✅ PASS: Same-namespace call returned HTTP 200

══════════════════════════════════════════════════════
  Final Score
══════════════════════════════════════════════════════
  ✅ Passed:  18
  ❌ Failed:  0
  ⚠️ Warnings: 2
  📊 Total:   20 checks

  🎉 All critical checks passed! Mesh security is configured correctly.
```

---

## Quick Reference — When Things Go Wrong

| Symptom | Likely Cause | Fix |
|---------|-------------|-----|
| All internal calls return 403 | `deny-all-default` deployed without `allow-same-namespace` | Deploy `allow-same-namespace.yaml` immediately |
| Pods show 1/1 instead of 2/2 | Sidecar not injected | `kubectl label namespace <ns> istio-injection=enabled` then restart pods |
| External API calls return 401 | JWT token expired or wrong issuer | Check OIDC provider URL in `req-auth.yaml` |
| Service-to-service calls fail after mTLS STRICT | Some pods missing sidecar | Check `kubectl get pods -n <ns> | grep -v 2/2` |
| Cross-namespace call succeeds | `deny-all-default` missing | Deploy Principle 1 configs |

---

## Summary for Dev Leads

1. **We don't change application code** — all security is at the infrastructure level (Istio/Envoy)
2. **4 layers of defense** — even if one layer is bypassed, the others still protect
3. **One script verifies everything** — run `test-mesh-policies.sh` after every deployment
4. **Each principle can be toggled** — use `values.yaml` to enable/disable during migration
5. **Penetration tests are automated** — 7 attack simulations prove the policies actually work
