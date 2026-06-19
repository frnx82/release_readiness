# 🧪 QA Tab — Quality Gate & GitOps Deployment Pipeline

## Overview

The QA Tab in the Release Readiness Dashboard provides a **fully integrated quality gate workflow** that bridges the gap between release nomination and production deployment. It implements a GitOps-first approach where all environment changes are driven by pushing versioned manifests to Git branches, with ArgoCD handling the actual cluster synchronization.

### Why It Matters

In a traditional release process, the handoff between development, QA, and production involves:
- Manual version tracking across Slack, spreadsheets, and emails
- No single source of truth for "what versions are being tested"
- Risk of version drift between what QA tested and what goes to production
- No audit trail of who triggered deployments and when

The QA Tab **eliminates all of this** by automating the entire flow from a single dashboard.

---

## Architecture

```
┌─────────────────────────────────────────────────────────────────┐
│                   Release Readiness Dashboard                    │
│                          (QA Tab)                                │
├─────────────────────────────────────────────────────────────────┤
│                                                                  │
│  Step 1: Prepare E2E    ──→  Push version.yaml to e2e branch    │
│  Step 2: Namespace Status ──→  Read QA namespace (K8s API)      │
│  Step 3: Test Pipelines  ──→  Trigger GitHub Actions workflows  │
│  Step 4: Drift Check     ──→  Compare board vs e2e branch       │
│  Step 5: Prepare Prod    ──→  Push version.yaml to prod/preprod │
│                                                                  │
└──────────────┬──────────────────────────────────────────────────┘
               │
               ▼
┌──────────────────────────┐     ┌────────────────────────┐
│  GitHub Repository       │     │  ArgoCD                │
│  (app-deployments)       │────▶│  (auto-sync on push)   │
│                          │     │                        │
│  branches:               │     │  Watches:              │
│  ├── e2e/version.yaml    │     │  ├── e2e → QA cluster  │
│  ├── prod/version.yaml   │     │  ├── prod → Prod       │
│  └── preprod/version.yaml│     │  └── preprod → PreProd  │
└──────────────────────────┘     └────────────────────────┘
```

---

## Features

### Step 1: Prepare E2E Environment

**What it does:**
- Collects all **nominated service versions** from the locked release board
- Fetches **current production versions** for non-nominated services (to create a complete environment)
- Merges both into a single `version.yaml` manifest
- Pushes `version.yaml` to the `e2e` branch of your deployment repository
- ArgoCD auto-detects the change and deploys to the QA namespace

**Why it's important:**
- Creates a **complete, reproducible environment** — not just the changed services, but everything
- Eliminates manual version file creation
- Provides a full audit trail of who prepared the environment and when

**version.yaml format:**
```yaml
release_date: "2026-06-19"
generated_at: "2026-06-18T19:30:00Z"
generated_by: "rajesh"
services:
  auth-service:
    image_tag: "v2.4.1"
    source: "board"          # From release board nomination
    kind: "Deployment"
  payment-gateway:
    image_tag: "v1.8.3"
    source: "production"     # Current production version (not nominated)
    kind: "Deployment"
  redis-cluster:
    image_tag: "v7.2.0"
    source: "board"
    kind: "StatefulSet"
```

### Step 2: QA Namespace Status

**What it does:**
- Reads the live QA/E2E namespace via Kubernetes API
- Shows all running services, their image tags, replica counts, and health status
- Provides a real-time view of what's actually deployed (vs. what was requested)

**Why it's important:**
- Confirms ArgoCD successfully synced the new versions
- Identifies services that failed to start (crash loops, image pull errors)
- No need to `kubectl` into the cluster manually

### Step 3: Test Pipelines

**What it does:**
- Provides one-click buttons to trigger **Smoke**, **E2E**, and **Regression** test suites
- Each test type triggers its own dedicated GitHub Actions workflow
- Shows test trigger status and links to the GitHub Actions run

**Configuration:**
Each test type maps to its own workflow file:

