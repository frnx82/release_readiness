# QA Automation MCP Servers — Design Document

## Tech Stack (Confirmed)

| Component | Technology |
|---|---|
| **Application** | 25+ Python microservices (API) + 3+ Node.js UI apps |
| **CI/CD** | GitHub Actions |
| **Test Pipeline** | Single workflow with `test_type` flag (e2e / smoke / regression) |
| **Performance Testing** | LoadRunner (separate — not integrated into dashboard) |
| **Test Reporting** | Allure |
| **Security Scanning** | Xray (already in CI pipeline — not in dashboard scope) |
| **Code Repository** | GitHub |
| **Container Platform** | OpenShift / K8s |
| **Test Environments** | Standing UAT + On-Demand ephemeral |

---

## Architecture

![MCP Architecture](images/mcp-architecture.png)

> **Note**: Performance testing (LoadRunner) and security scanning (Xray) both run independently in your CI pipeline and are not triggered from the dashboard. The dashboard focuses on E2E, smoke, and regression testing.

---

## MCP Server #1: Test Runner (GitHub Actions)

**Purpose**: Trigger the existing test pipeline from the dashboard via GitHub Actions `workflow_dispatch`.

### How It Works

You have a **single test pipeline** that accepts a `test_type` flag to run different test suites. The dashboard triggers this same workflow:

- **Manually**: QA clicks "▶ Run Tests" on the dashboard
- **Automatically**: Tests auto-trigger after the board locks at cutoff time (Wednesday 12 PM)

### Auto-Trigger After Cutoff

When the board auto-locks at cutoff, the dashboard automatically kicks off the full test suite:

![Auto-Trigger Flow](images/qa-auto-trigger-flow.png)

**How it works technically**:

```python
# In app.py — background scheduler
from apscheduler.schedulers.background import BackgroundScheduler

def _auto_trigger_tests():
    """Called every minute. Checks if board just locked and triggers tests."""
    board = _read_board()
    if not board:
        return

    # Only trigger once per release cycle
    if board.get('tests_auto_triggered'):
        return

    # Check if board just passed cutoff
    cutoff = board.get('cutoff', '')
    now = datetime.datetime.utcnow().isoformat()
    if now > cutoff and board.get('status') != 'locked':
        return  # Not past cutoff yet or already handled

    # Trigger: smoke → e2e → regression (sequential)
    for test_type in ['smoke', 'e2e', 'regression']:
        _github_post(
            f'/repos/{QA_TEST_REPO}/actions/workflows/{QA_WORKFLOW_FILE}/dispatches',
            data={
                "ref": "main",
                "inputs": {
                    "test_type": test_type,
                    "environment": "uat",
                }
            }
        )
        time.sleep(5)  # Brief delay between triggers

    # Mark as triggered (prevent re-trigger)
    board['tests_auto_triggered'] = True
    board['tests_auto_triggered_at'] = now
    _write_board(board)
    print(f"[QA Auto-Trigger] All test suites triggered at {now}")

scheduler = BackgroundScheduler()
scheduler.add_job(_auto_trigger_tests, 'interval', minutes=1)
scheduler.start()
```

### Configuration

| Env Var | Default | Description |
|---|---|---|
| `QA_AUTO_TRIGGER` | `true` | Enable/disable auto-trigger after cutoff |
| `QA_AUTO_TRIGGER_DELAY_MIN` | `5` | Minutes to wait after cutoff before triggering |
| `QA_AUTO_TRIGGER_ORDER` | `smoke,e2e,regression` | Order of test suites to trigger |

### Your Test Pipeline (Existing)

```yaml
# .github/workflows/test-pipeline.yml
name: Test Pipeline
on:
  push:
    branches: [main]
  workflow_dispatch:
    inputs:
      test_type:
        description: 'Type of tests to run'
        required: true
        type: choice
        options:
          - e2e
          - smoke
          - regression
      environment:
        description: 'Target environment'
        required: true
        default: 'uat'
        type: choice
        options:
          - uat
          - on-demand
      target_namespace:
        description: 'Target namespace (for on-demand envs)'
        required: false
        type: string

jobs:
  test:
    runs-on: [self-hosted]
    steps:
      - uses: actions/checkout@v4
      - name: Run Tests
        run: |
          pytest tests/ \
            --test-type=${{ inputs.test_type }} \
            --environment=${{ inputs.environment }} \
            --alluredir=allure-results
      - name: Publish Allure
        uses: allure-framework/publish-allure@v1
        with:
          results-dir: allure-results
```

