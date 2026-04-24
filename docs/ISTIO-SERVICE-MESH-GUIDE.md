# Istio Service Mesh on Google Distributed Cloud
## A Developer's Guide to Service-to-Service Communication with Anthos Service Mesh

---

## 1. Overview: What is Anthos Service Mesh?

Anthos Service Mesh (ASM) is Google's managed implementation of **Istio** on Google Distributed Cloud (GDC). It provides a **transparent infrastructure layer** that handles service-to-service communication without requiring any changes to your application code.

### Your Current Setup

| Component | Status |
|-----------|--------|
| Google Distributed Cloud | ✅ Deployed |
| Anthos Service Mesh | ✅ Enabled |
| Sidecar Injection | ✅ `istio-proxy` + `istio-init` per pod |
| CIDP (Cloud Identity) | ✅ Enabled |
| Auth Label | ✅ `auth-type: oidc` |
| Application Language | Python microservices |

---

## 2. Architecture Overview

![Full Microservices Architecture on GDC with ASM](images/full_architecture.png)

Every pod in your mesh has **3 containers**:

| Container | Purpose | Lifecycle |
|-----------|---------|-----------|
| **Your App** | Your Python microservice (Flask, FastAPI, etc.) | Runs continuously |
| **istio-proxy** (Envoy) | Sidecar proxy — intercepts ALL inbound/outbound traffic | Runs continuously alongside your app |
| **istio-init** | Init container — sets up `iptables` rules to redirect traffic to Envoy | Runs once at pod startup, then exits |

---

## 3. How Service-to-Service Communication Works

![Istio Service Mesh Communication Flow](images/mesh_communication_flow.png)

### The Key Insight: Your App Doesn't Know the Mesh Exists

When your **Order Service** calls the **Payment Service**, your Python code simply does:

```python
import requests

# Your app just makes a normal HTTP call — nothing special
response = requests.get("http://payment-service:8080/api/process")
```

Your app sends a **plain HTTP** request to a Kubernetes service name. It has **no idea** that:
- The request is being intercepted by Envoy
- mTLS certificates are being attached
- The traffic is encrypted on the wire
- Authorization policies are being evaluated
- Metrics and traces are being collected

---

## 4. The 6-Step Request Flow (What Actually Happens)

![Request Flow Through Istio Sidecar](images/request_flow_steps.png)

Here's what happens step-by-step when Service A calls Service B:

### Step 1: App Makes a Normal HTTP Request
```python
# In order-service (Python)
response = requests.post("http://payment-service:8080/api/charge", json={"amount": 99.99})
```
Your app sends a plain HTTP request to `payment-service:8080`. It uses standard Kubernetes DNS resolution.

### Step 2: istio-init iptables Rules Intercept the Traffic
The `istio-init` container (which ran at pod startup) configured `iptables` rules that redirect **all outbound TCP traffic** to the Envoy sidecar's port (15001). Your app's outbound request never goes directly to the network.

```bash
# What istio-init configured (you don't need to do this — it's automatic):
iptables -t nat -A OUTPUT -p tcp -j REDIRECT --to-ports 15001
```

### Step 3: Source Envoy Processes the Request
The Envoy sidecar in the **source pod** (Order Service):
- Looks up the destination service in its service registry
- Selects a healthy backend pod (load balancing)
- Applies any configured policies (retries, timeouts, circuit breaking)
- Attaches the **mTLS client certificate** (issued by istiod)
- Encrypts the request using TLS 1.3

### Step 4: Encrypted Traffic Over the Network
The request travels over the pod network as **fully encrypted mTLS traffic**. Even if someone captures packets on the network, they see only encrypted data. The identity is embedded in the SPIFFE certificate:
```
spiffe://cluster.local/ns/uat/sa/order-service
```

### Step 5: Destination Envoy Receives and Validates
The Envoy sidecar in the **destination pod** (Payment Service):
- Terminates the TLS connection
- Validates the source identity certificate
- Checks **AuthorizationPolicy** rules (is Order Service allowed to call Payment Service?)
- Checks **RequestAuthentication** if configured
- Records metrics (latency, status code, request size)

