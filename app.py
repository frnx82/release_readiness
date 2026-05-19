"""
Release Readiness Dashboard — AI-Powered Release Board
=====================================================
A standalone Flask application for operations/support teams to manage
release coordination with AI-powered readiness checks.

Features:
  - Release Board: nominate services from live K8s cluster
  - Version Drift Detection: compare nominations vs live cluster
  - AI Readiness Check: Gemini-powered per-service health analysis
  - Release Manifest Export: YAML + AI-generated release notes
  - ConfigMap-based storage: no external DB required
"""

from gevent import monkey
monkey.patch_all()

import os
import json
import re
import time
import datetime
import threading
import uuid
import random
import secrets
import base64
import yaml
import requests
from flask import Flask, request, jsonify, render_template, session, redirect, url_for
from flask_socketio import SocketIO
from kubernetes import client, config
from functools import wraps
from urllib.parse import urlencode
from werkzeug.middleware.proxy_fix import ProxyFix

# ══════════════════════════════════════════════════════════════════════════════
# GitHub / Deploy Configuration
# ══════════════════════════════════════════════════════════════════════════════
GITHUB_CLIENT_ID     = os.getenv('GITHUB_CLIENT_ID', '')
GITHUB_CLIENT_SECRET = os.getenv('GITHUB_CLIENT_SECRET', '')
GITHUB_TOKEN         = os.getenv('GITHUB_TOKEN', '')  # PAT fallback
DEPLOY_REPO          = os.getenv('DEPLOY_REPO', '')   # e.g. org/app-deployment
DEPLOY_WORKFLOW      = os.getenv('DEPLOY_WORKFLOW', 'deploy.yml')
BASE_URL             = os.getenv('BASE_URL', '').rstrip('/')  # external URL for OAuth

# ══════════════════════════════════════════════════════════════════════════════
# Jira Configuration
# ══════════════════════════════════════════════════════════════════════════════
JIRA_MCP_URL          = os.getenv('JIRA_MCP_URL', '')            # MCP server endpoint URL
JIRA_BASE_URL         = os.getenv('JIRA_BASE_URL', '')           # Jira instance URL (e.g. https://jira.company.com)
JIRA_EMAIL            = os.getenv('JIRA_EMAIL',
                        os.getenv('JIRA_SERVICE_ACCOUNT', ''))   # Jira email / service account
JIRA_PAT_TOKEN        = os.getenv('JIRA_PAT_TOKEN', '')          # Jira PAT token

# ══════════════════════════════════════════════════════════════════════════════
# Confluence Configuration
# ══════════════════════════════════════════════════════════════════════════════
CONFLUENCE_MCP_URL    = os.getenv('CONFLUENCE_MCP_URL', '')        # MCP server endpoint URL
CONFLUENCE_BASE_URL   = os.getenv('CONFLUENCE_BASE_URL', '').rstrip('/')  # e.g. https://your-org.atlassian.net/wiki
CONFLUENCE_EMAIL      = os.getenv('CONFLUENCE_EMAIL',
                        os.getenv('JIRA_EMAIL', ''))               # Reuse Jira email by default
CONFLUENCE_PAT_TOKEN  = os.getenv('CONFLUENCE_PAT_TOKEN',
                        os.getenv('JIRA_PAT_TOKEN', ''))           # Reuse Jira PAT by default
CONFLUENCE_SPACES     = [s.strip() for s in
                        os.getenv('CONFLUENCE_DEFAULT_SPACES', '').split(',') if s.strip()]

# GitHub Enterprise support
GITHUB_URL = os.getenv('GITHUB_URL', 'https://github.com').rstrip('/')
if GITHUB_URL == 'https://github.com':
    GITHUB_API = 'https://api.github.com'
else:
    GITHUB_API = f'{GITHUB_URL}/api/v3'

# SSL verification
SSL_VERIFY = os.getenv('SSL_VERIFY', 'true').lower() not in ('false', '0', 'no')
if not SSL_VERIFY:
    import urllib3
    urllib3.disable_warnings(urllib3.exceptions.InsecureRequestWarning)
    print('[Release Readiness] ⚠️  SSL verification DISABLED')

# Release history archive
_release_history = []  # archived boards from previous release cycles

# Proxy + Kerberos (same pattern as Pipeline Hub)
PROXY_URL = os.getenv('PROXY_URL', '')

def _build_gh_session():
    """Build a requests.Session with optional Kerberos proxy authentication."""
    s = requests.Session()
    s.verify = SSL_VERIFY
    if PROXY_URL:
        s.proxies = {'http': PROXY_URL, 'https': PROXY_URL}
        try:
            from requests_kerberos import HTTPKerberosAuth, OPTIONAL
            from requests.adapters import HTTPAdapter
            adapter = HTTPAdapter(max_retries=3)
            s.mount('https://', adapter)
            s.mount('http://', adapter)
            s.auth = HTTPKerberosAuth(mutual_authentication=OPTIONAL)
            print(f'[Release Readiness] ✅ Kerberos proxy auth enabled — proxy: {PROXY_URL}')
        except ImportError:
            print(f'[Release Readiness] ⚠️  requests-kerberos not installed. Proxy set but Kerberos auth unavailable.')
    else:
        print('[Release Readiness] ℹ️  No PROXY_URL set — direct connections to GitHub.')
    return s

gh_http = _build_gh_session()

# Auth mode detection (same as Pipeline Hub)
GH_AUTH_MODE = 'oauth' if (GITHUB_CLIENT_ID and GITHUB_CLIENT_SECRET) else 'pat'

def get_gh_token():
    """Get the GitHub token for the current request."""
    if GH_AUTH_MODE == 'oauth':
        return session.get('github_token', '')
    return GITHUB_TOKEN

def is_gh_authenticated():
    """Check if the current user is authenticated with GitHub."""
    if GH_AUTH_MODE == 'oauth':
        return bool(session.get('github_token'))
    return bool(GITHUB_TOKEN)

def _github_headers():
    token = get_gh_token()
    return {
        'Authorization': f'token {token}',
        'Accept': 'application/vnd.github.v3+json',
        'User-Agent': 'ReleaseReadiness/1.0'
    }

def _github_get(path, params=None):
    """GET request to GitHub API."""
    url = f'{GITHUB_API}{path}'
    try:
        r = gh_http.get(url, headers=_github_headers(), params=params, timeout=15)
        r.raise_for_status()
        return r.json()
    except requests.exceptions.HTTPError as e:
        print(f"[GitHub API] HTTP {e.response.status_code} for {path}: {e.response.text[:200]}")
        raise
    except Exception as e:
        print(f"[GitHub API] Error for {path}: {e}")
        raise

def _github_post(path, data=None):
    """POST request to GitHub API."""
    url = f'{GITHUB_API}{path}'
    try:
        r = gh_http.post(url, headers=_github_headers(), json=data, timeout=15)
        return r
    except Exception as e:
        print(f"[GitHub API] POST error for {path}: {e}")
        raise

def _get_callback_url():
    """Get the OAuth callback URL (uses BASE_URL if set)."""
    if BASE_URL:
        return f'{BASE_URL}/auth/callback'
    return url_for('auth_callback', _external=True)

# Print auth mode at startup
if GH_AUTH_MODE == 'oauth':
    _cb = f'{BASE_URL}/auth/callback' if BASE_URL else '(auto-detected)'
    print(f"[Release Readiness] GitHub OAuth mode — Client ID: {GITHUB_CLIENT_ID[:8]}...")
    print(f"    GitHub URL: {GITHUB_URL} | API: {GITHUB_API}")
    print(f"    Callback: {_cb}")
    print(f"    Deploy repo: {DEPLOY_REPO or '(not set)'} | Workflow: {DEPLOY_WORKFLOW}")
elif GITHUB_TOKEN:
    print(f"[Release Readiness] GitHub PAT mode — Deploy repo: {DEPLOY_REPO or '(not set)'}")
else:
    print("[Release Readiness] ⚠️  No GitHub auth configured. Deploy features will be unavailable.")
    print("    Set GITHUB_CLIENT_ID + GITHUB_CLIENT_SECRET for OAuth mode")

# Print Jira config status at startup
if JIRA_MCP_URL:
    print(f"[Release Readiness] ✅ Jira MCP server configured — URL: {JIRA_MCP_URL}")
    if JIRA_EMAIL:
        print(f"    Jira email: {JIRA_EMAIL}")
    if JIRA_PAT_TOKEN:
        print(f"    PAT token: {'*' * 8}...configured")
    auth_mode = 'Basic Auth' if (JIRA_EMAIL and JIRA_PAT_TOKEN) else 'Bearer' if JIRA_PAT_TOKEN else 'None'
    print(f"    Auth mode: {auth_mode}")
if JIRA_BASE_URL:
    print(f"[Release Readiness] ✅ Jira REST API configured — URL: {JIRA_BASE_URL}")
    print("    Fix version JQL search will use direct REST API")
elif JIRA_MCP_URL:
    print("[Release Readiness] ⚠️  JIRA_BASE_URL not set. Fix version search will try MCP search tools.")
    print("    If MCP search fails, set JIRA_BASE_URL to your Jira instance (e.g. https://jira.company.com)")
if not JIRA_MCP_URL and not JIRA_BASE_URL:
    print("[Release Readiness] ℹ️  No Jira configured. Jira integration disabled.")
    print("    Set JIRA_MCP_URL + JIRA_EMAIL + JIRA_PAT_TOKEN for MCP mode")
    print("    Set JIRA_BASE_URL + JIRA_EMAIL + JIRA_PAT_TOKEN for direct REST API mode")

# Print Confluence config status at startup
if CONFLUENCE_MCP_URL:
    print(f"[Release Readiness] ✅ Confluence MCP configured — URL: {CONFLUENCE_MCP_URL}")
    if CONFLUENCE_EMAIL:
        print(f"    Confluence email: {CONFLUENCE_EMAIL}")
    if CONFLUENCE_PAT_TOKEN:
        print(f"    PAT token: {'*' * 8}...configured")
    if CONFLUENCE_SPACES:
        print(f"    Default spaces: {', '.join(CONFLUENCE_SPACES)}")
elif CONFLUENCE_BASE_URL:
    print(f"[Release Readiness] ✅ Confluence REST API configured — URL: {CONFLUENCE_BASE_URL}")
else:
    print("[Release Readiness] ℹ️  No Confluence configured. Confluence Agent tab will be disabled.")
    print("    Set CONFLUENCE_MCP_URL for MCP mode, or CONFLUENCE_BASE_URL for direct REST API")

# ── Gemini SDK setup ─────────────────────────────────────────────────────────
GEMINI_MODEL = os.environ.get('GEMINI_MODEL', 'gemini-2.0-flash')
_model_client = None
_model_lock = threading.Lock()

def get_model():
    global _model_client
    if _model_client is not None:
        return _model_client
    with _model_lock:
        if _model_client is not None:
            return _model_client
        try:
            from google import genai
            from google.oauth2 import service_account

            sa_key_path = os.environ.get('GOOGLE_APPLICATION_CREDENTIALS', '')
            project = (os.environ.get('GCP_PROJECT_ID')
                       or os.environ.get('GOOGLE_CLOUD_PROJECT')
                       or os.environ.get('GEMINI_PROJECT_ID'))
            location = os.environ.get('GCP_REGION',
                       os.environ.get('GOOGLE_CLOUD_LOCATION', 'us-central1'))

            if sa_key_path and project:
                # Explicit service-account JSON file (on-prem GDC)
                creds = service_account.Credentials.from_service_account_file(
                    sa_key_path,
                    scopes=["https://www.googleapis.com/auth/cloud-platform"],
                )
                sa_email = json.load(open(sa_key_path)).get("client_email", "?")
                _model_client = genai.Client(
                    vertexai=True,
                    project=project,
                    location=location,
                    credentials=creds,
                )
                print(f"[gemini] Model client initialised via SA key "
                      f"(project={project}, model={GEMINI_MODEL}, sa={sa_email})")
            elif project:
                # Fallback: Application Default Credentials (GKE Workload Identity etc.)
                _model_client = genai.Client(
                    vertexai=True,
                    project=project,
                    location=location,
                )
                print(f"[gemini] Model client initialised via ADC "
                      f"(project={project}, model={GEMINI_MODEL})")
            else:
                # Last resort: API key (non-Vertex)
                api_key = os.environ.get('GEMINI_API_KEY', '')
                if api_key:
                    _model_client = genai.Client(api_key=api_key)
                    print(f"[gemini] Model client initialised via API key "
                          f"(model={GEMINI_MODEL})")
                else:
                    print("[gemini] No credentials configured. Set GOOGLE_APPLICATION_CREDENTIALS + GCP_PROJECT_ID")
        except Exception as e:
            print(f"[gemini] Init failed: {e}")
    return _model_client


def _prewarm_model():
    time.sleep(2)
    get_model()

threading.Thread(target=_prewarm_model, daemon=True).start()


# ── Gemini helpers ────────────────────────────────────────────────────────────
def gemini_generate_with_retry(prompt, max_retries=2):
    model = get_model()
    if not model:
        return None
    last_error = None
    for attempt in range(max_retries + 1):
        try:
            return model.models.generate_content(model=GEMINI_MODEL, contents=prompt)
        except Exception as e:
            err_str = str(e).lower()
            is_transient = any(kw in err_str for kw in [
                'eof occurred', 'ssl', 'connection reset',
                'server disconnected', 'broken pipe',
                'connection refused', 'timed out', 'timeout'
            ])
            if is_transient and attempt < max_retries:
                time.sleep(1.5 * (attempt + 1))
                last_error = e
                continue
            raise
    raise last_error


def parse_gemini_json(raw_text):
    text = raw_text.strip()
    text = re.sub(r'^```(?:json)?\s*', '', text)
    text = re.sub(r'\s*```$', '', text)
    text = text.strip()
    brace_start = text.find('{')
    bracket_start = text.find('[')
    if brace_start == -1:
        start = bracket_start
    elif bracket_start == -1:
        start = brace_start
    else:
        start = min(brace_start, bracket_start)
    if start != -1:
        if text[start] == '{':
            end = text.rfind('}')
        else:
            end = text.rfind(']')
        if end > start:
            text = text[start:end + 1]
    try:
        return json.loads(text)
    except json.JSONDecodeError:
        pass
    cleaned = re.sub(r',\s*([}\]])', r'\1', text)
    try:
        return json.loads(cleaned)
    except json.JSONDecodeError:
        pass
    raise json.JSONDecodeError("Could not parse Gemini response", text[:200], 0)


# ── TTL Cache ─────────────────────────────────────────────────────────────────
_cache = {}
_CACHE_TTL = 300  # 5 minutes

def _cache_get(key):
    entry = _cache.get(key)
    if entry and (time.time() - entry['ts'] < _CACHE_TTL):
        return entry['data']
    return None

def _cache_set(key, data):
    _cache[key] = {'data': data, 'ts': time.time()}


# ── Jira MCP Client ───────────────────────────────────────────────────────────
_JIRA_ID_PATTERN = re.compile(r'^[A-Z][A-Z0-9]+-\d+$')

def _parse_jira_ids(jira_ids_str):
    """Parse a comma-separated Jira IDs string into a list of clean IDs."""
    if not jira_ids_str:
        return []
    raw = [j.strip().upper() for j in jira_ids_str.split(',')]
    return [j for j in raw if j and _JIRA_ID_PATTERN.match(j)]


def _jira_mcp_call(tool_name, arguments, timeout=10):
    """Call a tool on the Jira MCP server via HTTP.

    Sends a JSON-RPC style request to the MCP server endpoint.
    Uses Basic Auth (email:PAT) for Jira authentication.
    Returns the tool result or None on failure.
    """
    if not JIRA_MCP_URL:
        return None
    try:
        headers = {
            'Content-Type': 'application/json',
            'Accept': 'application/json, text/event-stream',
        }

        # Auth: Jira-Token header (per MCP server docs) + fallback to Authorization
        if JIRA_PAT_TOKEN:
            headers['Jira-Token'] = JIRA_PAT_TOKEN
            headers['Authorization'] = f'Bearer {JIRA_PAT_TOKEN}'  # fallback
        if JIRA_EMAIL:
            headers['X-Jira-User-Email'] = JIRA_EMAIL

        payload = {
            'jsonrpc': '2.0',
            'id': str(uuid.uuid4()),
            'method': 'tools/call',
            'params': {
                'name': tool_name,
                'arguments': arguments
            }
        }

        # Use the shared session for proxy/Kerberos support if configured
        if PROXY_URL:
            r = gh_http.post(JIRA_MCP_URL, json=payload, headers=headers,
                             timeout=timeout, verify=SSL_VERIFY)
        else:
            r = requests.post(JIRA_MCP_URL, json=payload, headers=headers,
                             timeout=timeout, verify=SSL_VERIFY)
        r.raise_for_status()

        # Parse response — handle both JSON and SSE (text/event-stream) formats
        content_type = r.headers.get('Content-Type', '')
        raw_text = r.text.strip()

        if not raw_text:
            print(f"[Jira MCP] Empty response body from {tool_name} (Content-Type: {content_type})")
            return None

        # SSE format: lines like "event: message\ndata: {json}\n\n"
        if 'text/event-stream' in content_type or raw_text.startswith('event:') or raw_text.startswith('data:'):
            # Extract JSON from SSE data lines
            result = None
            for line in raw_text.split('\n'):
                line = line.strip()
                if line.startswith('data:'):
                    data_str = line[5:].strip()
                    if data_str:
                        try:
                            result = json.loads(data_str)
                        except json.JSONDecodeError:
                            continue
            if not result:
                print(f"[Jira MCP] Could not parse SSE response for {tool_name}: {raw_text[:200]}")
                return None
        else:
            # Standard JSON response
            try:
                result = json.loads(raw_text)
            except json.JSONDecodeError as e:
                print(f"[Jira MCP] Invalid JSON from {tool_name} (Content-Type: {content_type}): {raw_text[:200]}")
                return None

        # MCP response: { result: { content: [...] } }
        if 'result' in result:
            content = result['result']
            if isinstance(content, dict) and 'content' in content:
                # MCP standard: content is a list of {type, text} items
                texts = [c.get('text', '') for c in content.get('content', [])
                         if isinstance(c, dict)]
                return '\n'.join(texts) if texts else json.dumps(content)
            return json.dumps(content) if not isinstance(content, str) else content
        elif 'error' in result:
            print(f"[Jira MCP] Error from server: {result['error']}")
            return None
        return json.dumps(result)
    except requests.exceptions.Timeout:
        print(f"[Jira MCP] Timeout calling {tool_name}")
        return None
    except requests.exceptions.ConnectionError as e:
        print(f"[Jira MCP] Connection error: {e}")
        return None
    except requests.exceptions.HTTPError as e:
        status = e.response.status_code if e.response is not None else 'unknown'
        body = e.response.text[:200] if e.response is not None else ''
        print(f"[Jira MCP] HTTP {status} calling {tool_name}: {body}")
        return None
    except Exception as e:
        print(f"[Jira MCP] Error calling {tool_name}: {e}")
        return None