### Tools

| Tool Name | Parameters | What It Does |
|---|---|---|
| `test_run` | `test_type` (e2e\|smoke\|regression), `environment`, `version` | Triggers the single test pipeline with the right flag |
| `test_run_status` | `run_id` | Gets current status (queued, in_progress, completed) |
| `test_run_cancel` | `run_id` | Cancels a running workflow |
| `test_list_runs` | `test_type`, `limit` | Recent workflow runs filtered by test type |

### Implementation

```python
# test_runner_mcp/server.py
import httpx, os
from mcp.server.fastmcp import FastMCP

GITHUB_TOKEN = os.getenv('GITHUB_TOKEN')
GITHUB_OWNER = os.getenv('GITHUB_OWNER')
GITHUB_REPO  = os.getenv('QA_TEST_REPO')

HEADERS = {
    'Authorization': f'Bearer {GITHUB_TOKEN}',
    'Accept': 'application/vnd.github.v3+json',
}
BASE = f'https://api.github.com/repos/{GITHUB_OWNER}/{GITHUB_REPO}'

# Single workflow file — test type is an input parameter
TEST_WORKFLOW = os.getenv('QA_WORKFLOW_FILE', 'test-pipeline.yml')

server = FastMCP("test-runner")

@server.tool()
async def test_run(test_type: str, environment: str = "uat", version: str = "",
                   target_namespace: str = ""):
    """Trigger the test pipeline via GitHub Actions workflow_dispatch.
    
    test_type: e2e | smoke | regression
    environment: uat | on-demand
    """
    if test_type not in ('e2e', 'smoke', 'regression'):
        return {"error": f"Invalid test_type: {test_type}. Use: e2e, smoke, regression"}

    async with httpx.AsyncClient() as client:
        resp = await client.post(
            f"{BASE}/actions/workflows/{TEST_WORKFLOW}/dispatches",
            headers=HEADERS,
            json={
                "ref": "main",
                "inputs": {
                    "test_type": test_type,
                    "environment": environment,
                    "target_namespace": target_namespace,
                }
            }
        )

    if resp.status_code == 204:
        # GitHub doesn't return run ID immediately — poll for it
        import asyncio
        await asyncio.sleep(3)
        async with httpx.AsyncClient() as client:
            runs_resp = await client.get(
                f"{BASE}/actions/workflows/{TEST_WORKFLOW}/runs?per_page=1",
                headers=HEADERS
            )
        runs = runs_resp.json().get('workflow_runs', [])
        run = runs[0] if runs else {}

        return {
            "status": "triggered",
            "test_type": test_type,
            "environment": environment,
            "run_id": run.get('id'),
            "html_url": run.get('html_url'),
        }

    return {"error": f"Failed to trigger: HTTP {resp.status_code} - {resp.text}"}


@server.tool()
async def test_run_status(run_id: int):
    """Get the current status of a GitHub Actions workflow run."""
    async with httpx.AsyncClient() as client:
        resp = await client.get(f"{BASE}/actions/runs/{run_id}", headers=HEADERS)
    data = resp.json()
    return {
        "run_id": run_id,
        "status": data.get("status"),           # queued, in_progress, completed
        "conclusion": data.get("conclusion"),     # success, failure, cancelled
        "started_at": data.get("run_started_at"),
        "updated_at": data.get("updated_at"),
        "html_url": data.get("html_url"),
    }
```

---

## MCP Server #2: Test Results (Allure)

**Purpose**: Fetch test results from Allure reports and display them on the dashboard.

### Tools

| Tool Name | Parameters | What It Does |
|---|---|---|
| `test_results_latest` | `test_type`, `environment` | Gets the latest Allure results for a test type |
| `test_results_by_run` | `run_id` | Gets Allure results for a specific GHA run |
| `test_results_trend` | `test_type`, `days` | Pass/fail trend over time |
| `test_results_failures` | `run_id` | Detailed failure info (stack traces, screenshots) |

### Implementation

