# Confluence Agent — Design Document

> **Status:** Draft — Pending review  
> **Created:** 2026-05-18  
> **Project:** Release Readiness Dashboard  
> **Pattern:** Follows existing Jira MCP integration architecture

---

## Overview

Add a **📖 Confluence** tab to the Release Readiness dashboard that connects to the organization's Confluence instance via MCP (Model Context Protocol), enabling AI-powered search, summarization, and contextual knowledge retrieval — all without leaving the dashboard.

### Why This Matters

| Today (Manual) | With Confluence Agent |
|---|---|
| Switch to Confluence, search manually, browse pages | Type a question in natural language, get answers instantly |
| Copy-paste runbook steps during incidents | AI surfaces the relevant runbook automatically based on the release context |
| Forget which Confluence space has the deployment guide | Agent searches across ALL spaces you have access to |
| Release notes scattered across Confluence pages | Agent links release board data to relevant Confluence docs |
| New team members don't know where docs live | Just ask — "Where's the rollback procedure?" |

---

## Architecture

```
┌──────────────────────────────────────────────────────────────┐
│               Release Readiness Dashboard                     │
│                                                              │
│  ┌──────────┐  ┌──────────┐  ┌──────────────────────────┐   │
│  │ 📋 Board │  │ 💬 Chat  │  │ 📖 Confluence Agent      │   │
│  └────┬─────┘  └────┬─────┘  │                          │   │
│       │              │        │  [🔍 Search bar      ]   │   │
│       │              │        │  [Quick actions pills ]   │   │
│       │              │        │  [AI-powered results  ]   │   │
│       │              │        │  [Page preview cards  ]   │   │
│       │              │        └──────────┬───────────────┘   │
└───────┼──────────────┼──────────────────┼────────────────────┘
        │              │                  │
        │ (existing)   │ (existing)       │ JSON-RPC / MCP
        ▼              ▼                  ▼
  ┌──────────┐   ┌──────────┐   ┌──────────────────────┐
  │ K8s API  │   │ Gemini   │   │  Confluence MCP      │
  │ (cluster)│   │ (Vertex) │   │  Server              │
  └──────────┘   └──────────┘   │                      │
                                │  Tools:              │
                                │  • search_pages      │
                                │  • get_page_content  │
                                │  • search_by_label   │
                                │  • get_space_pages   │
                                │  • get_child_pages   │
                                │  • get_page_comments │
                                └──────────┬───────────┘
                                           │ REST API
                                           ▼
                                ┌──────────────────────┐
                                │  Confluence Cloud /   │
                                │  Data Center          │
                                │  (your-org.atlassian  │
                                │   .net)               │
                                └──────────────────────┘
```

---

## MCP Server Tools

### Tool 1: `confluence_search`

**Purpose:** Full-text search across all accessible Confluence spaces using CQL (Confluence Query Language).

```python
@mcp.tool()
def confluence_search(query: str, space_key: str = None, 
                      content_type: str = "page", max_results: int = 10) -> list:
    """Search Confluence using CQL (Confluence Query Language).
    
    Args:
        query: Search text (natural language or CQL)
        space_key: Optional space key to restrict search (e.g., "DEV", "OPS")
        content_type: "page", "blogpost", or "all"
        max_results: Maximum results to return (default 10)
    
    Returns:
        List of {id, title, space, url, excerpt, last_modified, author}
    """
    # Confluence API: GET /rest/api/content/search
    # CQL: text ~ "query" [AND space = "KEY"] AND type = "page"
```

### Tool 2: `confluence_get_page`

**Purpose:** Retrieve full page content (body, metadata, labels, version).

```python
@mcp.tool()
def confluence_get_page(page_id: str = None, title: str = None, 
                        space_key: str = None) -> dict:
    """Get a specific Confluence page by ID or by title+space.
    
    Args:
        page_id: Page ID (numeric)
        title: Page title (used with space_key)
        space_key: Space key (used with title)
    
    Returns:
        {id, title, space, body_html, body_text, labels, url, 
         version, last_modified, author, ancestors}
    """
    # Confluence API: GET /rest/api/content/{id}?expand=body.storage,version,ancestors
```