def _fetch_jira_issues(jira_ids):
    """Fetch multiple Jira issues via MCP server.

    Args:
        jira_ids: list of Jira IDs (e.g. ['PROJ-123', 'PROJ-456'])

    Returns:
        dict of {jira_id: {summary, description, status, type, priority}}
        Missing/failed issues are omitted from the result.
    """
    if not jira_ids or not JIRA_MCP_URL:
        return {}

    results = {}
    for jira_id in jira_ids:
        # Check cache first
        cache_key = ('jira_issue', jira_id)
        cached = _cache_get(cache_key)
        if cached:
            results[jira_id] = cached
            continue

        # Try fetching via MCP
        raw = _jira_mcp_call('jira_get_issue', {'issue_key': jira_id})
        if not raw:
            # Try alternative tool name
            raw = _jira_mcp_call('get_issue', {'issue_key': jira_id})
        if not raw:
            print(f"[Jira MCP] Could not fetch {jira_id}")
            continue

        # Parse the response — try JSON first, then key-value text, then raw text
        print(f"[Jira MCP] Raw response for {jira_id}: {str(raw)[:300]}")

        issue = None

        # Attempt 1: JSON response
        if isinstance(raw, str):
            try:
                data = json.loads(raw)
                issue = {
                    'id': jira_id,
                    'summary': (data.get('summary') or data.get('fields', {}).get('summary', '')),
                    'description': (data.get('description') or
                                   data.get('fields', {}).get('description', '') or ''),
                    'status': (data.get('status') or
                              data.get('fields', {}).get('status', {}).get('name', 'Unknown')),
                    'type': (data.get('issuetype') or data.get('issue_type') or
                            data.get('fields', {}).get('issuetype', {}).get('name', 'Task')),
                    'priority': (data.get('priority') or
                                data.get('fields', {}).get('priority', {}).get('name', 'Medium')),
                }
                # Check if we actually got data (not all empty strings)
                if issue['summary']:
                    print(f"[Jira MCP] Parsed {jira_id} via JSON: {issue['summary'][:80]}")
            except (json.JSONDecodeError, AttributeError):
                issue = None

        # Attempt 2: Key-value text format (e.g. "Key: PROJ-123\nSummary: Fix bug\n...")
        if not issue or not issue.get('summary'):
            raw_str = str(raw)
            field_map = {}
            for line in raw_str.split('\n'):
                line = line.strip()
                if ':' in line:
                    key, _, value = line.partition(':')
                    field_map[key.strip().lower()] = value.strip()

            summary = (field_map.get('summary') or field_map.get('title') or
                       field_map.get('headline') or '')
            description = (field_map.get('description') or field_map.get('details') or
                          field_map.get('body') or '')
            status = (field_map.get('status') or field_map.get('state') or 'Unknown')
            issue_type = (field_map.get('type') or field_map.get('issuetype') or
                         field_map.get('issue type') or 'Task')
            priority = (field_map.get('priority') or field_map.get('severity') or 'Medium')

            if summary:
                issue = {
                    'id': jira_id,
                    'summary': summary,
                    'description': description or raw_str[:500],
                    'status': status,
                    'type': issue_type,
                    'priority': priority,
                }
                print(f"[Jira MCP] Parsed {jira_id} via text: {summary[:80]}")

        # Attempt 3: Use full raw text as description
        if not issue or not issue.get('summary'):
            issue = {
                'id': jira_id,
                'summary': jira_id,
                'description': str(raw)[:1000],
                'status': 'Unknown',
                'type': 'Task',
                'priority': 'Medium',
            }
            print(f"[Jira MCP] Using raw text for {jira_id} (no structured fields found)")

        results[jira_id] = issue
        _cache_set(cache_key, issue)

    return results


def _fetch_jira_by_fix_version(fix_version):
    """Fetch all Jira tickets for a given fix version.

    Tries multiple methods in order:
    1. MCP server's jira_search_issues or search_issues tool (uses JIRA_MCP_URL)
    2. Direct Jira REST API with Bearer token (uses JIRA_BASE_URL + JIRA_PAT_TOKEN)
    3. Direct Jira REST API with Basic auth (uses JIRA_BASE_URL + JIRA_EMAIL + JIRA_PAT_TOKEN)

    Returns list of issue dicts: [{id, summary, description, type, status, priority, components}]
    """
    if not fix_version:
        return []

    jql = f'fixVersion = "{fix_version}" ORDER BY issuetype ASC'
    issues = []

    # ── Method 1: Try MCP server's search tool ──
    # Use short timeout (5s) and bail after first timeout to avoid 504 gateway errors.
    # The MCP server may not be reachable in production — quick-fail is critical.
    if JIRA_MCP_URL:
        print(f'[jira] Trying MCP search for fix version: {fix_version}')
        mcp_timed_out = False
        for tool_name in ['search_jira_issues', 'jira_search_issues', 'search_issues', 'jira_search']:
            if mcp_timed_out:
                print(f'[jira] Skipping MCP tool {tool_name} — MCP already timed out, falling back to REST')
                break
            raw = _jira_mcp_call(tool_name, {'jql': jql, 'max_results': 200}, timeout=5)
            if raw is None:
                # Timeout or connection error — don't try remaining tools
                mcp_timed_out = True
                continue
            if raw:
                try:
                    if isinstance(raw, str):
                        data = json.loads(raw)
                    elif isinstance(raw, dict):
                        data = raw
                    else:
                        data = {'issues': []}

                    # Handle different MCP response shapes
                    raw_issues = data.get('issues', data.get('result', {}).get('issues', []))
                    if isinstance(raw_issues, list):
                        for item in raw_issues:
                            if isinstance(item, dict):
                                fields = item.get('fields', item)
                                issues.append({
                                    'id': item.get('key', item.get('id', '?')),
                                    'summary': fields.get('summary', ''),
                                    'description': (fields.get('description') or '')[:500],
                                    'type': (fields.get('issuetype', {}) or {}).get('name', fields.get('type', 'Task')),
                                    'status': (fields.get('status', {}) if isinstance(fields.get('status'), dict) else {}).get('name', str(fields.get('status', '?'))),
                                    'priority': (fields.get('priority', {}) if isinstance(fields.get('priority'), dict) else {}).get('name', str(fields.get('priority', 'Medium'))),
                                    'components': [c.get('name', str(c)) for c in (fields.get('components') or []) if c]
                                })
                    if issues:
                        print(f'[jira] MCP search returned {len(issues)} issues for fix version {fix_version}')
                        return issues
                except Exception as e:
                    print(f'[jira] MCP search parse error for {tool_name}: {e}')
                    continue

    # ── Method 2: Direct Jira REST API ──
    jira_base = JIRA_BASE_URL

    if jira_base:
        print(f'[jira] Trying direct REST API for fix version: {fix_version} at {jira_base}')
        import base64
        headers = {'Content-Type': 'application/json', 'Accept': 'application/json'}

        # Auth: prefer Bearer (Jira Data Center PAT), then Basic as fallback
        if JIRA_PAT_TOKEN:
            headers['Authorization'] = f'Bearer {JIRA_PAT_TOKEN}'
            print(f'[jira] Using Bearer token auth')
        elif JIRA_EMAIL and JIRA_PAT_TOKEN:
            cred = base64.b64encode(f'{JIRA_EMAIL}:{JIRA_PAT_TOKEN}'.encode()).decode()
            headers['Authorization'] = f'Basic {cred}'
            print(f'[jira] Using Basic auth: {JIRA_EMAIL}')
        else:
            print('[jira] No auth credentials available for REST API')

        if 'Authorization' in headers:
            search_url = f'{jira_base}/rest/api/2/search'
            search_params = {
                'jql': jql,
                'maxResults': 200,
                'fields': 'summary,description,issuetype,status,priority,components'
            }
            search_body = {
                'jql': jql, 'maxResults': 200,
                'fields': ['summary', 'description', 'issuetype', 'status', 'priority', 'components']
            }
            http_session = gh_http if PROXY_URL else requests

            # Try GET first (Jira Server/Data Center), then POST (Jira Cloud)
            resp = None
            for method in ['GET', 'POST']:
                try:
                    if method == 'GET':
                        resp = http_session.get(search_url, headers=headers, params=search_params,
                                                timeout=20, verify=SSL_VERIFY)
                    else:
                        resp = http_session.post(search_url, headers=headers, json=search_body,
                                                 timeout=20, verify=SSL_VERIFY)
                    if resp.ok:
                        print(f'[jira] REST API {method} succeeded ({resp.status_code})')
                        break
                    else:
                        print(f'[jira] REST API {method} returned {resp.status_code}: {resp.text[:200]}')
                except Exception as e:
                    print(f'[jira] REST API {method} error: {e}')

            if resp and resp.ok:
                try:
                    for item in resp.json().get('issues', []):
                        fields = item.get('fields', {})
                        issues.append({
                            'id': item['key'],
                            'summary': fields.get('summary', ''),
                            'description': (fields.get('description') or '')[:500],
                            'type': (fields.get('issuetype') or {}).get('name', 'Task'),
                            'status': (fields.get('status') or {}).get('name', '?'),
                            'priority': (fields.get('priority') or {}).get('name', 'Medium'),
                            'components': [c.get('name', '') for c in (fields.get('components') or [])]
                        })
                    print(f'[jira] REST API returned {len(issues)} issues for fix version {fix_version}')
                except Exception as e:
                    print(f'[jira] REST API response parse error: {e}')

    if not issues:
        print(f'[jira] ❌ No Jira issues found for fix version {fix_version}.')
        if not JIRA_BASE_URL:
            print(f'[jira]    💡 Set JIRA_BASE_URL to your Jira instance URL (e.g. https://jira.company.com)')
            print(f'[jira]    Your JIRA_EMAIL and JIRA_PAT_TOKEN will be used for Basic auth automatically.')

    return issues


def _map_issues_to_services(fix_version_issues, service_names):
    """Map Jira issues to nominated services based on the Jira component field.

    Each Jira ticket's components are matched against service names using
    case-insensitive, dash/underscore-normalized comparison.

    Args:
        fix_version_issues: list of issue dicts from _fetch_jira_by_fix_version
        service_names: list of nominated service names from the board

    Returns:
        svc_map: dict of service_name → [issue dicts]
        unmatched: list of issue dicts with no matching service
    """
    svc_map = {}   # service_name → [issues]
    unmatched = []

    # Normalize service names for fuzzy matching
    def _norm(s):
        return s.lower().replace('-', '').replace('_', '').replace(' ', '')

    normalized_to_svc = {_norm(name): name for name in service_names}

    for issue in fix_version_issues:
        matched = False
        for comp in issue.get('components', []):
            comp_norm = _norm(comp)
            if comp_norm in normalized_to_svc:
                svc_name = normalized_to_svc[comp_norm]
                svc_map.setdefault(svc_name, []).append(issue)
                matched = True
                # Don't break — a ticket with multiple components can map to multiple services
        if not matched:
            unmatched.append(issue)

    if fix_version_issues:
        mapped_count = sum(len(v) for v in svc_map.values())
        print(f'[jira] Component mapping: {mapped_count} tickets mapped to {len(svc_map)} services, '
              f'{len(unmatched)} unmatched')

    return svc_map, unmatched


# ── Confluence MCP Client ─────────────────────────────────────────────────────

def _confluence_mcp_call(tool_name, arguments, timeout=10):
    """Call a tool on the Confluence MCP server via HTTP.
    Follows same JSON-RPC pattern as _jira_mcp_call().
    """
    if not CONFLUENCE_MCP_URL:
        return None
    try:
        headers = {
            'Content-Type': 'application/json',
            'Accept': 'application/json, text/event-stream',
        }
        if CONFLUENCE_PAT_TOKEN:
            headers['Authorization'] = f'Bearer {CONFLUENCE_PAT_TOKEN}'
        if CONFLUENCE_EMAIL:
            headers['X-Confluence-User-Email'] = CONFLUENCE_EMAIL

        payload = {
            'jsonrpc': '2.0',
            'id': str(uuid.uuid4()),
            'method': 'tools/call',
            'params': {
                'name': tool_name,
                'arguments': arguments
            }
        }

        if PROXY_URL:
            r = gh_http.post(CONFLUENCE_MCP_URL, json=payload, headers=headers,
                             timeout=timeout, verify=SSL_VERIFY)
        else:
            r = requests.post(CONFLUENCE_MCP_URL, json=payload, headers=headers,
                             timeout=timeout, verify=SSL_VERIFY)
        r.raise_for_status()

        content_type = r.headers.get('Content-Type', '')
        raw_text = r.text.strip()

        if not raw_text:
            print(f"[Confluence MCP] Empty response from {tool_name}")
            return None

        # SSE format
        if 'text/event-stream' in content_type or raw_text.startswith('event:') or raw_text.startswith('data:'):
            result = None
            for line in raw_text.split('\n'):
                line = line.strip()
                if line.startswith('data:'):
                    data_str = line[5:].strip()
                    if data_str:
                        try:
                            result = json.loads(data_str)
                        except json.JSONDecodeError:
                            continue
            if not result:
                print(f"[Confluence MCP] Could not parse SSE for {tool_name}")
                return None
        else:
            try:
                result = json.loads(raw_text)
            except json.JSONDecodeError:
                print(f"[Confluence MCP] Invalid JSON from {tool_name}: {raw_text[:200]}")
                return None

        # MCP response extraction
        if 'result' in result:
            content = result['result']
            if isinstance(content, dict) and 'content' in content:
                texts = [c.get('text', '') for c in content.get('content', [])
                         if isinstance(c, dict)]
                return '\n'.join(texts) if texts else json.dumps(content)
            return json.dumps(content) if not isinstance(content, str) else content
        elif 'error' in result:
            print(f"[Confluence MCP] Error: {result['error']}")
            return None
        return json.dumps(result)
    except requests.exceptions.Timeout:
        print(f"[Confluence MCP] Timeout calling {tool_name}")
        return None
    except requests.exceptions.ConnectionError as e:
        print(f"[Confluence MCP] Connection error: {e}")
        return None
    except requests.exceptions.HTTPError as e:
        status = e.response.status_code if e.response is not None else 'unknown'
        print(f"[Confluence MCP] HTTP {status} calling {tool_name}")
        return None
    except Exception as e:
        print(f"[Confluence MCP] Error calling {tool_name}: {e}")
        return None


def _confluence_auth_headers():
    """Build auth headers for direct Confluence REST API."""
    headers = {'Content-Type': 'application/json', 'Accept': 'application/json'}
    if CONFLUENCE_EMAIL and CONFLUENCE_PAT_TOKEN:
        import base64 as b64
        cred = b64.b64encode(f'{CONFLUENCE_EMAIL}:{CONFLUENCE_PAT_TOKEN}'.encode()).decode()
        headers['Authorization'] = f'Basic {cred}'
    elif CONFLUENCE_PAT_TOKEN:
        headers['Authorization'] = f'Bearer {CONFLUENCE_PAT_TOKEN}'
    return headers


def _confluence_search(query, space_key=None, max_results=10):
    """Search Confluence via MCP, with REST API fallback.

    Returns list of page dicts: [{id, title, space, url, excerpt, labels, ...}]
    """
    cache_key = ('confluence_search', query, space_key)
    cached = _cache_get(cache_key)
    if cached:
        return cached

    results = []

    # Method 1: MCP server
    if CONFLUENCE_MCP_URL:
        print(f'[confluence] Searching via MCP: "{query}"')
        for tool_name in ['confluence_search', 'search_confluence', 'search_pages', 'confluence_search_pages']:
            raw = _confluence_mcp_call(tool_name, {
                'query': query,
                'space_key': space_key or '',
                'max_results': max_results
            }, timeout=8)
            if raw:
                try:
                    # raw from _confluence_mcp_call may already be extracted text (not JSON)
                    if isinstance(raw, str):
                        raw_stripped = raw.strip()
                        # Handle NDJSON (multiple JSON lines)
                        if '\n' in raw_stripped and raw_stripped.startswith('{'):
                            lines = [l.strip() for l in raw_stripped.split('\n') if l.strip()]
                            data = None
                            for line in lines:
                                try:
                                    data = json.loads(line)
                                    if isinstance(data, (dict, list)):
                                        break
                                except json.JSONDecodeError:
                                    continue
                            if not data:
                                print(f'[confluence] MCP could not parse NDJSON from {tool_name}')
                                continue
                        else:
                            try:
                                data = json.loads(raw_stripped)
                            except json.JSONDecodeError:
                                # Not JSON — treat as plain text, skip to next tool
                                print(f'[confluence] MCP response from {tool_name} is not JSON, trying next tool')
                                continue
                    else:
                        data = raw

                    items = data if isinstance(data, list) else data.get('results', data.get('pages', []))
                    if isinstance(items, list) and items:
                        for item in items:
                            if isinstance(item, dict):
                                results.append({
                                    'id': str(item.get('id', item.get('pageId', ''))),
                                    'title': item.get('title', item.get('name', '?')),
                                    'space': item.get('space', item.get('spaceKey', item.get('space_key', '?'))),
                                    'url': item.get('url', item.get('webUrl', item.get('_links', {}).get('webui', ''))),
                                    'excerpt': (item.get('excerpt', item.get('snippet', item.get('body', ''))) or '')[:300],
                                    'last_modified': item.get('lastModified', item.get('last_modified',
                                                     item.get('version', {}).get('when', '') if isinstance(item.get('version'), dict) else '')),
                                    'author': item.get('author', item.get('lastModifiedBy',
                                              item.get('version', {}).get('by', {}).get('displayName', '') if isinstance(item.get('version'), dict) else '')),
                                    'labels': item.get('labels', []),
                                })
                        if results:
                            print(f'[confluence] MCP returned {len(results)} pages')
                            _cache_set(cache_key, results)
                            return results
                except Exception as e:
                    print(f'[confluence] MCP parse error ({tool_name}): {e}')
                    continue

    # Method 2: Direct REST API
    if CONFLUENCE_BASE_URL:
        print(f'[confluence] Searching via REST API: "{query}"')
        cql = f'text ~ "{query}" AND type = "page"'
        if space_key:
            cql += f' AND space = "{space_key}"'
        try:
            url = f'{CONFLUENCE_BASE_URL}/rest/api/content/search'
            params = {
                'cql': cql,
                'limit': max_results,
                'expand': 'space,version,metadata.labels'
            }
            http_session = gh_http if PROXY_URL else requests
            resp = http_session.get(url, headers=_confluence_auth_headers(),
                                    params=params, timeout=15, verify=SSL_VERIFY)
            resp.raise_for_status()
            for item in resp.json().get('results', []):
                results.append({
                    'id': str(item.get('id', '')),
                    'title': item.get('title', '?'),
                    'space': item.get('space', {}).get('key', '?'),
                    'url': f"{CONFLUENCE_BASE_URL}{item.get('_links', {}).get('webui', '')}",
                    'excerpt': item.get('excerpt', '')[:300],
                    'last_modified': item.get('version', {}).get('when', ''),
                    'author': item.get('version', {}).get('by', {}).get('displayName', ''),
                    'labels': [l['name'] for l in
                               item.get('metadata', {}).get('labels', {}).get('results', [])],
                })
            print(f'[confluence] REST API returned {len(results)} pages')
        except requests.exceptions.SSLError as e:
            print(f'[confluence] REST API SSL error: {e}')
            print(f'[confluence]   → If using self-signed certs, set SSL_VERIFY=false')
        except requests.exceptions.ConnectionError as e:
            print(f'[confluence] REST API connection error: {e}')
            print(f'[confluence]   → Check: is {CONFLUENCE_BASE_URL} reachable from this pod?')
            print(f'[confluence]   → If behind a proxy, set PROXY_URL')
        except requests.exceptions.Timeout:
            print(f'[confluence] REST API timeout (>15s)')
        except Exception as e:
            print(f'[confluence] REST API search error: {e}')

    _cache_set(cache_key, results)
    return results