| Test Type | Env Variable | Default Workflow |
|-----------|-------------|-----------------|
| Smoke | `QA_TEST_SMOKE_WORKFLOW` | `smoke-tests.yml` |
| E2E | `QA_TEST_E2E_WORKFLOW` | `e2e-tests.yml` |
| Regression | `QA_TEST_REGRESSION_WORKFLOW` | `regression-tests.yml` |

All workflows live in the `QA_TEST_REPO` repository and receive these inputs:
```yaml
inputs:
  test_type: "smoke"           # smoke | e2e | regression
  environment: "uat-testing"   # QA_NAMESPACE value
```

**Why it's important:**
- Centralizes test execution in the release dashboard
- Provides visibility to all stakeholders (not just QA engineers)
- Tracks which tests were run and by whom in the audit trail

### Step 4: Version Drift Check

**What it does:**
- Compares the **current** release board + production versions against the `version.yaml` that was previously pushed to the `e2e` branch
- Detects three types of drift:
  - **🔄 Version Changed** — A service version was updated on the board after E2E was prepared
  - **🆕 New** — A new service was nominated after E2E was prepared
  - **🗑️ Removed** — A service was de-nominated after E2E was prepared

**Why it's important:**
- Catches last-minute changes that could invalidate QA testing
- Ensures what was tested is exactly what goes to production
- Prevents the classic "but we tested a different version" problem
- Provides a clear go/no-go signal before proceeding to production

### Step 5: Prepare Prod / PreProd

**What it does:**
- Requires a **Change Ticket** number (e.g., `CHG0012345`) — enforces change management compliance
- Generates the final `version.yaml` with **only nominated services** (not the full environment)
- Injects the change ticket as the first field in the manifest
- Pushes `version.yaml` to both `prod` and `preprod` branches simultaneously
- ArgoCD auto-syncs to production and pre-production clusters

> **Key difference from E2E:** The E2E `version.yaml` (Step 1) includes **all services** (nominated + production versions) to create a complete testing environment. The Prod/PreProd `version.yaml` (Step 5) includes **only nominated services** — these are the services being deployed in this release.

**version.yaml for production (nominated only):**
```yaml
change_ticket: "CHG0012345"
release_date: "2026-06-19"
generated_at: "2026-06-18T20:00:00Z"
generated_by: "rajesh"
services:
  auth-service:
    image_tag: "v2.4.1"
    source: "board"
    nominated_by: "rajesh"
    kind: "Deployment"
  redis-cluster:
    image_tag: "v7.2.0"
    source: "board"
    nominated_by: "priya"
    kind: "StatefulSet"
  # Only nominated services — non-nominated prod services are NOT included
```

**Why it's important:**
- Enforces change management (no deployment without a ticket)
- **Only deploys what changed** — production pipeline receives only the services that were nominated, tested, and approved
- Creates a complete audit trail linking the release to an ITSM ticket
- Atomic push to both prod and preprod ensures consistency
- The change ticket in `version.yaml` can be parsed by downstream CI/CD pipelines

---

## Workflow: End-to-End Release Process

```
1. Development Phase
   └── Teams nominate service versions on the Board tab
   
2. Board Lock (cutoff time reached)
   └── No more nominations accepted
   
3. QA Tab unlocks ← (board must be locked first)
   │
   ├── Step 1: Click "Prepare E2E Environment"
   │   └── version.yaml pushed to e2e branch → ArgoCD deploys to QA
   │
   ├── Step 2: Verify QA Namespace Status
   │   └── Confirm all services are running and healthy
   │
   ├── Step 3: Run Test Pipelines
   │   ├── ▶ Smoke Tests (quick validation)
   │   ├── ▶ E2E Tests (full integration)
   │   └── ▶ Regression Tests (comprehensive)
   │
   ├── Step 4: Check Version Drift
   │   └── Ensure no changes since E2E was prepared
   │   └── If drift detected → re-run Step 1 and re-test
   │
   └── Step 5: Prepare Prod/PreProd
       ├── Enter Change Ticket (required)
       └── version.yaml pushed to prod + preprod branches
           └── ArgoCD deploys to production
```

---

## Configuration Reference

### Environment Variables

