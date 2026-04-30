# External Team Connection Guide — Service Mesh with CIDP Auth

## How Your Mesh Security Works (for external callers)

Your service mesh has 4 layers of security. An external team's request must pass through all of them:

```
External Team                          Your Namespace (demo-dev)
─────────────                          ────────────────────────

1. Get JWT token from CIDP ──────┐
   (client_id + client_secret)   │
                                 │
2. Call your API with token ─────┤
   Authorization: Bearer <JWT>   │
                                 ▼
┌──── Istio Gateway (ASM) ───────────────────────────────────┐
│                                                             │
│  Layer 1: VirtualService                                    │
│    Routes /api/v1/* → rest-api service                      │
│    Routes /api/v2/* → rest-api-v2 service                   │
│    Routes /api/insights/* → executive-insights-api          │
│                                                             │
│  Layer 2: RequestAuthentication (req-auth.yaml)             │
│    ✓ Validates JWT signature via JWKS URI                   │
│    ✓ Checks issuer = cidp.example.gdc.corp/oauth2           │
│    ✓ Forwards original token to backend                     │
│    ✓ Extracts email → Remote-User header                    │
│                                                             │
│  Layer 3: AuthorizationPolicy (authorization-authz.yaml)    │
│    ✓ Checks requestPrincipals matches CIDP issuer           │
│    ✓ Only allows traffic to the configured host             │
│                                                             │
│  Layer 4: mTLS (PeerAuthentication)                         │
│    ✓ Gateway-to-service traffic encrypted via mTLS          │
│                                                             │
│  ✅ Request reaches your service                             │
└─────────────────────────────────────────────────────────────┘
```

---

## Steps for the External Team

### Step 1: Get CIDP Client Credentials

The external team needs a **CIDP client registration** with:

| Item | What they need | Who provides it |
|------|---------------|-----------------|
| **Client ID** | Their CIDP client identifier | CIDP admin / IAM team |
| **Client Secret** | Their CIDP client secret | CIDP admin / IAM team |
| **Token Endpoint** | `https://cidp.example.gdc.corp/oauth2/connect/token` | You (from values.yaml) |
| **Scope** | The required scope for your APIs (e.g., `api.read`) | You / CIDP admin |
| **Audience** | Your service's audience identifier (if required) | You |

### Step 2: Acquire a JWT Token (Client Credentials Flow)

The external team calls the CIDP token endpoint using **OAuth2 Client Credentials Grant**:

```bash
# Token request
curl -X POST https://cidp.example.gdc.corp/oauth2/connect/token \
  -H "Content-Type: application/x-www-form-urlencoded" \
  -d "grant_type=client_credentials" \
  -d "client_id=THEIR_CLIENT_ID" \
  -d "client_secret=THEIR_CLIENT_SECRET" \
  -d "scope=api.read"
```

**Response:**
```json
{
  "access_token": "eyJhbGciOiJSUzI1NiIsInR5cCI6IkpXVCJ9...",
  "token_type": "Bearer",
  "expires_in": 3600
}
```

### Step 3: Call Your API with the Token

The external team includes the JWT in the `Authorization` header:

```bash
# Call your REST API v1
curl -X GET https://myapp.example.gdc.corp/api/v1/data \
  -H "Authorization: Bearer eyJhbGciOiJSUzI1NiIsInR5cCI6IkpXVCJ9..."

# Call your REST API v2
curl -X GET https://myapp.example.gdc.corp/api/v2/data \
  -H "Authorization: Bearer eyJhbGciOiJSUzI1NiIsInR5cCI6IkpXVCJ9..."

# Call Executive Insights API
curl -X GET https://myapp.example.gdc.corp/api/insights/summary \
  -H "Authorization: Bearer eyJhbGciOiJSUzI1NiIsInR5cCI6IkpXVCJ9..."
```

---

## What Happens Inside the Mesh (Request Flow)