def _confluence_get_page(page_id):
    """Get full page content by ID via MCP or REST."""
    cache_key = ('confluence_page', page_id)
    cached = _cache_get(cache_key)
    if cached:
        return cached

    page = None

    # MCP
    if CONFLUENCE_MCP_URL:
        for tool_name in ['confluence_get_page', 'get_confluence_page', 'get_page', 'confluence_get_page_content']:
            raw = _confluence_mcp_call(tool_name, {'page_id': page_id}, timeout=10)
            if raw:
                try:
                    if isinstance(raw, str):
                        raw_stripped = raw.strip()
                        if '\n' in raw_stripped and raw_stripped.startswith('{'):
                            lines = [l.strip() for l in raw_stripped.split('\n') if l.strip()]
                            data = None
                            for line in lines:
                                try:
                                    data = json.loads(line)
                                    if isinstance(data, dict):
                                        break
                                except json.JSONDecodeError:
                                    continue
                            if not data:
                                continue
                        else:
                            try:
                                data = json.loads(raw_stripped)
                            except json.JSONDecodeError:
                                print(f'[confluence] MCP page response from {tool_name} is not JSON')
                                continue
                    else:
                        data = raw
                    if isinstance(data, dict):
                        page = {
                            'id': str(data.get('id', page_id)),
                            'title': data.get('title', '?'),
                            'space': data.get('space', data.get('spaceKey', '?')),
                            'body_html': data.get('body_html', data.get('body', data.get('content', ''))),
                            'body_text': data.get('body_text', data.get('plainText',
                                         data.get('body_export', data.get('body', '')))),
                            'url': data.get('url', data.get('webUrl', '')),
                            'labels': data.get('labels', []),
                            'last_modified': data.get('lastModified', data.get('last_modified', '')),
                            'author': data.get('author', data.get('lastModifiedBy', '')),
                        }
                        if page.get('title') and page['title'] != '?':
                            _cache_set(cache_key, page)
                            return page
                except Exception as e:
                    print(f'[confluence] MCP page parse error: {e}')

    # REST fallback
    if CONFLUENCE_BASE_URL:
        try:
            url = f'{CONFLUENCE_BASE_URL}/rest/api/content/{page_id}'
            params = {'expand': 'body.view,body.storage,space,version,metadata.labels'}
            http_session = gh_http if PROXY_URL else requests
            resp = http_session.get(url, headers=_confluence_auth_headers(),
                                    params=params, timeout=15, verify=SSL_VERIFY)
            resp.raise_for_status()
            data = resp.json()
            body_html = data.get('body', {}).get('view', {}).get('value', '')
            # Strip HTML tags for plain text
            body_text = re.sub(r'<[^>]+>', ' ', body_html)
            body_text = re.sub(r'\s+', ' ', body_text).strip()
            page = {
                'id': str(data.get('id', page_id)),
                'title': data.get('title', '?'),
                'space': data.get('space', {}).get('key', '?'),
                'body_html': body_html,
                'body_text': body_text[:5000],
                'url': f"{CONFLUENCE_BASE_URL}{data.get('_links', {}).get('webui', '')}",
                'labels': [l['name'] for l in
                           data.get('metadata', {}).get('labels', {}).get('results', [])],
                'last_modified': data.get('version', {}).get('when', ''),
                'author': data.get('version', {}).get('by', {}).get('displayName', ''),
            }
        except Exception as e:
            print(f'[confluence] REST API page fetch error: {e}')

    if page:
        _cache_set(cache_key, page)
    return page


# ── Flask app ─────────────────────────────────────────────────────────────────
app = Flask(__name__)
app.wsgi_app = ProxyFix(app.wsgi_app, x_for=1, x_proto=1, x_host=1, x_prefix=1)
app.secret_key = os.environ.get('FLASK_SECRET_KEY', secrets.token_hex(32))

socketio = SocketIO(app, cors_allowed_origins="*", async_mode='gevent',
                    ping_timeout=120, ping_interval=30)

try:
    config.load_incluster_config()
except config.ConfigException:
    try:
        config.load_kube_config()
    except config.ConfigException:
        print("[k8s] Could not configure kubernetes client")


# ── K8s retry helper ──────────────────────────────────────────────────────────
def _k8s_retry(fn, *args, **kwargs):
    _STALE = ('Connection reset by peer', 'ProtocolError',
              'RemoteDisconnected', 'Connection aborted', 'MaxRetryError')
    for _attempt in range(2):
        try:
            return fn(*args, **kwargs)
        except Exception as _exc:
            if _attempt == 0 and any(k in str(_exc) for k in _STALE):
                time.sleep(0.5)
                continue
            raise


# ── Helpers ───────────────────────────────────────────────────────────────────
NAMESPACE = os.getenv('POD_NAMESPACE', 'default')
DEPLOY_ENV = os.getenv('DEPLOY_ENV', 'uat').lower()  # 'uat' or 'prod' — determines which cluster is local
RELEASE_CADENCE = os.getenv('RELEASE_CADENCE', 'friday')  # 'friday' or 'custom'
CUTOFF_DAY = int(os.getenv('CUTOFF_DAY', '2'))  # 0=Mon, 2=Wed
CUTOFF_HOUR = int(os.getenv('CUTOFF_HOUR', '17'))  # 17:00

# ── Artifactory (Custom Component Version Detection) ─────────────────────────
ARTIFACTORY_URL = os.getenv('ARTIFACTORY_URL', '').rstrip('/')
ARTIFACTORY_USER = os.getenv('ARTIFACTORY_USER', '')
ARTIFACTORY_TOKEN = os.getenv('ARTIFACTORY_TOKEN', '')

# ── Storage Backend ───────────────────────────────────────────────────────────
STORAGE_BACKEND = os.getenv('STORAGE_BACKEND', 'auto')   # 'auto', 'configmap', 'file'
BOARD_DATA_DIR  = os.getenv('BOARD_DATA_DIR', '/data/boards')

_STORAGE_MODE = None  # Set at startup: 'configmap' or 'file'


def _detect_storage_mode():
    """Auto-detect whether ConfigMap create/update permissions are available.
    Sets _STORAGE_MODE to 'configmap' or 'file'.
    """
    global _STORAGE_MODE

    if STORAGE_BACKEND in ('configmap', 'file'):
        _STORAGE_MODE = STORAGE_BACKEND
        print(f"[storage] Mode set explicitly: {_STORAGE_MODE}")
        if _STORAGE_MODE == 'file' or _STORAGE_MODE == 'auto':
            os.makedirs(BOARD_DATA_DIR, exist_ok=True)
        return

    # Auto-detect: try creating a probe ConfigMap
    try:
        v1 = client.CoreV1Api()
        probe_name = 'release-readiness-probe'
        probe = client.V1ConfigMap(
            metadata=client.V1ObjectMeta(name=probe_name,
                                         labels={'app': 'release-readiness', 'probe': 'true'}),
            data={'probe': 'storage-detection'}
        )
        _k8s_retry(v1.create_namespaced_config_map, NAMESPACE, probe)
        # Clean up probe
        try:
            _k8s_retry(v1.delete_namespaced_config_map, probe_name, NAMESPACE)
        except Exception:
            pass
        _STORAGE_MODE = 'configmap'
        print(f"[storage] ✅ ConfigMap permissions verified — using ConfigMap + file backup")
    except client.exceptions.ApiException as e:
        if e.status == 403:
            _STORAGE_MODE = 'file'
            print(f"[storage] ⚠️  No ConfigMap create/update permissions (403) — using file-only mode")
        else:
            _STORAGE_MODE = 'file'
            print(f"[storage] ⚠️  ConfigMap probe failed (HTTP {e.status}) — using file-only mode")
    except Exception as e:
        _STORAGE_MODE = 'file'
        print(f"[storage] ⚠️  K8s not available ({e}) — using file-only mode")

    # Ensure data directory exists for file mode (and backup in configmap mode)
    os.makedirs(BOARD_DATA_DIR, exist_ok=True)
    print(f"[storage] Data directory: {BOARD_DATA_DIR}")


# Run storage detection after K8s config is loaded
_detect_storage_mode()


def _get_current_release_date():
    """Calculate the next release Friday (or whatever cadence)."""
    today = datetime.date.today()
    days_until_friday = (4 - today.weekday()) % 7
    if days_until_friday == 0 and datetime.datetime.now().hour >= 18:
        days_until_friday = 7
    return (today + datetime.timedelta(days=days_until_friday)).isoformat()


def _get_cutoff_datetime():
    """Calculate the cutoff datetime (Wednesday 5 PM of the current release week)."""
    today = datetime.date.today()
    days_until_friday = (4 - today.weekday()) % 7
    if days_until_friday == 0 and datetime.datetime.now().hour >= 18:
        days_until_friday = 7
    release_friday = today + datetime.timedelta(days=days_until_friday)
    cutoff_date = release_friday - datetime.timedelta(days=(4 - CUTOFF_DAY) % 7)
    return datetime.datetime.combine(cutoff_date, datetime.time(CUTOFF_HOUR, 0)).isoformat()


def _board_configmap_name(release_date=None):
    if not release_date:
        release_date = _get_current_release_date()
    return f"release-board-{release_date}"


# ── File-based storage helpers ────────────────────────────────────────────────
def _board_file_path(release_date=None):
    """Get the file path for a board's JSON file on the PVC."""
    name = _board_configmap_name(release_date)
    return os.path.join(BOARD_DATA_DIR, f'{name}.json')


def _read_board_file(release_date=None):
    """Read board data from a JSON file on the PVC."""
    path = _board_file_path(release_date)
    try:
        with open(path, 'r') as f:
            return json.load(f)
    except FileNotFoundError:
        return None
    except Exception as e:
        print(f"[storage] Error reading {path}: {e}")
        return None


def _write_board_file(board_data, release_date=None):
    """Write board data to a JSON file on the PVC."""
    path = _board_file_path(release_date)
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, 'w') as f:
        json.dump(board_data, f, indent=2)


# ── Board read/write (dual-mode) ─────────────────────────────────────────────
def _read_board(release_date=None):
    """Read the release board. Uses ConfigMap or file based on detected storage mode."""
    if _STORAGE_MODE == 'configmap':
        try:
            v1 = client.CoreV1Api()
            cm_name = _board_configmap_name(release_date)
            cm = _k8s_retry(v1.read_namespaced_config_map, cm_name, NAMESPACE)
            return json.loads(cm.data.get('manifest.json', '{}'))
        except client.exceptions.ApiException as e:
            if e.status == 404:
                # ConfigMap not found — check if file backup exists
                return _read_board_file(release_date)
            raise
        except Exception:
            # K8s error — fall back to file
            return _read_board_file(release_date)
    else:
        # File-only mode
        return _read_board_file(release_date)


def _write_board(board_data, release_date=None):
    """Write the release board. Uses ConfigMap + file backup, or file-only."""
    # Always write to file (guaranteed to work if PVC/dir is available)
    try:
        _write_board_file(board_data, release_date)
    except Exception as e:
        print(f"[storage] ⚠️  File write failed: {e}")

    # Also write to ConfigMap if permissions exist
    if _STORAGE_MODE == 'configmap':
        try:
            v1 = client.CoreV1Api()
            cm_name = _board_configmap_name(release_date)
            body = client.V1ConfigMap(
                metadata=client.V1ObjectMeta(
                    name=cm_name,
                    labels={
                        'app': 'release-readiness',
                        'release-date': release_date or _get_current_release_date()
                    }
                ),
                data={'manifest.json': json.dumps(board_data, indent=2)}
            )
            try:
                _k8s_retry(v1.read_namespaced_config_map, cm_name, NAMESPACE)
                _k8s_retry(v1.replace_namespaced_config_map, cm_name, NAMESPACE, body)
            except client.exceptions.ApiException as e:
                if e.status == 404:
                    _k8s_retry(v1.create_namespaced_config_map, NAMESPACE, body)
                else:
                    raise
        except Exception as e:
            print(f"[storage] ⚠️  ConfigMap write failed (file backup exists): {e}")


def _generate_fix_version(release_date_str):
    """Generate Jira fix version from release date.
    Format: P<YY>.<MM>.<DD> e.g. P26.05.09"""
    try:
        d = datetime.date.fromisoformat(release_date_str)
        return f"P{d.strftime('%y.%m.%d')}"
    except (ValueError, TypeError):
        return ''


def _new_board():
    """Create an empty release board template."""
    release_date = _get_current_release_date()
    return {
        'release_date': release_date,
        'cutoff': _get_cutoff_datetime(),
        'fix_version': _generate_fix_version(release_date),
        'status': 'open',
        'services': {},
        'audit_trail': [],
        'exception_nominations': [],
        'created_at': datetime.datetime.utcnow().isoformat(),
        'finalized_by': None,
        'finalized_at': None
    }


def _extract_image_tag(image_str):
    """Extract tag from image string, e.g. 'registry.com/app:v2.3.1' -> 'v2.3.1'"""
    if ':' in image_str and '/' in image_str:
        return image_str.rsplit(':', 1)[-1]
    if ':' in image_str:
        return image_str.split(':')[-1]
    return 'latest'


def _extract_helm_version(labels):
    """Extract helm chart version from deployment labels."""
    if not labels:
        return None
    return (labels.get('helm.sh/chart') or
            labels.get('app.kubernetes.io/version') or
            labels.get('chart') or None)


# ── Custom components (non-K8s: Spark/PySpark on Linux servers) ───────────────
# Configure via env var: CUSTOM_COMPONENTS=name1:Type1:Description,name2:Type2:Description
# Or defaults to placeholder components for demo
_DEFAULT_CUSTOM_COMPONENTS = [
    {"name": "ingestion-pipeline",       "type": "Spark",   "description": "Main data ingestion from source systems"},
    {"name": "etl-transformer",          "type": "PySpark", "description": "Data transformation and enrichment"},
    {"name": "data-validator",           "type": "PySpark", "description": "Data quality validation rules"},
    {"name": "report-aggregator",        "type": "Spark",   "description": "Aggregation jobs for reporting"},
    {"name": "event-stream-processor",   "type": "Spark",   "description": "Real-time event stream processing"},
    {"name": "batch-reconciler",         "type": "PySpark", "description": "Batch reconciliation between systems"},
    {"name": "data-archiver",            "type": "PySpark", "description": "Historical data archival jobs"},
    {"name": "ml-feature-pipeline",      "type": "PySpark", "description": "ML feature extraction pipeline"},
    {"name": "audit-log-processor",      "type": "Spark",   "description": "Audit log processing and indexing"},
    {"name": "schema-migration-runner",  "type": "PySpark", "description": "Database schema migration runner"},
]

def _load_custom_components():
    raw = os.environ.get('CUSTOM_COMPONENTS', '')
    if not raw:
        return _DEFAULT_CUSTOM_COMPONENTS
    components = []
    for entry in raw.split(','):
        parts = entry.strip().split(':')
        if len(parts) >= 2:
            components.append({
                'name': parts[0].strip(),
                'type': parts[1].strip(),
                'description': parts[2].strip() if len(parts) > 2 else '',
                'artifactory_path': parts[3].strip() if len(parts) > 3 else ''
            })
    return components or _DEFAULT_CUSTOM_COMPONENTS

CUSTOM_COMPONENTS = _load_custom_components()
CUSTOM_COMPONENTS_MAP = {c['name']: c for c in CUSTOM_COMPONENTS}


# ══════════════════════════════════════════════════════════════════════════════
# Routes
# ══════════════════════════════════════════════════════════════════════════════

@app.route('/')
def index():
    return render_template('index.html')


@app.route('/api/ping')
def api_ping():
    return 'ok', 200


@app.route('/api/auth/status')
def api_auth_status():
    return jsonify({'authenticated': True, 'ts': datetime.datetime.utcnow().isoformat()})


# ── Service listing from live cluster ─────────────────────────────────────────
@app.route('/api/services')
def list_services():
    """List all deployable services from the K8s cluster with current versions.

    When DEPLOY_ENV=uat: reads from the LOCAL cluster (current behavior).
    When DEPLOY_ENV=prod: connects REMOTELY to UAT via UAT_CLUSTER_API/TOKEN.
    """
    namespace = request.args.get('namespace', NAMESPACE)

    # ── DEPLOY_ENV=prod: connect to remote UAT cluster ──
    if DEPLOY_ENV == 'prod':
        uat_ns = os.environ.get('UAT_NAMESPACE', namespace)
        cache_key = ('uat_services', uat_ns)
        cached = _cache_get(cache_key)
        if cached:
            return jsonify(cached)

        print(f'[uat] DEPLOY_ENV=prod → connecting REMOTELY to UAT cluster')
        api_client, error = _get_uat_api_client()
        if error:
            print(f'[uat] Client creation failed: {error}')
            return jsonify({
                'services': [], 'namespace': uat_ns,
                'cluster': os.environ.get('UAT_CLUSTER_API', ''),
                'count': 0, 'connected': False, 'error': error
            })

        uat_api_url = os.environ.get('UAT_CLUSTER_API', '')
        print(f'[uat] Connecting to {uat_api_url}, namespace={uat_ns}')
        try:
            services = _list_services_from_api(api_client, uat_ns, '[uat-remote]')
        except Exception as e:
            import traceback
            print(f'[uat] ❌ Connection FAILED: {type(e).__name__}: {e}')
            print(f'[uat] Traceback: {traceback.format_exc()}')
            return jsonify({
                'error': str(e), 'services': [], 'namespace': uat_ns,
                'cluster': uat_api_url, 'connected': False
            }), 500

        result = {
            'services': services, 'namespace': uat_ns,
            'cluster': uat_api_url, 'count': len(services),
            'connected': True, 'deploy_env': DEPLOY_ENV
        }
        _cache_set(cache_key, result)
        return jsonify(result)

    # ── DEPLOY_ENV=uat (default): read local cluster ──
    cache_key = ('services', namespace)
    cached = _cache_get(cache_key)
    if cached:
        return jsonify(cached)

    services = []
    try:
        apps_v1 = client.AppsV1Api()

        # Deployments
        try:
            deploys = _k8s_retry(apps_v1.list_namespaced_deployment, namespace).items
            for d in deploys:
                containers = d.spec.template.spec.containers or []
                image = containers[0].image if containers else ''
                services.append({
                    'name': d.metadata.name,
                    'kind': 'Deployment',
                    'image': image,
                    'image_tag': _extract_image_tag(image),
                    'helm_version': _extract_helm_version(d.metadata.labels),
                    'replicas': d.status.ready_replicas or 0,
                    'desired_replicas': d.spec.replicas or 1,
                    'available': (d.status.ready_replicas or 0) >= (d.spec.replicas or 1),
                    'created': d.metadata.creation_timestamp.isoformat() if d.metadata.creation_timestamp else None
                })
        except Exception as e:
            print(f"[services] Deployments error: {e}")

        # StatefulSets
        try:
            sts = _k8s_retry(apps_v1.list_namespaced_stateful_set, namespace).items
            for s in sts:
                containers = s.spec.template.spec.containers or []
                image = containers[0].image if containers else ''
                services.append({
                    'name': s.metadata.name,
                    'kind': 'StatefulSet',
                    'image': image,
                    'image_tag': _extract_image_tag(image),
                    'helm_version': _extract_helm_version(s.metadata.labels),
                    'replicas': s.status.ready_replicas or 0,
                    'desired_replicas': s.spec.replicas or 1,
                    'available': (s.status.ready_replicas or 0) >= (s.spec.replicas or 1),
                    'created': s.metadata.creation_timestamp.isoformat() if s.metadata.creation_timestamp else None
                })
        except Exception as e:
            print(f"[services] StatefulSets error: {e}")

        # DaemonSets
        try:
            dss = _k8s_retry(apps_v1.list_namespaced_daemon_set, namespace).items
            for d in dss:
                containers = d.spec.template.spec.containers or []
                image = containers[0].image if containers else ''
                services.append({
                    'name': d.metadata.name,
                    'kind': 'DaemonSet',
                    'image': image,
                    'image_tag': _extract_image_tag(image),
                    'helm_version': _extract_helm_version(d.metadata.labels),
                    'replicas': d.status.number_ready or 0,
                    'desired_replicas': d.status.desired_number_scheduled or 1,
                    'available': (d.status.number_ready or 0) >= (d.status.desired_number_scheduled or 1),
                    'created': d.metadata.creation_timestamp.isoformat() if d.metadata.creation_timestamp else None
                })
        except Exception as e:
            print(f"[services] DaemonSets error: {e}")

    except Exception as e:
        return jsonify({'error': str(e)}), 500

    result = {'services': services, 'namespace': namespace, 'count': len(services)}
    _cache_set(cache_key, result)
    return jsonify(result)


@app.route('/api/custom_components')
def list_custom_components():
    """List all configured custom components (non-K8s, e.g. Spark/PySpark)."""
    enriched = []
    for c in CUSTOM_COMPONENTS:
        entry = dict(c)
        entry['has_artifactory'] = bool(c.get('artifactory_path') and ARTIFACTORY_URL)
        enriched.append(entry)
    return jsonify({'components': enriched, 'count': len(enriched)})


# ── Artifactory Version Detection ─────────────────────────────────────────────
# Fetches available versions from Artifactory for custom components.
# Uses the /api/storage/ endpoint (Generic/Maven repos — folder-based versions).
# Auth: Basic auth with ARTIFACTORY_USER:ARTIFACTORY_TOKEN.