### Step 6: Request Delivered to the App
The destination Envoy forwards the decrypted request to your Payment Service app on `localhost:8080`. Your app receives a **plain HTTP request** — it never sees the mTLS certificates.

```python
# In payment-service (Python) — receives a normal request
@app.route('/api/charge', methods=['POST'])
def charge():
    data = request.json
    # Process payment — no mesh-related code needed
    return jsonify({"status": "charged", "amount": data["amount"]})
```

---

## 5. OIDC / CIDP Authentication Flow

![OIDC Authentication Flow with Istio and CIDP](images/oidc_auth_flow.png)

Your mesh has two distinct authentication flows:

### External Authentication (OIDC + CIDP)
For traffic **entering the mesh** from external clients:

1. Client sends request with `Authorization: Bearer <JWT>` header
2. **Istio Ingress Gateway** receives the request
3. **RequestAuthentication** CR validates the JWT against your OIDC/CIDP provider's JWKS endpoint
4. **AuthorizationPolicy** checks claims from the validated JWT (roles, groups, scopes)
5. If valid, request reaches your app pod

```yaml
# Example: RequestAuthentication for CIDP
apiVersion: security.istio.io/v1
kind: RequestAuthentication
metadata:
  name: cidp-auth
  namespace: uat
spec:
  selector:
    matchLabels:
      auth-type: oidc
  jwtRules:
  - issuer: "https://accounts.google.com"
    jwksUri: "https://www.googleapis.com/oauth2/v3/certs"
    forwardOriginalToken: true
```

### Internal Authentication (Automatic mTLS)
For traffic **between services inside the mesh**:

- **No JWT needed** — services authenticate each other via mTLS certificates
- Identity is derived from the Kubernetes ServiceAccount
- Certificate format: `spiffe://cluster.local/ns/{namespace}/sa/{service-account}`
- **Completely automatic** — zero code changes

> [!IMPORTANT]
> **For service-to-service calls within the mesh, your Python code does NOT need to handle any authentication.** The Envoy sidecars handle mTLS automatically. Your app just makes plain HTTP calls.

---

## 6. Do Developers Need to Change Application Code?

### Short Answer: NO for Core Mesh Features ✅

| Feature | Code Changes? | Details |
|---------|:---:|---------|
| mTLS encryption | ❌ None | Automatic via sidecar |
| Service discovery | ❌ None | Use K8s service names as before |
| Load balancing | ❌ None | Envoy handles it |
| Circuit breaking | ❌ None | Configured via `DestinationRule` CR |
| Retries & timeouts | ❌ None | Configured via `VirtualService` CR |
| Rate limiting | ❌ None | Configured via `EnvoyFilter` CR |
| Authorization | ❌ None | Configured via `AuthorizationPolicy` CR |
| Metrics collection | ❌ None | Envoy reports to Prometheus automatically |

### Optional: Trace Header Propagation ⚡

The **one thing** developers can optionally do is **propagate trace headers** for distributed tracing. Envoy generates trace headers, but they must be forwarded by your app to correlate traces across services.

```python
# OPTIONAL: Propagate tracing headers for distributed tracing
TRACE_HEADERS = [
    'x-request-id',
    'x-b3-traceid',
    'x-b3-spanid',
    'x-b3-parentspanid',
    'x-b3-sampled',
    'x-b3-flags',
    'x-ot-span-context',
    'traceparent',
    'tracestate',
]

@app.route('/api/orders', methods=['POST'])
def create_order():
    # Forward trace headers when calling another service
    headers = {h: request.headers.get(h) for h in TRACE_HEADERS if request.headers.get(h)}
    
    # Call payment service with propagated trace headers
    response = requests.post(
        "http://payment-service:8080/api/charge",
        json={"amount": 99.99},
        headers=headers  # ← This enables end-to-end tracing
    )
    return jsonify({"order_id": "ORD-001", "payment": response.json()})
```

> [!TIP]
> **This is optional but highly recommended.** Without header propagation, you'll see individual service traces but can't correlate them into an end-to-end request trace in Jaeger/Kiali.

