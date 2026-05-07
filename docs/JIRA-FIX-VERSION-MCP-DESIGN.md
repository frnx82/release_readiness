# Jira Fix Version MCP — Auto Release Notes Design

> **Status:** Draft — Pending implementation  
> **Created:** 2026-05-07  
> **Project:** Release Readiness Dashboard

---

## Overview

Instead of manually entering Jira IDs during nomination or scraping GitHub commits, use **Jira Fix Versions** as the single source of truth. A Jira Release MCP server would:

1. Accept a Fix Version name (e.g., `"Release 2024-05-09"`)
2. Pull all tickets tagged with that version from Jira
3. Read each ticket's description, type, status, and priority
4. Generate structured release notes automatically

## Why Fix Version Approach Is Better

| Approach | Pros | Cons |
|----------|------|------|
| **Manual Jira entry** | Simple | Error-prone, slow, incomplete |
| **GitHub commit scraping** | Automated | Needs repo mapping, Git tag matching, mono-repo complexity |
| **Jira Fix Version (this)** | No repo mapping, richer data, cross-team, already organized | Requires teams to tag Fix Versions in Jira |

---

## MCP Server: Required Tools

### Tool 1: `list_fix_versions`

**Purpose:** List all fix versions for a Jira project so the user can select one.

```python
@mcp.tool()
def list_fix_versions(project_key: str) -> list:
    """List all fix versions for a Jira project.
    
    Args:
        project_key: Jira project key (e.g., "BILL", "PAY")
    
    Returns:
        List of versions with name, released status, release date
    """
    # Jira API: GET /rest/api/2/project/{project_key}/versions
    # Response: [
    #   {"id": "10100", "name": "Release 2.4.0", "released": false, "releaseDate": "2024-05-09"},
    #   {"id": "10099", "name": "Release 2.3.0", "released": true, "releaseDate": "2024-04-25"},
    # ]
```

---

### Tool 2: `get_issues_by_fix_version`

**Purpose:** Get all issues tagged with a specific fix version.

```python
@mcp.tool()
def get_issues_by_fix_version(fix_version: str, project_key: str = None, max_results: int = 100) -> list:
    """Get all Jira issues tagged with a specific fix version.
    
    Args:
        fix_version: Fix version name (e.g., "Release 2.4.0")
        project_key: Optional project filter (e.g., "BILL")
        max_results: Max issues to return (default 100)
    
    Returns:
        List of issues with key, summary, type, status, priority, assignee, components
    """
    # JQL: fixVersion = "Release 2.4.0" [AND project = "BILL"]
    # Jira API: POST /rest/api/2/search
    # Body: {"jql": "fixVersion = \"Release 2.4.0\"", "maxResults": 100,
    #        "fields": ["summary", "issuetype", "status", "priority", "assignee", "components"]}
```

---

### Tool 3: `get_issue_details`

**Purpose:** Get full details of a single Jira issue including the description body.

```python
@mcp.tool()
def get_issue_details(issue_key: str) -> dict:
    """Get full details of a Jira issue including description.
    
    Args:
        issue_key: Jira issue key (e.g., "BILL-101")
    
    Returns:
        Full issue details: key, summary, description, status, type, priority,
        labels, components, fix versions, linked issues, assignee
    """
    # Jira API: GET /rest/api/2/issue/{issue_key}
    # Fields: summary, description, issuetype, status, priority,
    #         labels, components, fixVersions, issuelinks, assignee
```

---

### Tool 4: `generate_release_summary`

**Purpose:** High-level composite tool — fetches all issues for a fix version, reads their descriptions, and produces grouped release notes.

```python
@mcp.tool()
def generate_release_summary(fix_version: str, project_key: str = None) -> str:
    """Generate formatted release notes from all issues in a fix version.
    
    Groups issues by type (Feature, Bug, Improvement, Task) and includes
    summaries and descriptions for each.
    
    Args:
        fix_version: Fix version name
        project_key: Optional project filter
    
    Returns:
        Markdown-formatted release notes
    """
    # Implementation:
    # 1. Call get_issues_by_fix_version(fix_version, project_key)
    # 2. For each issue, call get_issue_details(issue.key)
    # 3. Group by issue type:
    #    - Story/Feature → "🆕 New Features"
    #    - Bug           → "🐛 Bug Fixes"
    #    - Improvement   → "⚡ Improvements"
    #    - Task/Sub-task → "🔧 Maintenance"
    # 4. Format as markdown with descriptions
```