def _version_freshness(date_str):
    """Determine version freshness relative to the current release cycle.
    Returns: 'current_week', 'previous_week', or 'stale'.
    """
    try:
        # Parse the date (Artifactory returns ISO format or lastModified format)
        if 'T' in date_str:
            dt = datetime.datetime.fromisoformat(date_str.replace('Z', '+00:00').replace('+00:00', ''))
        else:
            dt = datetime.datetime.strptime(date_str[:19], '%Y-%m-%d %H:%M:%S')
    except (ValueError, TypeError):
        return 'unknown'

    today = datetime.date.today()
    # Current release week: Monday of this week through Friday
    monday = today - datetime.timedelta(days=today.weekday())
    prev_monday = monday - datetime.timedelta(days=7)
    version_date = dt.date() if hasattr(dt, 'date') else dt

    if version_date >= monday:
        return 'current_week'
    elif version_date >= prev_monday:
        return 'previous_week'
    else:
        return 'stale'


def _fetch_artifactory_versions(artifactory_path):
    """Fetch available versions from Artifactory for a given path.
    Uses /api/storage/{path} with listFolders to enumerate version directories.
    Returns list of {'version': str, 'date': str, 'freshness': str}, sorted newest first.
    """
    if not ARTIFACTORY_URL or not artifactory_path:
        return []

    # Check cache first
    cache_key = f'artifactory_{artifactory_path}'
    cached = _cache_get(cache_key)
    if cached is not None:
        return cached

    import base64
    headers = {}
    if ARTIFACTORY_USER and ARTIFACTORY_TOKEN:
        creds = base64.b64encode(f"{ARTIFACTORY_USER}:{ARTIFACTORY_TOKEN}".encode()).decode()
        headers['Authorization'] = f'Basic {creds}'

    ssl_verify = os.getenv('ARTIFACTORY_VERIFY_SSL', os.getenv('SSL_VERIFY', 'true')).lower() != 'false'

    # Try multiple API patterns — different Artifactory setups use different paths:
    #  1. /api/storage/{path}              — Standard Artifactory OSS/Pro
    #  2. /artifactory/api/storage/{path}  — Artifactory with /artifactory/ context
    #  3. Direct path with JSON Accept     — Works on most Artifactory instances
    #  4. /api/storage/{path}?list         — Enhanced listing with dates (Enterprise)
    urls_to_try = [
        (f"{ARTIFACTORY_URL}/api/storage/{artifactory_path}", headers),
        (f"{ARTIFACTORY_URL}/artifactory/api/storage/{artifactory_path}", headers),
        (f"{ARTIFACTORY_URL}/{artifactory_path}/", {**headers, 'Accept': 'application/json'}),
        (f"{ARTIFACTORY_URL}/api/storage/{artifactory_path}?list&deep=0&listFolders=1", headers),
    ]

    data = None
    for url, req_headers in urls_to_try:
        try:
            print(f"[artifactory] Trying: {url}")
            resp = requests.get(url, headers=req_headers, timeout=15, verify=ssl_verify)
            if resp.status_code == 404:
                print(f"[artifactory] 404 for {url}, trying next...")
                continue
            if resp.status_code == 401 or resp.status_code == 403:
                print(f"[artifactory] Auth error ({resp.status_code}) for {url} — check ARTIFACTORY_USER/TOKEN")
                continue
            resp.raise_for_status()
            data = resp.json()
            print(f"[artifactory] ✅ Success: {url} (keys: {list(data.keys())[:5]})")
            break
        except requests.exceptions.RequestException as e:
            print(f"[artifactory] Error for {url}: {e}")
            continue
        except ValueError:
            print(f"[artifactory] Invalid JSON from {url}")
            continue

    if data is None:
        print(f"[artifactory] ❌ All URLs failed for {artifactory_path}")
        return []

    # Parse response — Artifactory /api/storage returns 'children' array
    # Each child: {"uri": "/1.2.3", "folder": true}
    # Or with ?list: {"files": [{"uri": "/1.2.3", "folder": true, "lastModified": "..."}]}
    versions = []

    if 'files' in data:
        # ?list response format
        for item in data.get('files', []):
            uri = item.get('uri', '').strip('/')
            if item.get('folder', False) and uri:
                versions.append({
                    'version': uri,
                    'date': item.get('lastModified', ''),
                    'size': item.get('size', 0)
                })
            elif uri and not item.get('folder', False):
                # File-based: extract version from filename
                # e.g. "app-1.2.3.jar" → "1.2.3"
                import re
                match = re.search(r'(\d+\.\d+[\w.-]*)', uri)
                if match:
                    versions.append({
                        'version': match.group(1),
                        'date': item.get('lastModified', ''),
                        'size': item.get('size', 0)
                    })
    elif 'children' in data:
        # Standard /api/storage response format
        for child in data.get('children', []):
            uri = child.get('uri', '').strip('/')
            if child.get('folder', False) and uri:
                versions.append({
                    'version': uri,
                    'date': '',  # No date in children response; need individual lookup
                    'size': 0
                })

    # If we have children but no dates, fetch the folder info for each (limited to top 10)
    if versions and not any(v['date'] for v in versions):
        for v in versions[:10]:
            try:
                folder_url = f"{ARTIFACTORY_URL}/api/storage/{artifactory_path}/{v['version']}"
                r2 = requests.get(folder_url, headers=headers, timeout=5, verify=ssl_verify)
                if r2.ok:
                    d2 = r2.json()
                    v['date'] = d2.get('lastModified', d2.get('created', ''))
            except Exception:
                pass

    # Add freshness labels
    for v in versions:
        v['freshness'] = _version_freshness(v['date']) if v['date'] else 'unknown'

    # Sort by date descending (newest first), unknown dates at end
    def sort_key(v):
        if v['date']:
            return v['date']
        return '0000'  # Unknown dates sort last
    versions.sort(key=sort_key, reverse=True)

    # Limit to 20 most recent
    versions = versions[:20]

    # Cache for 5 minutes
    _cache_set(cache_key, versions)
    return versions


@app.route('/api/artifactory/versions/<component_name>')
def get_artifactory_versions(component_name):
    """Fetch available versions from Artifactory for a custom component."""
    comp = CUSTOM_COMPONENTS_MAP.get(component_name)
    if not comp:
        return jsonify({'error': f'Component {component_name} not found'}), 404

    art_path = comp.get('artifactory_path', '')
    if not art_path:
        return jsonify({
            'component': component_name,
            'artifactory_configured': False,
            'message': 'No Artifactory path configured for this component',
            'versions': []
        })

    if not ARTIFACTORY_URL:
        return jsonify({
            'component': component_name,
            'artifactory_configured': False,
            'message': 'ARTIFACTORY_URL not configured',
            'versions': []
        })

    versions = _fetch_artifactory_versions(art_path)
    latest = versions[0] if versions else None

    return jsonify({
        'component': component_name,
        'artifactory_configured': True,
        'artifactory_path': art_path,
        'latest_version': latest['version'] if latest else None,
        'latest_date': latest['date'] if latest else None,
        'freshness': latest['freshness'] if latest else None,
        'versions': versions
    })


# ── Production Cluster (Remote OpenShift) ─────────────────────────────────────
# Connect to a remote OpenShift/K8s production cluster to list live versions.
# Required env vars:
#   PROD_CLUSTER_API   - OpenShift API URL, e.g. https://api.openshift-prod.example.com:6443
#   PROD_CLUSTER_TOKEN - ServiceAccount bearer token with 'view' role
#   PROD_NAMESPACE     - Target namespace in the prod cluster
# Optional:
#   PROD_CLUSTER_CA_CERT   - Path to CA certificate file for SSL verification
#   PROD_CLUSTER_VERIFY_SSL - Set to 'false' to disable SSL verification (not recommended)

def _get_prod_api_client():
    """Create a Kubernetes API client for the remote production cluster."""
    prod_api = os.environ.get('PROD_CLUSTER_API', '')
    prod_token = os.environ.get('PROD_CLUSTER_TOKEN', '')
    print(f'[prod] PROD_CLUSTER_API = {"SET (" + prod_api[:20] + "...)" if prod_api else "EMPTY"}')
    print(f'[prod] PROD_CLUSTER_TOKEN = {"SET (" + str(len(prod_token)) + " chars)" if prod_token else "EMPTY"}')
    if not prod_api or not prod_token:
        return None, f'PROD_CLUSTER_API {"✅" if prod_api else "❌ EMPTY"} and PROD_CLUSTER_TOKEN {"✅" if prod_token else "❌ EMPTY"} — both are required'

    prod_config = client.Configuration()
    prod_config.host = prod_api
    prod_config.api_key = {"authorization": f"Bearer {prod_token}"}
    # Connection timeout to prevent pod hang/OOM on unreachable clusters
    prod_config.connection_pool_maxsize = 4
    prod_config.retries = 1

    # SSL configuration
    ca_cert = os.environ.get('PROD_CLUSTER_CA_CERT', '')
    verify_ssl = os.environ.get('PROD_CLUSTER_VERIFY_SSL', 'true').lower()
    if ca_cert:
        prod_config.ssl_ca_cert = ca_cert
        prod_config.verify_ssl = True
    elif verify_ssl == 'false':
        prod_config.verify_ssl = False
        import urllib3
        urllib3.disable_warnings(urllib3.exceptions.InsecureRequestWarning)
    else:
        prod_config.verify_ssl = True

    return client.ApiClient(prod_config), None


# ── UAT Cluster (Remote) ─────────────────────────────────────────────────────
# Connect to a remote UAT cluster when app is deployed in production.
# Required env vars (only when DEPLOY_ENV=prod):
#   UAT_CLUSTER_API    - UAT OpenShift API URL
#   UAT_CLUSTER_TOKEN  - ServiceAccount bearer token with 'view' role
#   UAT_NAMESPACE      - Target namespace in the UAT cluster
# Optional:
#   UAT_CLUSTER_VERIFY_SSL - Set to 'false' to disable SSL verification

def _get_uat_api_client():
    """Create a Kubernetes API client for the remote UAT cluster."""
    uat_api = os.environ.get('UAT_CLUSTER_API', '')
    uat_token = os.environ.get('UAT_CLUSTER_TOKEN', '')
    print(f'[uat-remote] UAT_CLUSTER_API = {"SET (" + uat_api[:20] + "...)" if uat_api else "EMPTY"}')
    print(f'[uat-remote] UAT_CLUSTER_TOKEN = {"SET (" + str(len(uat_token)) + " chars)" if uat_token else "EMPTY"}')
    if not uat_api or not uat_token:
        return None, f'UAT_CLUSTER_API {"✅" if uat_api else "❌ EMPTY"} and UAT_CLUSTER_TOKEN {"✅" if uat_token else "❌ EMPTY"} — both are required'

    uat_config = client.Configuration()
    uat_config.host = uat_api
    uat_config.api_key = {"authorization": f"Bearer {uat_token}"}

    # SSL configuration
    verify_ssl = os.environ.get('UAT_CLUSTER_VERIFY_SSL', 'true').lower()
    if verify_ssl == 'false':
        uat_config.verify_ssl = False
        import urllib3
        urllib3.disable_warnings(urllib3.exceptions.InsecureRequestWarning)
    else:
        uat_config.verify_ssl = True

    return client.ApiClient(uat_config), None


def _list_services_from_api(api_client, namespace, log_prefix='[remote]'):
    """Shared helper to list Deployments/StatefulSets/DaemonSets from a K8s API client."""
    services = []
    try:
        apps_v1 = client.AppsV1Api(api_client)
    except Exception as e:
        print(f'{log_prefix} ❌ Failed to create AppsV1Api: {e}')
        raise
    print(f'{log_prefix} API client ready, listing deployments in namespace={namespace}...')

    # Deployments
    try:
        deploys = apps_v1.list_namespaced_deployment(namespace).items
        for d in deploys:
            containers = d.spec.template.spec.containers or []
            image = containers[0].image if containers else ''
            services.append({
                'name': d.metadata.name,
                'kind': 'Deployment',
                'image': image,
                'image_tag': _extract_image_tag(image),
                'helm_version': _extract_helm_version(d.metadata.labels),
                'replicas': d.status.ready_replicas or 0,
                'desired_replicas': d.spec.replicas or 1,
                'available': (d.status.ready_replicas or 0) >= (d.spec.replicas or 1),
                'created': d.metadata.creation_timestamp.isoformat() if d.metadata.creation_timestamp else None
            })
    except Exception as e:
        print(f"{log_prefix} Deployments error: {e}")

    # StatefulSets
    try:
        sts = apps_v1.list_namespaced_stateful_set(namespace).items
        for s in sts:
            containers = s.spec.template.spec.containers or []
            image = containers[0].image if containers else ''
            services.append({
                'name': s.metadata.name,
                'kind': 'StatefulSet',
                'image': image,
                'image_tag': _extract_image_tag(image),
                'helm_version': _extract_helm_version(s.metadata.labels),
                'replicas': s.status.ready_replicas or 0,
                'desired_replicas': s.spec.replicas or 1,
                'available': (s.status.ready_replicas or 0) >= (s.spec.replicas or 1),
                'created': s.metadata.creation_timestamp.isoformat() if s.metadata.creation_timestamp else None
            })
    except Exception as e:
        print(f"{log_prefix} StatefulSets error: {e}")

    # DaemonSets
    try:
        dss = apps_v1.list_namespaced_daemon_set(namespace).items
        for d in dss:
            containers = d.spec.template.spec.containers or []
            image = containers[0].image if containers else ''
            services.append({
                'name': d.metadata.name,
                'kind': 'DaemonSet',
                'image': image,
                'image_tag': _extract_image_tag(image),
                'helm_version': _extract_helm_version(d.metadata.labels),
                'replicas': d.status.number_ready or 0,
                'desired_replicas': d.status.desired_number_scheduled or 1,
                'available': (d.status.number_ready or 0) >= (d.status.desired_number_scheduled or 1),
                'created': d.metadata.creation_timestamp.isoformat() if d.metadata.creation_timestamp else None
            })
    except Exception as e:
        print(f"{log_prefix} DaemonSets error: {e}")

    return services


@app.route('/api/prod/services')
def list_prod_services():
    """List all services from the production cluster.

    When DEPLOY_ENV=prod: reads from the LOCAL cluster (we're already in prod).
    When DEPLOY_ENV=uat:  connects REMOTELY via PROD_CLUSTER_API/TOKEN.
    """
    prod_ns = os.environ.get('PROD_NAMESPACE', NAMESPACE)

    # Check cache first
    cache_key = ('prod_services', prod_ns)
    cached = _cache_get(cache_key)
    if cached:
        return jsonify(cached)

    # ── DEPLOY_ENV=prod: read local cluster ──
    if DEPLOY_ENV == 'prod':
        print(f'[prod] DEPLOY_ENV=prod → reading LOCAL cluster, namespace={prod_ns}')
        try:
            services = _list_services_from_api(client.ApiClient(), prod_ns, '[prod-local]')
            result = {
                'services': services, 'namespace': prod_ns,
                'cluster': 'local (production)', 'count': len(services),
                'connected': True, 'deploy_env': DEPLOY_ENV
            }
            _cache_set(cache_key, result)
            return jsonify(result)
        except Exception as e:
            print(f'[prod-local] ❌ Error reading local cluster: {e}')
            return jsonify({
                'error': str(e), 'services': [], 'namespace': prod_ns,
                'cluster': 'local (production)', 'connected': False
            }), 500

    # ── DEPLOY_ENV=uat: connect to remote prod cluster ──
    print(f'[prod] DEPLOY_ENV=uat → connecting REMOTELY to prod cluster')
    try:
        api_client, error = _get_prod_api_client()
    except Exception as e:
        print(f'[prod] ❌ Fatal error creating API client: {e}')
        return jsonify({
            'services': [], 'namespace': prod_ns,
            'cluster': os.environ.get('PROD_CLUSTER_API', ''),
            'count': 0, 'connected': False,
            'error': f'Failed to create API client: {type(e).__name__}: {str(e)[:200]}'
        })

    if error:
        print(f'[prod] Client creation failed: {error}')
        return jsonify({
            'services': [], 'namespace': prod_ns,
            'cluster': os.environ.get('PROD_CLUSTER_API', ''),
            'count': 0, 'connected': False, 'error': error
        })

    prod_api_url = os.environ.get('PROD_CLUSTER_API', '')
    print(f'[prod] Connecting to {prod_api_url}, namespace={prod_ns}')
    try:
        services = _list_services_from_api(api_client, prod_ns, '[prod-remote]')
    except Exception as e:
        import traceback
        tb = traceback.format_exc()
        error_type = type(e).__name__
        error_msg = str(e)[:300]
        detail = ''
        if hasattr(e, 'status'):
            detail = f' (HTTP {e.status})'
        if hasattr(e, 'reason'):
            detail += f' Reason: {e.reason}'
        if hasattr(e, 'body'):
            try:
                import json as _json
                body = _json.loads(e.body)
                detail += f' Message: {body.get("message", "")[:200]}'
            except Exception:
                detail += f' Body: {str(e.body)[:200]}'
        print(f'[prod] ❌ Connection FAILED: {error_type}: {error_msg}{detail}')
        print(f'[prod] Traceback: {tb}')
        return jsonify({
            'error': f'{error_type}: {error_msg}{detail}',
            'services': [], 'namespace': prod_ns,
            'cluster': prod_api_url, 'connected': False
        })

    result = {
        'services': services, 'namespace': prod_ns,
        'cluster': prod_api_url, 'count': len(services),
        'connected': True, 'deploy_env': DEPLOY_ENV
    }
    _cache_set(cache_key, result)
    return jsonify(result)


# ── Release Board CRUD ────────────────────────────────────────────────────────
@app.route('/api/release/current')
def get_current_release():
    """Get the current release board."""
    board = _read_board()
    if not board:
        board = _new_board()
        try:
            _write_board(board)
        except Exception as e:
            print(f"[release] Could not create board: {e}")

    # Auto-archival: if board is released and we're past the release date (11:59 PM),
    # automatically start a new cycle so the dashboard is fresh on Monday morning.
    if board.get('status') == 'released' and board.get('release_date'):
        try:
            release_date = datetime.date.fromisoformat(board['release_date'])
            # Archive after 11:59 PM on release day (i.e. the next calendar day)
            archive_threshold = datetime.datetime.combine(
                release_date + datetime.timedelta(days=1),
                datetime.time(0, 0)
            )
            if datetime.datetime.utcnow() >= archive_threshold:
                print(f"[release] Auto-archiving released board for {board['release_date']}, starting new cycle")
                board['audit_trail'].append({
                    'action': 'auto_archive',
                    'by': 'system',
                    'at': datetime.datetime.utcnow().isoformat(),
                    'note': f"Board auto-archived after release date {board['release_date']}"
                })
                _write_board(board)  # Save the audit entry to the old board
                board = _new_board()
                _write_board(board)
        except (ValueError, TypeError) as e:
            print(f"[release] Auto-archive date parse error: {e}")

    # Enrich with live metadata
    board['is_past_cutoff'] = datetime.datetime.utcnow().isoformat() > board.get('cutoff', '')
    board['nominated_count'] = len(board.get('services', {}))
    board['exception_count'] = len(board.get('exception_nominations', []))
    return jsonify(board)