| Variable | Required | Description |
|----------|----------|-------------|
| `QA_DEPLOY_REPO` | Yes | GitHub repo for version.yaml pushes (e.g., `your-org/app-deployments`) |
| `QA_NAMESPACE` | Yes | Kubernetes namespace for the QA/E2E environment (e.g., `uat-testing`) |
| `QA_TEST_REPO` | No | GitHub repo containing test pipeline workflows |
| `QA_TEST_SMOKE_WORKFLOW` | No | Workflow file for smoke tests (default: `smoke-tests.yml`) |
| `QA_TEST_E2E_WORKFLOW` | No | Workflow file for E2E tests (default: `e2e-tests.yml`) |
| `QA_TEST_REGRESSION_WORKFLOW` | No | Workflow file for regression tests (default: `regression-tests.yml`) |

### GitHub Repository Structure

Your deployment repository (`QA_DEPLOY_REPO`) should have these branches:

```
app-deployments/
├── e2e branch
│   └── version.yaml        ← Pushed by Step 1 (Prepare E2E)
├── prod branch
│   └── version.yaml        ← Pushed by Step 5 (Prepare Prod)
└── preprod branch
    └── version.yaml        ← Pushed by Step 5 (Prepare Prod)
```

ArgoCD should be configured to watch each branch and sync to the corresponding cluster/namespace.

### Kubernetes Prerequisites

The app requires a **ServiceAccount**, **Role**, and **RoleBinding** to interact with the Kubernetes API:

#### ServiceAccount

```yaml
apiVersion: v1
kind: ServiceAccount
metadata:
  name: release-readiness
  labels:
    app: release-readiness
```

> **Important:** The Deployment must reference this ServiceAccount via `serviceAccountName: release-readiness` and set `automountServiceAccountToken: true` so the K8s API client can authenticate.

#### Role

```yaml
apiVersion: rbac.authorization.k8s.io/v1
kind: Role
metadata:
  name: release-readiness
  labels:
    app: release-readiness
rules:
# Read workloads — Board tab, UAT/Prod Env tabs, AI Readiness, QA Namespace Status
- apiGroups: ["apps"]
  resources: ["deployments", "statefulsets", "replicasets"]
  verbs: ["get", "list", "watch"]

# Read pods & events — AI Readiness health checks, crash log analysis
- apiGroups: [""]
  resources: ["pods", "pods/log", "events", "services"]
  verbs: ["get", "list", "watch"]

# Read/Write ConfigMaps — board state persistence
- apiGroups: [""]
  resources: ["configmaps"]
  verbs: ["get", "list", "create", "update", "patch"]

# Read Helm releases (stored as secrets with label owner=helm)
- apiGroups: [""]
  resources: ["secrets"]
  verbs: ["get", "list"]
```

#### RoleBinding

```yaml
apiVersion: rbac.authorization.k8s.io/v1
kind: RoleBinding
metadata:
  name: release-readiness
  labels:
    app: release-readiness
roleRef:
  apiGroup: rbac.authorization.k8s.io
  kind: Role
  name: release-readiness
subjects:
- kind: ServiceAccount
  name: release-readiness
```

> **Cross-namespace note:** If the app is deployed in a different namespace than the QA namespace, you'll need either a **ClusterRole** + **ClusterRoleBinding**, or create an additional Role + RoleBinding in the QA namespace referencing the app's ServiceAccount from its home namespace.

### GitHub OAuth Scopes

The GitHub OAuth app needs these scopes for QA operations:
- `repo` — Push commits to branches (version.yaml)
- `workflow` — Trigger GitHub Actions workflows (test pipelines)

---

## API Endpoints

| Endpoint | Method | Description |
|----------|--------|-------------|
| `/api/qa/prepare` | `POST` | Generate version.yaml and push to e2e branch |
| `/api/qa/prepare/status` | `GET` | Get current QA preparation status |
| `/api/qa/env/services` | `GET` | List services in the QA namespace |
| `/api/qa/test/trigger` | `POST` | Trigger a test pipeline (body: `{ "test_type": "smoke" }`) |
| `/api/qa/drift-check` | `POST` | Compare board+prod versions against e2e version.yaml |
| `/api/qa/prepare-prod` | `POST` | Push version.yaml to prod+preprod (body: `{ "change_ticket": "CHG001" }`) |