```python
# test_results_mcp/server.py
ALLURE_URL   = os.getenv('ALLURE_URL')
ALLURE_TOKEN = os.getenv('ALLURE_TOKEN')

@server.tool()
async def test_results_latest(test_type: str, environment: str = "uat"):
    """Fetch latest test results from Allure."""
    async with httpx.AsyncClient() as client:
        resp = await client.get(
            f"{ALLURE_URL}/api/rs/launch/latest",
            params={"projectId": test_type, "env": environment},
            headers={"Authorization": f"Bearer {ALLURE_TOKEN}"}
        )
    data = resp.json()
    stat = data.get("statistic", {})
    total = stat.get("total", 0)
    passed = stat.get("passed", 0)

    return {
        "test_type": test_type,
        "total": total,
        "passed": passed,
        "failed": stat.get("failed", 0),
        "broken": stat.get("broken", 0),
        "skipped": stat.get("skipped", 0),
        "pass_rate": round(passed / max(total, 1) * 100, 1),
        "duration_seconds": data.get("duration", 0) / 1000,
        "report_url": f"{ALLURE_URL}/launch/{data.get('id')}",
    }
```

---

## MCP Server #3: Quality Gate

**Purpose**: Aggregate all test results into a **go/no-go decision** for release sign-off by the QA team.

### Quality Gate Rules (Configurable)

```json
{
  "rules": [
    {"name": "E2E Pass Rate",        "test_type": "e2e",        "metric": "pass_rate",      "threshold": 95,  "required": true},
    {"name": "Regression Pass Rate", "test_type": "regression", "metric": "pass_rate",      "threshold": 98,  "required": true},
    {"name": "Smoke Tests",          "test_type": "smoke",      "metric": "pass_rate",      "threshold": 100, "required": true}
  ]
}
```

### Dashboard Integration — Quality Gate Widget

```
┌──────────────────────────────────────────────────────────────┐
│ 🚦 Quality Gate: PASSED — QA Sign-Off Ready                 │
│──────────────────────────────────────────────────────────────│
│ ✅ E2E Pass Rate:        97.2% (threshold: 95%)             │
│ ✅ Regression Pass Rate:  99.1% (threshold: 98%)             │
│ ✅ Smoke Tests:           100% (threshold: 100%)             │
│──────────────────────────────────────────────────────────────│
│ [▶ Run All Tests]  [📋 Full Report]  [✅ QA Sign-Off]       │
└──────────────────────────────────────────────────────────────┘
```

---

## MCP Server #4: Environment Manager

**Purpose**: Manage on-demand test environments for the full application stack (25+ Python microservices + 3+ Node.js UI apps).

### The Challenge

With 25+ Python services and 3+ UI apps, spinning up a full environment is not trivial. Here's the practical strategy:

### On-Demand Environment Strategy

![On-Demand Environment Strategy](images/ondemand-env-diagram.png)

### Option A: Clone Namespace (Full Stack) — Simplest

Clone the entire UAT namespace into a new namespace. All services, configs, and secrets are copied, then image tags are overridden with the release board versions.

```python
@server.tool()
async def env_provision(release_version: str = "", ttl_hours: int = 4):
    """Create an on-demand test environment by cloning UAT."""
    import uuid
    env_name = f"test-env-{uuid.uuid4().hex[:6]}"
    
    v1 = client.CoreV1Api()
    apps_v1 = client.AppsV1Api()
    
    # 1. Create namespace with TTL
    v1.create_namespace(client.V1Namespace(
        metadata=client.V1ObjectMeta(
            name=env_name,
            labels={"purpose": "qa-testing", "managed-by": "release-readiness"},
            annotations={
                "expires-at": (datetime.utcnow() + timedelta(hours=ttl_hours)).isoformat(),
            }
        )
    ))
    
    # 2. Copy secrets and configmaps from UAT
    source_ns = os.getenv('UAT_NAMESPACE', 'uat')
    for secret in v1.list_namespaced_secret(source_ns).items:
        if secret.metadata.name.startswith('default-token'):
            continue  # Skip service account tokens
        secret.metadata = client.V1ObjectMeta(
            name=secret.metadata.name, namespace=env_name
        )
        v1.create_namespaced_secret(env_name, secret)
    
    for cm in v1.list_namespaced_config_map(source_ns).items:
        cm.metadata = client.V1ObjectMeta(
            name=cm.metadata.name, namespace=env_name
        )
        v1.create_namespaced_config_map(env_name, cm)
    
    # 3. Clone all deployments, override image tags from release board
    board_versions = _get_board_versions()  # From release board
    deployments = apps_v1.list_namespaced_deployment(source_ns).items
    
    for deploy in deployments:
        svc_name = deploy.metadata.name
        deploy.metadata = client.V1ObjectMeta(
            name=svc_name, namespace=env_name,
            labels=deploy.metadata.labels
        )
        # Override image tag if nominated on the board
        if svc_name in board_versions:
            for container in deploy.spec.template.spec.containers:
                base_image = container.image.rsplit(':', 1)[0]
                container.image = f"{base_image}:{board_versions[svc_name]}"
        
        # Scale down to 1 replica (save resources)
        deploy.spec.replicas = 1
        deploy.metadata.resource_version = None
        apps_v1.create_namespaced_deployment(env_name, deploy)
    
    # 4. Clone services (networking)
    for svc in v1.list_namespaced_service(source_ns).items:
        svc.metadata = client.V1ObjectMeta(
            name=svc.metadata.name, namespace=env_name,
            labels=svc.metadata.labels
        )
        svc.spec.cluster_ip = None  # Let K8s assign new IP
        v1.create_namespaced_service(env_name, svc)
    
    return {
        "environment": env_name,
        "services_deployed": len(deployments),
        "ttl_hours": ttl_hours,
        "status": "provisioning",
        "note": "All 25+ Python services + 3+ UI apps deployed at 1 replica each"
    }
```