@app.route('/api/release/nominate', methods=['POST'])
def nominate_service():
    """Nominate a service for the current release."""
    data = request.json or {}
    service_name = data.get('service_name', '').strip()
    notes = data.get('notes', '').strip()
    nominated_by = data.get('nominated_by', 'anonymous').strip()
    is_custom = data.get('is_custom', False)
    manual_version = data.get('manual_version', '').strip()
    jira_ids = data.get('jira_ids', '').strip()

    if not service_name:
        return jsonify({'error': 'service_name is required'}), 400

    board = _read_board()
    if not board:
        board = _new_board()

    # Exception nomination fields
    is_exception = data.get('is_exception', False)
    exception_reason = data.get('exception_reason', '').strip()
    exception_approver = data.get('exception_approver', '').strip()

    # Determine if board is effectively locked:
    # 1. Manually locked by Release Manager (status == 'locked'), OR
    # 2. Past the cutoff time (even if nobody clicked Lock Board yet)
    is_past_cutoff = datetime.datetime.utcnow().isoformat() > board.get('cutoff', '')
    board_is_locked = board.get('status') == 'locked' or is_past_cutoff

    if board_is_locked:
        if not is_exception:
            return jsonify({
                'error': 'Release board is locked (past cutoff). Use exception nomination.',
                'is_locked': True,
                'cutoff': board.get('cutoff')
            }), 403

        if not exception_reason or not exception_approver:
            return jsonify({
                'error': 'Exception nominations require a reason and approver name.'
            }), 400

    if board.get('status') == 'released':
        return jsonify({'error': 'This release has already been completed.'}), 403

    now = datetime.datetime.utcnow().isoformat()

    if is_custom:
        # Custom component — version entered manually by developer
        comp = CUSTOM_COMPONENTS_MAP.get(service_name, {})
        image = ''
        image_tag = manual_version or 'unknown'
        helm_version = None
        kind = comp.get('type', 'Custom')
    else:
        # K8s service — auto-fill version from live UAT cluster
        # IMPORTANT: The Board tab lists services from the UAT cluster (via /api/services),
        # so we must read the version from the same source.
        # When DEPLOY_ENV=prod, local cluster IS production — must use remote UAT client.
        # When DEPLOY_ENV=uat, local cluster IS UAT — use default in-cluster client.
        namespace = data.get('namespace', NAMESPACE)
        image = ''
        image_tag = ''
        helm_version = None
        kind = 'Deployment'

        try:
            apps_v1 = None
            if DEPLOY_ENV == 'prod':
                # Deployed in prod → read version from remote UAT cluster
                uat_api_client, uat_err = _get_uat_api_client()
                if uat_err:
                    print(f"[nominate] Cannot reach UAT cluster for version lookup: {uat_err}")
                    # Fall through with empty version — user can re-nominate later
                else:
                    uat_ns = os.environ.get('UAT_NAMESPACE', NAMESPACE)
                    apps_v1 = client.AppsV1Api(uat_api_client)
                    namespace = uat_ns
                    print(f"[nominate] DEPLOY_ENV=prod → reading version from REMOTE UAT cluster, ns={uat_ns}")
            else:
                # Deployed in UAT → read version from local cluster
                apps_v1 = client.AppsV1Api()
                print(f"[nominate] DEPLOY_ENV=uat → reading version from LOCAL cluster, ns={namespace}")

            if apps_v1:
                try:
                    d = _k8s_retry(apps_v1.read_namespaced_deployment, service_name, namespace)
                    containers = d.spec.template.spec.containers or []
                    image = containers[0].image if containers else ''
                    image_tag = _extract_image_tag(image)
                    helm_version = _extract_helm_version(d.metadata.labels)
                    kind = 'Deployment'
                except client.exceptions.ApiException:
                    try:
                        s = _k8s_retry(apps_v1.read_namespaced_stateful_set, service_name, namespace)
                        containers = s.spec.template.spec.containers or []
                        image = containers[0].image if containers else ''
                        image_tag = _extract_image_tag(image)
                        helm_version = _extract_helm_version(s.metadata.labels)
                        kind = 'StatefulSet'
                    except client.exceptions.ApiException:
                        pass
        except Exception as e:
            print(f"[nominate] K8s lookup error: {e}")

    # Check if re-nomination
    existing = board.get('services', {}).get(service_name)
    action_type = 're-nominate' if existing else 'nominate'

    if existing:
        old_tag = existing.get('image_tag', '')
        existing['image'] = image
        existing['image_tag'] = image_tag
        existing['helm_version'] = helm_version
        existing['notes'] = notes
        existing['jira_ids'] = jira_ids or existing.get('jira_ids', '')
        existing['updated_at'] = now
        existing['updated_by'] = nominated_by
        existing['version_history'].append({
            'from_tag': old_tag,
            'to_tag': image_tag,
            'changed_by': nominated_by,
            'changed_at': now,
            'reason': notes or 'Version update',
            'is_exception': is_exception,
            **(({'exception_approver': exception_approver, 'exception_reason': exception_reason})
               if is_exception else {})
        })

        # Exception metadata on re-nomination
        if is_exception:
            existing['is_exception'] = True
            existing['exception_reason'] = exception_reason
            existing['exception_approver'] = exception_approver
            existing['exception_at'] = now

        board['audit_trail'].append({
            'action': f'exception-{action_type}' if is_exception else action_type,
            'service': service_name,
            'from_version': old_tag,
            'to_version': image_tag,
            'by': nominated_by,
            'at': now,
            **(({'approver': exception_approver, 'reason': exception_reason})
               if is_exception else {})
        })
    else:
        svc_record = {
            'name': service_name,
            'kind': kind,
            'is_custom': is_custom,
            'image': image,
            'image_tag': image_tag,
            'helm_version': helm_version,
            'nominated_by': nominated_by,
            'nominated_at': now,
            'updated_at': now,
            'updated_by': nominated_by,
            'notes': notes,
            'jira_ids': jira_ids,
            'readiness': None,
            'readiness_details': None,
            'is_exception': is_exception,
            'version_history': [{
                'from_tag': None,
                'to_tag': image_tag,
                'changed_by': nominated_by,
                'changed_at': now,
                'reason': 'Exception nomination' if is_exception else 'Initial nomination',
                'is_exception': is_exception,
                **(({'exception_approver': exception_approver, 'exception_reason': exception_reason})
                   if is_exception else {})
            }]
        }

        # Exception metadata on new nomination
        if is_exception:
            svc_record['exception_reason'] = exception_reason
            svc_record['exception_approver'] = exception_approver
            svc_record['exception_at'] = now

        board.setdefault('services', {})[service_name] = svc_record

        board['audit_trail'].append({
            'action': f'exception-{action_type}' if is_exception else action_type,
            'service': service_name,
            'version': image_tag,
            'by': nominated_by,
            'at': now,
            **(({'approver': exception_approver, 'reason': exception_reason})
               if is_exception else {})
        })

    # Track exception nominations separately for reporting
    if is_exception:
        board.setdefault('exception_nominations', []).append({
            'service': service_name,
            'requested_by': nominated_by,
            'approver': exception_approver,
            'reason': exception_reason,
            'image_tag': image_tag,
            'at': now,
            'action': action_type
        })

    _write_board(board)
    return jsonify({
        'status': 'ok',
        'service': service_name,
        'image_tag': image_tag,
        'is_exception': is_exception
    })


@app.route('/api/release/rollback', methods=['POST'])
def rollback_version():
    """Rollback a nominated service to a previously nominated version."""
    data = request.json or {}
    service_name = data.get('service_name', '').strip()
    target_tag = data.get('target_tag', '').strip()
    rolled_back_by = data.get('rolled_back_by', 'anonymous').strip()

    if not service_name or not target_tag:
        return jsonify({'error': 'service_name and target_tag required'}), 400

    board = _read_board()
    if not board:
        return jsonify({'error': 'No active release board'}), 404
    if board.get('status') in ('locked', 'released'):
        return jsonify({'error': 'Release board is locked.'}), 403
    if service_name not in board.get('services', {}):
        return jsonify({'error': f'{service_name} is not nominated'}), 404

    svc = board['services'][service_name]
    old_tag = svc.get('image_tag', '')
    if old_tag == target_tag:
        return jsonify({'status': 'ok', 'message': 'Already at that version'}), 200

    now = datetime.datetime.utcnow().isoformat()

    # Update the image tag (and image path if it's a K8s service)
    if not svc.get('is_custom'):
        base_image = svc.get('image', '').rsplit(':', 1)[0] if ':' in svc.get('image', '') else svc.get('image', '')
        svc['image'] = f'{base_image}:{target_tag}'
    svc['image_tag'] = target_tag
    svc['updated_at'] = now
    svc['updated_by'] = rolled_back_by

    svc.setdefault('version_history', []).append({
        'from_tag': old_tag, 'to_tag': target_tag,
        'changed_by': rolled_back_by, 'changed_at': now,
        'reason': f'Rollback from {old_tag}'
    })
    board['audit_trail'].append({
        'action': 'rollback', 'service': service_name,
        'from_version': old_tag, 'to_version': target_tag,
        'by': rolled_back_by, 'at': now
    })
    _write_board(board)
    return jsonify({'status': 'ok', 'service': service_name, 'image_tag': target_tag})


@app.route('/api/release/remove', methods=['DELETE'])
def remove_nomination():
    """Remove a service nomination."""
    data = request.json or {}
    service_name = data.get('service_name', '').strip()
    removed_by = data.get('removed_by', 'anonymous').strip()

    if not service_name:
        return jsonify({'error': 'service_name is required'}), 400

    board = _read_board()
    if not board:
        return jsonify({'error': 'No active release board'}), 404

    if board.get('status') in ('locked', 'released'):
        return jsonify({'error': 'Release board is locked.'}), 403

    if service_name not in board.get('services', {}):
        return jsonify({'error': f'{service_name} is not nominated'}), 404

    del board['services'][service_name]
    board['audit_trail'].append({
        'action': 'remove',
        'service': service_name,
        'by': removed_by,
        'at': datetime.datetime.utcnow().isoformat()
    })

    _write_board(board)
    return jsonify({'status': 'ok', 'removed': service_name})


@app.route('/api/release/fix_version', methods=['POST'])
def update_fix_version():
    """Update the fix version on the board."""
    data = request.json or {}
    fix_version = data.get('fix_version', '').strip()
    if not fix_version:
        return jsonify({'error': 'fix_version is required'}), 400
    board = _read_board()
    if not board:
        return jsonify({'error': 'No active board'}), 404
    board['fix_version'] = fix_version
    board['audit_trail'].append({
        'action': 'update_fix_version', 'by': data.get('updated_by', 'unknown'),
        'at': datetime.datetime.utcnow().isoformat(),
        'note': f'Fix version changed to {fix_version}'
    })
    _write_board(board)
    return jsonify({'status': 'ok', 'fix_version': fix_version})


@app.route('/api/release/jira_by_fix_version', methods=['POST'])
def jira_by_fix_version():
    """Fetch all Jira tickets for a given fix version."""
    data = request.json or {}
    fix_version = data.get('fix_version', '').strip()
    if not fix_version:
        return jsonify({'error': 'fix_version is required'}), 400

    try:
        issues = _fetch_jira_by_fix_version(fix_version)
        if issues:
            return jsonify({'fix_version': fix_version, 'total': len(issues), 'issues': issues})
        return jsonify({
            'fix_version': fix_version, 'total': 0, 'issues': [],
            'info': 'No tickets found. Ensure JIRA_MCP_URL or JIRA_BASE_URL + JIRA_PAT_TOKEN are configured.'
        })
    except Exception as e:
        return jsonify({'error': str(e), 'fix_version': fix_version, 'total': 0, 'issues': []}), 500


@app.route('/api/release/finalize', methods=['POST'])
def finalize_release():
    """Lock the release board (cutoff)."""
    data = request.json or {}
    finalized_by = data.get('finalized_by', 'release-manager')

    board = _read_board()
    if not board:
        return jsonify({'error': 'No active release board'}), 404

    if board.get('status') == 'released':
        return jsonify({'error': 'Already released. Cannot lock a completed release.', 'board_status': 'released'}), 400

    if board.get('status') == 'locked':
        return jsonify({'error': 'Board is already locked.', 'board_status': 'locked'}), 400

    now = datetime.datetime.utcnow().isoformat()
    board['status'] = 'locked'
    board['finalized_by'] = finalized_by
    board['finalized_at'] = now
    board['audit_trail'].append({
        'action': 'finalize',
        'by': finalized_by,
        'at': now
    })

    _write_board(board)
    return jsonify({'status': 'locked', 'finalized_by': finalized_by})


@app.route('/api/release/unlock', methods=['POST'])
def unlock_release():
    """Unlock a locked board so QA/RM can edit nominations."""
    data = request.json or {}
    unlocked_by = data.get('unlocked_by', 'release-manager')

    board = _read_board()
    if not board:
        return jsonify({'error': 'No active release board'}), 404

    if board.get('status') == 'released':
        return jsonify({'error': 'Cannot unlock a completed release.', 'board_status': 'released'}), 400

    if board.get('status') != 'locked':
        return jsonify({'error': 'Board is not locked.', 'board_status': board.get('status')}), 400

    now = datetime.datetime.utcnow().isoformat()
    board['status'] = 'open'
    board['audit_trail'].append({
        'action': 'unlock',
        'by': unlocked_by,
        'at': now,
        'note': 'Board unlocked for editing'
    })

    _write_board(board)
    return jsonify({'status': 'open', 'unlocked_by': unlocked_by})


@app.route('/api/release/complete', methods=['POST'])
def complete_release():
    """Mark the release as completed and archive it."""
    data = request.json or {}
    completed_by = data.get('completed_by', 'release-manager')

    board = _read_board()
    if not board:
        return jsonify({'error': 'No active release board'}), 404

    if board.get('status') == 'released':
        return jsonify({'error': 'Already released.', 'board_status': 'released'}), 400

    now = datetime.datetime.utcnow().isoformat()
    board['status'] = 'released'
    board['released_at'] = now
    board['released_by'] = completed_by
    board['audit_trail'].append({
        'action': 'release',
        'by': completed_by,
        'at': now
    })

    _write_board(board)

    # Archive board snapshot to release history
    import copy
    snapshot = copy.deepcopy(board)
    _release_history.append(snapshot)

    return jsonify({'status': 'released'})


@app.route('/api/release/new_cycle', methods=['POST'])
def start_new_cycle():
    """Start a new release cycle (creates a fresh board)."""
    board = _read_board()
    if board and board.get('status') != 'released':
        return jsonify({
            'error': 'Current board is not released yet. Complete the current release first.',
            'board_status': board.get('status')
        }), 400

    new_board = _new_board()
    _write_board(new_board)
    return jsonify({
        'status': 'ok',
        'release_date': new_board['release_date'],
        'message': f"New release cycle started for {new_board['release_date']}"
    })


@app.route('/api/release/history')
def get_release_history():
    """Return archived release boards from previous cycles."""
    summaries = []
    for board in reversed(_release_history):
        svc_list = []
        for name, svc in board.get('services', {}).items():
            svc_list.append({
                'name': name,
                'image_tag': svc.get('image_tag', ''),
                'helm_version': svc.get('helm_version', ''),
                'nominated_by': svc.get('nominated_by', ''),
                'readiness': svc.get('readiness', 'unknown'),
                'jira_ids': svc.get('jira_ids', ''),
                'notes': svc.get('notes', '')
            })
        summaries.append({
            'release_date': board.get('release_date', ''),
            'fix_version': board.get('fix_version', ''),
            'status': board.get('status', ''),
            'released_at': board.get('released_at', ''),
            'released_by': board.get('released_by', ''),
            'finalized_by': board.get('finalized_by', ''),
            'service_count': len(board.get('services', {})),
            'exception_count': len(board.get('exception_nominations', [])),
            'services': svc_list,
            'cutoff': board.get('cutoff', '')
        })
    return jsonify({'history': summaries, 'count': len(summaries)})

@app.route('/api/release/exceptions')
def get_exception_stats():
    """Get exception nomination stats for the current release board."""
    board = _read_board()
    exceptions = board.get('exception_nominations', []) if board else []

    # Group by requester
    by_requester = {}
    for exc in exceptions:
        key = exc.get('requested_by', 'unknown')
        by_requester.setdefault(key, []).append(exc)

    # Group by approver
    by_approver = {}
    for exc in exceptions:
        key = exc.get('approver', 'unknown')
        by_approver.setdefault(key, []).append(exc)

    return jsonify({
        'release_date': board.get('release_date') if board else None,
        'total_exceptions': len(exceptions),
        'exceptions': exceptions,
        'by_requester': {k: len(v) for k, v in by_requester.items()},
        'by_approver': {k: len(v) for k, v in by_approver.items()}
    })

# ── Version Drift Detection ──────────────────────────────────────────────────
@app.route('/api/release/drift')
def check_drift():
    """Compare nominated versions against live cluster versions."""
    board = _read_board()
    if not board or not board.get('services'):
        return jsonify({'drift_items': [], 'message': 'No nominations to check'})

    namespace = request.args.get('namespace', NAMESPACE)
    drift_items = []

    for svc_name, svc_data in board.get('services', {}).items():
        nominated_tag = svc_data.get('image_tag', '')
        kind = svc_data.get('kind', 'Deployment')
        live_tag = ''
        live_image = ''

        try:
            apps_v1 = client.AppsV1Api()
            if kind == 'Deployment':
                d = _k8s_retry(apps_v1.read_namespaced_deployment, svc_name, namespace)
                containers = d.spec.template.spec.containers or []
                live_image = containers[0].image if containers else ''
            elif kind == 'StatefulSet':
                s = _k8s_retry(apps_v1.read_namespaced_stateful_set, svc_name, namespace)
                containers = s.spec.template.spec.containers or []
                live_image = containers[0].image if containers else ''
            live_tag = _extract_image_tag(live_image)
        except Exception as e:
            print(f"[drift] Error checking {svc_name}: {e}")
            live_tag = 'unknown'

        drift_status = 'match'
        if live_tag != nominated_tag:
            # Determine severity of drift
            if _is_major_version_change(nominated_tag, live_tag):
                drift_status = 'major_drift'
            else:
                drift_status = 'drift'

        drift_items.append({
            'service': svc_name,
            'nominated_tag': nominated_tag,
            'live_tag': live_tag,
            'live_image': live_image,
            'drift_status': drift_status
        })

    return jsonify({'drift_items': drift_items})


def _is_major_version_change(tag_a, tag_b):
    """Check if the version change is a major bump (e.g. v2.x -> v3.x)."""
    def extract_major(tag):
        m = re.search(r'(\d+)', tag or '')
        return int(m.group(1)) if m else 0
    return extract_major(tag_a) != extract_major(tag_b)