---

## Benefits Summary

### For Release Managers
- **Single pane of glass** for the entire QA-to-production pipeline
- **Enforced quality gates** — can't deploy to prod without testing first
- **Change management compliance** — change ticket required for production
- **Full audit trail** — every action logged with who/when/what

### For QA Engineers
- **One-click test execution** — no need to navigate GitHub Actions
- **Live namespace monitoring** — see deployment health in real-time
- **Drift detection** — know immediately if the environment changed

### For DevOps / SREs
- **GitOps-native** — all changes are Git commits, fully auditable
- **ArgoCD integration** — no custom deployment scripts or kubectl commands
- **Reproducible environments** — version.yaml is the single source of truth

### For Engineering Leadership
- **Visibility** — see release progress without asking teams
- **Risk reduction** — drift checks catch mismatches before production
- **Compliance** — audit trail and change ticket enforcement for SOX/SOC2

---

## Future Enhancements — MCP Integration

> See [QA-AUTOMATION-MCP-DESIGN.md](QA-AUTOMATION-MCP-DESIGN.md) for full design details.

### Current vs. MCP-Enhanced Capabilities

The current QA Tab handles core workflow natively (direct GitHub API calls from `app.py`). MCP servers are a **Phase 2 enhancement** that add richer observability and automation.

| Capability | Current (Built-in) | MCP-Enhanced (Future) |
|---|---|---|
| **Trigger tests** | ✅ Direct GitHub Actions `workflow_dispatch` | MCP Server #1: Test Runner — same mechanism, reusable across tools |
| **Test results** | ❌ Link to GitHub Actions only | MCP Server #2: Allure integration — show pass/fail counts, trends, stack traces inside the dashboard |
| **Quality gate** | ⚠️ Static "Pending" badge | MCP Server #3: Automated go/no-go based on configurable thresholds (e.g., "E2E ≥ 95%") |
| **Environment prep** | ✅ Push version.yaml to branch | MCP Server #4: ArgoCD sync monitoring ("15/28 synced"), teardown (scale to 0) |
| **Drift check** | ✅ Compare board vs. e2e branch | Same — no MCP needed |
| **Prod/PreProd push** | ✅ Push with change ticket | Same — no MCP needed |

### MCP Server #1: Test Runner

**What it adds:** A standalone, reusable MCP server that can be consumed by the dashboard, AI chatbot, or any MCP-compatible client.

**Current approach:** `app.py` calls GitHub Actions API directly — works but is tightly coupled to the dashboard.

**MCP approach:** Separate `test-runner-mcp` service with tools: `test_run`, `test_run_status`, `test_run_cancel`, `test_list_runs`.

**When to implement:** When you want AI agents or other tools to trigger tests independently.

### MCP Server #2: Test Results (Allure)

**What it adds:** Pull Allure test reports directly into the dashboard — pass/fail counts, failure details, trends over time.

**Current gap:** After triggering tests, users must click through to GitHub Actions → Allure to see results.

**MCP tools:** `test_results_latest`, `test_results_by_run`, `test_results_trend`, `test_results_failures`.

**Dashboard widget (future):**
```
┌──────────────────────────────────────────────────────────────┐
│  📊 Test Results — Latest Run                                │
│──────────────────────────────────────────────────────────────│
│  Smoke:      ✅ 42/42 passed (100%)     Duration: 3m 12s     │
│  E2E:        ✅ 187/192 passed (97.4%)  Duration: 28m 45s    │
│  Regression: ✅ 1,204/1,218 (98.9%)     Duration: 1h 12m     │
│──────────────────────────────────────────────────────────────│
│  [📋 Full Allure Report]  [📈 Trend (7 days)]               │
└──────────────────────────────────────────────────────────────┘
```

**When to implement:** When your QA team has Allure deployed and accessible via API.

### MCP Server #3: Quality Gate

**What it adds:** Automated pass/fail decision based on configurable rules — replaces the manual "QA Sign-Off."