### Python Helper: Trace Header Middleware

For Flask apps, you can create a simple middleware:

```python
from flask import Flask, request, g
import requests as req_lib

app = Flask(__name__)

TRACE_HEADERS = [
    'x-request-id', 'x-b3-traceid', 'x-b3-spanid',
    'x-b3-parentspanid', 'x-b3-sampled', 'x-b3-flags',
    'traceparent', 'tracestate',
]

@app.before_request
def capture_trace_headers():
    """Capture incoming trace headers for propagation."""
    g.trace_headers = {h: request.headers.get(h) for h in TRACE_HEADERS if request.headers.get(h)}

def mesh_call(method, url, **kwargs):
    """Make an HTTP call with automatic trace header propagation."""
    headers = kwargs.pop('headers', {})
    headers.update(getattr(g, 'trace_headers', {}))
    return req_lib.request(method, url, headers=headers, **kwargs)

# Usage:
@app.route('/api/process')
def process():
    # Automatically propagates trace headers
    result = mesh_call('GET', 'http://user-service:8080/api/profile')
    return jsonify(result.json())
```

---

## 7. Advantages for Development Teams

![Anthos Service Mesh Advantages](images/mesh_advantages.png)

### 🔒 Security — Zero-Trust by Default

| What You Get | Without Mesh | With Mesh |
|-------------|-------------|-----------|
| Encryption | You implement TLS in every service | ✅ Automatic mTLS everywhere |
| Identity | You manage API keys/tokens | ✅ SPIFFE identity per service |
| Authorization | You code auth checks in every service | ✅ Declarative `AuthorizationPolicy` |
| Cert management | You rotate certs manually | ✅ Auto-rotation every 24h |

**Example: Restrict which services can call your payment service:**

```yaml
apiVersion: security.istio.io/v1
kind: AuthorizationPolicy
metadata:
  name: payment-service-policy
  namespace: uat
spec:
  selector:
    matchLabels:
      app: payment-service
  action: ALLOW
  rules:
  - from:
    - source:
        principals:
        - "cluster.local/ns/uat/sa/order-service"
        - "cluster.local/ns/uat/sa/api-gateway"
    to:
    - operation:
        methods: ["POST"]
        paths: ["/api/charge", "/api/refund"]
```

### 🚦 Traffic Management — Canary & A/B Deployments

Deploy new versions gradually without code changes:

```yaml
# Send 90% of traffic to v1, 10% to v2 (canary)
apiVersion: networking.istio.io/v1
kind: VirtualService
metadata:
  name: payment-service
  namespace: uat
spec:
  hosts:
  - payment-service
  http:
  - route:
    - destination:
        host: payment-service
        subset: v1
      weight: 90
    - destination:
        host: payment-service
        subset: v2
      weight: 10
---
apiVersion: networking.istio.io/v1
kind: DestinationRule
metadata:
  name: payment-service
  namespace: uat
spec:
  host: payment-service
  subsets:
  - name: v1
    labels:
      version: v1
  - name: v2
    labels:
      version: v2
```

### ⚡ Resilience — Retries, Timeouts, Circuit Breaking

```yaml
# Automatic retries and timeouts
apiVersion: networking.istio.io/v1
kind: VirtualService
metadata:
  name: payment-service
  namespace: uat
spec:
  hosts:
  - payment-service
  http:
  - timeout: 10s
    retries:
      attempts: 3
      perTryTimeout: 3s
      retryOn: "5xx,reset,connect-failure"
    route:
    - destination:
        host: payment-service
---
# Circuit breaking
apiVersion: networking.istio.io/v1
kind: DestinationRule
metadata:
  name: payment-service
  namespace: uat
spec:
  host: payment-service
  trafficPolicy:
    connectionPool:
      tcp:
        maxConnections: 100
      http:
        h2UpgradePolicy: DEFAULT
        http1MaxPendingRequests: 100
        http2MaxRequests: 1000
    outlierDetection:
      consecutive5xxErrors: 5
      interval: 30s
      baseEjectionTime: 60s
      maxEjectionPercent: 50
```

