"""
Test: Verify kubernetes Python client sends Authorization header correctly.

Tests two approaches:
1. api_key["authorization"] = "Bearer <token>"  (WRONG - what we had)
2. api_key["BearerToken"] = "<token>" + api_key_prefix["BearerToken"] = "Bearer"  (CORRECT)

This creates a mock HTTP server and checks what headers the K8s client actually sends.
"""

import threading
import json
from http.server import HTTPServer, BaseHTTPRequestHandler
from kubernetes import client

# Track what Authorization header the server receives
received_headers = {}


class MockK8sHandler(BaseHTTPRequestHandler):
    """Mock K8s API server that captures the Authorization header."""
    def do_GET(self):
        received_headers['Authorization'] = self.headers.get('Authorization', 'NONE')
        received_headers['path'] = self.path
        # Return a minimal valid K8s deployment list response
        response = {
            "apiVersion": "apps/v1",
            "kind": "DeploymentList",
            "items": [
                {
                    "metadata": {"name": "test-svc", "labels": {}, "creationTimestamp": "2024-01-01T00:00:00Z"},
                    "spec": {
                        "replicas": 1,
                        "selector": {"matchLabels": {"app": "test"}},
                        "template": {
                            "metadata": {"labels": {"app": "test"}},
                            "spec": {"containers": [{"name": "app", "image": "registry/app:v1.2.3"}]}
                        }
                    },
                    "status": {"readyReplicas": 1, "replicas": 1}
                }
            ]
        }
        body = json.dumps(response).encode()
        self.send_response(200)
        self.send_header('Content-Type', 'application/json')
        self.send_header('Content-Length', len(body))
        self.end_headers()
        self.wfile.write(body)

    def log_message(self, fmt, *args):
        pass  # Suppress server logs


def test_auth_method(method_name, cfg):
    """Make a K8s API call and check what auth header was sent."""
    global received_headers
    received_headers = {}

    api_client = client.ApiClient(cfg)
    apps_v1 = client.AppsV1Api(api_client=api_client)

    try:
        result = apps_v1.list_namespaced_deployment("test-namespace")
        auth_header = received_headers.get('Authorization', 'NONE')
        print(f"\n{'='*60}")
        print(f"Method: {method_name}")
        print(f"  api_key = {cfg.api_key}")
        print(f"  api_key_prefix = {cfg.api_key_prefix}")
        print(f"  Authorization header sent: {auth_header}")
        if auth_header.startswith('Bearer ') and 'my-secret-token' in auth_header:
            print(f"  ✅ PASS — Token sent correctly!")
        elif auth_header == 'NONE':
            print(f"  ❌ FAIL — No Authorization header sent (system:anonymous)")
        else:
            print(f"  ⚠️  Unexpected header value")
        print(f"  Deployments returned: {len(result.items)}")
    except Exception as e:
        print(f"\n{'='*60}")
        print(f"Method: {method_name}")
        print(f"  ❌ ERROR: {e}")
    finally:
        api_client.close()


def main():
    # Start mock K8s API server
    server = HTTPServer(('127.0.0.1', 16443), MockK8sHandler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    print("Mock K8s API server running on http://127.0.0.1:16443")

    token = "my-secret-token-eyJhbGciOiJSUzI1NiIsInR5cCI6IkpXVCJ9"

    # ── Test 1: WRONG way (what we had before) ──
    cfg1 = client.Configuration()
    cfg1.host = "http://127.0.0.1:16443"
    cfg1.api_key = {"authorization": f"Bearer {token}"}
    cfg1.verify_ssl = False
    test_auth_method("WRONG: api_key['authorization']", cfg1)

    # ── Test 2: CORRECT way (BearerToken key) ──
    cfg2 = client.Configuration()
    cfg2.host = "http://127.0.0.1:16443"
    cfg2.api_key = {"BearerToken": token}
    cfg2.api_key_prefix = {"BearerToken": "Bearer"}
    cfg2.verify_ssl = False
    test_auth_method("CORRECT: api_key['BearerToken']", cfg2)

    # ── Test 3: Using load_incluster_config style (reference) ──
    cfg3 = client.Configuration()
    cfg3.host = "http://127.0.0.1:16443"
    cfg3.api_key = {"BearerToken": token}
    cfg3.api_key_prefix = {"BearerToken": "Bearer"}
    cfg3.verify_ssl = False
    # Simulate what load_incluster_config does
    test_auth_method("REFERENCE: load_incluster_config style", cfg3)

    print(f"\n{'='*60}")
    print("CONCLUSION:")
    print("  If 'WRONG' shows 'NONE' and 'CORRECT' shows 'Bearer my-secret-token...'")
    print("  then the BearerToken fix is the correct solution.")
    print(f"{'='*60}")

    server.shutdown()


if __name__ == '__main__':
    main()
