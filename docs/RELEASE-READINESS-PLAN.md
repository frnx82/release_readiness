# Release Readiness — AI-Powered Feature Plan

> **Goal:** Replace the manual Teams-channel-based release coordination process with an AI-powered Release Board inside the dashboard, eliminating version typos, missed services, and manual Jira updates.

---

## The Problem Today

| Step | Who | How | Pain Point |
|---|---|---|---|
| **1. Nominate** | Developers | Post in Teams: "billing-service v2.3.1, helm 0.5.0" | Unstructured, versions typed manually, messages get buried |
| **2. Collect** | Support/DevOps | Read through Teams messages, compile a spreadsheet | Time-consuming, error-prone, chasing developers for missing info |
| **3. Validate** | Release Manager | Cross-check versions, confirm testing | No automated health/stability checks |
| **4. Document** | Someone | Manually update Jira release ticket | Duplicate effort, copy-paste errors |
| **5. Release** | DevOps | Deploy on Friday | Hope nothing was missed 🤞 |

**Key constraints:**
- 30 microservices, trunk-based development, continuous UAT deployments
- Any subset (1-10+) may be released on a given Friday
- Only developers know which services are "ready" — this is a human decision
- Wednesday 5 PM cutoff, Friday release cadence

---

## Solution: Release Board + AI Validation

### Architecture Overview

![Release Board Architecture](diagrams/release_board_architecture.png)

---

## Feature 1: Release Board UI

### What Developers See

A new **"Release"** tab in the dashboard with a board for the current release cycle:

![Release Board UI](diagrams/release_board_ui.png)

### Nomination Flow

1. Developer clicks **"+ Nominate a Service"**
2. Dropdown shows all 30 services **from the live UAT cluster** — no manual typing
3. Image tag + Helm chart version **auto-populated** from what's currently deployed
4. Developer adds optional notes ("Fixed payment timeout bug")
5. Nomination appears on the board with their name and timestamp

### No Manual Version Entry
The version comes directly from the Kubernetes API:
- **Image tag:** from `deployment.spec.template.spec.containers[0].image`
- **Helm chart:** from `metadata.labels["helm.sh/chart"]` or `metadata.labels["app.kubernetes.io/version"]`

This eliminates version typos entirely.

---

## Feature 2: Re-Nomination (Version Update)

### Why This Is Needed
After nominating, a developer may fix a bug and push a new image. The dashboard handles this automatically:

### Version Drift Detection
The dashboard continuously compares nominated versions against the live UAT cluster:

| Scenario | Display | Action |
|---|---|---|
| **🟢 Match** | Nominated v2.3.2, UAT has v2.3.2 | All good |
| **🟡 Drift** | Nominated v2.3.2, UAT now has v2.3.4 | "2 newer versions exist — update nomination?" |
| **🔴 Major Drift** | Nominated v2.3.2, UAT has v3.0.0 | "Major version change detected — re-review required" |

### Update Flow
1. Dev pushes code fix → CI builds v2.3.3 → ArgoCD deploys to UAT
2. Release Board detects drift: `⚠️ billing-service: UAT has v2.3.3 but nomination is for v2.3.2`
3. Dev clicks **"🔄 Update to v2.3.3"** → adds a note → nomination updates
4. AI re-runs readiness check on the new version automatically

### Nomination History (Audit Trail)
Every nomination change is logged:

![Nomination Version History](diagrams/release_nomination_history.png)

### Access Control

| Window | Who Can Nominate/Update | Who Can Remove |
|---|---|---|
| **Before cutoff** | Any developer | Nominator or Release Manager |
| **After cutoff** | Release Manager only | Release Manager only |
| **After release** | Board is locked (read-only) | Nobody |

---

## Feature 3: AI Readiness Check (Gemini-Powered)

When nominations are submitted or updated, Gemini runs an automated readiness check:

### Per-Service Checks

| Check | What AI Analyzes | Data Source |
|---|---|---|
| **Health Status** | Is this service healthy in UAT right now? | Existing AI Diagnose feature |
| **Stability** | Any OOMKills, CrashLoopBackOff, or restarts in last 48 hours? | K8s events API |
| **Version Delta** | Patch / Minor / Major version change from production? | Prod snapshot vs. nomination |
| **Config Changes** | New/changed env vars, ConfigMap keys, or Secret references? | Existing Config Explainer |
| **Resource Changes** | Did CPU/memory requests or limits change? | K8s API comparison |
| **Probe Status** | Are readiness and liveness probes configured? | K8s pod spec |
| **Security** | Any new Critical/High findings from Security Audit? | Existing Security Scan |
| **Dependency Risk** | Does this change require another service to be co-released? | AI analysis of service connectivity |

### Readiness Scoring