**Pros**: Complete environment, exact copy of UAT  
**Cons**: Resource heavy (25+ pods), takes 3-5 min to spin up  
**Mitigation**: Scale all to 1 replica, auto-delete after 4h TTL

### Option B: Override Subset (Targeted Testing) — Most Practical

Only deploy the **changed services** into a new namespace. Route unchanged services to the standing UAT via service mesh or DNS.

```python
@server.tool()
async def env_provision_targeted(services: list, versions: dict, ttl_hours: int = 4):
    """Create an on-demand env with only the changed services.
    Unchanged services route to standing UAT via ExternalName services.
    """
    env_name = f"test-env-{uuid.uuid4().hex[:6]}"
    source_ns = os.getenv('UAT_NAMESPACE', 'uat')
    
    # 1. Create namespace
    v1.create_namespace(...)
    
    # 2. Deploy ONLY the nominated/changed services
    for svc_name in services:
        version = versions.get(svc_name)
        deploy = apps_v1.read_namespaced_deployment(svc_name, source_ns)
        # Override image tag
        for container in deploy.spec.template.spec.containers:
            base_image = container.image.rsplit(':', 1)[0]
            container.image = f"{base_image}:{version}"
        deploy.spec.replicas = 1
        deploy.metadata = client.V1ObjectMeta(name=svc_name, namespace=env_name)
        apps_v1.create_namespaced_deployment(env_name, deploy)
    
    # 3. For all OTHER services, create ExternalName services → UAT
    all_services = v1.list_namespaced_service(source_ns).items
    for svc in all_services:
        if svc.metadata.name not in services:
            # Point to UAT via DNS (service mesh routing)
            ext_svc = client.V1Service(
                metadata=client.V1ObjectMeta(name=svc.metadata.name, namespace=env_name),
                spec=client.V1ServiceSpec(
                    type="ExternalName",
                    external_name=f"{svc.metadata.name}.{source_ns}.svc.cluster.local"
                )
            )
            v1.create_namespaced_service(env_name, ext_svc)
    
    return {
        "environment": env_name,
        "changed_services": services,
        "routed_to_uat": len(all_services) - len(services),
        "status": "provisioning",
        "note": f"Only {len(services)} services deployed. Others route to UAT."
    }
```

**Pros**: Fast (deploy only 2-5 services), resource efficient  
**Cons**: Requires service-to-service DNS resolution across namespaces  
**Best for**: Testing specific services before release (the common case)

### Option C: Helm/Kustomize + ArgoCD (GitOps) — Production Grade

If you use Helm charts or Kustomize, create on-demand environments via ArgoCD ApplicationSet:

```yaml
# argocd/applicationset-on-demand.yaml
apiVersion: argoproj.io/v1alpha1
kind: ApplicationSet
metadata:
  name: qa-test-environments
spec:
  generators:
    - list:
        elements: []  # Dashboard adds entries via ArgoCD API
  template:
    metadata:
      name: "test-env-{{name}}"
    spec:
      project: qa-testing
      source:
        repoURL: https://github.com/your-org/helm-charts
        path: full-stack
        helm:
          values: |
            global.imageTag: "{{version}}"
            replicas: 1
      destination:
        namespace: "test-env-{{name}}"
```

**Pros**: GitOps, declarative, reproducible  
**Cons**: Requires ArgoCD setup, Helm charts for all 28+ services  
**Best for**: Mature platform teams

### Recommendation

> Start with **Option B** (targeted) for day-to-day testing, with **Option A** (full clone) available for major releases. Option C is a future improvement when you adopt GitOps.

### Tools

