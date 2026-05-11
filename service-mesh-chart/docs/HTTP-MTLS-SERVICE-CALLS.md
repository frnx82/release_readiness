# Service Mesh — HTTP & mTLS: How Service-to-Service Calls Work

## The Question

> "I understand HTTP calls will fail with service-to-service calls — is that true?"

## The Answer

**No — HTTP service-to-service calls within the same namespace will still work.** The Istio sidecar (Envoy proxy) transparently upgrades them to mTLS. Your application code does **not** need to change.

---

## How It Works

When your app makes a plain HTTP call to another service, the Istio sidecar intercepts it and handles mTLS automatically:

```
Pod A (your app)                         Pod B (target service)
┌──────────────────────┐                 ┌──────────────────────┐
│  App Container       │                 │  App Container       │
│  curl http://svc-b   │                 │  Receives plain HTTP │
│       │               │                │       ▲               │
│       ▼               │                │       │               │
│  istio-proxy sidecar │  ══ mTLS ═══►  │  istio-proxy sidecar │
│  (Envoy)             │  encrypted     │  (Envoy)             │
└──────────────────────┘  over network  └──────────────────────┘
```

1. **App sends plain HTTP** → goes to `localhost` sidecar (never leaves the pod unencrypted)
2. **Sidecar encrypts** → sends mTLS over the network using SPIFFE certificates
3. **Target sidecar decrypts** → delivers plain HTTP to the target app container

Your application code keeps using `http://service-name:port` — **no `https://` needed**.

---

## What WILL and WON'T Work

| Scenario | Result | Why |
|---|---|---|
| **Same namespace, both have sidecars** | ✅ Works | Sidecar upgrades HTTP → mTLS transparently |
| **Same namespace, app uses `http://`** | ✅ Works | App talks to local sidecar, sidecar handles encryption |
| **Pod WITHOUT sidecar → your service** | ❌ Blocked | mTLS STRICT rejects plaintext connections from outside the mesh |
| **Different namespace → your service** | ❌ Blocked | `deny-all-default` AuthorizationPolicy blocks cross-namespace traffic |
| **External request without JWT** | ❌ Blocked | `authorization-authz` policy requires valid OIDC token |
| **Rogue ServiceAccount in same namespace** | ❌/⚠️ Depends | Blocked if per-service allow-lists use specific SA names; allowed if using `sa/*` wildcard |

---

## Key Concepts

### mTLS STRICT vs PERMISSIVE

| Mode | Plain HTTP Accepted? | mTLS Accepted? | Use Case |
|---|---|---|---|
| **STRICT** | ❌ No | ✅ Yes | Production — rejects non-mesh traffic |
| **PERMISSIVE** | ✅ Yes | ✅ Yes | Migration — while onboarding services to the mesh |

Our configuration uses **STRICT** (`PeerAuthentication.spec.mtls.mode: STRICT`).

### Why Plain HTTP Still Works Inside the Mesh

The Envoy sidecar uses **iptables rules** (set up by `istio-init`) to intercept all outbound traffic from the app container. The app never makes a direct network call — it goes through the local sidecar first. The sidecar then:

1. Resolves the destination service
2. Establishes a mutual TLS connection to the target pod's sidecar
3. Both sides verify each other's SPIFFE identity certificates
4. The encrypted request reaches the target sidecar, which decrypts and forwards to the target app

This is why **your app code doesn't need `https://`** — the encryption happens at the sidecar layer, not the application layer.

---

## How to Verify mTLS is Active

### 1. Check TLS Handshake Stats
```bash
kubectl exec <pod> -c istio-proxy -- curl -s localhost:15000/stats | grep ssl.handshake
# Output: ssl.handshake: 42  (count > 0 = mTLS is working)
```

### 2. Check SPIFFE Identity
```bash
kubectl exec <pod> -c istio-proxy -- curl -s localhost:15000/certs | grep spiffe
# Output: spiffe://cluster.local/ns/your-namespace/sa/your-service-account
```

### 3. Check Envoy Server Header
```bash
kubectl exec <pod> -c <app-container> -- curl -sI http://target-svc:8080/ | grep server
# Output: server: envoy  (proves traffic is flowing through the mesh)
```

### 4. Enable Access Logging (see every request)
```bash
# Temporarily enable on a running pod (no restart needed)
kubectl exec <pod> -c istio-proxy -- curl -X POST localhost:15000/logging?level=debug -s

# Watch the logs
kubectl logs <pod> -c istio-proxy -f

# Reset when done
kubectl exec <pod> -c istio-proxy -- curl -X POST localhost:15000/logging?level=warning -s
```

Or add these annotations to `deploy.yaml` (requires pod restart):
```yaml
annotations:
  sidecar.istio.io/logLevel: "info"
  sidecar.istio.io/componentLogLevel: "filter:debug,router:debug,http:debug"
```

Or deploy the `Telemetry` resource for namespace-wide access logging:
```yaml
apiVersion: telemetry.istio.io/v1alpha1
kind: Telemetry
metadata:
  name: access-logging
spec:
  accessLogging:
    - providers:
        - name: envoy
```

---

## The 4 Security Principles (Quick Reference)

| # | Principle | What It Does | Policy |
|---|---|---|---|
| 1 | **Tenant Isolation** | Deny all external, allow same namespace | `deny-all-default` + `allow-same-namespace` |
| 2 | **mTLS STRICT** | Encrypt all service-to-service traffic | `PeerAuthentication` (mode: STRICT) |
| 3 | **OIDC/JWT Validation** | Authenticate external users | `RequestAuthentication` + `authorization-authz` |
| 4 | **Service Allow-Lists** | Per-service caller whitelists | Per-service `AuthorizationPolicy` with SA principals |

---

## Summary

> **Your application keeps using `http://` for service-to-service calls.** The Istio sidecar transparently handles mTLS encryption. Plain HTTP only fails when the caller is **outside the mesh** (no sidecar) or from a **different namespace** (blocked by policy).