**Example Output:**
```markdown
## Release Notes — Release 2.4.0 (2024-05-09)

**18 issues resolved across 4 projects**

### 🆕 New Features (6)
- **BILL-102**: Multi-currency support for EU markets
  Added EUR, GBP, and CHF alongside USD. Includes exchange rate API integration.
- **PAY-201**: Stripe Connect for marketplace payouts
  Direct payouts to marketplace sellers with onboarding flow and scheduling.
- **AUTH-301**: OIDC token refresh flow
  Automatic token refresh 5 minutes before expiry.

### 🐛 Bug Fixes (5)
- **BILL-101**: Fix decimal rounding in invoice calculations [Critical]
  Switched to Decimal type for all monetary calculations.
- **PAY-202**: Payment retry logic for declined cards [Critical]
  Exponential backoff retry with configurable max attempts.

### ⚡ Improvements (4)
- **INV-501**: Optimize inventory sync for large catalogs
  Delta sync and parallel processing for 100k+ SKU catalogs.

### 🔧 Maintenance (3)
- **DEVOPS-701**: Liveness/readiness probe tuning
  Reduces false-positive restarts by 60%.
```

---

### Tool 5: `search_issues`

**Purpose:** Flexible JQL search for advanced queries (e.g., unreleased issues, sprint-scoped, etc.)

```python
@mcp.tool()
def search_issues(jql: str, max_results: int = 50) -> list:
    """Execute a raw JQL query against Jira.
    
    Args:
        jql: JQL query string (e.g., "project = BILL AND fixVersion = '2.4.0' AND status = Done")
        max_results: Max issues to return
    
    Returns:
        List of matching issues with key, summary, type, status
    """
    # Jira API: POST /rest/api/2/search
    # Body: {"jql": jql, "maxResults": max_results}
```

---

## Integration Flow

```
┌──────────────────────────────────────────────────────────────┐
│                    Release Readiness Dashboard               │
│                                                              │
│  Fix Version: [ Release 2.4.0          ▼ ]                   │
│               [🔍 Pull Jira Tickets]                         │
│                                                              │
│  Found 18 tickets:                                           │
│  ┌────────┬───────────────────────────────┬──────┬─────────┐ │
│  │ Key    │ Summary                       │ Type │ Status  │ │
│  ├────────┼───────────────────────────────┼──────┼─────────┤ │
│  │BILL-101│ Fix decimal rounding          │ Bug  │ Done    │ │
│  │BILL-102│ Multi-currency support        │Story │ Done    │ │
│  │PAY-201 │ Stripe Connect integration    │Story │ Done    │ │
│  │AUTH-301│ OIDC token refresh            │Story │ Done    │ │
│  │...     │ (14 more)                     │      │         │ │
│  └────────┴───────────────────────────────┴──────┴─────────┘ │
│                                                              │
│  [🤖 Generate AI Release Notes]                              │
│                                                              │
└──────────────────────────────────────────────────────────────┘
```

### Step-by-step:

1. **User selects Fix Version** from a dropdown (populated by `list_fix_versions`)
2. **Dashboard calls MCP** → `get_issues_by_fix_version("Release 2.4.0")`
3. **Table shows all tickets** — user can review, deselect irrelevant ones
4. **User clicks "Generate AI Release Notes"**
5. **MCP fetches descriptions** → `get_issue_details` for each selected ticket
6. **AI generates grouped notes** with summaries from Jira descriptions
7. **Release notes are attached** to the release board

---

## Configuration

```yaml
# MCP Server config
JIRA_BASE_URL: "https://your-org.atlassian.net"
JIRA_AUTH_TOKEN: "<api-token>"           # or OAuth
JIRA_DEFAULT_PROJECTS: "BILL,PAY,AUTH,ORD,INV"  # projects to scan
```

## Jira API Authentication Options

| Method | Best For |
|--------|----------|
| **API Token** (Basic Auth) | Jira Cloud — simplest setup |
| **OAuth 2.0** | Jira Cloud — enterprise/SSO environments |
| **Personal Access Token** | Jira Data Center / Server (on-prem) |

---

## Advantages Over GitHub Commit Approach

1. **No repo mapping needed** — Fix Versions span all repos and projects
2. **Richer context** — Full descriptions, acceptance criteria, not just commit one-liners  
3. **Already organized** — Issue types (Story/Bug/Task) give natural grouping
4. **Cross-team visibility** — One Fix Version captures work from all teams
5. **Status awareness** — Can filter by Done/In Progress to flag incomplete work
6. **Less config** — Just Jira URL + token, no per-service GitHub repo mapping
7. **Works for non-code changes** — Process changes, config updates, documentation

---

## Future Enhancements

- **Auto-suggest Fix Version** based on release date match
- **Flag incomplete tickets** — Warn if any tickets in the Fix Version are not "Done"
- **Link to Confluence** — Auto-publish release notes to a Confluence page
- **Slack notification** — Post release notes to a release channel
- **Historical comparison** — Compare Fix Versions across releases for trend analysis