| Tool Name | Parameters | What It Does |
|---|---|---|
| `env_provision_full` | `ttl_hours` | Clone entire UAT namespace (all 28+ services) |
| `env_provision_targeted` | `services`, `versions`, `ttl_hours` | Deploy only changed services, route rest to UAT |
| `env_status` | `name` | Check environment health (pods ready?) |
| `env_teardown` | `name` | Delete an on-demand environment |
| `env_list` | | List active on-demand environments |

### Auto-Cleanup

A CronJob runs every hour and deletes expired test environments:

```yaml
# cleanup-cronjob.yaml
apiVersion: batch/v1
kind: CronJob
metadata:
  name: qa-env-cleanup
spec:
  schedule: "0 * * * *"  # Every hour
  jobTemplate:
    spec:
      template:
        spec:
          containers:
          - name: cleanup
            image: bitnami/kubectl
            command:
            - /bin/sh
            - -c
            - |
              NOW=$(date -u +%Y-%m-%dT%H:%M:%S)
              for ns in $(kubectl get ns -l purpose=qa-testing -o jsonpath='{.items[*].metadata.name}'); do
                EXPIRES=$(kubectl get ns $ns -o jsonpath='{.metadata.annotations.expires-at}')
                if [ "$NOW" \> "$EXPIRES" ]; then
                  echo "Deleting expired namespace: $ns"
                  kubectl delete ns $ns
                fi
              done
```

---

## Implementation Priority

| Priority | MCP Server | Effort | Impact |
|---|---|---|---|
| **P0** | 🧪 Test Runner | 2-3 days | Trigger e2e/smoke/regression from dashboard |
| **P0** | 📊 Test Results | 2-3 days | Show Allure results on the board |
| **P1** | ✅ Quality Gate | 1-2 days | Go/no-go for QA sign-off |

| **P2** | 🖥️ Env Manager (Targeted) | 3-4 days | On-demand envs for changed services |
| **P3** | 🖥️ Env Manager (Full Clone) | 2-3 days | Full stack clone for major releases |

---

## Sequence: Full Flow

![QA Full Sequence Flow](images/qa-sequence-flow.png)

---

## Folder Structure

```
enterprise-mcp-servers/
├── test-runner-mcp/
│   ├── server.py             # MCP server — triggers single GHA pipeline
│   ├── github_actions.py     # GitHub Actions API wrapper
│   ├── config.py             # Workflow file + test_type mapping
│   └── Dockerfile
├── test-results-mcp/
│   ├── server.py             # MCP server — reads Allure results
│   ├── allure_client.py      # Allure report API client
│   └── Dockerfile
├── quality-gate-mcp/
│   ├── server.py             # MCP server — aggregates checks
│   ├── rules_engine.py       # Configurable threshold checks
│   └── Dockerfile
└── env-manager-mcp/
    ├── server.py             # MCP server — namespace provisioning
    ├── openshift_client.py   # K8s API for clone/targeted deploy
    ├── cleanup_cronjob.yaml  # Auto-delete expired namespaces
    └── Dockerfile
```

---

## Environment Variables

```bash
# Test Runner MCP
GITHUB_TOKEN=ghp_xxx                  # PAT (reuse existing)
GITHUB_OWNER=your-org
QA_TEST_REPO=your-org/qa-tests
QA_WORKFLOW_FILE=test-pipeline.yml    # Single workflow file

# Test Results MCP
ALLURE_URL=https://allure.company.com
ALLURE_TOKEN=xxx

# Environment Manager MCP
OPENSHIFT_API=https://api.ocp.company.com:6443
OPENSHIFT_TOKEN=xxx
UAT_NAMESPACE=uat-prod
```

---

## Resolved Questions

| Question | Answer |
|---|---|
| Application stack | **25+ Python microservices** (API) + **3+ Node.js UI apps** |
| CI tool | **GitHub Actions** — code is on GitHub |
| Test pipeline structure | **Single workflow** with `test_type` flag (e2e / smoke / regression) |
| Performance testing | **LoadRunner** (separate tool, not in dashboard) |
| Test reporting | **Allure** |
| Test management tool | **None** — tests live in GitHub repos |
| Security scanning | **Xray** — already runs in CI pipeline, not in dashboard scope |
| Environment strategy | **Both**: standing UAT + on-demand (targeted or full clone) |
| Test suite priority | **All equal** — QA runs e2e, smoke, regression. All must pass |
| Release sign-off | **QA team** signs off the release (no dedicated release managers) |