| Score | Meaning | Action Required |
|---|---|---|
| **🟢 Ready** | All checks pass, no concerning signals | Can proceed |
| **🟡 Review** | Non-blocking concerns (e.g., major version bump, new config keys) | Release Manager should review the flagged items |
| **🔴 Risk** | Blocking issues (e.g., service crashing in UAT, security vulnerability) | Should not release until resolved |

### AI Summary (Gemini generates this)

![AI Readiness Summary](diagrams/release_ai_readiness.png)

---

## Feature 4: Release Manifest Export

Once the Release Manager approves, one-click export generates:

### A. Structured Release Manifest

```yaml
release:
  name: "Release 2026-03-14"
  cutoff: "2026-03-12T17:00:00Z"
  approved_by: "@release-manager"
  services:
    - name: billing-service
      image: registry.example.com/billing:v2.3.3
      helm_chart: billing-chart-0.5.0
      change_type: patch
      nominated_by: "@rajesh"
      readiness: green
    - name: payment-gateway
      image: registry.example.com/payment:v2.0.0
      helm_chart: payment-chart-0.4.0
      change_type: major
      nominated_by: "@john"
      readiness: yellow
      notes: "3 new config keys — verify in prod"
```

### B. AI-Generated Release Notes (for Jira/Teams)

```markdown
## Release Notes — March 14, 2026

**5 services updated:**

| Service | Version | Change | Notes |
|---|---|---|---|
| billing-service | v2.3.1 → v2.3.3 | Patch | Payment retry bug fix |
| payment-gateway | v1.8.2 → v2.0.0 | Major | New retry/timeout config |
| user-service | v3.1.0 → v3.2.0 | Minor | Profile update feature |
| order-service | v1.5.1 → v1.5.2 | Patch | Logging improvement |

**AI Risk Assessment:** 🟡 Review payment-gateway config keys in production.
```

### C. Jira Ticket Auto-Update (Phase 2)
If Jira API is available, automatically update the release ticket with the manifest.

---

## Feature 5: Release History & Analytics

Over time, the dashboard builds a release history:

![Release History](diagrams/release_history.png)

**AI can analyze patterns:**
- "billing-service has had issues in 3 of the last 5 releases — recommend additional testing"
- "Average release size: 4.5 services. Last release was larger than usual (8 services)"
- "notification-svc has been held from release twice — investigate systemic stability issues"

---

## Data Storage Design

| Option | Pros | Cons | Recommendation |
|---|---|---|---|
| **ConfigMap** | Native K8s, version-controlled, no external deps | 1MB size limit, no query capability | ✅ **Phase 1** — simple, fits well |
| **SQLite** | Query history, unlimited size, audit trail | Needs persistent volume | Phase 2 if history grows |
| **External DB** | Full featured, shared across instances | Additional infrastructure | Only if multi-instance needed |

**Phase 1 approach:** Store nominations as a ConfigMap:
```yaml
apiVersion: v1
kind: ConfigMap
metadata:
  name: release-board-2026-03-14
  labels:
    app: gdc-dashboard
    release-date: "2026-03-14"
data:
  manifest.json: |
    { "cutoff": "...", "services": [...], "status": "open" }
```

---

## Implementation Phases

### Phase 1: Release Board + Nomination (Core)
- New "Release" tab in the UI
- Service nomination from live cluster dropdown
- Auto-fill image tag and Helm version from K8s API
- Version drift detection (compare nomination vs. live UAT)
- Re-nomination with audit trail
- Basic cutoff enforcement (open/closed)
- ConfigMap-based storage

### Phase 2: AI Readiness Check
- Per-service Gemini health/stability analysis on nomination
- Readiness scoring (🟢/🟡/🔴) with AI explanation
- Automatic re-check when version is updated
- Release summary with AI risk assessment

### Phase 3: Export & Integration
- Release manifest YAML export
- AI-generated release notes (Markdown)
- Formatted output for Teams/Slack
- Production snapshot storage (for version diff in next cycle)

### Phase 4: History, Analytics & Jira
- Release history with trend analysis
- AI pattern detection across releases
- Jira API integration for auto-ticket-update
- Rollback tracking

---

## API Endpoints (Planned)

| Endpoint | Method | Description |
|---|---|---|
| `/api/release/current` | `GET` | Get current release board |
| `/api/release/nominate` | `POST` | Nominate a service |
| `/api/release/update` | `PUT` | Update a nomination version |
| `/api/release/remove` | `DELETE` | Remove a nomination |
| `/api/release/finalize` | `POST` | Lock the board at cutoff |
| `/api/release/export` | `GET` | Export release manifest |
| `/api/ai/release_readiness` | `POST` | AI readiness check for all nominated services |
| `/api/ai/release_notes` | `POST` | Generate AI release notes |
| `/api/release/history` | `GET` | Past releases |
| `/api/release/snapshot` | `POST` | Save production versions post-release |