# ── AI Readiness Check ────────────────────────────────────────────────────────
@app.route('/api/ai/release_readiness', methods=['POST'])
def ai_release_readiness():
    """Run AI-powered readiness checks on all nominated services."""
    cache_key = ('readiness', _get_current_release_date())
    cached = _cache_get(cache_key)
    if cached:
        return jsonify(cached)

    board = _read_board()
    if not board or not board.get('services'):
        return jsonify({'error': 'No nominations to check'}), 400

    namespace = request.args.get('namespace', NAMESPACE)

    # Collect cluster health data
    service_summaries = []
    v1 = client.CoreV1Api()

    for svc_name, svc_data in board.get('services', {}).items():
        summary_lines = [f"Service: {svc_name}"]
        summary_lines.append(f"  Kind: {svc_data.get('kind', 'Deployment')}")
        summary_lines.append(f"  Image: {svc_data.get('image', '?')}")
        summary_lines.append(f"  Nominated Tag: {svc_data.get('image_tag', '?')}")
        summary_lines.append(f"  Helm Chart: {svc_data.get('helm_version', 'N/A')}")
        summary_lines.append(f"  Nominated By: {svc_data.get('nominated_by', '?')}")

        # Check pod health
        try:
            pods = _k8s_retry(v1.list_namespaced_pod, namespace,
                              label_selector=f"app={svc_name}").items
            if not pods:
                pods = _k8s_retry(v1.list_namespaced_pod, namespace,
                                  label_selector=f"app.kubernetes.io/name={svc_name}").items
            running = sum(1 for p in pods if p.status.phase == 'Running')
            restarts = sum(
                sum(cs.restart_count for cs in (p.status.container_statuses or []))
                for p in pods
            )
            summary_lines.append(f"  Pods: {running}/{len(pods)} running, {restarts} total restarts")

            # Check for recent events (CrashLoopBackOff, OOMKilled)
            for p in pods:
                for cs in (p.status.container_statuses or []):
                    if cs.state.waiting and cs.state.waiting.reason:
                        summary_lines.append(f"  ⚠️ Container {cs.name}: {cs.state.waiting.reason}")
                    if cs.last_state.terminated and cs.last_state.terminated.reason:
                        summary_lines.append(f"  ⚠️ Container {cs.name} last terminated: {cs.last_state.terminated.reason}")
        except Exception as e:
            summary_lines.append(f"  Pods: error checking - {e}")

        # Check for probes
        try:
            apps_v1 = client.AppsV1Api()
            d = _k8s_retry(apps_v1.read_namespaced_deployment, svc_name, namespace)
            for c in (d.spec.template.spec.containers or []):
                has_readiness = 'yes' if c.readiness_probe else 'MISSING'
                has_liveness = 'yes' if c.liveness_probe else 'MISSING'
                summary_lines.append(f"  Probes: readiness={has_readiness}, liveness={has_liveness}")

                # Resource limits
                res = getattr(c, 'resources', None)
                if res:
                    summary_lines.append(f"  Resources: limits={getattr(res, 'limits', 'not set')}, requests={getattr(res, 'requests', 'not set')}")
                else:
                    summary_lines.append(f"  Resources: ⚠️ NOT SET")
        except Exception:
            pass

        service_summaries.append('\n'.join(summary_lines))

    all_summaries = '\n\n'.join(service_summaries)

    if not get_model():
        # Deterministic fallback
        checks = {}
        for svc_name in board.get('services', {}):
            checks[svc_name] = {
                'readiness': 'yellow',
                'score': 70,
                'checks': {'health': 'unknown', 'stability': 'unknown'},
                'summary': 'AI unavailable — manual review recommended',
                'risks': []
            }
        result = {'overall': 'yellow', 'services': checks, 'gemini_powered': False}
        return jsonify(result)

    prompt = f"""You are a release engineer validating services for a production release.

For each nominated service below, evaluate readiness based on:
1. Health Status — are pods running normally?
2. Stability — any restarts, CrashLoopBackOff, OOMKills?
3. Probes — are readiness and liveness probes configured?
4. Resource Limits — are CPU/memory limits set?
5. Image Tag — is it using a proper versioned tag (not :latest)?

Return a JSON object with this structure:
{{
  "overall": "green" | "yellow" | "red",
  "summary": "<one-sentence overall assessment>",
  "services": {{
    "<service-name>": {{
      "readiness": "green" | "yellow" | "red",
      "score": <0-100>,
      "summary": "<one-sentence per-service assessment>",
      "checks": {{
        "health": "pass" | "warning" | "fail",
        "stability": "pass" | "warning" | "fail",
        "probes": "pass" | "warning" | "fail",
        "resources": "pass" | "warning" | "fail",
        "image_tag": "pass" | "warning" | "fail"
      }},
      "risks": ["<risk description>", ...]
    }}
  }}
}}

Return ONLY valid JSON. No markdown fences.

=== NOMINATED SERVICES ===
{all_summaries[:10000]}
"""

    try:
        response = gemini_generate_with_retry(prompt)
        raw = response.text if response else ''
        if not raw.strip():
            result = {'overall': 'yellow', 'services': {}, 'gemini_powered': False,
                      'summary': 'AI returned empty response — try again.'}
            return jsonify(result)

        result = parse_gemini_json(raw)
        result['gemini_powered'] = True
        _cache_set(cache_key, result)

        # Write readiness back to the board
        board = _read_board()
        if board:
            for svc_name, svc_check in result.get('services', {}).items():
                if svc_name in board.get('services', {}):
                    board['services'][svc_name]['readiness'] = svc_check.get('readiness')
                    board['services'][svc_name]['readiness_details'] = svc_check
            _write_board(board)

        return jsonify(result)
    except Exception as e:
        print(f"[readiness] Error: {e}")
        return jsonify({'overall': 'yellow', 'services': {}, 'error': str(e), 'gemini_powered': False}), 500


# ── Release Export ────────────────────────────────────────────────────────────
@app.route('/api/release/export')
def export_release():
    """Export release manifest as YAML + AI-generated release notes."""
    board = _read_board()
    if not board or not board.get('services'):
        return jsonify({'error': 'No nominations to export'}), 400

    fmt = request.args.get('format', 'json')

    # Build manifest
    manifest = {
        'release': {
            'name': f"Release {board.get('release_date', 'unknown')}",
            'fix_version': board.get('fix_version', ''),
            'cutoff': board.get('cutoff'),
            'status': board.get('status'),
            'finalized_by': board.get('finalized_by'),
            'services': []
        }
    }

    for svc_name, svc_data in board.get('services', {}).items():
        manifest['release']['services'].append({
            'name': svc_name,
            'image': svc_data.get('image', ''),
            'image_tag': svc_data.get('image_tag', ''),
            'helm_chart': svc_data.get('helm_version', ''),
            'nominated_by': svc_data.get('nominated_by', ''),
            'jira_ids': svc_data.get('jira_ids', ''),
            'readiness': svc_data.get('readiness', 'unknown'),
            'notes': svc_data.get('notes', '')
        })

    if fmt == 'yaml':
        return app.response_class(
            yaml.dump(manifest, default_flow_style=False),
            mimetype='text/yaml',
            headers={'Content-Disposition': f'attachment; filename=release-{board["release_date"]}.yaml'}
        )

    return jsonify(manifest)


@app.route('/api/jira/issues', methods=['POST'])
def fetch_jira_details():
    """Fetch details for given Jira IDs via MCP server."""
    data = request.json or {}
    jira_ids_str = data.get('jira_ids', '').strip()
    jira_ids = _parse_jira_ids(jira_ids_str)

    if not jira_ids:
        return jsonify({'issues': [], 'errors': ['No valid Jira IDs provided']}), 400

    if not JIRA_MCP_URL:
        return jsonify({'issues': [], 'errors': ['Jira MCP server not configured'],
                       'configured': False})

    issues = _fetch_jira_issues(jira_ids)
    found = list(issues.values())
    missing = [j for j in jira_ids if j not in issues]
    errors = [f'{j}: not found or fetch failed' for j in missing]

    return jsonify({'issues': found, 'errors': errors,
                   'configured': True, 'fetched': len(found)})


# ── Confluence Agent API ──────────────────────────────────────────────────────

@app.route('/api/confluence/search', methods=['POST'])
def api_confluence_search():
    """Search Confluence and optionally generate AI summary."""
    data = request.json or {}
    query = data.get('query', '').strip()
    space = data.get('space_key', '').strip() or None
    ai_summary = data.get('ai_summary', True)

    if not query:
        return jsonify({'results': [], 'ai_summary': None, 'error': 'No query provided'}), 400

    if not CONFLUENCE_MCP_URL and not CONFLUENCE_BASE_URL:
        return jsonify({'results': [], 'ai_summary': None,
                       'configured': False,
                       'error': 'Confluence not configured. Set CONFLUENCE_MCP_URL or CONFLUENCE_BASE_URL.'})

    results = _confluence_search(query, space, max_results=10)

    # AI summarization
    summary = None
    if ai_summary and results:
        try:
            pages_text = []
            for r in results[:3]:
                page = _confluence_get_page(r['id'])
                if page and page.get('body_text'):
                    pages_text.append(f"## {page['title']}\n{page['body_text'][:2000]}")
                elif r.get('excerpt'):
                    pages_text.append(f"## {r['title']}\n{r['excerpt']}")

            if pages_text:
                prompt = f"""Based on these Confluence documentation pages from the organization's wiki, answer the user's question concisely and accurately.

User Question: {query}

Confluence Pages:
{'---'.join(pages_text)}

Instructions:
- Provide a direct, actionable answer.
- If the question is about a procedure, give step-by-step instructions.
- Cite which page each piece of information comes from.
- Flag any warnings, caveats, or prerequisites.
- Use markdown formatting (bold, lists, code blocks) for readability.
- If the pages don't contain enough information to answer fully, say what's missing."""
                response = gemini_generate_with_retry(prompt)
                if response and response.text:
                    summary = response.text
        except Exception as e:
            print(f'[confluence] AI summary error: {e}')

    return jsonify({
        'results': results,
        'ai_summary': summary,
        'query': query,
        'total': len(results),
        'configured': True
    })


@app.route('/api/confluence/page/<page_id>')
def api_confluence_page(page_id):
    """Get full page content for inline preview."""
    if not CONFLUENCE_MCP_URL and not CONFLUENCE_BASE_URL:
        return jsonify({'error': 'Confluence not configured'}), 503

    page = _confluence_get_page(page_id)
    if not page:
        return jsonify({'error': 'Page not found or fetch failed'}), 404
    return jsonify(page)


@app.route('/api/confluence/labels', methods=['POST'])
def api_confluence_by_labels():
    """Search Confluence pages by label(s)."""
    data = request.json or {}
    labels = data.get('labels', [])
    space = data.get('space_key', '').strip() or None

    if not labels:
        return jsonify({'results': [], 'error': 'No labels provided'}), 400

    # Try MCP first
    if CONFLUENCE_MCP_URL:
        for tool_name in ['confluence_search_by_label', 'search_by_label', 'confluence_label_search']:
            raw = _confluence_mcp_call(tool_name, {'labels': labels, 'space_key': space or ''})
            if raw:
                try:
                    data_parsed = json.loads(raw) if isinstance(raw, str) else raw
                    items = data_parsed if isinstance(data_parsed, list) else data_parsed.get('results', [])
                    results = []
                    for item in items:
                        if isinstance(item, dict):
                            results.append({
                                'id': str(item.get('id', '')),
                                'title': item.get('title', '?'),
                                'space': item.get('space', item.get('spaceKey', '?')),
                                'url': item.get('url', ''),
                                'labels': item.get('labels', labels),
                                'excerpt': (item.get('excerpt', '') or '')[:300],
                            })
                    if results:
                        return jsonify({'results': results, 'total': len(results)})
                except Exception as e:
                    print(f'[confluence] Label search parse error: {e}')

    # Fallback: CQL search
    label_cql = ' OR '.join(f'label = "{l}"' for l in labels)
    results = _confluence_search(label_cql, space)
    return jsonify({'results': results, 'total': len(results)})


@app.route('/api/confluence/status')
def api_confluence_status():
    """Check Confluence integration status."""
    return jsonify({
        'configured': bool(CONFLUENCE_MCP_URL or CONFLUENCE_BASE_URL),
        'mcp_url': bool(CONFLUENCE_MCP_URL),
        'rest_url': bool(CONFLUENCE_BASE_URL),
        'spaces': CONFLUENCE_SPACES,
    })


@app.route('/api/ai/release_notes', methods=['POST'])
def generate_release_notes():
    """Generate AI-powered release notes for Teams/Jira.

    When a fix version is set, fetches ALL Jira tickets for that fix version
    and includes their descriptions in the release notes. When Jira IDs are
    also associated per-service, those are merged in.
    """
    try:
        print('[release-notes] Starting AI release notes generation...')
        board = _read_board()
        if not board or not board.get('services'):
            return jsonify({'error': 'No nominations to generate notes for'}), 400

        fix_version = board.get('fix_version', '')

        # ── Step 1: Fetch Jira tickets by fix version ──
        fix_version_issues = _fetch_jira_by_fix_version(fix_version)

        # ── Step 2: Map fix-version issues to services by Jira component ──
        service_names = list(board['services'].keys())
        component_svc_map, unmatched_issues = _map_issues_to_services(
            fix_version_issues, service_names)

        # ── Step 3: Collect per-nomination Jira IDs (manual entries) ──
        all_jira_ids = []
        manual_svc_jira_map = {}  # service_name → [jira_ids]
        for svc_name, svc_data in board['services'].items():
            ids = _parse_jira_ids(svc_data.get('jira_ids', ''))
            if ids:
                manual_svc_jira_map[svc_name] = ids
                all_jira_ids.extend(ids)

        jira_details = {}
        if all_jira_ids:
            jira_details = _fetch_jira_issues(list(set(all_jira_ids)))

        # ── Build combined per-service Jira map ──
        # Merge component-mapped + manually-entered Jira IDs per service
        combined_svc_jiras = {}  # service_name → [issue dicts]
        for svc_name in board['services']:
            combined = []
            seen_ids = set()
            # Component-mapped issues (from fix version)
            for issue in component_svc_map.get(svc_name, []):
                if issue['id'] not in seen_ids:
                    combined.append(issue)
                    seen_ids.add(issue['id'])
            # Manually entered Jira IDs
            for jid in manual_svc_jira_map.get(svc_name, []):
                if jid not in seen_ids:
                    issue = jira_details.get(jid, {'id': jid, 'summary': '', 'type': 'Task', 'status': '?'})
                    combined.append(issue)
                    seen_ids.add(jid)
            if combined:
                combined_svc_jiras[svc_name] = combined

        # All issues (for total count)
        all_issues = {}
        for issue in fix_version_issues:
            all_issues[issue['id']] = issue
        for jid, issue in jira_details.items():
            all_issues[jid] = issue

        total_jira = len(all_issues)
        print(f'[release-notes] {total_jira} total Jira issues, '
              f'{len(combined_svc_jiras)} services have mapped tickets, '
              f'{len(unmatched_issues)} unmatched')

        if not get_model():
            # Deterministic fallback (no Gemini)
            lines = [f"## Release Notes — {board.get('release_date', 'Unknown')}"]
            if fix_version:
                lines.append(f"**Fix Version:** `{fix_version}`\n")
            lines.append(f"**{len(board['services'])} services nominated for release:**\n")
            lines.append("| Service | Version | Jira Tickets | Helm Chart | Notes |")
            lines.append("|---|---|---|---|---|")
            for svc_name, svc_data in board['services'].items():
                # Jira column: show only tickets mapped to THIS service
                svc_issues = combined_svc_jiras.get(svc_name, [])
                jira_col = ', '.join(i['id'] for i in svc_issues) if svc_issues else '—'

                if svc_data.get('is_custom'):
                    ver_str = svc_data.get('image_tag', '?')
                else:
                    tag = svc_data.get('image_tag', '?')
                    helm = svc_data.get('helm_version')
                    ver_str = f"{tag} (Helm: {helm})" if helm else tag
                lines.append(f"| {svc_name} | {ver_str} | {jira_col} | {svc_data.get('helm_version', 'N/A')} | {svc_data.get('notes', '')} |")

            # "What's Changed" — organized by service
            if all_issues:
                lines.append("\n## What's Changed\n")
                if fix_version and fix_version_issues:
                    lines.append(f"*Jira tickets from fix version `{fix_version}` ({len(fix_version_issues)} tickets):*\n")

                # Per-service changes
                for svc_name in board['services']:
                    svc_issues = combined_svc_jiras.get(svc_name, [])
                    if svc_issues:
                        lines.append(f"\n### 📦 {svc_name}")
                        for issue in svc_issues:
                            desc_preview = (issue.get('description', '') or '')[:120]
                            if len(issue.get('description', '')) > 120:
                                desc_preview += '...'
                            itype = issue.get('type', 'Task')
                            icon = '🆕' if itype in ('Story', 'Feature') else '🐛' if itype == 'Bug' else '⚡' if itype == 'Improvement' else '🔧'
                            lines.append(f"- {icon} **{issue['id']}** [{itype}]: {issue.get('summary', '?')} [{issue.get('status', '?')}]")
                            if desc_preview:
                                lines.append(f"  > {desc_preview}")

                # Unmatched tickets (no component match)
                if unmatched_issues:
                    lines.append(f"\n### 📋 Other Changes ({len(unmatched_issues)} tickets)")
                    lines.append("> *These Jira tickets have no component matching a nominated service.*\n")
                    for issue in unmatched_issues:
                        comps = ', '.join(issue.get('components', [])) or 'No component'
                        lines.append(f"- **{issue['id']}** [{issue.get('type', 'Task')}]: {issue.get('summary', '?')} [{issue.get('status', '?')}] — Components: {comps}")

            # Post-cutoff exception warning
            exc_services = [n for n, s in board['services'].items() if s.get('is_exception')]
            if exc_services:
                lines.append("\n## ⚠️ Post-Cutoff Changes\n")
                lines.append("> The following services were nominated **after the cutoff deadline** as exceptions.\n")
                for n in exc_services:
                    s = board['services'][n]
                    lines.append(f"- **{n}** (`{s.get('image_tag', '?')}`) — Approved by: {s.get('exception_approver', '?')}, Reason: {s.get('exception_reason', '?')}")

            return jsonify({'notes': '\n'.join(lines), 'gemini_powered': False,
                           'jira_enriched': total_jira > 0, 'jira_count': total_jira,
                           'fix_version': fix_version,
                           'release_date': board.get('release_date')})

        # ── Build AI prompt with per-service Jira context ──
        service_list = []
        exception_services = []
        for svc_name, svc_data in board['services'].items():
            is_custom = svc_data.get('is_custom', False)
            tag = svc_data.get('image_tag', '?')
            helm = svc_data.get('helm_version')
            if is_custom:
                version_info = f"version={tag}, component_type={svc_data.get('kind', 'Custom')}"
            else:
                version_info = f"image_tag={tag}, helm_chart_version={helm or 'N/A'}"

            svc_line = (f"- {svc_name} [{('CUSTOM COMPONENT' if is_custom else 'K8S SERVICE')}]: "
                        f"{version_info}, "
                        f"readiness={svc_data.get('readiness','?')}, "
                        f"notes=\"{svc_data.get('notes','')}\"")

            # Flag exception nominations
            if svc_data.get('is_exception'):
                svc_line += (f", EXCEPTION_NOMINATION=true, "
                            f"exception_reason=\"{svc_data.get('exception_reason', '')}\", "
                            f"exception_approver=\"{svc_data.get('exception_approver', '')}\"")
                exception_services.append(svc_name)

            # Append ONLY the Jira tickets that belong to THIS service
            svc_issues = combined_svc_jiras.get(svc_name, [])
            if svc_issues:
                svc_ids = [i['id'] for i in svc_issues]
                svc_line += f", jira_tickets=[{', '.join(svc_ids)}]"
                for issue in svc_issues:
                    desc = (issue.get('description', '') or '')[:1500]
                    svc_line += (f"\n    JIRA {issue['id']}: type={issue.get('type','Task')}, "
                                f"status={issue.get('status','?')}, "
                                f"priority={issue.get('priority','Medium')}, "
                                f"summary=\"{issue.get('summary','')}\", "
                                f"description=\"{desc}\"")
            service_list.append(svc_line)

        jira_instruction = ""
        if all_issues:
            jira_instruction = """\n\nJira tickets are associated with each service based on the Jira component field.
Use the Jira ticket summaries and descriptions to explain WHAT actually changed in each service.
Only associate a Jira ticket with the service it is listed under. Organize changes by:
- 🆕 New Features
- 🐛 Bug Fixes
- ⚡ Improvements
- 🔧 Maintenance
"""

        has_jira_context = bool(combined_svc_jiras) or bool(fix_version_issues)
        whats_changed_instruction = (
            'A detailed "What\'s Changed" section organized by SERVICE, then by change type '
            '(Features, Bug Fixes, Improvements, Maintenance) within each service. '
            'For each Jira ticket, write EXACTLY 2-3 sentences explaining: what was changed, '
            'why it was changed, and any technical details from the ticket description. '
            'Only include tickets under the service they are mapped to. '
            'Be consistent — every ticket gets the same level of detail.'
            if has_jira_context else
            'A brief summary of upcoming changes based on service notes '
            '(1-2 sentences per service)'
        )

        exception_instruction = ""
        if exception_services:
            exc_list = ', '.join(exception_services)
            exception_instruction = (
                f"\n\nIMPORTANT: The following services were nominated AFTER the cutoff as "
                f"exception nominations: {exc_list}. Add a '⚠️ Post-Cutoff Changes' section "
                f"at the end highlighting these exception nominations, who requested them, "
                f"the reason, and who approved them. Flag these as higher risk.\n"
            )

        newline = '\n'
        # Include unmatched issues context for AI
        unmatched_context = ''
        if unmatched_issues:
            um_lines = [f'\nUnmatched Jira tickets ({len(unmatched_issues)} — no component matching a nominated service):']
            for issue in unmatched_issues:
                comps = ', '.join(issue.get('components', [])) or 'No component'
                desc = (issue.get('description', '') or '')[:200]
                um_lines.append(f"  - {issue['id']} ({issue.get('type','Task')}) [Components: {comps}]: {issue.get('summary', '?')} — {desc}")
            unmatched_context = '\n'.join(um_lines)

        prompt = f"""Generate professional release notes for the operations team.

Release Date: {board.get('release_date', 'Unknown')}
Fix Version: {fix_version or 'Not set'}
Status: {board.get('status', 'open')}

Nominated services (each service lists ONLY its associated Jira tickets):
{newline.join(service_list)}
{unmatched_context}
{jira_instruction}{exception_instruction}
STRICT FORMAT RULES — follow these exactly:

1. Executive Summary: EXACTLY 2-3 sentences summarising the release scope and impact.

2. Service table with columns: Service | Version | Jira Tickets | Change Summary | Risk Level
   JIRA TICKETS COLUMN: List ONLY the Jira ticket IDs associated with that specific service. If none, put "—".
   VERSION COLUMN RULES:
   - For K8S SERVICE entries: ALWAYS show BOTH values as "<image_tag> (Helm: <helm_chart_version>)"
     Example: "v3.2.1 (Helm: 2.5.0)". If helm is N/A, show "<image_tag> (Helm: N/A)".
   - For CUSTOM COMPONENT entries: show ONLY the version value as-is (no Helm).
     Example: "v1.4.0"
   CHANGE SUMMARY COLUMN: EXACTLY 2-3 sentences per service. Not less, not more.
   RISK LEVEL: One of: 🟢 Low | 🟡 Medium | 🔴 High

3. {whats_changed_instruction}

{'4. An "Other Changes" section listing any unmatched Jira tickets that do not belong to a specific service.' if unmatched_issues else ''}

{'5' if unmatched_issues else '4'}. AI Risk Assessment: A brief risk summary paragraph.

{'6' if unmatched_issues else '5'}. {('A ⚠️ Post-Cutoff Exception Nominations section listing each exception, who requested it, who approved it, and the reason.') if exception_services else ''}
Return ONLY the markdown text, no JSON wrapping. Do NOT wrap in ```markdown``` code fences.
"""

        # ── Async generation: return job_id immediately, generate in background ──
        job_id = str(uuid.uuid4())[:8]
        _release_notes_jobs[job_id] = {'status': 'running', 'notes': '', 'error': None,
                                        'gemini_powered': True, 'jira_enriched': total_jira > 0,
                                        'jira_count': total_jira, 'fix_version': fix_version,
                                        'release_date': board.get('release_date')}

        def _generate_in_background(jid, p):
            try:
                print(f'[release-notes] Job {jid}: calling Gemini...')
                response = gemini_generate_with_retry(p)
                notes = response.text if response else 'AI unavailable'
                _release_notes_jobs[jid]['notes'] = notes
                _release_notes_jobs[jid]['status'] = 'done'
                print(f'[release-notes] Job {jid}: done ({len(notes)} chars)')
            except Exception as e:
                print(f'[release-notes] Job {jid}: Gemini error: {e}')
                _release_notes_jobs[jid]['error'] = str(e)
                _release_notes_jobs[jid]['status'] = 'error'

        t = threading.Thread(target=_generate_in_background, args=(job_id, prompt), daemon=True)
        t.start()
        print(f'[release-notes] Started async job {job_id} with {total_jira} Jira issues')
        return jsonify({'job_id': job_id, 'status': 'running'})

    except Exception as outer_e:
        import traceback
        tb = traceback.format_exc()
        print(f'[release-notes] FATAL ERROR: {outer_e}\n{tb}')
        return jsonify({'error': f'Internal error: {str(outer_e)}', 'notes': '', 'gemini_powered': False}), 500