### 📊 Observability — Metrics, Traces, and Logs (Free)

Without writing a single line of telemetry code, you get:

| Metric | Automatically Captured |
|--------|----------------------|
| Request rate (RPS) | ✅ Per service, per endpoint |
| Latency (P50, P95, P99) | ✅ Per service pair |
| Error rate (4xx, 5xx) | ✅ Per service, per endpoint |
| Connection count | ✅ TCP level |
| Request size / Response size | ✅ Per request |

Access via:
- **Kiali** → Service topology visualization
- **Grafana** → Pre-built Istio dashboards
- **Jaeger** → Distributed tracing (needs header propagation)
- **Prometheus** → Raw metrics queries

---

## 8. Enforcing mTLS Across the Namespace

To ensure ALL communication in your namespace is encrypted:

```yaml
# Enforce STRICT mTLS — reject any plain-text traffic
apiVersion: security.istio.io/v1
kind: PeerAuthentication
metadata:
  name: default
  namespace: uat
spec:
  mtls:
    mode: STRICT
```

Modes:

| Mode | Behavior |
|------|----------|
| `STRICT` | Only accept mTLS connections (recommended for production) |
| `PERMISSIVE` | Accept both mTLS and plain text (useful during migration) |
| `DISABLE` | Disable mTLS (not recommended) |

---

## 9. Common Scenarios for Your Dev Team

### Scenario 1: "I need to call another microservice"

**Answer:** Just call it. No changes needed.

```python
# This is ALL you need. The mesh handles everything else.
response = requests.get("http://user-service:8080/api/users/123")
```

### Scenario 2: "I need to restrict who can call my service"

**Answer:** Apply an `AuthorizationPolicy`. No code changes.

### Scenario 3: "I want to deploy a new version gradually"

**Answer:** Apply a `VirtualService` + `DestinationRule` for canary routing. No code changes.

### Scenario 4: "My downstream service is flaky"

**Answer:** Add retry/timeout/circuit-breaking via `VirtualService` + `DestinationRule`. No code changes.

### Scenario 5: "I need to see which services call my service"

**Answer:** Open Kiali dashboard — the service graph shows all traffic flows in real time.

### Scenario 6: "I need to debug a slow request across 5 services"

**Answer:** Add trace header propagation (the one optional code change), then use Jaeger to trace the full request path.

---

## 10. What NOT to Do (Anti-patterns)

| ❌ Don't Do This | ✅ Do This Instead |
|-----------------|-------------------|
| Implement TLS in your Python code | Let the mesh handle it via mTLS |
| Add auth middleware for internal service calls | Use `AuthorizationPolicy` CR |
| Build retry logic in every service | Configure retries in `VirtualService` |
| Add rate limiting code | Use `DestinationRule` or `EnvoyFilter` |
| Run your own Prometheus exporter for HTTP metrics | The mesh already exports them |
| Use IP-based access control | Use SPIFFE identity-based `AuthorizationPolicy` |

---

## 11. Quick Reference: Key Istio CRDs

| CRD | Purpose | Example Use |
|-----|---------|-------------|
| `PeerAuthentication` | Configure mTLS mode | Enforce STRICT mTLS |
| `RequestAuthentication` | Validate JWTs from external clients | OIDC/CIDP integration |
| `AuthorizationPolicy` | Control who can call what | Allow only Order Service → Payment Service |
| `VirtualService` | Route traffic, retries, timeouts | Canary deployments, A/B testing |
| `DestinationRule` | Circuit breaking, load balancing, subsets | Connection pooling, outlier detection |
| `Gateway` | Configure ingress/egress | Expose services externally |
| `ServiceEntry` | Register external services in the mesh | Call external APIs through the mesh |
| `EnvoyFilter` | Advanced Envoy configuration | Custom rate limiting, header manipulation |

---

## 12. How to Verify Service Mesh Traffic is Working

![Mesh Verification Checklist](images/mesh_verification_flow.png)

Follow these steps in order to confirm your service mesh is functioning correctly.

---

### Step 1: Verify Sidecar Injection