### Tool 3: `confluence_search_by_label`

**Purpose:** Find all pages with specific labels (e.g., `runbook`, `release-process`, `deployment`).

```python
@mcp.tool()
def confluence_search_by_label(labels: list, space_key: str = None, 
                                max_results: int = 20) -> list:
    """Find pages tagged with specific labels.
    
    Args:
        labels: List of label names (e.g., ["runbook", "deployment"])
        space_key: Optional space key filter
        max_results: Max results
    
    Returns:
        List of {id, title, space, url, labels, excerpt}
    """
    # CQL: label IN ("runbook", "deployment") [AND space = "KEY"]
```

### Tool 4: `confluence_get_space_pages`

**Purpose:** List all pages in a Confluence space (top-level or full tree).

```python
@mcp.tool()
def confluence_get_space_pages(space_key: str, depth: str = "root") -> list:
    """List pages in a Confluence space.
    
    Args:
        space_key: Space key (e.g., "DEV", "OPS", "REL")
        depth: "root" for top-level only, "all" for full tree
    
    Returns:
        List of {id, title, url, children_count, last_modified}
    """
    # Confluence API: GET /rest/api/space/{key}/content/page
```

### Tool 5: `confluence_get_child_pages`

**Purpose:** Navigate page hierarchy (useful for structured runbooks/wikis).

```python
@mcp.tool()
def confluence_get_child_pages(page_id: str) -> list:
    """Get child pages of a given parent page.
    
    Args:
        page_id: Parent page ID
    
    Returns:
        List of child {id, title, url, has_children}
    """
    # Confluence API: GET /rest/api/content/{id}/child/page
```

### Tool 6: `confluence_get_comments`

**Purpose:** Get discussion/comments on a page (useful for decision context).

```python
@mcp.tool()
def confluence_get_comments(page_id: str, max_results: int = 20) -> list:
    """Get comments/discussion on a Confluence page.
    
    Args:
        page_id: Page ID
        max_results: Max comments to return
    
    Returns:
        List of {id, author, body_text, created_date}
    """
    # Confluence API: GET /rest/api/content/{id}/child/comment
```

---

## Frontend: Confluence Tab Design

### Tab Layout

```
┌─────────────────────────────────────────────────────────────────┐
│ 📖 Confluence Agent                                    🔄 Clear │
│                                                                 │
│ ┌─────────────────────────────────────────────────────────────┐ │
│ │ 🔍 Ask about your org's documentation...                    │ │
│ │    (e.g., "rollback procedure for billing service")         │ │
│ └─────────────────────────────────────────────────────────────┘ │
│                                                                 │
│ Quick Actions:                                                  │
│ ┌──────────────┐ ┌──────────────┐ ┌────────────────────────┐   │
│ │📚 Runbooks   │ │🚀 Deploy Docs│ │📋 Release Procedures   │   │
│ └──────────────┘ └──────────────┘ └────────────────────────┘   │
│ ┌──────────────┐ ┌──────────────┐ ┌────────────────────────┐   │
│ │🔧 Troubleshoot│ │📊 Architecture│ │🏷️ Search by Label     │   │
│ └──────────────┘ └──────────────┘ └────────────────────────┘   │
│                                                                 │
│ ─── Results ────────────────────────────────────────────────── │
│                                                                 │
│ ┌─────────────────────────────────────────────────────────────┐ │
│ │ 🤖 AI Summary                                               │ │
│ │ Based on 3 Confluence pages, here's what I found:           │ │
│ │                                                             │ │
│ │ The rollback procedure for billing-service involves:        │ │
│ │ 1. Check current deployment with `kubectl get deploy`       │ │
│ │ 2. Execute rollback: `kubectl rollout undo deploy/billing`  │ │
│ │ 3. Verify health: monitor /health endpoint for 5 min        │ │
│ │                                                             │ │
│ │ ⚠️ Note: Database migrations cannot be rolled back          │ │
│ │    automatically — contact DBA team.                        │ │
│ └─────────────────────────────────────────────────────────────┘ │
│                                                                 │
│ 📄 Source Pages:                                                │
│ ┌─────────────────────────────────────────────────────────────┐ │
│ │ 📖 Billing Service — Deployment Runbook                     │ │
│ │ Space: DEV • Last updated: 3 days ago • By: john.doe        │ │
│ │ Labels: runbook, billing, deployment                        │ │
│ │ "...Step 1: Verify UAT → Step 2: Lock board..."            │ │
│ │ [👁 Preview] [🔗 Open in Confluence]                        │ │
│ ├─────────────────────────────────────────────────────────────┤ │
│ │ 📖 Production Release Checklist v3.2                        │ │
│ │ Space: OPS • Last updated: 1 week ago • By: jane.smith      │ │
│ │ Labels: release, checklist, production                      │ │
│ │ "...Pre-release: Run version drift → AI readiness..."       │ │
│ │ [👁 Preview] [🔗 Open in Confluence]                        │ │
│ └─────────────────────────────────────────────────────────────┘ │
│                                                                 │
│ ┌─────────────────────────────────────────────────────────────┐ │
│ │ Ask a follow-up question...                          Send ➤ │ │
│ └─────────────────────────────────────────────────────────────┘ │
└─────────────────────────────────────────────────────────────────┘
```