# In-memory job store for async release notes generation
_release_notes_jobs = {}

@app.route('/api/ai/release_notes/<job_id>')
def release_notes_status(job_id):
    """Poll for async release notes generation status."""
    job = _release_notes_jobs.get(job_id)
    if not job:
        return jsonify({'error': 'Job not found'}), 404
    if job['status'] == 'running':
        return jsonify({'status': 'running'})
    elif job['status'] == 'error':
        return jsonify({'status': 'error', 'error': job['error']}), 500
    else:
        result = {k: v for k, v in job.items() if k != 'status'}
        result['status'] = 'done'
        # Clean up old job after retrieval
        if len(_release_notes_jobs) > 20:
            oldest = list(_release_notes_jobs.keys())[0]
            _release_notes_jobs.pop(oldest, None)
        return jsonify(result)


# ── Release History ───────────────────────────────────────────────────────────
@app.route('/api/release/history')
def release_history():
    """List past release boards."""
    try:
        v1 = client.CoreV1Api()
        cms = _k8s_retry(v1.list_namespaced_config_map, NAMESPACE,
                         label_selector='app=release-readiness').items
        history = []
        for cm in cms:
            try:
                data = json.loads(cm.data.get('manifest.json', '{}'))
                history.append({
                    'release_date': data.get('release_date', cm.metadata.name),
                    'status': data.get('status', 'unknown'),
                    'service_count': len(data.get('services', {})),
                    'finalized_by': data.get('finalized_by'),
                    'created_at': data.get('created_at')
                })
            except Exception:
                pass
        history.sort(key=lambda x: x.get('release_date', ''), reverse=True)
        return jsonify({'releases': history})
    except Exception as e:
        return jsonify({'releases': [], 'error': str(e)})


# ══════════════════════════════════════════════════════════════════════════════
# GitHub OAuth (real — same pattern as Pipeline Hub)
# ══════════════════════════════════════════════════════════════════════════════

@app.route('/login')
def login():
    """Redirect to GitHub's OAuth authorization page."""
    if GH_AUTH_MODE != 'oauth':
        # PAT mode — no OAuth needed, return to home
        return redirect('/')
    state = secrets.token_hex(16)
    session['oauth_state'] = state
    params = {
        'client_id': GITHUB_CLIENT_ID,
        'redirect_uri': _get_callback_url(),
        'scope': 'repo workflow',
        'state': state,
    }
    github_auth_url = f'{GITHUB_URL}/login/oauth/authorize?{urlencode(params)}'
    return redirect(github_auth_url)


@app.route('/auth/callback')
def auth_callback():
    """Handle the OAuth callback from GitHub."""
    if GH_AUTH_MODE != 'oauth':
        return redirect('/')

    # Verify state to prevent CSRF
    state = request.args.get('state', '')
    if state != session.get('oauth_state'):
        return jsonify({'error': 'Invalid OAuth state. Please try logging in again.'}), 403

    code = request.args.get('code', '')
    if not code:
        return jsonify({'error': 'No authorization code received from GitHub.'}), 400

    # Exchange code for token (dedicated session to avoid Kerberos retry conflicts)
    try:
        token_url = f'{GITHUB_URL}/login/oauth/access_token'
        token_payload = {
            'client_id': GITHUB_CLIENT_ID,
            'client_secret': GITHUB_CLIENT_SECRET,
            'code': code,
            'redirect_uri': _get_callback_url(),
        }
        oauth_http = requests.Session()
        oauth_http.verify = SSL_VERIFY
        if PROXY_URL:
            oauth_http.proxies = {'http': PROXY_URL, 'https': PROXY_URL}
            try:
                from requests_kerberos import HTTPKerberosAuth, OPTIONAL
                oauth_http.auth = HTTPKerberosAuth(
                    mutual_authentication=OPTIONAL,
                    force_preemptive=False,
                )
            except ImportError:
                pass

        print(f'[OAuth] Exchanging code for token via {token_url} (proxy: {PROXY_URL or "none"})')
        token_response = oauth_http.post(
            token_url,
            headers={'Accept': 'application/json'},
            data=token_payload,
            timeout=30,
        )
        oauth_http.close()
        token_data = token_response.json()

        if 'access_token' not in token_data:
            error = token_data.get('error_description', token_data.get('error', 'Unknown error'))
            print(f"[OAuth] Token exchange failed: {error}")
            return jsonify({'error': f'OAuth failed: {error}'}), 400

        access_token = token_data['access_token']
        session['github_token'] = access_token

        # Fetch user info
        user_response = gh_http.get(
            f'{GITHUB_API}/user',
            headers={
                'Authorization': f'token {access_token}',
                'Accept': 'application/vnd.github.v3+json',
            },
            timeout=10,
        )
        if user_response.status_code == 200:
            user_data = user_response.json()
            session['github_user'] = {
                'login': user_data.get('login', ''),
                'name': user_data.get('name', ''),
                'avatar_url': user_data.get('avatar_url', ''),
                'logged_in': True,
            }

        session.pop('oauth_state', None)
        print(f"[OAuth] User {session.get('github_user', {}).get('login', 'unknown')} logged in successfully")
        return redirect('/')

    except Exception as e:
        print(f"[OAuth] Error during token exchange: {e}")
        return jsonify({'error': f'OAuth error: {str(e)}'}), 500


@app.route('/logout')
def logout():
    """Clear session and redirect home."""
    user = session.get('github_user', {}).get('login', 'unknown')
    session.clear()
    print(f"[OAuth] User {user} logged out")
    return redirect('/')


@app.route('/api/github/status')
def github_status():
    """Check if user is authenticated with GitHub."""
    if is_gh_authenticated():
        user = session.get('github_user', {})
        if user:
            return jsonify({'logged_in': True, 'user': user, 'auth_mode': GH_AUTH_MODE})
        # PAT mode — try to fetch user from API
        if GH_AUTH_MODE == 'pat':
            try:
                data = _github_get('/user')
                user = {
                    'login': data.get('login', 'service-account'),
                    'name': data.get('name', 'Service Account'),
                    'avatar_url': data.get('avatar_url', ''),
                    'logged_in': True,
                }
                session['github_user'] = user
                return jsonify({'logged_in': True, 'user': user, 'auth_mode': GH_AUTH_MODE})
            except Exception:
                return jsonify({'logged_in': True, 'user': {
                    'login': 'service-account', 'name': 'Service Account',
                    'avatar_url': '', 'logged_in': True
                }, 'auth_mode': GH_AUTH_MODE})
    return jsonify({'logged_in': False, 'auth_mode': GH_AUTH_MODE})


@app.route('/api/github/logout', methods=['POST'])
def github_logout():
    """Clear GitHub session (API endpoint for frontend)."""
    user = session.get('github_user', {}).get('login', 'unknown')
    session.pop('github_token', None)
    session.pop('github_user', None)
    print(f"[OAuth] User {user} logged out via API")
    return jsonify({'status': 'ok'})


# ══════════════════════════════════════════════════════════════════════════════
# Deploy — Real GitHub Actions workflow_dispatch (UAT only)
# ══════════════════════════════════════════════════════════════════════════════

import base64

def _parse_workflow_inputs(content_b64):
    """Parse workflow_dispatch inputs from base64-encoded workflow YAML."""
    inputs = []
    try:
        raw = base64.b64decode(content_b64).decode('utf-8')
        wf = yaml.safe_load(raw)
        if not wf or not isinstance(wf, dict):
            return inputs
        on_section = wf.get('on', wf.get(True, {}))
        if isinstance(on_section, dict):
            dispatch = on_section.get('workflow_dispatch', {})
            if isinstance(dispatch, dict) and dispatch.get('inputs'):
                for name, cfg in dispatch['inputs'].items():
                    inp = {
                        'name': name,
                        'type': cfg.get('type', 'string'),
                        'description': cfg.get('description', ''),
                        'default': str(cfg.get('default', '')),
                        'required': cfg.get('required', False),
                    }
                    if cfg.get('options'):
                        inp['options'] = cfg['options']
                    inputs.append(inp)
    except Exception as e:
        print(f"[parse_inputs] Error: {e}")
    return inputs


def _time_ago(iso_str):
    """Convert ISO timestamp to human-readable 'X ago' string."""
    if not iso_str:
        return 'Never'
    try:
        dt = datetime.datetime.fromisoformat(iso_str.replace('Z', '+00:00'))
        now = datetime.datetime.now(datetime.timezone.utc)
        delta = now - dt
        secs = int(delta.total_seconds())
        if secs < 60:
            return f'{secs}s ago'
        if secs < 3600:
            return f'{secs // 60}m ago'
        if secs < 86400:
            return f'{secs // 3600}h ago'
        return f'{secs // 86400}d ago'
    except Exception:
        return iso_str


@app.route('/api/deploy/workflows')
def deploy_workflows():
    """List all active workflows from the deployment repo with dispatch inputs."""
    if not is_gh_authenticated():
        return jsonify({'workflows': [], 'error': 'GitHub login required'}), 401
    if not DEPLOY_REPO:
        return jsonify({'workflows': [], 'repo': '', 'error': 'DEPLOY_REPO not configured'})

    try:
        owner, repo = DEPLOY_REPO.split('/', 1)
    except ValueError:
        return jsonify({'workflows': [], 'repo': DEPLOY_REPO, 'error': 'Invalid DEPLOY_REPO format'})

    try:
        data = _github_get(f'/repos/{owner}/{repo}/actions/workflows')
        workflows = []

        for w in data.get('workflows', []):
            if w.get('state') != 'active':
                continue

            # Get last run for this workflow
            last_conclusion = None
            last_run_ago = 'Never'
            last_run_by = '--'
            duration = '--'
            branch = ''

            try:
                runs_data = _github_get(
                    f'/repos/{owner}/{repo}/actions/workflows/{w["id"]}/runs',
                    {'per_page': 1}
                )
                runs = runs_data.get('workflow_runs', [])
                if runs:
                    run = runs[0]
                    last_conclusion = run.get('conclusion') or run.get('status')
                    last_run_ago = _time_ago(run.get('created_at'))
                    last_run_by = (run.get('actor') or {}).get('login', '--')
                    branch = run.get('head_branch', '')
                    if run.get('created_at') and run.get('updated_at'):
                        try:
                            start = datetime.datetime.fromisoformat(run['created_at'].replace('Z', '+00:00'))
                            end = datetime.datetime.fromisoformat(run['updated_at'].replace('Z', '+00:00'))
                            dur_secs = (end - start).total_seconds()
                            if dur_secs < 60:
                                duration = f'{int(dur_secs)}s'
                            elif dur_secs < 3600:
                                duration = f'{int(dur_secs // 60)}m {int(dur_secs % 60)}s'
                            else:
                                duration = f'{int(dur_secs // 3600)}h {int((dur_secs % 3600) // 60)}m'
                        except Exception:
                            pass
            except Exception as e:
                print(f"[deploy_workflows] Error getting runs for {w['name']}: {e}")

            # Parse workflow_dispatch inputs from the YAML file
            dispatch_inputs = []
            try:
                file_data = _github_get(f'/repos/{owner}/{repo}/contents/{w["path"]}')
                if file_data.get('content'):
                    dispatch_inputs = _parse_workflow_inputs(file_data['content'])
            except Exception:
                pass

            workflows.append({
                'id': w['id'],
                'name': w['name'],
                'file': w['path'].split('/')[-1],
                'state': w['state'],
                'last_conclusion': last_conclusion,
                'last_run_ago': last_run_ago,
                'duration': duration,
                'last_run_by': last_run_by,
                'branch': branch,
                'dispatch_inputs': dispatch_inputs,
            })

        return jsonify({'workflows': workflows, 'repo': DEPLOY_REPO})

    except Exception as e:
        print(f"[deploy_workflows] Error: {e}")
        return jsonify({'workflows': [], 'repo': DEPLOY_REPO, 'error': str(e)}), 500


@app.route('/api/deploy/trigger', methods=['POST'])
def deploy_trigger():
    """Trigger a GitHub Actions workflow_dispatch on the deployment repo."""
    if not is_gh_authenticated():
        return jsonify({'error': 'GitHub login required'}), 401
    if not DEPLOY_REPO:
        return jsonify({'error': 'DEPLOY_REPO not configured. Set the DEPLOY_REPO environment variable.'}), 500

    data = request.json or {}
    workflow_id = data.get('workflow_id', '')
    inputs = data.get('inputs', {})

    # Force environment to UAT
    if 'environment' in inputs:
        inputs['environment'] = 'uat'

    gh_user = session.get('github_user', {})
    now = datetime.datetime.utcnow().isoformat()

    try:
        owner, repo = DEPLOY_REPO.split('/', 1)
    except ValueError:
        return jsonify({'error': f'Invalid DEPLOY_REPO format: "{DEPLOY_REPO}". Expected "owner/repo".'}), 500

    if not workflow_id:
        return jsonify({'error': 'workflow_id is required'}), 400

    try:
        response = _github_post(
            f'/repos/{owner}/{repo}/actions/workflows/{workflow_id}/dispatches',
            {
                'ref': data.get('branch', 'main'),
                'inputs': inputs,
            }
        )

        if response.status_code == 204:
            triggered_by = gh_user.get('login', 'unknown')
            print(f"[Deploy] {triggered_by} triggered workflow {workflow_id} on {DEPLOY_REPO}: "
                  f"inputs={inputs}")

            # Wait briefly and fetch the most recent run
            time.sleep(2)
            run_info = {'run_id': None, 'html_url': None}
            try:
                runs_data = _github_get(
                    f'/repos/{owner}/{repo}/actions/workflows/{workflow_id}/runs',
                    {'per_page': 1, 'event': 'workflow_dispatch'}
                )
                if runs_data.get('workflow_runs'):
                    latest = runs_data['workflow_runs'][0]
                    run_info = {
                        'run_id': latest['id'],
                        'html_url': latest.get('html_url', ''),
                    }
            except Exception as e:
                print(f"[Deploy] Could not fetch run ID: {e}")

            # Add to board audit trail
            board = _read_board()
            if board:
                board['audit_trail'].append({
                    'action': 'deploy_triggered',
                    'service': inputs.get('service', ''),
                    'version': inputs.get('version', ''),
                    'environment': 'uat',
                    'workflow_id': workflow_id,
                    'run_id': run_info.get('run_id'),
                    'by': triggered_by, 'at': now
                })
                _write_board(board)

            return jsonify({
                'status': 'ok',
                'run': {
                    'run_id': run_info.get('run_id'),
                    'inputs': inputs,
                    'environment': 'uat',
                    'status': 'queued',
                    'triggered_by': triggered_by,
                    'triggered_at': now,
                    'html_url': run_info.get('html_url', ''),
                    'repo': DEPLOY_REPO,
                    'workflow_id': workflow_id,
                }
            })

        elif response.status_code == 422:
            error = ''
            try:
                error = response.json().get('message', '')
            except Exception:
                error = response.text[:200]
            return jsonify({'error': f'Cannot trigger: {error}. Ensure workflow has workflow_dispatch trigger.'}), 422
        elif response.status_code == 403:
            return jsonify({'error': 'Permission denied. Your GitHub account may not have write access.'}), 403
        elif response.status_code == 404:
            return jsonify({'error': f'Workflow not found in repo "{DEPLOY_REPO}". Check config.'}), 404
        else:
            return jsonify({'error': f'GitHub returned {response.status_code}: {response.text[:200]}'}), response.status_code

    except Exception as e:
        print(f"[Deploy] Error: {e}")
        return jsonify({'error': str(e)}), 500


@app.route('/api/deploy/status/<run_id>')
def deploy_status(run_id):
    """Poll status of a GitHub Actions workflow run."""
    if not is_gh_authenticated():
        return jsonify({'error': 'Not authenticated'}), 401
    if not DEPLOY_REPO:
        return jsonify({'error': 'DEPLOY_REPO not configured'}), 500

    try:
        owner, repo = DEPLOY_REPO.split('/', 1)
        run_data = _github_get(f'/repos/{owner}/{repo}/actions/runs/{run_id}')

        # Fetch job steps for this run
        steps = []
        try:
            jobs_data = _github_get(f'/repos/{owner}/{repo}/actions/runs/{run_id}/jobs')
            for job in jobs_data.get('jobs', []):
                for step in job.get('steps', []):
                    st = step.get('status', 'queued')
                    conclusion = step.get('conclusion', '')
                    status = 'completed' if conclusion == 'success' else (
                        'failed' if conclusion in ('failure', 'cancelled') else (
                        'in_progress' if st == 'in_progress' else 'pending'
                    ))
                    steps.append({
                        'name': step.get('name', 'Step'),
                        'status': status,
                    })
        except Exception:
            pass

        # Map GitHub run status to our format
        gh_status = run_data.get('status', 'queued')
        conclusion = run_data.get('conclusion')
        if gh_status == 'completed':
            status = 'completed' if conclusion == 'success' else 'failure'
        elif gh_status == 'in_progress':
            status = 'in_progress'
        else:
            status = 'queued'

        return jsonify({
            'run_id': run_id,
            'status': status,
            'conclusion': conclusion,
            'html_url': run_data.get('html_url', ''),
            'steps': steps,
            'completed_at': run_data.get('updated_at'),
        })

    except Exception as e:
        return jsonify({'error': str(e)}), 500