**Quality gate rules (configurable):**
```json
{
  "rules": [
    {"name": "Smoke Tests",          "test_type": "smoke",      "metric": "pass_rate", "threshold": 100, "required": true},
    {"name": "E2E Pass Rate",        "test_type": "e2e",        "metric": "pass_rate", "threshold": 95,  "required": true},
    {"name": "Regression Pass Rate", "test_type": "regression", "metric": "pass_rate", "threshold": 98,  "required": true}
  ]
}
```

**Dashboard widget (future):**
```
┌──────────────────────────────────────────────────────────────┐
│ 🚦 Quality Gate: PASSED — QA Sign-Off Ready                 │
│──────────────────────────────────────────────────────────────│
│ ✅ Smoke Tests:           100% (threshold: 100%)             │
│ ✅ E2E Pass Rate:         97.2% (threshold: 95%)             │
│ ✅ Regression Pass Rate:  99.1% (threshold: 98%)             │
│──────────────────────────────────────────────────────────────│
│ [▶ Run All Tests]  [📋 Full Report]  [✅ QA Sign-Off]       │
└──────────────────────────────────────────────────────────────┘
```

**When to implement:** After MCP Server #2 (Test Results) — Quality Gate depends on having test results data.

### MCP Server #4: Environment Manager

**What it adds:** Real-time ArgoCD sync progress monitoring and environment teardown.

**Current approach:** Push version.yaml → ArgoCD auto-syncs (fire and forget).

**MCP approach:** Monitor sync progress ("15/28 services synced"), show per-service health, scale down on release completion.

**MCP tools:** `env_prepare`, `env_status`, `env_diff`, `env_teardown`.

**When to implement:** When you need real-time deployment visibility or automated teardown after testing.

### Auto-Trigger Tests After Board Lock (Future)

A planned enhancement where tests automatically trigger when the board locks at cutoff time:

```
Board auto-locks at cutoff (e.g., Wednesday 12 PM)
       ↓
Dashboard detects lock event
       ↓
Step 1: Auto-run "Prepare E2E" (push version.yaml)
       ↓
Step 2: Wait for ArgoCD sync (via MCP Server #4)
       ↓
Step 3: Auto-trigger test suites in sequence:
        smoke → e2e → regression
       ↓
Step 4: Quality Gate evaluates results (via MCP Server #3)
       ↓
Step 5: Notify QA team — "Environment ready, tests passed/failed"
```

**Configuration (future env vars):**

| Variable | Default | Description |
|---|---|---|
| `QA_AUTO_TRIGGER` | `false` | Enable auto-trigger after board lock |
| `QA_AUTO_TRIGGER_DELAY_MIN` | `5` | Minutes to wait after lock before triggering |
| `QA_AUTO_TRIGGER_ORDER` | `smoke,e2e,regression` | Order of test suites |

### Implementation Priority

| Priority | Enhancement | Effort | Dependency |
|---|---|---|---|
| **P0** | Test Results MCP (Allure) | 2-3 days | Allure server deployed and accessible |
| **P1** | Quality Gate MCP | 1-2 days | Test Results MCP (P0) |
| **P2** | Environment Manager MCP (ArgoCD) | 3-5 days | ArgoCD API accessible from dashboard |
| **P3** | Auto-Trigger on Board Lock | 1-2 days | Test Runner + APScheduler |

### Folder Structure (Future)

```
enterprise-mcp-servers/
├── test-runner-mcp/
│   ├── server.py             # MCP server — triggers GHA pipelines
│   ├── github_actions.py     # GitHub Actions API wrapper
│   └── Dockerfile
├── test-results-mcp/
│   ├── server.py             # MCP server — reads Allure results
│   ├── allure_client.py      # Allure report API client
│   └── Dockerfile
├── quality-gate-mcp/
│   ├── server.py             # MCP server — aggregates pass/fail
│   ├── rules_engine.py       # Configurable threshold checks
│   └── Dockerfile
└── env-manager-mcp/
    ├── server.py             # MCP server — ArgoCD monitoring
    ├── argocd_client.py      # ArgoCD API client
    ├── manifest_builder.py   # Merges board + prod versions
    └── Dockerfile
```