### Feature Breakdown

#### 1. 🔍 AI-Powered Natural Language Search
- User types: *"How do I rollback billing service?"*
- Backend converts to CQL, fetches top results via MCP
- Gemini reads page contents and generates a **synthesized answer** with citations

#### 2. 🏷️ Quick Action Pills (Pre-built Searches)
| Pill | What it does |
|---|---|
| 📚 Runbooks | `label = "runbook"` → shows all operational runbooks |
| 🚀 Deploy Docs | `label IN ("deployment", "deploy", "cd")` |
| 📋 Release Procedures | `text ~ "release process" OR label = "release"` |
| 🔧 Troubleshooting | `label = "troubleshooting" OR text ~ "troubleshoot"` |
| 📊 Architecture | `label IN ("architecture", "design", "adr")` |
| 🏷️ Search by Label | Opens a label input field for custom label search |

#### 3. 📄 Page Preview Cards
Each result shows:
- **Title** with emoji based on labels (📚 for runbooks, 🚀 for deploy, etc.)
- **Space** badge (DEV, OPS, REL)
- **Last updated** timestamp + author
- **Labels** as colored pills
- **Excerpt** — first 200 chars with search terms highlighted
- **[👁 Preview]** — Expands inline to show full rendered page content (HTML → styled)
- **[🔗 Open in Confluence]** — Direct link to the actual Confluence page

#### 4. 🤖 AI Summary Panel
When results are found, Gemini generates:
- A **concise answer** synthesized from all matching pages
- **Step-by-step instructions** if the query is procedural
- **⚠️ Warnings/caveats** extracted from the page content
- **Source citations** linking back to specific Confluence pages

#### 5. 💬 Conversational Follow-ups
After the first search, users can ask follow-up questions:
- *"What about the database migration step?"*
- *"Who last updated this runbook?"*
- *"Show me the architecture diagram for this service"*

The agent maintains context from the previous search.

#### 6. 📋 Release-Contextual Suggestions
The agent automatically suggests relevant searches based on what's on the release board:
- If `billing-service` is nominated → suggest *"billing-service runbook"*
- If board is locked → suggest *"release checklist"*
- If AI readiness shows 🔴 Risk → suggest *"troubleshooting [service-name]"*

---

## Backend Implementation

### Configuration (env vars)

```bash
# Confluence MCP Server connection
CONFLUENCE_MCP_URL=https://confluence-mcp.your-org.com    # MCP server endpoint

# Direct Confluence API (fallback, same as Jira dual-mode)
CONFLUENCE_BASE_URL=https://your-org.atlassian.net/wiki   # Confluence instance
CONFLUENCE_EMAIL=svc-account@company.com                  # Service account email
CONFLUENCE_PAT_TOKEN=<api-token>                          # API token or PAT

# Optional: restrict to specific spaces
CONFLUENCE_DEFAULT_SPACES=DEV,OPS,REL,INFRA               # Comma-separated space keys
```

### Python: MCP Client (follows Jira pattern)