Every meshed pod should show **2/2** (or **3/3** during init) in the `READY` column:

```bash
kubectl get pods -n uat
```

**✅ Expected (mesh working):**
```
NAME                              READY   STATUS    RESTARTS
order-service-7b9d4f6c88-x2j4k   2/2     Running   0
payment-service-5c8f7d9b44-m8p2   2/2     Running   0
user-service-6d7e8f0a33-k5n1     2/2     Running   0
```

**❌ Problem (sidecar not injected):**
```
NAME                              READY   STATUS    RESTARTS
order-service-7b9d4f6c88-x2j4k   1/1     Running   0
```

**Fix:** Ensure the namespace has the injection label:
```bash
# Check label
kubectl get namespace uat --show-labels | grep istio

# If missing, add it
kubectl label namespace uat istio-injection=enabled

# Restart pods to pick up sidecar
kubectl rollout restart deployment -n uat
```

---

### Step 2: Verify Sidecar Containers Are Running

Inspect the containers inside a specific pod:

```bash
kubectl describe pod <pod-name> -n uat | grep -A 2 "Container ID"
```

Or list all containers:
```bash
kubectl get pod <pod-name> -n uat -o jsonpath='{.spec.containers[*].name}'
```

**✅ Expected:** `order-service istio-proxy`

Check init containers:
```bash
kubectl get pod <pod-name> -n uat -o jsonpath='{.spec.initContainers[*].name}'
```

**✅ Expected:** `istio-init` (or `istio-validation`)

---

### Step 3: Verify mTLS is Active

Check that mTLS is established between services:

```bash
# Check mTLS status for all services in the namespace
istioctl authn tls-check <pod-name>.uat -n uat
```

**✅ Expected output:**
```
HOST:PORT                                STATUS   SERVER        CLIENT       AUTHN POLICY     DESTINATION RULE
payment-service.uat.svc.cluster.local    OK       STRICT        ISTIO_MUTUAL default/uat      -
user-service.uat.svc.cluster.local       OK       STRICT        ISTIO_MUTUAL default/uat      -
order-service.uat.svc.cluster.local      OK       STRICT        ISTIO_MUTUAL default/uat      -
```

Key things to look for:
- **STATUS = OK** → mTLS handshake is working
- **SERVER = STRICT** → Only mTLS connections accepted (no plaintext)
- **CLIENT = ISTIO_MUTUAL** → Client is sending mTLS certificates

**Alternative:** Check PeerAuthentication policy:
```bash
kubectl get peerauthentication -n uat
```

**✅ Expected:**
```
NAME      MODE     AGE
default   STRICT   30d
```

---

### Step 4: Test Service-to-Service Connectivity

Exec into a pod and call another service to confirm mesh routing works:

```bash
# Exec into order-service pod
kubectl exec -it deploy/order-service -n uat -c order-service -- /bin/sh

# From inside the pod, call payment-service
curl -v http://payment-service:8080/health
```

**✅ Expected:** HTTP 200 response from the payment service.

**What to look for in verbose output (`-v`):**
```
* Connected to payment-service (10.96.45.123) port 8080
> GET /health HTTP/1.1
> Host: payment-service:8080
< HTTP/1.1 200 OK
< x-envoy-upstream-service-time: 3
< server: envoy
```

Key indicators the mesh is working:
- `server: envoy` header → response came through the Envoy sidecar
- `x-envoy-upstream-service-time` header → Envoy measured the upstream latency
- `x-request-id` header → Envoy generated a trace ID

> [!TIP]
> If you see `server: envoy` in the response headers, it confirms the traffic is going through the mesh sidecar, not directly to the app.

---

### Step 5: Check Envoy Proxy Sync Status

Verify that all sidecar proxies are synchronized with the control plane (istiod):

```bash
istioctl proxy-status -n uat
```