```
External Team's Request
    │
    ▼
① Istio Gateway (ASM)
    │  Accepts HTTPS traffic on myapp.example.gdc.corp
    │
    ▼
② VirtualService
    │  Routes /api/v1/* → rest-api:8080
    │  Routes /api/v2/* → rest-api-v2:8080
    │  etc.
    │
    ▼
③ RequestAuthentication (req-auth.yaml)
    │  - Extracts JWT from Authorization header
    │  - Downloads JWKS keys from cidp.example.gdc.corp/oauth2/connect/jwk_uri
    │  - Validates: signature ✓, expiry ✓, issuer ✓
    │  - Extracts email claim → sets Remote-User header
    │  - Forwards original token to backend (forwardOriginalToken: true)
    │
    │  ❌ If invalid/expired → 401 Unauthorized
    │
    ▼
④ AuthorizationPolicy (authorization-authz.yaml)
    │  - Checks requestPrincipals: "cidp.example.gdc.corp/oauth2/*"
    │  - This matches any valid JWT from your CIDP provider
    │
    │  ❌ If no valid principal → 403 Forbidden
    │
    ▼
⑤ mTLS (automatic)
    │  Gateway → Service encrypted via mutual TLS
    │
    ▼
⑥ Your Service receives the request with:
    - Original JWT token in Authorization header
    - Remote-User header set to the email from the JWT
    - mTLS-verified connection from the gateway
```

---

## Available API Endpoints

Based on your `values.yaml`, the external team can access:

| Endpoint | Backend Service | Port |
|----------|----------------|------|
| `https://myapp.example.gdc.corp/api/v1/*` | rest-api | 8080 |
| `https://myapp.example.gdc.corp/api/v2/*` | rest-api-v2 | 8080 |
| `https://myapp.example.gdc.corp/api/insights/*` | executive-insights-api | 8080 |
| `https://myapp.example.gdc.corp/api/reports/*` | reporting-service | 8080 |
| `https://myapp.example.gdc.corp/api/notifications/*` | notification-service | 8080 |

---

## Troubleshooting Common Errors

### 401 Unauthorized
**Meaning**: JWT token is invalid, expired, or missing.

| Check | Fix |
|-------|-----|
| Token expired? | Tokens expire after `expires_in` seconds. Request a new one. |
| Wrong issuer? | Token must be from `cidp.example.gdc.corp/oauth2` |
| Missing header? | Must include `Authorization: Bearer <token>` |
| JWKS unreachable? | Istio can't download signing keys. Check ServiceEntry for egress. |

```bash
# Debug: Decode the JWT to check issuer and expiry
echo "<token>" | cut -d'.' -f2 | base64 -d 2>/dev/null | python3 -m json.tool
```

### 403 Forbidden
**Meaning**: JWT is valid but the AuthorizationPolicy rejected the request.

| Check | Fix |
|-------|-----|
| Wrong audience? | The `aud` claim may not match what your policy expects |
| Wrong host? | Request must be to `myapp.example.gdc.corp` (from values.yaml) |
| deny-all blocking? | Check allow-after-authz policy exists |
| Different CIDP instance? | Token issuer must exactly match `cidp_oauth2_base_url` |

### 404 Not Found
**Meaning**: The URL path doesn't match any VirtualService route.

| Check | Fix |
|-------|-----|
| Wrong path prefix? | Must start with `/api/v1/`, `/api/v2/`, `/api/insights/`, etc. |
| Missing trailing slash? | Routes use `prefix: /api/v1/` — include the trailing slash |

### 503 Service Unavailable
**Meaning**: The backend service is down or has no healthy pods.

---

## What You Need to Provide the External Team

Send them this checklist:

```
📋 External Team Integration Checklist
─────────────────────────────────────────

1. Token Endpoint:  https://cidp.example.gdc.corp/oauth2/connect/token
2. Grant Type:      client_credentials
3. Base URL:        https://myapp.example.gdc.corp
4. Available APIs:
   - /api/v1/*          (REST API v1)
   - /api/v2/*          (REST API v2)
   - /api/insights/*    (Executive Insights)
   - /api/reports/*     (Reporting)
   - /api/notifications/*  (Notifications)
5. Required Header: Authorization: Bearer <JWT_TOKEN>
6. Token Lifetime:  Check expires_in field (typically 3600s / 1 hour)
7. Refresh:         Request a new token before expiry
                    (no refresh_token in client_credentials flow)
```