```python
# ── Confluence MCP Client ─────────────────────────────────────────────────────
CONFLUENCE_MCP_URL    = os.getenv('CONFLUENCE_MCP_URL', '')
CONFLUENCE_BASE_URL   = os.getenv('CONFLUENCE_BASE_URL', '')
CONFLUENCE_EMAIL      = os.getenv('CONFLUENCE_EMAIL', os.getenv('JIRA_EMAIL', ''))
CONFLUENCE_PAT_TOKEN  = os.getenv('CONFLUENCE_PAT_TOKEN', os.getenv('JIRA_PAT_TOKEN', ''))
CONFLUENCE_SPACES     = [s.strip() for s in os.getenv('CONFLUENCE_DEFAULT_SPACES', '').split(',') if s.strip()]


def _confluence_mcp_call(tool_name, arguments, timeout=10):
    """Call a tool on the Confluence MCP server via HTTP.
    Follows same JSON-RPC pattern as _jira_mcp_call().
    """
    if not CONFLUENCE_MCP_URL:
        return None
    # ... (identical pattern to _jira_mcp_call)


def _confluence_search(query, space_key=None, max_results=10):
    """Search Confluence via MCP, with REST API fallback."""
    # Method 1: MCP
    if CONFLUENCE_MCP_URL:
        raw = _confluence_mcp_call('confluence_search', {
            'query': query, 'space_key': space_key, 'max_results': max_results
        })
        if raw:
            return _parse_confluence_results(raw)

    # Method 2: Direct REST API
    if CONFLUENCE_BASE_URL:
        return _confluence_rest_search(query, space_key, max_results)

    return []


def _confluence_get_page(page_id):
    """Get full page content via MCP, with REST API fallback."""
    if CONFLUENCE_MCP_URL:
        raw = _confluence_mcp_call('confluence_get_page', {'page_id': page_id})
        if raw:
            return _parse_confluence_page(raw)

    if CONFLUENCE_BASE_URL:
        return _confluence_rest_get_page(page_id)

    return None
```

### Flask Routes

```python
@app.route('/api/confluence/search', methods=['POST'])
def confluence_search():
    """Search Confluence and optionally generate AI summary."""
    data = request.json
    query = data.get('query', '')
    space = data.get('space_key')
    ai_summary = data.get('ai_summary', True)
    
    # Search via MCP/REST
    results = _confluence_search(query, space)
    
    # Optional: AI summarization
    summary = None
    if ai_summary and results:
        # Fetch full content for top 3 results
        pages_text = []
        for r in results[:3]:
            page = _confluence_get_page(r['id'])
            if page:
                pages_text.append(f"## {page['title']}\n{page['body_text'][:2000]}")
        
        if pages_text:
            prompt = f"""Based on these Confluence pages, answer the user's question.
            
Question: {query}

Pages:
{chr(10).join(pages_text)}

Provide a concise, actionable answer. Include step-by-step instructions if applicable.
Cite which page each piece of information came from.
Flag any warnings or caveats."""
            
            response = gemini_generate_with_retry(prompt)
            if response:
                summary = response.text
    
    return jsonify({
        'results': results,
        'ai_summary': summary,
        'query': query,
        'total': len(results)
    })


@app.route('/api/confluence/page/<page_id>')
def confluence_page(page_id):
    """Get full page content for preview."""
    page = _confluence_get_page(page_id)
    if not page:
        return jsonify({'error': 'Page not found'}), 404
    return jsonify(page)


@app.route('/api/confluence/labels', methods=['POST'])
def confluence_by_labels():
    """Search by label(s)."""
    data = request.json
    labels = data.get('labels', [])
    space = data.get('space_key')
    
    if CONFLUENCE_MCP_URL:
        raw = _confluence_mcp_call('confluence_search_by_label', {
            'labels': labels, 'space_key': space
        })
        if raw:
            return jsonify({'results': _parse_confluence_results(raw)})
    
    # Fallback: CQL search
    cql = ' OR '.join(f'label = "{l}"' for l in labels)
    results = _confluence_search(cql, space)
    return jsonify({'results': results})


@app.route('/api/confluence/suggest')
def confluence_suggestions():
    """Generate contextual suggestions based on current board state."""
    board = _load_board()
    suggestions = []
    
    # Suggest runbooks for nominated services
    for svc in board.get('services', []):
        name = svc.get('name', '')
        suggestions.append({
            'query': f'{name} runbook',
            'label': f'📚 {name} runbook',
            'reason': f'{name} is nominated for release'
        })
    
    # If board is locked, suggest release checklist
    if board.get('locked'):
        suggestions.insert(0, {
            'query': 'release checklist production',
            'label': '📋 Release checklist',
            'reason': 'Board is locked — review release procedure'
        })
    
    return jsonify({'suggestions': suggestions[:6]})
```