**✅ Expected (all SYNCED):**
```
NAME                                    CDS     LDS     EDS     RDS     ECDS    ISTIOD                    VERSION
order-service-7b9d4f6c88-x2j4k.uat     SYNCED  SYNCED  SYNCED  SYNCED  -       istiod-6f8c7d9b4-abc12    1.20.2
payment-service-5c8f7d9b44-m8p2.uat     SYNCED  SYNCED  SYNCED  SYNCED  -       istiod-6f8c7d9b4-abc12    1.20.2
user-service-6d7e8f0a33-k5n1.uat        SYNCED  SYNCED  SYNCED  SYNCED  -       istiod-6f8c7d9b4-abc12    1.20.2
```

**❌ Problem:** If any column shows `STALE` or `NOT SENT`:
```bash
# Force config re-sync by restarting the proxy
kubectl delete pod <pod-name> -n uat
```

What each column means:

| Column | Full Name | What It Does |
|--------|-----------|-------------|
| **CDS** | Cluster Discovery Service | Service endpoints the proxy knows about |
| **LDS** | Listener Discovery Service | Ports the proxy is listening on |
| **EDS** | Endpoint Discovery Service | Individual pod IPs for each service |
| **RDS** | Route Discovery Service | HTTP routing rules |

---

### Step 6: Inspect Envoy Proxy Logs

Check the sidecar logs for connection and request details:

```bash
# View istio-proxy logs for a specific pod
kubectl logs deploy/order-service -n uat -c istio-proxy --tail=50
```

**✅ Healthy log (successful mTLS request):**
```
[2026-04-24T12:30:15.123Z] "GET /api/users/123 HTTP/1.1" 200 - via_upstream -
"-" 0 234 12 11 "-" "python-requests/2.31.0" "abc123-trace-id"
"user-service.uat.svc.cluster.local:8080" "10.244.1.15:8080"
outbound|8080||user-service.uat.svc.cluster.local 10.244.2.8:54321
```

Key fields:
- `200` → HTTP status code
- `12` → Total request time (ms)
- `outbound|8080||user-service.uat.svc.cluster.local` → Envoy routed this through the mesh
- `via_upstream` → Request successfully proxied

**❌ Error log (connection refused):**
```
[2026-04-24T12:30:15.123Z] "GET /api/users/123 HTTP/1.1" 503 UF upstream_reset_before_response_started
```
- `503 UF` → Upstream failure, service unreachable

**Enable debug logging temporarily:**
```bash
# Increase Envoy log level for a specific pod
istioctl proxy-config log <pod-name>.uat --level debug

# Reset back to warning level
istioctl proxy-config log <pod-name>.uat --level warning
```

---

### Step 7: Verify Mesh Metrics in Prometheus

Check that Istio is generating traffic metrics:

```bash
# Port-forward to Prometheus
kubectl port-forward svc/prometheus -n istio-system 9090:9090
```

Then open `http://localhost:9090` and query:

```promql
# Total requests between services
istio_requests_total{
  reporter="source",
  source_workload="order-service",
  destination_service="payment-service.uat.svc.cluster.local"
}
```

**✅ Expected:** Non-zero counter values with labels showing source/destination.

Other useful queries:

```promql
# Request latency P95
histogram_quantile(0.95, sum(rate(istio_request_duration_milliseconds_bucket{
  destination_service="payment-service.uat.svc.cluster.local"
}[5m])) by (le))

# Error rate
sum(rate(istio_requests_total{
  response_code=~"5.*",
  destination_service="payment-service.uat.svc.cluster.local"
}[5m]))

# Active connections
istio_tcp_connections_opened_total{destination_service="payment-service.uat.svc.cluster.local"}
```

---

### Step 8: Verify with Kiali Service Graph (Visual)

If Kiali is installed:

```bash
# Port-forward to Kiali
kubectl port-forward svc/kiali -n istio-system 20001:20001
```

Open `http://localhost:20001` → Navigate to **Graph** → Select your namespace (`uat`).

**✅ What you should see:**
- Green edges between services → successful traffic
- Lock icons on edges → mTLS enabled
- Request rates on each edge → active traffic flow
- Service nodes with health indicators

**❌ What indicates problems:**
- Red edges → 5xx errors
- Missing edges → no traffic flowing
- No lock icon → mTLS not active on that path

---

### Quick Verification Checklist

Run this all-in-one check script from your terminal:

```bash
#!/bin/bash
NAMESPACE="uat"

echo "═══ 1. Sidecar Injection ═══"
kubectl get pods -n $NAMESPACE -o custom-columns='NAME:.metadata.name,READY:.status.containerStatuses[*].ready,CONTAINERS:.spec.containers[*].name' | head -20

echo ""
echo "═══ 2. PeerAuthentication (mTLS) ═══"
kubectl get peerauthentication -n $NAMESPACE -o wide

echo ""
echo "═══ 3. AuthorizationPolicy ═══"
kubectl get authorizationpolicy -n $NAMESPACE -o wide

echo ""
echo "═══ 4. Proxy Sync Status ═══"
istioctl proxy-status -n $NAMESPACE 2>/dev/null | head -15

echo ""
echo "═══ 5. VirtualServices & DestinationRules ═══"
echo "VirtualServices:"
kubectl get virtualservice -n $NAMESPACE 2>/dev/null || echo "  None"
echo "DestinationRules:"
kubectl get destinationrule -n $NAMESPACE 2>/dev/null || echo "  None"

echo ""
echo "═══ 6. Service-to-Service Test ═══"
SOURCE_POD=$(kubectl get pod -n $NAMESPACE -l app=order-service -o jsonpath='{.items[0].metadata.name}' 2>/dev/null)
if [ -n "$SOURCE_POD" ]; then
    echo "Testing from $SOURCE_POD → payment-service..."
    kubectl exec $SOURCE_POD -n $NAMESPACE -c order-service -- \
        curl -s -o /dev/null -w "HTTP %{http_code} | Time: %{time_total}s | Server: %{header.server}\n" \
        http://payment-service:8080/health 2>/dev/null || echo "  Could not reach payment-service"
else
    echo "  No order-service pod found to test from"
fi

echo ""
echo "═══ 7. Recent Proxy Errors ═══"
SOURCE_POD=$(kubectl get pod -n $NAMESPACE -o jsonpath='{.items[0].metadata.name}' 2>/dev/null)
if [ -n "$SOURCE_POD" ]; then
    kubectl logs $SOURCE_POD -n $NAMESPACE -c istio-proxy --tail=10 2>/dev/null | grep -E "(503|404|connection refused|upstream)" || echo "  No recent errors"
fi
```

---

### Common Issues & Fixes

| Symptom | Cause | Fix |
|---------|-------|-----|
| Pod shows `1/1` instead of `2/2` | Sidecar not injected | Add `istio-injection=enabled` label to namespace, restart pods |
| `503 UF` in proxy logs | Destination service not available | Check if destination pod is running and healthy |
| `503 UC` in proxy logs | Upstream connection failure | Check network policies, service port mismatch |
| `RBAC: access denied` in logs | `AuthorizationPolicy` blocking | Check policy rules, verify source identity |
| `STALE` in `proxy-status` | Envoy config out of sync | Delete and recreate the pod |
| `connection refused` on port 15012 | istiod is down | Check `kubectl get pods -n istio-system` |
| mTLS shows `PERMISSIVE` not `STRICT` | PeerAuthentication not set | Apply `PeerAuthentication` with `mode: STRICT` |
| Trace headers missing | App not propagating headers | Add the trace header propagation code from Section 6 |

---

## 13. Summary

> [!IMPORTANT]
> **The #1 takeaway for your development team:** The service mesh is an infrastructure concern, not an application concern. Developers should continue writing normal Python HTTP services. The mesh handles security, reliability, and observability transparently.

### What the Mesh Gives You for Free (No Code Changes):
- ✅ Encrypted service-to-service communication (mTLS)
- ✅ Automatic identity and certificate management
- ✅ Request-level metrics and monitoring
- ✅ Traffic control (canary, A/B, blue/green)
- ✅ Resilience patterns (retries, timeouts, circuit breaking)
- ✅ Access control policies (who can call what)

### The One Optional Enhancement:
- ⚡ Propagate trace headers for end-to-end distributed tracing

---

*Document prepared for internal distribution to development teams working with Istio/Anthos Service Mesh on GDC.*