@app.route('/api/deploy/history')
def deploy_history():
    """List recent deploy runs from the deployment repo."""
    if not is_gh_authenticated():
        return jsonify({'runs': [], 'error': 'Not authenticated'}), 401
    if not DEPLOY_REPO:
        return jsonify({'runs': [], 'error': 'DEPLOY_REPO not configured'})

    try:
        owner, repo = DEPLOY_REPO.split('/', 1)
        data = _github_get(
            f'/repos/{owner}/{repo}/actions/workflows/{DEPLOY_WORKFLOW}/runs',
            {'per_page': 20, 'event': 'workflow_dispatch'}
        )
        runs = []
        for run in data.get('workflow_runs', []):
            runs.append({
                'run_id': run['id'],
                'status': run['status'],
                'conclusion': run.get('conclusion'),
                'triggered_by': (run.get('actor') or {}).get('login', 'unknown'),
                'triggered_at': run.get('created_at', ''),
                'completed_at': run.get('updated_at'),
                'html_url': run.get('html_url', ''),
            })
        return jsonify({'runs': runs, 'count': len(runs)})

    except Exception as e:
        return jsonify({'runs': [], 'error': str(e)})


# ══════════════════════════════════════════════════════════════════════════════
# AI Release Chatbot — Gemini Function Calling
# ══════════════════════════════════════════════════════════════════════════════

_chat_sessions = {}   # session_id → { 'history': [...], 'namespace': str }

# ── 6 Release-Specific Tool Functions ─────────────────────────────────────────

def _tool_get_board() -> str:
    """Return the current release board: all nominated services, versions, status."""
    try:
        board = _read_board()
        if not board or not board.get('services'):
            return ("No release board found or no services nominated yet. "
                    f"Release date: {_get_current_release_date()}, Status: no board created.")
        lines = [
            f"Release Date: {board.get('release_date', '?')}",
            f"Board Status: {board.get('status', 'open')}",
            f"Cutoff: {board.get('cutoff', '?')}",
            f"Total Services: {len(board['services'])}",
            "",
            "Service | Image Tag | Helm Chart | Jira Tickets | Nominated By | Nominated At | Kind",
            "--------|-----------|------------|--------------|--------------|--------------|-----",
        ]
        for name, svc in board['services'].items():
            jira_col = svc.get('jira_ids', '') or '—'
            lines.append(
                f"{name} | {svc.get('image_tag', '?')} | "
                f"{svc.get('helm_chart_version', 'n/a')} | "
                f"{jira_col} | "
                f"{svc.get('nominated_by', '?')} | "
                f"{svc.get('nominated_at', '?')} | "
                f"{svc.get('kind', 'Deployment')}"
            )
        return '\n'.join(lines)
    except Exception as e:
        return f'Error reading board: {e}'


def _tool_get_service_status(service_name: str) -> str:
    """Return a single service's nomination details, plus live pod health."""
    try:
        board = _read_board()
        if not board or not board.get('services'):
            return "No release board found."
        svc = board['services'].get(service_name)
        if not svc:
            available = ', '.join(board['services'].keys())
            return (f"Service '{service_name}' is NOT nominated on the board. "
                    f"Available services: {available}")
        lines = [
            f"Service: {service_name}",
            f"Image Tag: {svc.get('image_tag', '?')}",
            f"Helm Chart: {svc.get('helm_chart_version', 'n/a')}",
            f"Kind: {svc.get('kind', 'Deployment')}",
            f"Nominated By: {svc.get('nominated_by', '?')}",
            f"Nominated At: {svc.get('nominated_at', '?')}",
            f"Notes: {svc.get('notes', '')}",
            f"Jira Tickets: {svc.get('jira_ids', '') or 'None'}",
        ]
        # Check live pod health
        try:
            v1 = client.CoreV1Api()
            pods = _k8s_retry(v1.list_namespaced_pod, NAMESPACE,
                              label_selector=f'app={service_name}')
            if pods.items:
                lines.append(f"\nLive Pods ({len(pods.items)}):")
                for p in pods.items:
                    cstats = p.status.container_statuses or []
                    restarts = sum(c.restart_count for c in cstats)
                    ready = sum(1 for c in cstats if c.ready)
                    total = len(cstats) or len(p.spec.containers)
                    lines.append(f"  {p.metadata.name}: {p.status.phase} "
                                 f"ready={ready}/{total} restarts={restarts}")
            else:
                lines.append("\nNo pods found matching this service name.")
        except Exception:
            lines.append("\n(Could not fetch live pod data)")
        return '\n'.join(lines)
    except Exception as e:
        return f'Error: {e}'


def _tool_check_drift() -> str:
    """Compare nominated versions against live cluster versions for all services."""
    try:
        board = _read_board()
        if not board or not board.get('services'):
            return "No nominations to check for drift."
        drift_items = []
        for svc_name, svc_data in board['services'].items():
            nominated_tag = svc_data.get('image_tag', '')
            kind = svc_data.get('kind', 'Deployment')
            live_tag = ''
            try:
                apps_v1 = client.AppsV1Api()
                if kind == 'Deployment':
                    d = _k8s_retry(apps_v1.read_namespaced_deployment, svc_name, NAMESPACE)
                    containers = d.spec.template.spec.containers or []
                    live_tag = _extract_image_tag(containers[0].image) if containers else '?'
                elif kind == 'StatefulSet':
                    s = _k8s_retry(apps_v1.read_namespaced_stateful_set, svc_name, NAMESPACE)
                    containers = s.spec.template.spec.containers or []
                    live_tag = _extract_image_tag(containers[0].image) if containers else '?'
            except Exception:
                live_tag = 'unknown'
            status = '✅ match' if live_tag == nominated_tag else '⚠️ DRIFT'
            drift_items.append(f"{svc_name} | nominated: {nominated_tag} | live: {live_tag} | {status}")

        header = "Service | Nominated | Live | Status\n--------|-----------|------|-------\n"
        return header + '\n'.join(drift_items)
    except Exception as e:
        return f'Error checking drift: {e}'


def _tool_get_readiness() -> str:
    """Return AI readiness scores for all nominated services (cached if available)."""
    try:
        cache_key = ('readiness', _get_current_release_date())
        cached = _cache_get(cache_key)
        if cached:
            results = cached.get('results', [])
            lines = ["Service | Score | Status | Summary",
                      "--------|-------|--------|--------"]
            for r in results:
                lines.append(
                    f"{r.get('service', '?')} | {r.get('score', '?')}/100 | "
                    f"{r.get('status', '?')} | {r.get('summary', '')[:80]}"
                )
            return '\n'.join(lines)
        # No cached results — tell the user to run the check
        return ("No readiness results cached yet. The user should click "
                "'🤖 AI Readiness' tab and run the check first, or POST to "
                "/api/ai/release_readiness to generate scores.")
    except Exception as e:
        return f'Error fetching readiness: {e}'


def _tool_get_audit_trail(limit: int = 20) -> str:
    """Return recent audit trail entries from the release board."""
    try:
        board = _read_board()
        if not board:
            return "No release board found."
        trail = board.get('audit_trail', [])
        if not trail:
            return "Audit trail is empty — no actions recorded yet."
        entries = trail[-limit:]
        lines = ["Action | By | At | Details",
                  "-------|----|----|--------"]
        for e in reversed(entries):
            detail = ''
            if e.get('service'):
                detail = f"service={e['service']}"
            if e.get('image_tag'):
                detail += f" tag={e['image_tag']}"
            if e.get('notes'):
                detail += f" notes={e['notes'][:40]}"
            lines.append(
                f"{e.get('action', '?')} | {e.get('by', '?')} | "
                f"{e.get('at', '?')} | {detail}"
            )
        return '\n'.join(lines)
    except Exception as e:
        return f'Error fetching audit trail: {e}'


def _tool_get_uat_services() -> str:
    """List all services currently running in the UAT namespace with versions."""
    try:
        apps_v1 = client.AppsV1Api()
        services = []
        # Deployments
        try:
            deploys = _k8s_retry(apps_v1.list_namespaced_deployment, NAMESPACE).items
            for d in deploys:
                containers = d.spec.template.spec.containers or []
                image = containers[0].image if containers else ''
                tag = _extract_image_tag(image)
                ready = d.status.ready_replicas or 0
                desired = d.spec.replicas or 1
                services.append(f"{d.metadata.name} | Deployment | {tag} | {ready}/{desired}")
        except Exception:
            pass
        # StatefulSets
        try:
            sts = _k8s_retry(apps_v1.list_namespaced_stateful_set, NAMESPACE).items
            for s in sts:
                containers = s.spec.template.spec.containers or []
                image = containers[0].image if containers else ''
                tag = _extract_image_tag(image)
                ready = s.status.ready_replicas or 0
                desired = s.spec.replicas or 1
                services.append(f"{s.metadata.name} | StatefulSet | {tag} | {ready}/{desired}")
        except Exception:
            pass
        if not services:
            return f"No services found in namespace '{NAMESPACE}'."
        header = f"Namespace: {NAMESPACE}\n\nService | Kind | Image Tag | Ready\n--------|------|-----------|------\n"
        return header + '\n'.join(services)
    except Exception as e:
        return f'Error listing UAT services: {e}'


# ── Gemini Tool Declarations ─────────────────────────────────────────────────

from google.genai import types as _genai_types

_STR = _genai_types.Schema(type='STRING')
_INT = _genai_types.Schema(type='INTEGER')

def _rr_schema(props: dict, required: list = None) -> _genai_types.Schema:
    return _genai_types.Schema(
        type='OBJECT',
        properties={k: _genai_types.Schema(type=v[0], description=v[1]) for k, v in props.items()},
        required=required or []
    )

RELEASE_TOOLS = _genai_types.Tool(function_declarations=[
    _genai_types.FunctionDeclaration(
        name='release_get_board',
        description='Get the current release board with all nominated services, versions, who nominated them, and board status (open/locked/released).',
        parameters=_rr_schema({})
    ),
    _genai_types.FunctionDeclaration(
        name='release_get_service_status',
        description='Get detailed status of a specific nominated service including version, who nominated it, and live pod health.',
        parameters=_rr_schema(
            {'service_name': ('STRING', 'The service name to look up, e.g. billing-service')},
            required=['service_name']
        )
    ),
    _genai_types.FunctionDeclaration(
        name='release_check_drift',
        description='Check version drift — compare nominated versions against what is actually running in the UAT cluster.',
        parameters=_rr_schema({})
    ),
    _genai_types.FunctionDeclaration(
        name='release_get_readiness',
        description='Get AI readiness scores for all nominated services. Shows health score (0-100), status (green/yellow/red), and summary.',
        parameters=_rr_schema({})
    ),
    _genai_types.FunctionDeclaration(
        name='release_get_audit_trail',
        description='Get the audit trail — a chronological log of all board actions (nominations, removals, lock, release, rollbacks).',
        parameters=_rr_schema(
            {'limit': ('INTEGER', 'Number of recent entries to return (default 20)')},
            required=[]
        )
    ),
    _genai_types.FunctionDeclaration(
        name='release_get_uat_services',
        description='List all services currently deployed in the UAT Kubernetes namespace with their image tags and replica status.',
        parameters=_rr_schema({})
    ),
])

# Dispatcher: maps function name → Python function call
_RELEASE_TOOL_MAP = {
    'release_get_board':          lambda a: _tool_get_board(),
    'release_get_service_status': lambda a: _tool_get_service_status(**a),
    'release_check_drift':        lambda a: _tool_check_drift(),
    'release_get_readiness':      lambda a: _tool_get_readiness(),
    'release_get_audit_trail':    lambda a: _tool_get_audit_trail(**{k: v for k, v in a.items() if k == 'limit'}),
    'release_get_uat_services':   lambda a: _tool_get_uat_services(),
}


# ── Chat Endpoint ─────────────────────────────────────────────────────────────

@app.route('/api/ai/converse', methods=['POST'])
def ai_converse():
    """AI Release Chatbot — Gemini function-calling agent for release questions."""
    session_id = request.headers.get('X-Session-Id', 'default')
    data = request.json or {}
    message = data.get('message', '').strip()

    if not message:
        return jsonify({'error': 'No message provided'}), 400

    mdl = get_model()
    if not mdl:
        return jsonify({'reply': '⚠️ Gemini is not configured. '
                                  'Set GCP_PROJECT_ID or GEMINI_API_KEY to enable the chatbot.'}), 200

    try:
        system_instruction = (
            f"You are an expert Release Readiness Assistant embedded in the Release Readiness Dashboard. "
            f"You help developers, QA engineers, and release managers with questions about the current release.\n\n"
            f"You have access to live release board tools. When answering:\n"
            f"1. Use the available tools to fetch REAL data from the release board and K8s cluster\n"
            f"2. Give specific, data-backed answers — never guess or make up service names/versions\n"
            f"3. Use Markdown formatting with tables where helpful\n"
            f"4. Be concise and actionable\n"
            f"5. If asked about a specific service, use release_get_service_status\n"
            f"6. If asked about drift, use release_check_drift\n"
            f"7. If asked about readiness/health, use release_get_readiness\n"
            f"8. For general release questions, use release_get_board\n\n"
            f"Current namespace: {NAMESPACE}\n"
            f"Current release date: {_get_current_release_date()}\n"
            f"Cutoff: {_get_cutoff_datetime()}"
        )

        # Session history management
        if session_id not in _chat_sessions:
            _chat_sessions[session_id] = {'history': [], 'namespace': NAMESPACE}

        sess = _chat_sessions[session_id]
        history = sess['history']

        # Add user message
        history.append(_genai_types.Content(
            role='user',
            parts=[_genai_types.Part(text=message)]
        ))

        # ── Agentic tool-calling loop (max 5 iterations) ──────────────────
        MAX_ITERATIONS = 5
        final_reply = None

        for iteration in range(MAX_ITERATIONS):
            response = mdl.models.generate_content(
                model=GEMINI_MODEL,
                contents=history,
                config=_genai_types.GenerateContentConfig(
                    system_instruction=system_instruction,
                    tools=[RELEASE_TOOLS],
                    tool_config=_genai_types.ToolConfig(
                        function_calling_config=_genai_types.FunctionCallingConfig(
                            mode='AUTO'
                        )
                    ),
                    temperature=0.3,
                )
            )

            candidate = response.candidates[0] if response.candidates else None
            if not candidate:
                final_reply = '⚠️ No response from Gemini.'
                break

            # Collect function calls and text parts
            function_calls = []
            text_parts = []
            for part in (candidate.content.parts or []):
                if part.function_call:
                    function_calls.append(part.function_call)
                elif part.text:
                    text_parts.append(part.text)

            if not function_calls:
                # Final answer — no more tool calls
                final_reply = '\n'.join(text_parts) or '(no response)'
                history.append(candidate.content)
                break

            # Append assistant's tool-call turn
            history.append(candidate.content)

            # Execute each tool call
            tool_response_parts = []
            for fc in function_calls:
                fn_name = fc.name
                fn_args = dict(fc.args) if fc.args else {}
                print(f'[chatbot] Calling tool: {fn_name}({fn_args})')

                if fn_name in _RELEASE_TOOL_MAP:
                    try:
                        result = _RELEASE_TOOL_MAP[fn_name](fn_args)
                    except Exception as tool_err:
                        result = f'Tool error: {tool_err}'
                else:
                    result = f'Unknown tool: {fn_name}'

                tool_response_parts.append(
                    _genai_types.Part(
                        function_response=_genai_types.FunctionResponse(
                            name=fn_name,
                            response={'result': result}
                        )
                    )
                )

            # Append tool results
            history.append(_genai_types.Content(role='user', parts=tool_response_parts))

        else:
            final_reply = final_reply or '⚠️ Agent reached maximum reasoning steps.'

        # Trim history to avoid token overflow
        if len(history) > 30:
            sess['history'] = history[-30:]
        else:
            sess['history'] = history

        return jsonify({'reply': final_reply, 'session_id': session_id})

    except Exception as e:
        print(f'[chatbot] Error: {e}')
        return jsonify({'error': f'Gemini error: {str(e)}'}), 500


@app.route('/api/ai/converse/reset', methods=['POST'])
def ai_converse_reset():
    """Clear chat session history."""
    session_id = request.headers.get('X-Session-Id', 'default')
    _chat_sessions.pop(session_id, None)
    return jsonify({'status': 'cleared', 'session_id': session_id})


# ██████████████████████████████████████████████████████████████████████████████
if __name__ == '__main__':
    # Startup: log critical env var status
    print('=' * 60)
    print('  Release Readiness Dashboard — Startup Config')
    print('=' * 60)
    print(f'  🌍 DEPLOY_ENV = {DEPLOY_ENV.upper()}')
    if DEPLOY_ENV == 'prod':
        print(f'     → Prod Env tab: reads LOCAL cluster')
        print(f'     → UAT Env tab:  connects REMOTELY via UAT_CLUSTER_*')
    else:
        print(f'     → UAT Env tab:  reads LOCAL cluster')
        print(f'     → Prod Env tab: connects REMOTELY via PROD_CLUSTER_*')
    print()
    _vars = {
        'PROD_CLUSTER_API': os.environ.get('PROD_CLUSTER_API', ''),
        'PROD_CLUSTER_TOKEN': os.environ.get('PROD_CLUSTER_TOKEN', ''),
        'PROD_NAMESPACE': os.environ.get('PROD_NAMESPACE', ''),
        'PROD_CLUSTER_VERIFY_SSL': os.environ.get('PROD_CLUSTER_VERIFY_SSL', 'true'),
        'UAT_CLUSTER_API': os.environ.get('UAT_CLUSTER_API', ''),
        'UAT_CLUSTER_TOKEN': os.environ.get('UAT_CLUSTER_TOKEN', ''),
        'UAT_NAMESPACE': os.environ.get('UAT_NAMESPACE', ''),
        'UAT_CLUSTER_VERIFY_SSL': os.environ.get('UAT_CLUSTER_VERIFY_SSL', 'true'),
        'JIRA_MCP_URL': os.environ.get('JIRA_MCP_URL', ''),
        'JIRA_PAT_TOKEN': os.environ.get('JIRA_PAT_TOKEN', ''),
        'JIRA_BASE_URL': os.environ.get('JIRA_BASE_URL', ''),
        'CONFLUENCE_MCP_URL': os.environ.get('CONFLUENCE_MCP_URL', ''),
        'CONFLUENCE_BASE_URL': os.environ.get('CONFLUENCE_BASE_URL', ''),
        'GEMINI_API_KEY': os.environ.get('GEMINI_API_KEY', ''),
        'GCP_PROJECT_ID': os.environ.get('GCP_PROJECT_ID', ''),
        'ARTIFACTORY_URL': os.environ.get('ARTIFACTORY_URL', ''),
        'ARTIFACTORY_USER': os.environ.get('ARTIFACTORY_USER', ''),
        'ARTIFACTORY_TOKEN': os.environ.get('ARTIFACTORY_TOKEN', ''),
    }
    for k, v in _vars.items():
        if v:
            masked = v[:8] + '...' if len(v) > 12 else '***'
            print(f'  ✅ {k} = {masked}')
        else:
            print(f'  ⚠️  {k} = (not set)')
    print('=' * 60)
    socketio.run(app, host='0.0.0.0', port=int(os.getenv('PORT', 8080)), debug=True)