---

## Confluence REST API Fallback

If no MCP server is available, the dashboard connects directly to Confluence REST API (same dual-mode pattern as Jira):

```python
def _confluence_rest_search(query, space_key=None, max_results=10):
    """Direct Confluence REST API search."""
    cql = f'text ~ "{query}" AND type = "page"'
    if space_key:
        cql += f' AND space = "{space_key}"'
    
    url = f'{CONFLUENCE_BASE_URL}/rest/api/content/search'
    headers = _confluence_auth_headers()
    params = {
        'cql': cql,
        'limit': max_results,
        'expand': 'space,version,metadata.labels'
    }
    
    resp = requests.get(url, headers=headers, params=params,
                        timeout=15, verify=SSL_VERIFY)
    resp.raise_for_status()
    
    results = []
    for item in resp.json().get('results', []):
        results.append({
            'id': item['id'],
            'title': item['title'],
            'space': item.get('space', {}).get('key', '?'),
            'space_name': item.get('space', {}).get('name', ''),
            'url': f"{CONFLUENCE_BASE_URL}{item.get('_links', {}).get('webui', '')}",
            'excerpt': item.get('excerpt', ''),
            'last_modified': item.get('version', {}).get('when', ''),
            'author': item.get('version', {}).get('by', {}).get('displayName', ''),
            'labels': [l['name'] for l in item.get('metadata', {}).get('labels', {}).get('results', [])]
        })
    return results
```

---

## Implementation Priority

| Phase | Feature | Effort |
|---|---|---|
| **Phase 1** | Search + page preview + direct links | 🟢 Low |
| **Phase 2** | AI summarization (Gemini reads pages) | 🟡 Medium |
| **Phase 3** | Contextual suggestions from board state | 🟡 Medium |
| **Phase 4** | Conversational follow-ups (stateful chat) | 🟡 Medium |
| **Phase 5** | Auto-link runbooks to nominated services | 🔴 High |

---

## RBAC / Security

- **Authentication**: Same token used for Jira works for Confluence (Atlassian Cloud)
- **Space Permissions**: MCP server respects Confluence space permissions — users only see what they have access to
- **No Write Access**: All MCP tools are read-only — no page creation, editing, or deletion
- **Token Scope**: `read:confluence-content.all` is the only required OAuth scope

---

## Comparison: Jira MCP vs Confluence MCP

| Aspect | Jira MCP (existing) | Confluence MCP (proposed) |
|---|---|---|
| **Data type** | Structured (tickets, fields) | Unstructured (wiki pages, rich text) |
| **Query language** | JQL | CQL |
| **Primary use** | Pull ticket details for release notes | Search & surface knowledge |
| **AI role** | Generate release notes from tickets | Summarize pages, answer questions |
| **Auth** | Same Atlassian token | Same Atlassian token |
| **MCP client** | `_jira_mcp_call()` | `_confluence_mcp_call()` (identical pattern) |
| **Fallback** | Direct Jira REST API | Direct Confluence REST API |

---

## Tab Placement

```
📋 Board │ 🔀 Drift │ 🤖 AI │ 📜 Audit │ 📦 Export │ 🖥️ UAT │ 🏭 Prod │ 📚 History │ 🚀 Deploy │ 📖 Confluence │ 💬 Chat │ ❓ Help
```

The Confluence tab goes between Deploy and Chat — making it a knowledge layer alongside the existing AI Chat.

---

*Last updated: 2026-05-18*
