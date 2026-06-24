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
import copy
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
import asyncio
# NOTE: httpx is imported LAZILY (inside functions) to avoid conflicts with
# gevent monkey.patch_all(). Importing httpx at module level corrupts urllib3
# connections used by the kubernetes client, causing 504 Gateway Timeouts.
# See: _build_confluence_httpx_factory() and _discover_confluence_mcp_tools()

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
# QA Tab — GitOps Deployment Configuration
# ══════════════════════════════════════════════════════════════════════════════
QA_DEPLOY_REPO       = os.getenv('QA_DEPLOY_REPO', '') or DEPLOY_REPO  # Repo for version.yaml push (e.g. org/app-deployment)
QA_NAMESPACE         = os.getenv('QA_NAMESPACE', 'uat-testing')
QA_TEST_REPO         = os.getenv('QA_TEST_REPO', '')   # Repo with E2E test pipelines (e.g. org/e2e-app-repo)
QA_TEST_WORKFLOWS    = {
    'smoke':      os.getenv('QA_TEST_SMOKE_WORKFLOW', 'smoke-tests.yml'),
    'e2e':        os.getenv('QA_TEST_E2E_WORKFLOW', 'e2e-tests.yml'),
    'regression': os.getenv('QA_TEST_REGRESSION_WORKFLOW', 'regression-tests.yml'),
}

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

# Release history archive (loaded from PVC file after storage init — see _detect_storage_mode)
_release_history = []  # populated by _read_history_file() after BOARD_DATA_DIR is set

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
            from urllib3.util.retry import Retry
            # ⚠️ Keep retries LOW — each retry through a Kerberos proxy involves
            # a full 407→negotiate→CONNECT cycle that takes 5-15 seconds.
            # With 5 sequential API calls in qa_prepare, total time must stay
            # under the Istio VirtualService timeout (120s).
            retry_strategy = Retry(
                total=1,                    # 1 retry max (was 3 — caused 504s)
                backoff_factor=0.5,
                status_forcelist=[502, 503], # Only retry on gateway errors, NOT 401/407
            )
            adapter = HTTPAdapter(max_retries=retry_strategy)
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

# Thread-local storage for token override in background greenlets
# (Flask session isn't available outside a request context)
import threading
_token_local = threading.local()

def get_gh_token():
    """Get the GitHub token for the current request or background greenlet."""
    # Check thread-local override first (set by background workers)
    override = getattr(_token_local, 'github_token', None)
    if override:
        return override
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
        r = gh_http.get(url, headers=_github_headers(), params=params, timeout=(5, 10))
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
        r = gh_http.post(url, headers=_github_headers(), json=data, timeout=(5, 10))
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
_CONFLUENCE_TOOLS = {}  # Discovered tool names from MCP server

def _discover_confluence_mcp_tools():
    """Discover available tools from the Confluence MCP server.
    Uses fastmcp SDK when available, falls back to raw HTTP JSON-RPC.
    """
    global _CONFLUENCE_TOOLS

    if not CONFLUENCE_MCP_URL:
        return

    # Method A: fastmcp SDK (proper MCP protocol)
    try:
        from fastmcp import Client as McpClient
        from fastmcp.client.transports import StreamableHttpTransport

        mcp_headers = {}
        if CONFLUENCE_PAT_TOKEN:
            mcp_headers['Authorization'] = f'Bearer {CONFLUENCE_PAT_TOKEN}'
        if CONFLUENCE_EMAIL:
            mcp_headers['X-Confluence-User-Email'] = CONFLUENCE_EMAIL

        def _factory(**kwargs):
            import httpx
            kwargs.pop('verify', None)
            incoming = kwargs.pop('headers', {}) or {}
            merged = {**incoming, **mcp_headers}
            return httpx.AsyncClient(headers=merged, verify=False, **kwargs)

        transport = StreamableHttpTransport(
            CONFLUENCE_MCP_URL, httpx_client_factory=_factory,
        )

        async def _discover():
            async with McpClient(transport) as mcp:
                return await mcp.list_tools()

        try:
            tools = asyncio.run(_discover())
        except RuntimeError:
            import concurrent.futures
            with concurrent.futures.ThreadPoolExecutor() as pool:
                tools = pool.submit(lambda: asyncio.run(_discover())).result(timeout=15)

        for t in tools:
            _CONFLUENCE_TOOLS[t.name] = t.description or ''
        print(f'[confluence] MCP tool discovery (fastmcp): found {len(_CONFLUENCE_TOOLS)} tools')
        for name, desc in _CONFLUENCE_TOOLS.items():
            print(f'    \u2192 {name}: {desc[:80]}')
        return
    except ImportError:
        print('[confluence] fastmcp not installed \u2014 trying raw HTTP discovery')
    except Exception as e:
        print(f'[confluence] fastmcp discovery failed: {e}')
        print(f'    Trying raw HTTP fallback...')

    # Method B: Raw HTTP JSON-RPC (fallback)
    try:
        headers = {
            'Content-Type': 'application/json',
            'Accept': 'application/json, text/event-stream',
        }
        if CONFLUENCE_PAT_TOKEN:
            headers['Authorization'] = f'Bearer {CONFLUENCE_PAT_TOKEN}'
        payload = {
            'jsonrpc': '2.0',
            'id': str(uuid.uuid4()),
            'method': 'tools/list',
            'params': {}
        }
        http_session = gh_http if PROXY_URL else requests
        r = http_session.post(CONFLUENCE_MCP_URL, json=payload, headers=headers,
                              timeout=10, verify=SSL_VERIFY)
        r.raise_for_status()
        raw = r.text.strip()
        result = None
        if raw.startswith('event:') or raw.startswith('data:'):
            for line in raw.split('\n'):
                line = line.strip()
                if line.startswith('data:'):
                    try:
                        result = json.loads(line[5:].strip())
                    except json.JSONDecodeError:
                        continue
        else:
            try:
                result = json.loads(raw)
            except json.JSONDecodeError:
                pass

        if result:
            tools = result.get('result', result).get('tools', [])
            if isinstance(tools, list):
                for t in tools:
                    _CONFLUENCE_TOOLS[t.get('name', '')] = t.get('description', '')
                print(f'[confluence] MCP tool discovery (raw HTTP): found {len(_CONFLUENCE_TOOLS)} tools')
                for name, desc in _CONFLUENCE_TOOLS.items():
                    print(f'    \u2192 {name}: {desc[:80]}')
            else:
                print(f'[confluence] tools/list unexpected format: {str(result)[:200]}')
        else:
            print(f'[confluence] tools/list returned no parseable result')
            print(f'    raw response: {raw[:300]}')
    except Exception as e:
        print(f'[confluence] MCP tool discovery failed: {e}')
        print(f'    Will try default tool names at runtime')

if CONFLUENCE_MCP_URL:
    print(f"[Release Readiness] \u2705 Confluence MCP configured \u2014 URL: {CONFLUENCE_MCP_URL}")
    if CONFLUENCE_EMAIL:
        print(f"    Confluence email: {CONFLUENCE_EMAIL}")
    if CONFLUENCE_PAT_TOKEN:
        print(f"    PAT token: {'*' * 8}...configured")
    if CONFLUENCE_SPACES:
        print(f"    Default spaces: {', '.join(CONFLUENCE_SPACES)}")
    # NOTE: Skipping MCP tool discovery at startup — httpx (used by fastmcp)
    # conflicts with gevent monkey.patch_all() and corrupts K8s client connections.
    # Tool names are tried at runtime instead (see _confluence_mcp_call).
    print("    Tool discovery deferred to first use")
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

def _build_confluence_httpx_factory(auth_headers):
    """Build an httpx client factory for fastmcp, matching test-confluence.py."""
    def factory(**kwargs):
        import httpx
        kwargs.pop('verify', None)
        incoming = kwargs.pop('headers', {}) or {}
        merged = {**incoming, **auth_headers}
        return httpx.AsyncClient(headers=merged, verify=False, **kwargs)
    return factory


# Track MCP server health for fast-fail on repeated timeouts
_confluence_mcp_health = {
    'consecutive_timeouts': 0,
    'last_success': 0,
    'last_failure': 0,
    'last_error': '',
}

def _confluence_mcp_call(tool_name, arguments, timeout=30, max_retries=1):
    """Call a tool on the Confluence MCP server with retry logic.
    Uses fastmcp SDK (StreamableHttp transport) when available,
    falls back to raw HTTP JSON-RPC.

    Retries on timeout/connection errors up to max_retries times.
    Tracks consecutive failures to enable fast-fail on subsequent calls.
    """
    global _confluence_mcp_health
    if not CONFLUENCE_MCP_URL:
        return None

    import time as _time

    # Fast-fail: if we've had 3+ consecutive timeouts in the last 2 minutes,
    # don't keep hammering the MCP server — it's likely down.
    if (_confluence_mcp_health['consecutive_timeouts'] >= 3 and
            _time.time() - _confluence_mcp_health['last_failure'] < 120):
        print(f'[Confluence MCP] CIRCUIT BREAKER: {_confluence_mcp_health["consecutive_timeouts"]} '
              f'consecutive timeouts — skipping call to {tool_name}. '
              f'Last error: {_confluence_mcp_health["last_error"]}')
        return None

    for attempt in range(max_retries + 1):
        result = _confluence_mcp_call_once(tool_name, arguments, timeout=timeout, attempt=attempt)
        if result is not None:
            # Success — reset health counters
            _confluence_mcp_health['consecutive_timeouts'] = 0
            _confluence_mcp_health['last_success'] = _time.time()
            return result
        # None means timeout/error — retry with backoff
        if attempt < max_retries:
            backoff = 2 * (attempt + 1)
            print(f'[Confluence MCP] Retrying {tool_name} in {backoff}s (attempt {attempt + 2}/{max_retries + 1})')
            _time.sleep(backoff)

    # All retries exhausted
    _confluence_mcp_health['consecutive_timeouts'] += 1
    _confluence_mcp_health['last_failure'] = _time.time()
    print(f'[Confluence MCP] All {max_retries + 1} attempts failed for {tool_name} '
          f'(consecutive_timeouts={_confluence_mcp_health["consecutive_timeouts"]})')
    return None


def _confluence_mcp_call_once(tool_name, arguments, timeout=30, attempt=0):
    """Single attempt to call a tool on the Confluence MCP server."""
    global _confluence_mcp_health
    if not CONFLUENCE_MCP_URL:
        return None

    import time as _time
    call_start = _time.time()
    print(f'[Confluence MCP] Calling {tool_name} (timeout={timeout}s, attempt={attempt + 1}, url={CONFLUENCE_MCP_URL})')

    # ── Method A: fastmcp SDK (same protocol as test-confluence.py) ──
    try:
        from fastmcp import Client as McpClient
        from fastmcp.client.transports import StreamableHttpTransport

        mcp_headers = {}
        if CONFLUENCE_PAT_TOKEN:
            mcp_headers['Authorization'] = f'Bearer {CONFLUENCE_PAT_TOKEN}'
        if CONFLUENCE_EMAIL:
            mcp_headers['X-Confluence-User-Email'] = CONFLUENCE_EMAIL

        transport = StreamableHttpTransport(
            CONFLUENCE_MCP_URL,
            httpx_client_factory=_build_confluence_httpx_factory(mcp_headers),
        )

        async def _call():
            async with McpClient(transport) as mcp:
                result = await mcp.call_tool(tool_name, arguments)
                return result

        try:
            raw_result = asyncio.run(_call())
        except RuntimeError:
            # Event loop already running (gevent) — use thread
            import concurrent.futures
            with concurrent.futures.ThreadPoolExecutor() as pool:
                raw_result = pool.submit(lambda: asyncio.run(_call())).result(timeout=timeout)

        # Extract text from MCP result (same as test-confluence.py extract_tool_payload)
        if isinstance(raw_result, dict):
            return json.dumps(raw_result)
        structured = getattr(raw_result, 'structured_content', None)
        if isinstance(structured, dict):
            return json.dumps(structured)
        content = getattr(raw_result, 'content', None)
        if isinstance(content, list):
            texts = []
            for item in content:
                text = getattr(item, 'text', None)
                if isinstance(text, str):
                    texts.append(text)
            if texts:
                return '\n'.join(texts)
        if raw_result is not None:
            return str(raw_result)
        return None

    except ImportError:
        print(f'[Confluence MCP] fastmcp not installed — falling back to raw HTTP')
        pass  # fastmcp not installed, fall through to raw HTTP
    except TimeoutError:
        elapsed = round((_time.time() - call_start) * 1000, 1)
        _confluence_mcp_health['last_error'] = f'fastmcp timeout after {elapsed}ms'
        print(f'[Confluence MCP] fastmcp TIMEOUT after {elapsed}ms calling {tool_name}')
        # Fall through to raw HTTP as backup
    except Exception as e:
        elapsed = round((_time.time() - call_start) * 1000, 1)
        _confluence_mcp_health['last_error'] = f'fastmcp error: {type(e).__name__}: {str(e)[:100]}'
        print(f'[Confluence MCP] fastmcp error after {elapsed}ms calling {tool_name}: {type(e).__name__}: {e}')
        # Fall through to raw HTTP as backup

    # ── Method B: Raw HTTP JSON-RPC (fallback if fastmcp not installed) ──
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

        print(f'[Confluence MCP] Raw HTTP POST to {CONFLUENCE_MCP_URL} (timeout={timeout}s, proxy={"YES" if PROXY_URL else "NO"})')
        if PROXY_URL:
            r = gh_http.post(CONFLUENCE_MCP_URL, json=payload, headers=headers,
                             timeout=timeout, verify=SSL_VERIFY)
        else:
            r = requests.post(CONFLUENCE_MCP_URL, json=payload, headers=headers,
                              timeout=timeout, verify=SSL_VERIFY)
        elapsed = round((_time.time() - call_start) * 1000, 1)
        print(f'[Confluence MCP] Raw HTTP response: HTTP {r.status_code}, {len(r.text)} bytes, {elapsed}ms')
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
        elapsed = round((_time.time() - call_start) * 1000, 1)
        _confluence_mcp_health['last_error'] = f'raw HTTP timeout after {elapsed}ms (limit={timeout}s)'
        print(f"[Confluence MCP] Timeout calling {tool_name} after {elapsed}ms (timeout={timeout}s, url={CONFLUENCE_MCP_URL})")
        return None
    except requests.exceptions.ConnectionError as e:
        elapsed = round((_time.time() - call_start) * 1000, 1)
        err_msg = str(e)[:150]
        _confluence_mcp_health['last_error'] = f'connection error after {elapsed}ms: {err_msg}'
        print(f"[Confluence MCP] Connection error to {CONFLUENCE_MCP_URL} after {elapsed}ms: {e}")
        print(f"[Confluence MCP] PROXY_URL={'SET: ' + PROXY_URL[:30] if PROXY_URL else 'NOT SET'}")
        print(f"[Confluence MCP] Tip: If MCP server is on a different network, set PROXY_URL or check firewall rules")
        return None
    except requests.exceptions.HTTPError as e:
        status = e.response.status_code if e.response is not None else 'unknown'
        _confluence_mcp_health['last_error'] = f'HTTP {status} error'
        print(f"[Confluence MCP] HTTP {status} calling {tool_name}")
        return None
    except Exception as e:
        _confluence_mcp_health['last_error'] = f'{type(e).__name__}: {str(e)[:100]}'
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


_STOP_WORDS = frozenset({
    # Question words
    'what', 'where', 'when', 'which', 'who', 'whom', 'whose', 'why', 'how',
    # Articles / determiners
    'a', 'an', 'the', 'this', 'that', 'these', 'those',
    # Pronouns
    'i', 'me', 'my', 'we', 'our', 'you', 'your', 'he', 'she', 'it', 'they', 'them',
    # Common verbs / auxiliaries
    'is', 'are', 'was', 'were', 'be', 'been', 'being',
    'do', 'does', 'did', 'doing',
    'have', 'has', 'had', 'having',
    'can', 'could', 'will', 'would', 'shall', 'should', 'may', 'might', 'must',
    # Prepositions / conjunctions
    'in', 'on', 'at', 'to', 'for', 'of', 'with', 'by', 'from', 'about',
    'and', 'or', 'but', 'not', 'if', 'so', 'as', 'up', 'out',
    # Filler
    'please', 'tell', 'find', 'get', 'give', 'show', 'need', 'want', 'know',
})


def _extract_search_keywords(query):
    """Extract meaningful search keywords from a natural-language question.

    Strips stop words, question words, and filler so that CQL text/title
    search gets targeted terms instead of a whole sentence.

    Examples:
        'what is the jenkins url' → 'jenkins url'
        'how do I deploy to production' → 'deploy production'
        'where can I find the runbook for payments' → 'runbook payments'
    """
    # Tokenise, lowercase, strip punctuation
    tokens = re.findall(r'[a-zA-Z0-9_\-\.]+', query.lower())
    keywords = [t for t in tokens if t not in _STOP_WORDS and len(t) > 1]
    if not keywords:
        # Fallback: use all alphanumeric tokens (user query was entirely stop words)
        keywords = [t for t in tokens if len(t) > 1]
    return ' '.join(keywords)


def _confluence_search(query, space_key=None, max_results=20):
    """Search Confluence via MCP, with REST API fallback.

    Uses multi-strategy search: title match first, then full-text,
    with keyword extraction for natural-language queries.

    Returns list of page dicts: [{id, title, space, url, excerpt, labels, ...}]
    """
    cache_key = ('confluence_search', query, space_key)
    cached = _cache_get(cache_key)
    if cached:
        return cached

    results = []

    # Method 1: MCP server — multi-strategy search
    if CONFLUENCE_MCP_URL:
        # Extract keywords from natural-language question for better CQL matching
        keywords = _extract_search_keywords(query)
        print(f'[confluence] Searching via MCP: "{query}" → keywords: "{keywords}"')

        # Determine spaces to search — user-specified, or ALL configured defaults
        if space_key:
            spaces_to_search = [space_key]
        elif CONFLUENCE_SPACES:
            spaces_to_search = CONFLUENCE_SPACES
            print(f'[confluence] Searching across {len(spaces_to_search)} default space(s): {", ".join(spaces_to_search)}')
        else:
            spaces_to_search = [None]  # No space filter

        # Use discovered tools — only pick 'confluence_search' (not confluence_search_user)
        if _CONFLUENCE_TOOLS and 'confluence_search' in _CONFLUENCE_TOOLS:
            search_tools = ['confluence_search']
        elif _CONFLUENCE_TOOLS:
            search_tools = [name for name in _CONFLUENCE_TOOLS
                          if name == 'confluence_search' or (name.endswith('_search') and 'user' not in name)]
            if not search_tools:
                search_tools = ['confluence_search']
        else:
            search_tools = ['confluence_search']

        # Build CQL queries — multiple strategies per space:
        #   Strategy 1: title match (pages with relevant titles surface first)
        #   Strategy 2: full-text match (exact keyword phrase in content)
        #   Strategy 3: individual keyword OR (catches pages mentioning SOME keywords)
        keyword_list = keywords.split()  # Split for individual keyword strategies
        cql_queries = []
        for sp in spaces_to_search:
            space_clause = f'space="{sp}" AND ' if sp else ''
            # Strategy 1: title match using extracted keywords
            cql_queries.append({
                'cql': f'{space_clause}type=page AND title ~ "{keywords}" ORDER BY lastModified DESC',
                'label': f'title~"{keywords}"' + (f' in {sp}' if sp else ''),
            })
            # Strategy 2: full-text match using all keywords together
            cql_queries.append({
                'cql': f'{space_clause}type=page AND text ~ "{keywords}" ORDER BY lastModified DESC',
                'label': f'text~"{keywords}"' + (f' in {sp}' if sp else ''),
            })
            # Strategy 3: individual keyword OR — catches pages that mention
            # some keywords even if they don't appear together as a phrase.
            # e.g. "openshift egress static ips" → finds page about "openshift migration"
            # that also mentions "egress" somewhere in the body.
            if len(keyword_list) > 1:
                or_clauses = ' OR '.join([f'text ~ "{kw}"' for kw in keyword_list])
                cql_queries.append({
                    'cql': f'{space_clause}type=page AND ({or_clauses}) ORDER BY lastModified DESC',
                    'label': f'text~OR({",".join(keyword_list)})' + (f' in {sp}' if sp else ''),
                })

        seen_ids = set()  # Deduplicate across strategies and spaces
        mcp_timeout_count = 0  # Track timeouts — allow some failures before giving up
        MAX_MCP_TIMEOUTS = 2   # Allow up to 2 timeouts before skipping remaining strategies

        for cql_info in cql_queries:
            if mcp_timeout_count >= MAX_MCP_TIMEOUTS:
                print(f'[confluence] Skipping CQL strategy "{cql_info["label"]}" — {mcp_timeout_count} MCP timeouts already')
                break
            if len(results) >= max_results:
                break

            cql = cql_info['cql']
            remaining = max_results - len(results)
            print(f'[confluence] Strategy: {cql_info["label"]}, CQL: {cql}')

            for tool_name in search_tools:
                if mcp_timeout_count >= MAX_MCP_TIMEOUTS:
                    break
                raw = _confluence_mcp_call(tool_name, {
                    'cql': cql,
                    'limit': remaining
                }, timeout=30, max_retries=1)
                if raw is None:
                    mcp_timeout_count += 1
                    print(f'[confluence] MCP timeout/error #{mcp_timeout_count} (max={MAX_MCP_TIMEOUTS})')
                    continue
                if raw:
                    try:
                        # raw from _confluence_mcp_call may already be extracted text (not JSON)
                        if isinstance(raw, str):
                            raw_stripped = raw.strip()
                            # Log first 200 chars on first strategy for debugging
                            if cql_info is cql_queries[0]:
                                print(f'[confluence] MCP raw response preview: {raw_stripped[:200]}')
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
                                    print(f'[confluence]   response was: {raw_stripped[:200]}')
                                    continue
                        else:
                            data = raw

                        # Log the response shape for debugging
                        if isinstance(data, dict):
                            print(f'[confluence] MCP response keys: {list(data.keys())[:10]}')
                        elif isinstance(data, list):
                            print(f'[confluence] MCP response is a list with {len(data)} items')

                        # Extract results from various response shapes
                        items = []
                        if isinstance(data, list):
                            items = data
                        elif isinstance(data, dict):
                            # Try common result keys
                            for key in ['results', 'pages', 'content', 'data', 'items', 'value']:
                                candidate = data.get(key)
                                if isinstance(candidate, list) and candidate:
                                    items = candidate
                                    print(f'[confluence] Found results under key: {key} ({len(items)} items)')
                                    break
                                elif isinstance(candidate, dict):
                                    # Nested: data.results or data.data.results
                                    for subkey in ['results', 'pages', 'content']:
                                        sub = candidate.get(subkey)
                                        if isinstance(sub, list) and sub:
                                            items = sub
                                            print(f'[confluence] Found results under key: {key}.{subkey} ({len(items)} items)')
                                            break
                                    if items:
                                        break
                            # If no list found but dict has 'id' and 'title', treat as single result
                            if not items and data.get('id') and data.get('title'):
                                items = [data]
                                print(f'[confluence] Single page result: {data.get("title")}')
                        if isinstance(items, list) and items:
                            new_count = 0
                            for item in items:
                                if isinstance(item, dict):
                                    page_id = str(item.get('id', item.get('pageId', '')))
                                    if page_id in seen_ids:
                                        continue  # Deduplicate
                                    seen_ids.add(page_id)
                                    results.append({
                                        'id': page_id,
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
                                    new_count += 1
                            if new_count:
                                print(f'[confluence] Strategy "{cql_info["label"]}" added {new_count} new pages (total: {len(results)})')
                    except Exception as e:
                        print(f'[confluence] MCP parse error ({tool_name}): {e}')
                        continue

        # ── Strategy 3: Browse ALL pages fallback ──────────────────────────
        # If keyword search returned few results, list ALL pages in the space
        # so the AI can scan titles and find relevant pages that CQL missed.
        if len(results) < 5 and mcp_timeout_count < MAX_MCP_TIMEOUTS:
            print(f'[confluence] Keyword search returned only {len(results)} results — browsing all pages in space(s)')
            for sp in spaces_to_search:
                if mcp_timeout_count >= MAX_MCP_TIMEOUTS or len(results) >= max_results:
                    break
                if not sp:
                    continue  # Can't browse without a space key
                browse_cql = f'space="{sp}" AND type=page ORDER BY lastModified DESC'
                remaining = max_results - len(results)
                print(f'[confluence] Strategy: browse-all in {sp}, CQL: {browse_cql}')
                for tool_name in search_tools:
                    if mcp_timeout_count >= MAX_MCP_TIMEOUTS:
                        break
                    raw = _confluence_mcp_call(tool_name, {
                        'cql': browse_cql,
                        'limit': min(remaining, 50)  # Fetch up to 50 page titles
                    }, timeout=20, max_retries=0)  # No retries for browse — less critical
                    if raw is None:
                        mcp_timeout_count += 1
                        continue
                    if raw:
                        try:
                            if isinstance(raw, str):
                                raw_stripped = raw.strip()
                                if '\n' in raw_stripped and raw_stripped.startswith('{'):
                                    lines_list = [l.strip() for l in raw_stripped.split('\n') if l.strip()]
                                    data = None
                                    for line in lines_list:
                                        try:
                                            data = json.loads(line)
                                            if isinstance(data, (dict, list)):
                                                break
                                        except json.JSONDecodeError:
                                            continue
                                    if not data:
                                        continue
                                else:
                                    try:
                                        data = json.loads(raw_stripped)
                                    except json.JSONDecodeError:
                                        continue
                            else:
                                data = raw
                            items = []
                            if isinstance(data, list):
                                items = data
                            elif isinstance(data, dict):
                                for key in ['results', 'pages', 'content', 'data', 'items', 'value']:
                                    candidate = data.get(key)
                                    if isinstance(candidate, list) and candidate:
                                        items = candidate
                                        break
                                    elif isinstance(candidate, dict):
                                        for subkey in ['results', 'pages', 'content']:
                                            sub = candidate.get(subkey)
                                            if isinstance(sub, list) and sub:
                                                items = sub
                                                break
                                        if items:
                                            break
                                if not items and data.get('id') and data.get('title'):
                                    items = [data]
                            if isinstance(items, list) and items:
                                new_count = 0
                                for item in items:
                                    if isinstance(item, dict):
                                        page_id = str(item.get('id', item.get('pageId', '')))
                                        if page_id in seen_ids:
                                            continue
                                        seen_ids.add(page_id)
                                        results.append({
                                            'id': page_id,
                                            'title': item.get('title', item.get('name', '?')),
                                            'space': item.get('space', item.get('spaceKey', item.get('space_key', sp))),
                                            'url': item.get('url', item.get('webUrl', item.get('_links', {}).get('webui', ''))),
                                            'excerpt': (item.get('excerpt', item.get('snippet', item.get('body', ''))) or '')[:300],
                                            'last_modified': item.get('lastModified', item.get('last_modified',
                                                             item.get('version', {}).get('when', '') if isinstance(item.get('version'), dict) else '')),
                                            'author': item.get('author', item.get('lastModifiedBy',
                                                      item.get('version', {}).get('by', {}).get('displayName', '') if isinstance(item.get('version'), dict) else '')),
                                            'labels': item.get('labels', []),
                                            '_browse_result': True,  # Mark as browse result (not keyword match)
                                        })
                                        new_count += 1
                                if new_count:
                                    print(f'[confluence] Browse-all in {sp} added {new_count} pages (total: {len(results)})')
                        except Exception as e:
                            print(f'[confluence] Browse-all parse error: {e}')
                            continue

        # MCP was configured and attempted — always return here.
        # Do NOT fall through to REST API (it times out behind proxy
        # and returns HTML error pages, causing "unexpected token '<'" in UI).
        if results:
            # ── Ensure every result has a working Confluence URL ──────
            # MCP search results often come without a URL, or with a relative path.
            # Construct a direct link from CONFLUENCE_BASE_URL + page ID.
            for r in results:
                page_url = r.get('url', '')
                if not page_url or page_url == '?':
                    # Build URL from base URL + page ID (works for Confluence Cloud and Server)
                    if CONFLUENCE_BASE_URL:
                        r['url'] = f"{CONFLUENCE_BASE_URL}/pages/viewpage.action?pageId={r['id']}"
                    elif CONFLUENCE_MCP_URL:
                        # Extract base from MCP URL (e.g. https://confluence.company.com/...)
                        from urllib.parse import urlparse
                        parsed = urlparse(CONFLUENCE_MCP_URL)
                        base = f"{parsed.scheme}://{parsed.hostname}"
                        if parsed.port and parsed.port not in (80, 443):
                            base += f":{parsed.port}"
                        r['url'] = f"{base}/pages/viewpage.action?pageId={r['id']}"
                elif page_url.startswith('/'):
                    # Relative URL — prepend base URL
                    if CONFLUENCE_BASE_URL:
                        r['url'] = f"{CONFLUENCE_BASE_URL}{page_url}"

            print(f'[confluence] MCP search complete: {len(results)} unique pages found')
            _cache_set(cache_key, results)
            return results
        print('[confluence] MCP returned no results — returning empty (skipping REST to avoid timeout)')
        _cache_set(cache_key, results)
        return results

    # REST API path — only reached when CONFLUENCE_MCP_URL is NOT set
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
        # Use discovered tools if available
        # Use the exact tool name from discovery
        if _CONFLUENCE_TOOLS and 'confluence_get_page' in _CONFLUENCE_TOOLS:
            page_tools = ['confluence_get_page']
        elif _CONFLUENCE_TOOLS:
            page_tools = [name for name in _CONFLUENCE_TOOLS
                         if name.startswith('confluence_get_page') and 'children' not in name]
            if not page_tools:
                page_tools = ['confluence_get_page']
        else:
            page_tools = ['confluence_get_page']
        for tool_name in page_tools:
            # Try multiple parameter names — different MCP servers use different conventions
            raw = _confluence_mcp_call(tool_name, {'page_id': page_id}, timeout=10)
            if raw is None:
                raw = _confluence_mcp_call(tool_name, {'pageId': page_id}, timeout=10)
            if raw is None:
                raw = _confluence_mcp_call(tool_name, {'id': page_id}, timeout=10)
            if raw:
                try:
                    data = None
                    if isinstance(raw, str):
                        raw_stripped = raw.strip()
                        print(f'[confluence] MCP page response preview ({tool_name}): {raw_stripped[:200]}')
                        # Try JSON parse first
                        if raw_stripped.startswith('{'):
                            if '\n' in raw_stripped:
                                lines = [l.strip() for l in raw_stripped.split('\n') if l.strip()]
                                for line in lines:
                                    try:
                                        data = json.loads(line)
                                        if isinstance(data, dict):
                                            break
                                    except json.JSONDecodeError:
                                        continue
                            else:
                                try:
                                    data = json.loads(raw_stripped)
                                except json.JSONDecodeError:
                                    data = None
                        elif raw_stripped.startswith('['):
                            try:
                                parsed_list = json.loads(raw_stripped)
                                if isinstance(parsed_list, list) and parsed_list:
                                    data = parsed_list[0] if isinstance(parsed_list[0], dict) else None
                            except json.JSONDecodeError:
                                data = None

                        # If NOT JSON, treat the raw text as the page content itself
                        # Many MCP servers return markdown/text directly instead of JSON
                        if data is None and len(raw_stripped) > 10:
                            print(f'[confluence] MCP page response is raw text ({len(raw_stripped)} chars) — using as body_text')
                            page = {
                                'id': str(page_id),
                                'title': f'Page {page_id}',
                                'space': '?',
                                'body_html': '',
                                'body_text': raw_stripped[:8000],
                                'url': '',
                                'labels': [],
                                'last_modified': '',
                                'author': '',
                            }
                            _cache_set(cache_key, page)
                            return page
                    else:
                        data = raw

                    if isinstance(data, dict):
                        # Extract body content — try multiple field names
                        body_html = (data.get('body_html', '') or
                                     data.get('body', {}).get('view', {}).get('value', '') if isinstance(data.get('body'), dict) else '' or
                                     data.get('body', '') if isinstance(data.get('body'), str) else '' or
                                     data.get('content', ''))
                        body_text = (data.get('body_text', '') or
                                     data.get('plainText', '') or
                                     data.get('body_export', '') or
                                     (data.get('body', '') if isinstance(data.get('body'), str) else ''))
                        # If we have body_html but no body_text, strip HTML tags
                        if body_html and not body_text:
                            body_text = re.sub(r'<[^>]+>', ' ', body_html)
                            body_text = re.sub(r'\s+', ' ', body_text).strip()

                        page = {
                            'id': str(data.get('id', page_id)),
                            'title': data.get('title', '?'),
                            'space': data.get('space', data.get('spaceKey', '?')),
                            'body_html': body_html,
                            'body_text': body_text,
                            'url': data.get('url', data.get('webUrl', '')),
                            'labels': data.get('labels', []),
                            'last_modified': data.get('lastModified', data.get('last_modified', '')),
                            'author': data.get('author', data.get('lastModifiedBy', '')),
                        }
                        if page.get('title') and page['title'] != '?':
                            _cache_set(cache_key, page)
                            return page
                        elif body_html or body_text:
                            # Page has content but no title — still usable
                            print(f'[confluence] MCP page has content but no title for {page_id}')
                            _cache_set(cache_key, page)
                            return page
                except Exception as e:
                    print(f'[confluence] MCP page parse error: {e}')

        # MCP was configured and attempted — return whatever we have.
        # Do NOT fall through to REST API (it times out behind proxy
        # and returns HTML, causing "unexpected token '<'" in the UI).
        if page:
            print(f'[confluence] MCP get_page succeeded for {page_id}: {page.get("title", "?")}')
            _cache_set(cache_key, page)
        else:
            print(f'[confluence] MCP get_page returned no usable data for {page_id} — skipping REST to avoid timeout')
        return page

    # REST fallback — only reached when CONFLUENCE_MCP_URL is NOT set
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

# Session secret key:
# - FLASK_SECRET_KEY env var → stable across restarts and workers (recommended)
# - Fallback → derive from POD_NAMESPACE + hostname so it's at least consistent
#   across gunicorn workers in the same pod (but changes on pod restart)
_explicit_secret = os.environ.get('FLASK_SECRET_KEY', '')
if _explicit_secret:
    app.secret_key = _explicit_secret
else:
    import hashlib
    _ns = os.environ.get('POD_NAMESPACE', 'default')
    _host = os.environ.get('HOSTNAME', 'release-readiness')
    _fallback = hashlib.sha256(f'release-readiness-{_ns}-{_host}'.encode()).hexdigest()
    app.secret_key = _fallback
    print('[Release Readiness] ⚠️  No FLASK_SECRET_KEY set — using derived key.')
    print('    Sessions will be lost on pod restart. Set FLASK_SECRET_KEY in your K8s secret for stable sessions.')

# Session cookie settings — keep sessions alive for 7 days and survive browser restarts
app.config['SESSION_COOKIE_SAMESITE'] = 'Lax'
app.config['SESSION_COOKIE_HTTPONLY'] = True
app.config['PERMANENT_SESSION_LIFETIME'] = datetime.timedelta(days=7)

socketio = SocketIO(app, cors_allowed_origins="*", async_mode='gevent',
                    ping_timeout=120, ping_interval=30)

# ── Global JSON error handlers for API routes ─────────────────────────────────
# Prevents Flask from returning HTML error pages to frontend API callers,
# which causes "unexpected < ... invalid JSON" parse errors.
@app.errorhandler(Exception)
def handle_exception(e):
    """Return JSON for API routes, HTML for page routes."""
    from flask import request as req
    if req.path.startswith('/api/'):
        code = getattr(e, 'code', 500) if hasattr(e, 'code') else 500
        print(f'[api-error] {req.method} {req.path}: {type(e).__name__}: {e}')
        return jsonify({'error': f'{type(e).__name__}: {str(e)}'}), code
    # Non-API routes: let Flask handle normally
    raise e

@app.errorhandler(404)
def handle_404(e):
    from flask import request as req
    if req.path.startswith('/api/'):
        return jsonify({'error': f'API endpoint not found: {req.path}'}), 404
    return e

@app.errorhandler(500)
def handle_500(e):
    from flask import request as req
    if req.path.startswith('/api/'):
        print(f'[api-error] 500 at {req.path}: {e}')
        return jsonify({'error': f'Internal server error: {str(e)}'}), 500
    return e

try:
    config.load_incluster_config()
except config.ConfigException:
    try:
        config.load_kube_config()
    except config.ConfigException:
        print("[k8s] Could not configure kubernetes client")

# ── Fix: Inject SA token via default_headers ──────────────────────────────────
# The kubernetes client's api_key auth mechanism is broken under gevent
# monkey.patch_all() in this Docker image. Diagnostic /api/k8s-diag proved:
#   - Raw urllib3 with Authorization header → 200 OK (73 deployments)
#   - kubernetes client api_key mechanism  → 403 Forbidden (system:anonymous)
# Fix: Read the SA token and set it directly as a default header on the ApiClient.
_SA_TOKEN_PATH = '/var/run/secrets/kubernetes.io/serviceaccount/token'
_local_api_client = None

if os.path.exists(_SA_TOKEN_PATH):
    with open(_SA_TOKEN_PATH, 'r') as f:
        _sa_token = f.read().strip()
    # Create ApiClient from the default config (host, CA cert set by load_incluster_config)
    _local_api_client = client.ApiClient()
    # Inject the Authorization header directly — bypasses broken api_key mechanism
    _local_api_client.default_headers['Authorization'] = f'Bearer {_sa_token}'
    print(f"[k8s] ✅ SA token injected via default_headers ({len(_sa_token)} chars)")
else:
    print("[k8s] No SA token file — running in dev mode")


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
CUTOFF_HOUR = int(os.getenv('CUTOFF_HOUR', '12'))  # 12:00 (noon) in CUTOFF_TZ
CUTOFF_TZ_OFFSET = int(os.getenv('CUTOFF_TZ_OFFSET', '-4'))  # UTC offset: -4=EDT, -5=EST

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
        v1 = client.CoreV1Api(api_client=_local_api_client)
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

# ── History persistence helpers ───────────────────────────────────────────────
_HISTORY_FILE = os.path.join(BOARD_DATA_DIR, 'release_history.json')


def _read_history_file():
    """Load release history from a JSON file on the PVC."""
    try:
        with open(_HISTORY_FILE, 'r') as f:
            data = json.load(f)
        if isinstance(data, list):
            return data
        return []
    except FileNotFoundError:
        return []
    except Exception as e:
        print(f"[storage] Error reading history file: {e}")
        return []


def _write_history_file():
    """Persist the in-memory _release_history list to the PVC."""
    try:
        os.makedirs(os.path.dirname(_HISTORY_FILE), exist_ok=True)
        with open(_HISTORY_FILE, 'w') as f:
            json.dump(_release_history, f, indent=2)
        print(f"[storage] ✅ History persisted ({len(_release_history)} releases → {_HISTORY_FILE})")
    except Exception as e:
        print(f"[storage] ⚠️  History write failed: {e}")

# ── Load persisted history on startup ─────────────────────────────────────────
_release_history = _read_history_file()
if _release_history:
    print(f"[storage] ✅ Loaded {len(_release_history)} past releases from history file")
else:
    print(f"[storage] No release history file found — starting fresh")


def _get_current_release_date():
    """Calculate the next release Friday (or whatever cadence)."""
    today = datetime.date.today()
    days_until_friday = (4 - today.weekday()) % 7
    if days_until_friday == 0 and datetime.datetime.now().hour >= 18:
        days_until_friday = 7
    return (today + datetime.timedelta(days=days_until_friday)).isoformat()


def _get_cutoff_datetime():
    """Calculate the cutoff datetime in UTC based on CUTOFF_DAY, CUTOFF_HOUR, and CUTOFF_TZ_OFFSET.

    CUTOFF_HOUR is in the local timezone (e.g. 12 = noon EST).
    We convert to UTC for comparison with datetime.utcnow().
    Example: 12:00 EST (UTC-5) → 17:00 UTC.
    """
    today = datetime.date.today()
    days_until_friday = (4 - today.weekday()) % 7
    if days_until_friday == 0 and datetime.datetime.utcnow().hour >= 22:
        days_until_friday = 7
    release_friday = today + datetime.timedelta(days=days_until_friday)
    cutoff_date = release_friday - datetime.timedelta(days=(4 - CUTOFF_DAY) % 7)
    # Convert local cutoff time to UTC: subtract the TZ offset
    # e.g. 12:00 EST (offset=-5) → 12:00 - (-5) = 17:00 UTC
    cutoff_local = datetime.datetime.combine(cutoff_date, datetime.time(CUTOFF_HOUR, 0))
    cutoff_utc = cutoff_local - datetime.timedelta(hours=CUTOFF_TZ_OFFSET)
    return cutoff_utc.isoformat()


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
            v1 = client.CoreV1Api(api_client=_local_api_client)
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
            v1 = client.CoreV1Api(api_client=_local_api_client)
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
    {"name": "ingestion-pipeline",       "type": "Spark",   "description": "Main data ingestion from source systems",  "artifactory_path": "spark-releases/ingestion-pipeline"},
    {"name": "etl-transformer",          "type": "PySpark", "description": "Data transformation and enrichment",       "artifactory_path": "pyspark-releases/etl-transformer"},
    {"name": "data-validator",           "type": "PySpark", "description": "Data quality validation rules",            "artifactory_path": "pyspark-releases/data-validator"},
    {"name": "report-aggregator",        "type": "Spark",   "description": "Aggregation jobs for reporting",            "artifactory_path": "spark-releases/report-aggregator"},
    {"name": "event-stream-processor",   "type": "Spark",   "description": "Real-time event stream processing",        "artifactory_path": "spark-releases/event-stream-processor"},
    {"name": "batch-reconciler",         "type": "PySpark", "description": "Batch reconciliation between systems",      "artifactory_path": "pyspark-releases/batch-reconciler"},
    {"name": "data-archiver",            "type": "PySpark", "description": "Historical data archival jobs",             "artifactory_path": "pyspark-releases/data-archiver"},
    {"name": "ml-feature-pipeline",      "type": "PySpark", "description": "ML feature extraction pipeline",           "artifactory_path": "pyspark-releases/ml-feature-pipeline"},
    {"name": "audit-log-processor",      "type": "Spark",   "description": "Audit log processing and indexing",        "artifactory_path": "spark-releases/audit-log-processor"},
    {"name": "schema-migration-runner",  "type": "PySpark", "description": "Database schema migration runner",         "artifactory_path": "pyspark-releases/schema-migration-runner"},
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


# ── Network diagnostic (GitHub connectivity) ──────────────────────────────────
@app.route('/api/network-diag')
def network_diagnostic():
    """Diagnostic endpoint to test GitHub connectivity from this pod.
    
    Tests 4 methods and reports results — hit this from a browser when
    OAuth fails to see which network paths work.
    """
    import socket
    results = {
        'cluster_info': {
            'proxy_url': PROXY_URL or 'NOT SET',
            'github_url': GITHUB_URL,
            'github_api': GITHUB_API,
            'ssl_verify': SSL_VERIFY,
            'hostname': socket.gethostname(),
        },
        'env_proxies': {
            'HTTP_PROXY': os.environ.get('HTTP_PROXY', os.environ.get('http_proxy', 'NOT SET')),
            'HTTPS_PROXY': os.environ.get('HTTPS_PROXY', os.environ.get('https_proxy', 'NOT SET')),
            'NO_PROXY': os.environ.get('NO_PROXY', os.environ.get('no_proxy', 'NOT SET')),
        },
        'tests': []
    }

    test_url = f'{GITHUB_URL}/login/oauth/access_token'

    # Test 1: DNS resolution
    try:
        from urllib.parse import urlparse
        host = urlparse(GITHUB_URL).hostname
        ips = socket.getaddrinfo(host, 443, socket.AF_INET)
        ip_list = list(set(addr[4][0] for addr in ips))
        results['tests'].append({
            'name': 'DNS Resolution',
            'target': host,
            'status': 'OK',
            'detail': f'Resolved to: {ip_list}'
        })
    except Exception as e:
        results['tests'].append({
            'name': 'DNS Resolution',
            'target': host,
            'status': 'FAILED',
            'detail': str(e)
        })

    # Test 2: Direct TCP connect (no proxy, no TLS)
    try:
        sock = socket.create_connection((host, 443), timeout=5)
        sock.close()
        results['tests'].append({
            'name': 'Direct TCP :443',
            'target': f'{host}:443',
            'status': 'OK',
            'detail': 'TCP handshake succeeded — direct egress to GitHub is open'
        })
    except Exception as e:
        results['tests'].append({
            'name': 'Direct TCP :443',
            'target': f'{host}:443',
            'status': 'FAILED',
            'detail': f'{e} — direct egress blocked, proxy required'
        })

    # Test 3: Direct HTTPS (no proxy)
    try:
        s = requests.Session()
        s.verify = SSL_VERIFY
        s.proxies = {'http': '', 'https': ''}
        s.trust_env = False
        r = s.get(f'{GITHUB_URL}', timeout=10)
        s.close()
        results['tests'].append({
            'name': 'Direct HTTPS',
            'target': GITHUB_URL,
            'status': 'OK',
            'detail': f'HTTP {r.status_code} — direct HTTPS works'
        })
    except Exception as e:
        results['tests'].append({
            'name': 'Direct HTTPS',
            'target': GITHUB_URL,
            'status': 'FAILED',
            'detail': str(e)
        })

    # Test 4: Proxy + Kerberos (bare session — Pipeline Hub pattern)
    if PROXY_URL:
        try:
            s = requests.Session()
            s.verify = SSL_VERIFY
            s.proxies = {'http': PROXY_URL, 'https': PROXY_URL}
            kerberos_status = 'not installed'
            try:
                from requests_kerberos import HTTPKerberosAuth, OPTIONAL
                s.auth = HTTPKerberosAuth(
                    mutual_authentication=OPTIONAL,
                    force_preemptive=False,
                )
                kerberos_status = 'configured'
            except ImportError:
                kerberos_status = 'NOT INSTALLED — pip install requests-kerberos'
            r = s.get(f'{GITHUB_URL}', timeout=15)
            s.close()
            results['tests'].append({
                'name': 'Proxy + Kerberos (bare session)',
                'target': f'{GITHUB_URL} via {PROXY_URL}',
                'status': 'OK',
                'detail': f'HTTP {r.status_code} — proxy works! Kerberos: {kerberos_status}'
            })
        except Exception as e:
            results['tests'].append({
                'name': 'Proxy + Kerberos (bare session)',
                'target': f'{GITHUB_URL} via {PROXY_URL}',
                'status': 'FAILED',
                'detail': f'{e}. Kerberos: {kerberos_status}'
            })

        # Test 5: Proxy via gh_http (with retries — may fail due to Kerberos conflict)
        try:
            r = gh_http.get(f'{GITHUB_URL}', timeout=15)
            results['tests'].append({
                'name': 'Proxy via gh_http (with retries)',
                'target': f'{GITHUB_URL} via {PROXY_URL}',
                'status': 'OK',
                'detail': f'HTTP {r.status_code} — gh_http session works'
            })
        except Exception as e:
            results['tests'].append({
                'name': 'Proxy via gh_http (with retries)',
                'target': f'{GITHUB_URL} via {PROXY_URL}',
                'status': 'FAILED',
                'detail': f'{e} — retries may conflict with Kerberos CONNECT tunnel'
            })
    else:
        results['tests'].append({
            'name': 'Proxy Tests',
            'target': 'N/A',
            'status': 'SKIPPED',
            'detail': 'PROXY_URL not set'
        })

    # Test 6: Check for Kerberos ticket
    try:
        import subprocess
        klist = subprocess.run(['klist'], capture_output=True, text=True, timeout=5)
        if klist.returncode == 0:
            # Parse principal from output
            lines = klist.stdout.strip().split('\n')
            principal = next((l for l in lines if 'Principal' in l or 'principal' in l), lines[0] if lines else '(unknown)')
            results['tests'].append({
                'name': 'Kerberos Ticket (klist)',
                'status': 'OK',
                'detail': principal.strip()
            })
        else:
            results['tests'].append({
                'name': 'Kerberos Ticket (klist)',
                'status': 'FAILED',
                'detail': f'No valid ticket: {klist.stderr.strip()}'
            })
    except FileNotFoundError:
        results['tests'].append({
            'name': 'Kerberos Ticket (klist)',
            'status': 'SKIPPED',
            'detail': 'klist binary not found in container'
        })
    except Exception as e:
        results['tests'].append({
            'name': 'Kerberos Ticket (klist)',
            'status': 'FAILED',
            'detail': str(e)
        })

    return jsonify(results)

# ── Confluence MCP diagnostic ─────────────────────────────────────────────────
@app.route('/api/confluence-diag')
def confluence_diagnostic():
    """Diagnostic endpoint to test Confluence MCP connectivity step by step."""
    from urllib.parse import urlparse
    import socket
    import time as _time

    results = {
        'deploy_env': DEPLOY_ENV,
        'confluence_mcp_url': CONFLUENCE_MCP_URL or 'NOT SET',
        'confluence_base_url': CONFLUENCE_BASE_URL or 'NOT SET',
        'confluence_email': CONFLUENCE_EMAIL[:5] + '...' if CONFLUENCE_EMAIL else 'NOT SET',
        'confluence_pat_token': f'{len(CONFLUENCE_PAT_TOKEN)} chars' if CONFLUENCE_PAT_TOKEN else 'NOT SET',
        'confluence_spaces': CONFLUENCE_SPACES or 'NOT SET',
        'proxy_url': PROXY_URL[:30] + '...' if PROXY_URL else 'NOT SET',
        'ssl_verify': SSL_VERIFY,
    }

    if not CONFLUENCE_MCP_URL:
        results['error'] = 'CONFLUENCE_MCP_URL is not configured'
        return jsonify(results)

    parsed = urlparse(CONFLUENCE_MCP_URL)
    host = parsed.hostname
    port = parsed.port or (443 if parsed.scheme == 'https' else 80)
    results['parsed_host'] = host
    results['parsed_port'] = port

    # Step 1: DNS resolution
    try:
        ips = socket.getaddrinfo(host, port, socket.AF_UNSPEC, socket.SOCK_STREAM)
        results['step1_dns'] = {
            'status': 'OK',
            'resolved_ips': list(set(ip[4][0] for ip in ips))[:5]
        }
    except Exception as e:
        results['step1_dns'] = {'status': 'FAIL', 'error': str(e)}
        results['diagnosis'] = 'DNS resolution failed — the hostname cannot be resolved from this pod. Check DNS config or use IP address.'
        return jsonify(results)

    # Step 2: TCP connectivity
    try:
        start = _time.time()
        sock = socket.create_connection((host, port), timeout=5)
        elapsed = round((_time.time() - start) * 1000, 1)
        sock.close()
        results['step2_tcp'] = {
            'status': 'OK',
            'connect_time_ms': elapsed
        }
    except Exception as e:
        results['step2_tcp'] = {'status': 'FAIL', 'error': str(e)}
        results['diagnosis'] = f'TCP connection to {host}:{port} failed — firewall, network policy, or proxy blocking the connection.'
        return jsonify(results)

    # Step 3: HTTP reachability (simple GET to the MCP URL)
    try:
        start = _time.time()
        http_session = gh_http if PROXY_URL else requests
        r = http_session.get(CONFLUENCE_MCP_URL, timeout=10, verify=SSL_VERIFY,
                             headers={'Accept': 'application/json'})
        elapsed = round((_time.time() - start) * 1000, 1)
        results['step3_http_get'] = {
            'status': 'OK',
            'http_status': r.status_code,
            'response_time_ms': elapsed,
            'content_type': r.headers.get('Content-Type', ''),
            'response_preview': r.text[:200] if r.text else ''
        }
    except requests.exceptions.SSLError as e:
        results['step3_http_get'] = {'status': 'FAIL', 'error': f'SSL error: {str(e)[:200]}'}
        results['diagnosis'] = 'SSL certificate validation failed. Try setting UAT_CLUSTER_VERIFY_SSL=false or fix cert chain.'
    except requests.exceptions.Timeout:
        results['step3_http_get'] = {'status': 'FAIL', 'error': 'Timeout (10s)'}
        results['diagnosis'] = f'HTTP GET to {CONFLUENCE_MCP_URL} timed out. The MCP server may be unreachable from this pod.'
    except requests.exceptions.ConnectionError as e:
        results['step3_http_get'] = {'status': 'FAIL', 'error': str(e)[:300]}
        results['diagnosis'] = 'Connection refused or reset. MCP server may not be running, or a proxy/firewall is blocking.'
    except Exception as e:
        results['step3_http_get'] = {'status': 'FAIL', 'error': str(e)[:300]}

    # Step 4: MCP protocol test (tools/list)
    try:
        start = _time.time()
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
            'id': 'diag-tools-list',
            'method': 'tools/list',
            'params': {}
        }

        http_session = gh_http if PROXY_URL else requests
        r = http_session.post(CONFLUENCE_MCP_URL, json=payload, headers=headers,
                              timeout=15, verify=SSL_VERIFY)
        elapsed = round((_time.time() - start) * 1000, 1)

        results['step4_mcp_tools_list'] = {
            'status': 'OK' if r.status_code == 200 else f'HTTP {r.status_code}',
            'response_time_ms': elapsed,
            'content_type': r.headers.get('Content-Type', ''),
            'response_preview': r.text[:500] if r.text else ''
        }

        # Parse tools if successful
        try:
            raw = r.text.strip()
            # Handle SSE format
            if raw.startswith('event:') or raw.startswith('data:'):
                for line in raw.split('\n'):
                    if line.strip().startswith('data:'):
                        data_str = line.strip()[5:].strip()
                        if data_str:
                            data = json.loads(data_str)
                            if 'result' in data:
                                tools_result = data['result']
                                if isinstance(tools_result, dict) and 'tools' in tools_result:
                                    tool_names = [t.get('name', '?') for t in tools_result['tools']]
                                    results['step4_mcp_tools_list']['tools_found'] = tool_names
                                    break
            else:
                data = json.loads(raw)
                if 'result' in data:
                    tools_result = data['result']
                    if isinstance(tools_result, dict) and 'tools' in tools_result:
                        tool_names = [t.get('name', '?') for t in tools_result['tools']]
                        results['step4_mcp_tools_list']['tools_found'] = tool_names
        except Exception as parse_e:
            results['step4_mcp_tools_list']['parse_note'] = f'Could not parse tools: {str(parse_e)[:100]}'

    except requests.exceptions.Timeout:
        results['step4_mcp_tools_list'] = {'status': 'FAIL', 'error': 'Timeout (15s) on tools/list'}
        results['diagnosis'] = 'MCP server reachable via HTTP but tools/list timed out. The MCP server may be overloaded or the Confluence backend is slow.'
    except Exception as e:
        results['step4_mcp_tools_list'] = {'status': 'FAIL', 'error': str(e)[:300]}

    # Step 5: MCP search test (actual confluence_search call)
    try:
        start = _time.time()
        raw = _confluence_mcp_call('confluence_search', {
            'cql': 'type=page ORDER BY lastModified DESC',
            'limit': 1
        }, timeout=30, max_retries=0)  # No retries for diagnostic — we want raw timing
        elapsed = round((_time.time() - start) * 1000, 1)

        results['step5_mcp_search'] = {
            'status': 'OK' if raw else 'EMPTY',
            'response_time_ms': elapsed,
            'response_preview': str(raw)[:300] if raw else 'None (timeout or error)'
        }
    except Exception as e:
        results['step5_mcp_search'] = {'status': 'FAIL', 'error': str(e)[:300]}

    # Step 6: MCP health state (circuit breaker)
    results['step6_mcp_health'] = {
        'consecutive_timeouts': _confluence_mcp_health['consecutive_timeouts'],
        'last_error': _confluence_mcp_health['last_error'] or 'none',
        'circuit_breaker_open': (
            _confluence_mcp_health['consecutive_timeouts'] >= 3 and
            _time.time() - _confluence_mcp_health['last_failure'] < 120
        ) if _confluence_mcp_health['last_failure'] else False,
    }

    # Summary
    all_ok = all(
        results.get(f'step{i}_{k}', {}).get('status', '').startswith('OK')
        for i, k in [(1, 'dns'), (2, 'tcp'), (3, 'http_get'), (4, 'mcp_tools_list')]
    )
    results['overall'] = 'ALL STEPS PASSED' if all_ok else 'ISSUES DETECTED — check failed steps'

    return jsonify(results)


@app.route('/api/confluence-mcp-health')
def confluence_mcp_health():
    """Quick health status of the Confluence MCP connection.
    Returns circuit breaker state, timeout history, and last error.
    Much lighter than /api/confluence-diag (no network calls).
    """
    import time as _time

    health = dict(_confluence_mcp_health)  # Copy to avoid mutation during read
    now = _time.time()

    status = 'healthy'
    if not CONFLUENCE_MCP_URL:
        status = 'not_configured'
    elif health['consecutive_timeouts'] >= 3 and now - health['last_failure'] < 120:
        status = 'circuit_open'  # Circuit breaker is active — calls are being skipped
    elif health['consecutive_timeouts'] > 0:
        status = 'degraded'

    result = {
        'status': status,
        'mcp_url': CONFLUENCE_MCP_URL or 'NOT SET',
        'consecutive_timeouts': health['consecutive_timeouts'],
        'last_error': health['last_error'] or 'none',
        'last_success_ago': f"{round(now - health['last_success'])}s" if health['last_success'] else 'never',
        'last_failure_ago': f"{round(now - health['last_failure'])}s" if health['last_failure'] else 'never',
        'circuit_breaker': {
            'is_open': status == 'circuit_open',
            'threshold': 3,
            'cooldown_seconds': 120,
            'resets_in': f"{max(0, round(120 - (now - health['last_failure'])))}s" if health['last_failure'] else 'n/a',
        },
        'tips': [],
    }

    if status == 'circuit_open':
        result['tips'] = [
            'Circuit breaker is OPEN — all Confluence MCP calls are being skipped',
            f'Last error: {health["last_error"]}',
            'POST /api/confluence-mcp-reset to manually reset the circuit breaker',
            'Check if the Confluence MCP pod is running: kubectl get pods | grep confluence',
            'Check MCP pod logs: kubectl logs <confluence-mcp-pod> --tail=50',
        ]
    elif status == 'degraded':
        result['tips'] = [
            f'{health["consecutive_timeouts"]} timeout(s) detected — MCP server may be slow or partially down',
            'Run /api/confluence-diag for a full step-by-step diagnostic',
        ]

    return jsonify(result)


@app.route('/api/confluence-mcp-reset', methods=['POST'])
def confluence_mcp_reset():
    """Reset the Confluence MCP circuit breaker.
    Use after fixing the MCP server to allow calls to resume immediately
    without waiting for the 2-minute cooldown.
    """
    global _confluence_mcp_health
    old_state = dict(_confluence_mcp_health)
    _confluence_mcp_health = {
        'consecutive_timeouts': 0,
        'last_success': 0,
        'last_failure': 0,
        'last_error': '',
    }
    print(f'[Confluence MCP] Circuit breaker RESET by user '
          f'(was: {old_state["consecutive_timeouts"]} consecutive timeouts)')
    return jsonify({
        'status': 'reset',
        'message': 'Circuit breaker reset. Next Confluence MCP call will be attempted.',
        'previous_state': {
            'consecutive_timeouts': old_state['consecutive_timeouts'],
            'last_error': old_state['last_error'],
        }
    })


# ── K8s connectivity diagnostic ───────────────────────────────────────────────
@app.route('/api/k8s-diag')
def k8s_diagnostic():
    """Diagnostic endpoint to test K8s connectivity multiple ways."""
    import traceback
    results = {}
    namespace = NAMESPACE

    # Method 1: Fresh load_incluster_config + default AppsV1Api
    try:
        config.load_incluster_config()
        api1 = client.AppsV1Api()
        deploys = api1.list_namespaced_deployment(namespace).items
        results['method1_fresh_incluster'] = {
            'status': 'OK',
            'deployments': len(deploys),
            'names': [d.metadata.name for d in deploys[:5]]
        }
    except Exception as e:
        results['method1_fresh_incluster'] = {
            'status': 'FAIL',
            'error': str(e)[:300]
        }

    # Method 2: _local_api_client
    try:
        api2 = client.AppsV1Api()
        deploys = api2.list_namespaced_deployment(namespace).items
        results['method2_local_client'] = {
            'status': 'OK',
            'deployments': len(deploys),
            'names': [d.metadata.name for d in deploys[:5]]
        }
    except Exception as e:
        results['method2_local_client'] = {
            'status': 'FAIL',
            'error': str(e)[:300]
        }

    # Method 4: _local_api_client with default_headers (THE FIX)
    try:
        if _local_api_client:
            api4 = client.AppsV1Api(api_client=_local_api_client)
            deploys = api4.list_namespaced_deployment(namespace).items
            results['method4_default_headers'] = {
                'status': 'OK',
                'deployments': len(deploys),
                'names': [d.metadata.name for d in deploys[:5]]
            }
        else:
            results['method4_default_headers'] = {
                'status': 'SKIP',
                'reason': 'No _local_api_client (dev mode)'
            }
    except Exception as e:
        results['method4_default_headers'] = {
            'status': 'FAIL',
            'error': str(e)[:300]
        }

    # Method 3: Raw HTTP with urllib3 (bypasses kubernetes client entirely)
    try:
        import urllib3
        token_path = '/var/run/secrets/kubernetes.io/serviceaccount/token'
        ca_path = '/var/run/secrets/kubernetes.io/serviceaccount/ca.crt'
        k8s_host = os.environ.get('KUBERNETES_SERVICE_HOST', '')
        k8s_port = os.environ.get('KUBERNETES_SERVICE_PORT', '443')

        if os.path.exists(token_path):
            with open(token_path) as f:
                token = f.read().strip()
            http = urllib3.PoolManager(ca_certs=ca_path if os.path.exists(ca_path) else None)
            resp = http.request(
                'GET',
                f'https://{k8s_host}:{k8s_port}/apis/apps/v1/namespaces/{namespace}/deployments',
                headers={'Authorization': f'Bearer {token}'},
                timeout=10
            )
            body = json.loads(resp.data.decode())
            if resp.status == 200:
                items = body.get('items', [])
                results['method3_raw_urllib3'] = {
                    'status': 'OK',
                    'http_status': resp.status,
                    'deployments': len(items),
                    'names': [i['metadata']['name'] for i in items[:5]]
                }
            else:
                results['method3_raw_urllib3'] = {
                    'status': 'FAIL',
                    'http_status': resp.status,
                    'message': body.get('message', '')[:200]
                }
        else:
            results['method3_raw_urllib3'] = {
                'status': 'SKIP',
                'reason': 'No SA token file found'
            }
    except Exception as e:
        results['method3_raw_urllib3'] = {
            'status': 'ERROR',
            'error': str(e)[:300]
        }

    # Environment info
    results['env'] = {
        'namespace': namespace,
        'deploy_env': DEPLOY_ENV,
        'k8s_host': os.environ.get('KUBERNETES_SERVICE_HOST', 'NOT SET'),
        'k8s_port': os.environ.get('KUBERNETES_SERVICE_PORT', 'NOT SET'),
        'sa_token_exists': os.path.exists('/var/run/secrets/kubernetes.io/serviceaccount/token'),
        'ca_cert_exists': os.path.exists('/var/run/secrets/kubernetes.io/serviceaccount/ca.crt'),
    }

    return jsonify(results)


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

    print(f'[services] DEPLOY_ENV=uat → reading LOCAL cluster, namespace={namespace}')

    # Check if in-cluster config was loaded
    try:
        _cfg = client.Configuration.get_default_copy()
        print(f'[services] K8s API host: {_cfg.host}')
        print(f'[services] K8s API key set: {bool(_cfg.api_key)}')
    except Exception as _e:
        print(f'[services] ⚠️  Cannot read K8s config: {_e}')

    services = []
    errors = []
    try:
        apps_v1 = client.AppsV1Api(api_client=_local_api_client)

        # Deployments
        try:
            deploys = _k8s_retry(apps_v1.list_namespaced_deployment, namespace).items
            print(f'[services] ✅ Deployments: {len(deploys)} found')
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
            err_msg = f'Deployments: {type(e).__name__}: {str(e)[:200]}'
            errors.append(err_msg)
            print(f"[services] ❌ {err_msg}")

        # StatefulSets
        try:
            sts = _k8s_retry(apps_v1.list_namespaced_stateful_set, namespace).items
            print(f'[services] ✅ StatefulSets: {len(sts)} found')
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
            err_msg = f'StatefulSets: {type(e).__name__}: {str(e)[:200]}'
            errors.append(err_msg)
            print(f"[services] ❌ {err_msg}")

        # DaemonSets
        try:
            dss = _k8s_retry(apps_v1.list_namespaced_daemon_set, namespace).items
            print(f'[services] ✅ DaemonSets: {len(dss)} found')
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
            err_msg = f'DaemonSets: {type(e).__name__}: {str(e)[:200]}'
            errors.append(err_msg)
            print(f"[services] ❌ {err_msg}")

    except Exception as e:
        print(f'[services] ❌ Fatal K8s error: {e}')
        return jsonify({'error': str(e)}), 500

    print(f'[services] Total: {len(services)} services, {len(errors)} errors')
    result = {'services': services, 'namespace': namespace, 'count': len(services)}
    if errors:
        result['error'] = ' | '.join(errors)
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

    # Also capture the parent folder's lastModified as a fallback date
    parent_last_modified = data.get('lastModified', data.get('created', ''))

    if 'files' in data:
        # ?list response format — dates are included inline
        for item in data.get('files', []):
            uri = item.get('uri', '').strip('/')
            if item.get('folder', False) and uri:
                versions.append({
                    'version': uri,
                    'date': item.get('lastModified', ''),
                    'size': item.get('size', 0),
                    '_date_source': 'list_api',
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
                        'size': item.get('size', 0),
                        '_date_source': 'list_api',
                    })
    elif 'children' in data:
        # Standard /api/storage response format — NO dates in children
        for child in data.get('children', []):
            uri = child.get('uri', '').strip('/')
            if child.get('folder', False) and uri:
                versions.append({
                    'version': uri,
                    'date': '',  # Will be fetched individually below
                    'size': 0,
                    '_date_source': 'pending',
                })

    if not versions:
        print(f'[artifactory] No versions found in response for {artifactory_path}')
        _cache_set(cache_key, [])
        return []

    # ── Fetch upload dates for ALL versions missing dates ──────────────
    # Use concurrent requests for speed (fetch ALL, not just top 10)
    versions_needing_dates = [v for v in versions if not v['date']]
    if versions_needing_dates:
        print(f'[artifactory] Fetching dates for {len(versions_needing_dates)} versions...')
        from concurrent.futures import ThreadPoolExecutor, as_completed

        def _fetch_version_date(version_entry):
            """Fetch lastModified/created date for a single version folder."""
            ver = version_entry['version']
            try:
                folder_url = f"{ARTIFACTORY_URL}/api/storage/{artifactory_path}/{ver}"
                r2 = requests.get(folder_url, headers=headers, timeout=8, verify=ssl_verify)
                if r2.ok:
                    d2 = r2.json()
                    # Prefer lastModified (actual upload date), fall back to created
                    date = d2.get('lastModified', '') or d2.get('created', '')
                    if date:
                        version_entry['date'] = date
                        version_entry['_date_source'] = 'folder_api'
                        return True
            except Exception as e:
                print(f'[artifactory] Date fetch failed for {ver}: {e}')
            return False

        # Fetch concurrently — max 5 parallel requests to avoid hammering Artifactory
        with ThreadPoolExecutor(max_workers=5) as executor:
            futures = {executor.submit(_fetch_version_date, v): v for v in versions_needing_dates}
            for future in as_completed(futures):
                try:
                    future.result()
                except Exception:
                    pass

        dated_count = sum(1 for v in versions if v['date'])
        print(f'[artifactory] Got dates for {dated_count}/{len(versions)} versions')

    # ── Parse dates into comparable datetime objects for accurate sorting ──
    def _parse_date(date_str):
        """Parse various Artifactory date formats into a datetime object."""
        if not date_str:
            return None
        try:
            # ISO format: "2026-05-19T14:30:00.000Z" or "2026-05-19T14:30:00.000+00:00"
            cleaned = date_str.replace('Z', '+00:00')
            # Remove timezone for naive comparison
            if '+' in cleaned and 'T' in cleaned:
                cleaned = cleaned[:cleaned.rfind('+')]
            elif cleaned.endswith('+00:00'):
                cleaned = cleaned[:-6]
            return datetime.datetime.fromisoformat(cleaned)
        except (ValueError, TypeError):
            pass
        try:
            # Fallback: "2026-05-19 14:30:00"
            return datetime.datetime.strptime(date_str[:19], '%Y-%m-%d %H:%M:%S')
        except (ValueError, TypeError):
            pass
        return None

    # ── Sort by upload date descending (newest first) ──────────────────
    # Versions with dates sort first; versions without dates sort last
    epoch = datetime.datetime(1970, 1, 1)  # For unknown dates
    for v in versions:
        v['_parsed_date'] = _parse_date(v['date']) or epoch

    versions.sort(key=lambda v: v['_parsed_date'], reverse=True)

    if versions and versions[0]['_parsed_date'] != epoch:
        print(f'[artifactory] Sorted by upload date. Newest: {versions[0]["version"]} ({versions[0]["date"]})')
    elif versions:
        print(f'[artifactory] ⚠ No upload dates available — version order may not reflect recency')

    # Clean up internal fields and add freshness labels
    for v in versions:
        del v['_parsed_date']
        v['freshness'] = _version_freshness(v['date']) if v['date'] else 'unknown'

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

    return jsonify({
        'component': component_name,
        'artifactory_configured': True,
        'artifactory_path': art_path,
        'version_count': len(versions),
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
    # ── CRITICAL: Set socket-level timeouts to prevent indefinite hangs ──
    # Without these, an unreachable prod cluster will block the worker thread
    # forever, causing liveness probe failures and pod restarts (SIGTERM).
    prod_config.connect_timeout = 10   # seconds to establish TCP connection
    prod_config.read_timeout = 15      # seconds to read response

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
    # ── CRITICAL: Do NOT use uat_config.api_key = {...} ──────────────────────
    # The kubernetes client's api_key auth mechanism is broken under gevent
    # monkey.patch_all() (see local client fix at line ~1625).
    # Symptoms: token never gets sent → K8s API sees "system:anonymous" → 403.
    # Fix: inject the Bearer token via default_headers on the ApiClient instead.
    # ─────────────────────────────────────────────────────────────────────────
    # ── CRITICAL: Set socket-level timeouts to prevent indefinite hangs ──
    uat_config.connect_timeout = 10
    uat_config.read_timeout = 15

    # SSL configuration
    verify_ssl = os.environ.get('UAT_CLUSTER_VERIFY_SSL', 'true').lower()
    if verify_ssl == 'false':
        uat_config.verify_ssl = False
        import urllib3
        urllib3.disable_warnings(urllib3.exceptions.InsecureRequestWarning)
    else:
        uat_config.verify_ssl = True

    api_client_obj = client.ApiClient(uat_config)
    # Inject Authorization header directly — bypasses broken api_key mechanism
    api_client_obj.default_headers['Authorization'] = f'Bearer {uat_token}'
    print(f'[uat-remote] ✅ Token injected via default_headers ({len(uat_token)} chars)')

    return api_client_obj, None


def _list_services_from_api(api_client, namespace, log_prefix='[remote]'):
    """Shared helper to list Deployments/StatefulSets/DaemonSets from a K8s API client."""
    services = []
    try:
        apps_v1 = client.AppsV1Api(api_client)
    except Exception as e:
        print(f'{log_prefix} ❌ Failed to create AppsV1Api: {e}')
        raise
    print(f'{log_prefix} API client ready, listing deployments in namespace={namespace}...')

    # Use _timeout_seconds on each K8s API call to prevent indefinite blocking.
    # This is the server-side timeout — the K8s API server will abort after this.
    _timeout_seconds = 15

    # Deployments
    try:
        deploys = apps_v1.list_namespaced_deployment(namespace, _request_timeout=_timeout_seconds).items
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
        sts = apps_v1.list_namespaced_stateful_set(namespace, _request_timeout=_timeout_seconds).items
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
        dss = apps_v1.list_namespaced_daemon_set(namespace, _request_timeout=_timeout_seconds).items
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

    # Auto-lifecycle: when the release date has passed, archive the board and start a new cycle.
    # This handles two scenarios:
    #   1. Board was manually "Mark Released" → status='released' → archive + new board
    #   2. Board was NEVER released (still 'open'/'locked') → auto-release + archive + new board
    if board.get('release_date'):
        try:
            release_date = datetime.date.fromisoformat(board['release_date'])
            # Archive after 11:59 PM on release day (i.e. the next calendar day)
            archive_threshold = datetime.datetime.combine(
                release_date + datetime.timedelta(days=1),
                datetime.time(0, 0)
            )
            now = datetime.datetime.utcnow()

            if now >= archive_threshold:
                board_status = board.get('status', 'open')

                # If board was never released, auto-mark it as released first
                if board_status in ('open', 'locked'):
                    print(f"[release] Auto-releasing board for {board['release_date']} (was '{board_status}', release date has passed)")
                    board['status'] = 'released'
                    board['released_at'] = now.isoformat()
                    board['released_by'] = 'system (auto-released)'
                    board['audit_trail'].append({
                        'action': 'auto_release',
                        'by': 'system',
                        'at': now.isoformat(),
                        'note': f"Board auto-released — release date {board['release_date']} has passed without manual release"
                    })

                # Archive to history (only if not already added by complete_release)
                if board_status == 'released' or board.get('status') == 'released':
                    # Avoid duplicate: complete_release() already appends to _release_history
                    already_in_history = any(
                        h.get('release_date') == board.get('release_date') and h.get('fix_version') == board.get('fix_version')
                        for h in _release_history
                    )
                    print(f"[release] Auto-archiving board for {board['release_date']}, starting new cycle")
                    board['audit_trail'].append({
                        'action': 'auto_archive',
                        'by': 'system',
                        'at': now.isoformat(),
                        'note': f"Board auto-archived after release date {board['release_date']}"
                    })
                    if not already_in_history:
                        _release_history.append(copy.deepcopy(board))
                        _write_history_file()  # Persist to PVC
                    _write_board(board)  # Save the audit entry to the old board
                    board = _new_board()
                    _write_board(board)
        except (ValueError, TypeError) as e:
            print(f"[release] Auto-archive date parse error: {e}")

    # Enrich with live metadata
    # ── CRITICAL: Use the LIVE cutoff for the current release window, not the
    # stored cutoff which can become stale if the board rolls over weeks.
    # The stored cutoff is from when the board was created — if that was last
    # week, it's already in the past and would incorrectly show "Locked".
    live_cutoff = _get_cutoff_datetime()
    stored_cutoff = board.get('cutoff', '')

    # If the board's release_date matches the current release window, use the
    # stored cutoff (it's correct). Otherwise use the live recalculated cutoff.
    current_release = _get_current_release_date()
    effective_cutoff = stored_cutoff if board.get('release_date') == current_release else live_cutoff

    # Update the board's cutoff to the effective one so the UI always shows the right time
    if effective_cutoff != stored_cutoff:
        board['cutoff'] = effective_cutoff
        print(f"[release] Updated stale cutoff {stored_cutoff} → {effective_cutoff} (board release_date={board.get('release_date')}, current={current_release})")

    now_iso = datetime.datetime.utcnow().isoformat()
    board['is_past_cutoff'] = now_iso > effective_cutoff
    board['nominated_count'] = len(board.get('services', {}))
    board['exception_count'] = len(board.get('exception_nominations', []))

    # Debug: log the lock decision
    print(f"[release] Board state: status={board.get('status')}, is_past_cutoff={board['is_past_cutoff']}, "
          f"manual_unlock={board.get('manual_unlock')}, cutoff={effective_cutoff}, now={now_iso}")

    # Auto-reflect locked state in UI when past cutoff
    # The nomination endpoint already blocks regular nominations past cutoff,
    # but the UI needs status='locked' to show the correct buttons (Board Locked + Unlock).
    # IMPORTANT: Do NOT re-lock if the board was manually unlocked (manual_unlock flag).
    # This prevents the auto-lock from overriding a release manager's explicit unlock.
    if board['is_past_cutoff'] and board.get('status') == 'open' and not board.get('manual_unlock'):
        board['status'] = 'locked'
        board['auto_locked'] = True  # Flag so UI can distinguish manual vs auto lock
        _write_board(board)  # Persist so the lock state survives pod restarts
        print(f"[release] ✅ Auto-locked board (past cutoff {effective_cutoff})")

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
    # Use the effective cutoff (recalculated for current release window if board is stale)
    current_release = _get_current_release_date()
    effective_cutoff = board.get('cutoff', '') if board.get('release_date') == current_release else _get_cutoff_datetime()
    is_past_cutoff = datetime.datetime.utcnow().isoformat() > effective_cutoff
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
                apps_v1 = client.AppsV1Api(api_client=_local_api_client)
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
    board['manual_unlock'] = False  # Clear: explicit lock overrides prior unlock
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
    board['manual_unlock'] = True   # Prevent auto-lock from re-locking
    board['auto_locked'] = False
    board['audit_trail'].append({
        'action': 'unlock',
        'by': unlocked_by,
        'at': now,
        'note': 'Board manually unlocked for editing (auto-lock suppressed)'
    })

    _write_board(board)
    return jsonify({'status': 'open', 'unlocked_by': unlocked_by, 'manual_unlock': True})


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
    _release_history.append(copy.deepcopy(board))
    _write_history_file()  # Persist to PVC

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
    """Compare nominated versions against UAT live cluster versions.

    When DEPLOY_ENV=uat: reads from the LOCAL cluster (we're IN uat).
    When DEPLOY_ENV=prod: connects REMOTELY to UAT via UAT_CLUSTER_API/TOKEN.
    """
    board = _read_board()
    if not board or not board.get('services'):
        return jsonify({'drift_items': [], 'message': 'No nominations to check'})

    namespace = request.args.get('namespace', NAMESPACE)
    drift_items = []
    cluster_label = 'UAT'

    # ── Get the correct K8s API client for the UAT cluster ──
    if DEPLOY_ENV == 'prod':
        # App is in prod → connect REMOTELY to UAT
        uat_ns = os.environ.get('UAT_NAMESPACE', namespace)
        api_client, error = _get_uat_api_client()
        if error:
            print(f'[drift] ❌ Cannot connect to UAT cluster: {error}')
            return jsonify({
                'drift_items': [],
                'error': f'Cannot connect to UAT cluster for drift check: {error}',
                'cluster': 'uat-remote',
                'deploy_env': DEPLOY_ENV,
            })
        apps_v1 = client.AppsV1Api(api_client)
        namespace = uat_ns
        cluster_label = f'UAT (remote: {os.environ.get("UAT_CLUSTER_API", "")[:40]})'
        print(f'[drift] DEPLOY_ENV=prod → checking drift against REMOTE UAT cluster, ns={namespace}')
    else:
        # App is in UAT → read LOCAL cluster
        apps_v1 = client.AppsV1Api(api_client=_local_api_client)
        cluster_label = 'UAT (local)'
        print(f'[drift] DEPLOY_ENV=uat → checking drift against LOCAL cluster, ns={namespace}')

    for svc_name, svc_data in board.get('services', {}).items():
        nominated_tag = svc_data.get('image_tag', '')
        kind = svc_data.get('kind', 'Deployment')
        live_tag = ''
        live_image = ''

        try:
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

    return jsonify({
        'drift_items': drift_items,
        'cluster': cluster_label,
        'namespace': namespace,
        'deploy_env': DEPLOY_ENV,
    })


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
    v1 = client.CoreV1Api(api_client=_local_api_client)

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
            apps_v1 = client.AppsV1Api(api_client=_local_api_client)
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
    try:
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

        # Multi-strategy search with keyword extraction (space defaults handled inside)
        results = _confluence_search(query, space, max_results=20)

        # AI summarization — read top 5 keyword-matched pages in full,
        # include all titles (including browse results) as context
        summary = None
        if ai_summary and results:
            try:
                # Separate keyword-matched results from browse-all results
                keyword_results = [r for r in results if not r.get('_browse_result')]
                browse_results = [r for r in results if r.get('_browse_result')]

                pages_text = []
                # Read top 5 keyword-matched pages in full (these are the most relevant)
                pages_to_read = keyword_results[:5]
                # If we have fewer than 5 keyword results, supplement with browse results
                if len(pages_to_read) < 5 and browse_results:
                    pages_to_read += browse_results[:5 - len(pages_to_read)]

                for r in pages_to_read:
                    page = _confluence_get_page(r['id'])
                    if page and page.get('body_text'):
                        pages_text.append(f"## {page['title']}\n{page['body_text'][:3000]}")
                    elif r.get('excerpt'):
                        pages_text.append(f"## {r['title']}\n{r['excerpt']}")

                # Build title lists for AI context
                keyword_titles = [f"- {r['title']}" for r in keyword_results]
                browse_titles = [f"- {r['title']}" for r in browse_results]
                all_titles_section = ""
                if keyword_titles:
                    all_titles_section += f"Pages Matching Keywords ({len(keyword_results)}):\n" + '\n'.join(keyword_titles)
                if browse_titles:
                    all_titles_section += f"\n\nOther Pages in This Space ({len(browse_results)}):\n" + '\n'.join(browse_titles)
                if not keyword_titles and not browse_titles:
                    all_titles_section = "(no pages found)"

                if pages_text:
                    prompt = f"""Based on these Confluence documentation pages from the organization's wiki, answer the user's question concisely and accurately.

User Question: {query}

{all_titles_section}

Full Content of Top {len(pages_text)} Pages:
{'---'.join(pages_text)}

Instructions:
- Extract and provide the SPECIFIC answer the user is looking for (URLs, commands, configuration values, steps, etc.).
- If the question asks for a URL, path, or specific value, find it in the page content and present it prominently.
- Provide a direct, actionable answer — do not just summarize the page topics.
- If the question is about a procedure, give step-by-step instructions.
- Cite which page each piece of information comes from.
- IMPORTANT: Review the "Other Pages in This Space" list carefully. If any page TITLE looks like it could answer the user's question but its full content wasn't read, explicitly recommend the user check that page.
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
    except Exception as e:
        print(f'[confluence] Search endpoint error: {e}')
        return jsonify({'results': [], 'ai_summary': None, 'error': str(e),
                       'configured': True}), 500


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

    # Try MCP first — use confluence_search with CQL label= syntax
    if CONFLUENCE_MCP_URL:
        label_cql_parts = ' AND '.join([f'label="{l}"' for l in labels])
        label_cql = f'type=page AND {label_cql_parts}'
        if space:
            label_cql += f' AND space="{space}"'
        for tool_name in ['confluence_search']:
            raw = _confluence_mcp_call(tool_name, {'cql': label_cql, 'limit': 20})
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
    rediscover = request.args.get('rediscover', '')
    if rediscover:
        _discover_confluence_mcp_tools()
    return jsonify({
        'configured': bool(CONFLUENCE_MCP_URL or CONFLUENCE_BASE_URL),
        'mcp_url': bool(CONFLUENCE_MCP_URL),
        'rest_url': bool(CONFLUENCE_BASE_URL),
        'spaces': CONFLUENCE_SPACES,
        'discovered_tools': _CONFLUENCE_TOOLS,
        'discovered_count': len(_CONFLUENCE_TOOLS),
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

# NOTE: /api/release/history is handled by get_release_history() above (line ~3204).
# A legacy ConfigMap-based version was removed here — it was shadowing the correct
# handler and returning a different response shape ('releases' vs 'history' key).


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

    # Exchange code for token
    # Strategy: Try direct connection first (many corp networks allow github.com
    # through egress), then fall back to the existing proxy-authenticated gh_http
    # session. Python urllib3 cannot do Kerberos/Negotiate for CONNECT tunnels,
    # so a dedicated proxy session will always get 407 on HTTPS.
    try:
        token_url = f'{GITHUB_URL}/login/oauth/access_token'
        token_payload = {
            'client_id': GITHUB_CLIENT_ID,
            'client_secret': GITHUB_CLIENT_SECRET,
            'code': code,
            'redirect_uri': _get_callback_url(),
        }
        token_headers = {'Accept': 'application/json'}
        token_response = None
        _direct_err = None  # persist across except blocks (Python 3 deletes `as e` vars)

        # Phase 1: Try direct (no proxy) — works if egress to github.com is open
        try:
            print(f'[OAuth] Phase 1: Trying direct connection to {token_url}')
            direct_http = requests.Session()
            direct_http.verify = SSL_VERIFY
            # Explicitly clear proxy to bypass any env-level HTTP_PROXY/HTTPS_PROXY
            direct_http.proxies = {'http': '', 'https': ''}
            direct_http.trust_env = False
            token_response = direct_http.post(
                token_url,
                headers=token_headers,
                data=token_payload,
                timeout=15,
            )
            direct_http.close()
            print(f'[OAuth] Phase 1 SUCCESS: direct connection worked (status={token_response.status_code})')
        except Exception as direct_err:
            _direct_err = direct_err  # save before Python 3 deletes it
            print(f'[OAuth] Phase 1 FAILED (direct): {direct_err}')
            token_response = None

        # Phase 2: If direct failed, try via proxy with Kerberos.
        # IMPORTANT: Do NOT use gh_http — it has HTTPAdapter(max_retries=3)
        # which conflicts with Kerberos 407 proxy-auth handshake, corrupting
        # the CONNECT tunnel. Use a bare session with Kerberos + NO retries,
        # exactly matching the Pipeline Hub's working OAuth pattern.
        if token_response is None:
            try:
                print(f'[OAuth] Phase 2: Trying via proxy with Kerberos (proxy: {PROXY_URL or "none"})')
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
                        print(f'[OAuth] Kerberos auth configured for proxy')
                    except ImportError:
                        print(f'[OAuth] WARNING: requests-kerberos not installed — proxy auth will fail')
                token_response = oauth_http.post(
                    token_url,
                    headers=token_headers,
                    data=token_payload,
                    timeout=30,
                )
                oauth_http.close()
                print(f'[OAuth] Phase 2 SUCCESS: proxy connection worked (status={token_response.status_code})')
            except Exception as proxy_err:
                print(f'[OAuth] Phase 2 FAILED (proxy): {proxy_err}')
                return jsonify({
                    'error': f'Cannot reach GitHub for OAuth. '
                             f'Direct: {_direct_err}. '
                             f'Proxy: {proxy_err}. '
                             f'Check network/proxy configuration.'
                }), 502
        token_data = token_response.json()

        if 'access_token' not in token_data:
            error = token_data.get('error_description', token_data.get('error', 'Unknown error'))
            print(f"[OAuth] Token exchange failed: {error}")
            return jsonify({'error': f'OAuth failed: {error}'}), 400

        access_token = token_data['access_token']
        session['github_token'] = access_token
        session.permanent = True  # Use PERMANENT_SESSION_LIFETIME (7 days)

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
# QA Tab — GitOps Deployment Pipeline
# ══════════════════════════════════════════════════════════════════════════════

# In-memory state for QA flow progress
_qa_state = {
    'status': 'idle',        # idle | preparing | e2e_pushed | error
    'e2e_commit_url': None,
    'version_manifest': None,
    'error': None,
    'nominated_count': 0,
    'prod_count': 0,
    'total_count': 0,
    'prepared_at': None,
    'prepared_by': None,
    # Prod/preprod tracking
    'prod_status': 'idle',   # idle | pushing | pushed | error
    'prod_commit_url': None,
    'preprod_commit_url': None,
    'change_ticket': None,
    'prod_pushed_at': None,
    'prod_pushed_by': None,
}


def _fetch_prod_services_internal():
    """Fetch production service list (reuses the prod cluster logic).
    Returns a list of dicts: [{name, image, image_tag, kind, ...}]
    """
    prod_ns = os.environ.get('PROD_NAMESPACE', NAMESPACE)

    if DEPLOY_ENV == 'prod':
        # We ARE in prod — read local cluster
        try:
            return _list_services_from_api(client.ApiClient(), prod_ns, '[qa-prod-local]')
        except Exception as e:
            print(f'[qa] Error reading local prod: {e}')
            return []
    else:
        # We're in UAT — connect remotely to prod
        try:
            api_client, error = _get_prod_api_client()
            if error:
                print(f'[qa] Prod client error: {error}')
                return []
            return _list_services_from_api(api_client, prod_ns, '[qa-prod-remote]')
        except Exception as e:
            print(f'[qa] Error connecting to prod: {e}')
            return []


def _build_version_manifest(board, change_ticket=None):
    """Build a version manifest dict from board nominations + prod services.
    Returns (manifest_dict, nominated_count, prod_count).
    """
    # Get nominated services from board
    nominated = {}
    for svc_name, svc_data in board.get('services', {}).items():
        nominated[svc_name] = {
            'image': svc_data.get('image', ''),
            'image_tag': svc_data.get('image_tag', svc_data.get('version', '')),
            'kind': svc_data.get('kind', 'Deployment'),
            'source': 'board',
            'nominated_by': svc_data.get('nominated_by', ''),
        }

    # Get prod services
    prod_services = _fetch_prod_services_internal()

    # Merge: board nominations + non-nominated prod versions
    version_manifest = {}
    if change_ticket:
        version_manifest['change_ticket'] = change_ticket
    version_manifest['release_date'] = board.get('release_date', '')
    version_manifest['qa_namespace'] = QA_NAMESPACE
    version_manifest['generated_at'] = datetime.datetime.utcnow().isoformat()
    version_manifest['services'] = {}

    for svc in prod_services:
        svc_name = svc['name']
        if svc_name in nominated:
            version_manifest['services'][svc_name] = nominated[svc_name]
        else:
            version_manifest['services'][svc_name] = {
                'image': svc.get('image', ''),
                'image_tag': svc.get('image_tag', ''),
                'kind': svc.get('kind', 'Deployment'),
                'source': 'production',
            }

    # Also add any board nominations not found in prod (new services)
    for svc_name, svc_data in nominated.items():
        if svc_name not in version_manifest['services']:
            version_manifest['services'][svc_name] = svc_data

    prod_count = len(version_manifest['services']) - len(nominated)
    return version_manifest, len(nominated), max(prod_count, 0)


def _push_version_yaml(version_yaml_content, branch, commit_message):
    """Push version.yaml to a specific branch in QA_DEPLOY_REPO.

    Creates the branch from default branch if it doesn't exist.
    Returns dict with commit_url, or error.
    """
    if not QA_DEPLOY_REPO:
        return {'error': 'QA_DEPLOY_REPO not configured'}

    try:
        owner, repo = QA_DEPLOY_REPO.split('/', 1)
    except ValueError:
        return {'error': f'Invalid QA_DEPLOY_REPO format: "{QA_DEPLOY_REPO}"'}

    file_path = 'version.yaml'

    # Pre-flight: check if the token is actually valid
    token = get_gh_token()
    if not token:
        return {'error': 'GitHub token is empty — session may have expired. Please re-login.'}

    try:
        # 1. Get default branch SHA
        repo_info = _github_get(f'/repos/{owner}/{repo}')
        default_branch = repo_info.get('default_branch', 'main')
        main_ref = _github_get(f'/repos/{owner}/{repo}/git/ref/heads/{default_branch}')
        main_sha = main_ref['object']['sha']

        # 2. Create or update branch
        try:
            _github_get(f'/repos/{owner}/{repo}/git/ref/heads/{branch}')
            # Branch exists — keep it (don't force-reset for prod/preprod)
            print(f'[qa] Branch {branch} exists')
        except Exception:
            # Branch doesn't exist — create it
            _github_post(f'/repos/{owner}/{repo}/git/refs', {
                'ref': f'refs/heads/{branch}',
                'sha': main_sha
            })
            print(f'[qa] Created {branch} branch from {default_branch} ({main_sha[:8]})')

        # 3. Create/update version.yaml on the branch
        existing_sha = None
        try:
            check_resp = gh_http.get(
                f'{GITHUB_API}/repos/{owner}/{repo}/contents/{file_path}?ref={branch}',
                headers=_github_headers(), timeout=(5, 10)
            )
            if check_resp.status_code == 200:
                ct = check_resp.headers.get('content-type', '')
                if 'application/json' in ct or 'application/vnd.github' in ct:
                    existing_sha = check_resp.json().get('sha')
                    print(f'[qa] Found existing version.yaml on {branch} (sha={existing_sha[:8]})')
                else:
                    print(f'[qa] version.yaml check returned non-JSON (content-type: {ct}) — will create fresh')
            elif check_resp.status_code == 401:
                return {'error': 'GitHub token expired or revoked. Please re-login.'}
            else:
                print(f'[qa] version.yaml not found on {branch} branch (HTTP {check_resp.status_code}) — will create fresh')
        except Exception:
            print(f'[qa] Could not check version.yaml on {branch} — will create fresh')

        payload = {
            'message': commit_message,
            'content': base64.b64encode(version_yaml_content.encode()).decode(),
            'branch': branch
        }
        if existing_sha:
            payload['sha'] = existing_sha

        put_resp = gh_http.put(
            f'{GITHUB_API}/repos/{owner}/{repo}/contents/{file_path}',
            headers=_github_headers(), json=payload, timeout=(5, 10)
        )
        if put_resp.status_code == 401:
            return {'error': 'GitHub token expired or revoked. Please re-login.'}
        if put_resp.status_code not in (200, 201):
            return {'error': f'Failed to push version.yaml: {put_resp.status_code} {put_resp.text[:200]}'}

        # Safe JSON parse for the commit response
        ct = put_resp.headers.get('content-type', '')
        if 'application/json' in ct or 'application/vnd.github' in ct:
            commit_data = put_resp.json()
        else:
            print(f'[qa] Push succeeded (HTTP {put_resp.status_code}) but response is not JSON — commit URL unavailable')
            commit_data = {}
        commit_url = (commit_data.get('commit') or {}).get('html_url', '')
        print(f'[qa] Pushed version.yaml to {branch} branch')

        return {'commit_url': commit_url, 'branch': branch}

    except requests.exceptions.HTTPError as e:
        status = getattr(e.response, 'status_code', None)
        if status == 401:
            return {'error': 'GitHub token expired or revoked. Please re-login.'}
        print(f'[qa] Push error ({branch}): HTTP {status}: {e}')
        return {'error': f'GitHub API error (HTTP {status}): {str(e)[:200]}'}
    except Exception as e:
        print(f'[qa] Push error ({branch}): {e}')
        return {'error': str(e)}


def _fetch_version_yaml_from_branch(branch):
    """Fetch and parse version.yaml from a branch in QA_DEPLOY_REPO.
    Returns parsed dict or None.
    """
    if not QA_DEPLOY_REPO:
        return None
    try:
        owner, repo = QA_DEPLOY_REPO.split('/', 1)
        resp = gh_http.get(
            f'{GITHUB_API}/repos/{owner}/{repo}/contents/version.yaml?ref={branch}',
            headers=_github_headers(), timeout=(5, 10)
        )
        if resp.status_code != 200:
            print(f'[qa] version.yaml not found on {branch} (HTTP {resp.status_code})')
            return None
        content = base64.b64decode(resp.json().get('content', '')).decode('utf-8')
        return yaml.safe_load(content)
    except Exception as e:
        print(f'[qa] Could not fetch version.yaml from {branch}: {e}')
        return None


# ── QA API Endpoints ──────────────────────────────────────────────────────────

@app.route('/api/qa/prepare', methods=['POST'])
def qa_prepare():
    """Step 1: Generate version.yaml and push to e2e branch.

    Returns 202 immediately — the GitHub push runs in a background greenlet.
    Frontend polls /api/qa/prepare/status for completion.
    """
    global _qa_state

    if not is_gh_authenticated():
        return jsonify({'error': 'GitHub login required'}), 401
    if not QA_DEPLOY_REPO:
        return jsonify({'error': 'QA_DEPLOY_REPO not configured. Set QA_DEPLOY_REPO or DEPLOY_REPO environment variable.'}), 500

    # Check board is locked
    try:
        board = _read_board()
    except Exception as e:
        print(f'[qa] Error reading board: {e}')
        return jsonify({'error': f'Failed to read release board: {str(e)}'}), 500

    if not board:
        return jsonify({'error': 'No release board found'}), 404

    is_past_cutoff = datetime.datetime.utcnow().isoformat() > (board.get('cutoff') or '')
    board_is_locked = board.get('status') == 'locked' or is_past_cutoff

    if not board_is_locked:
        return jsonify({
            'error': 'Board must be locked before QA preparation. Lock the board or wait for cutoff.',
            'board_status': board.get('status'),
            'cutoff': board.get('cutoff')
        }), 400

    # Prevent double-submit
    if _qa_state.get('status') == 'preparing':
        return jsonify({'status': 'preparing', 'message': 'Already in progress — poll /api/qa/prepare/status'}), 202

    # Build version manifest (fast — no network calls)
    try:
        version_manifest, nom_count, prod_count = _build_version_manifest(board)
    except Exception as e:
        print(f'[qa] Error building version manifest: {e}')
        return jsonify({'error': f'Failed to build version manifest: {str(e)}'}), 500

    # Capture session data before spawning background (Flask session not available there)
    gh_user = session.get('github_user', {}).get('login', 'unknown')
    gh_token = get_gh_token()
    release_date = board.get('release_date', '')

    # Set state to preparing
    _qa_state['status'] = 'preparing'
    _qa_state['error'] = None

    # ── Background worker: push to GitHub ─────────────────────────
    def _do_push():
        """Run the GitHub push in background (gevent greenlet)."""
        global _qa_state
        # Set thread-local token so _github_headers() works outside Flask request
        _token_local.github_token = gh_token
        try:
            version_yaml = yaml.dump(version_manifest, default_flow_style=False, sort_keys=False)

            # Build headers with the captured token (not from session)
            push_headers = {
                'Authorization': f'token {gh_token}',
                'Accept': 'application/vnd.github.v3+json',
                'User-Agent': 'ReleaseReadiness/1.0'
            }

            # Use _push_version_yaml (it reads token from session, which
            # is fine because we're using the same gevent context)
            push_result = _push_version_yaml(
                version_yaml,
                branch='e2e',
                commit_message=f'chore: update version.yaml for QA release {release_date}'
            )

            if 'error' in push_result:
                _qa_state['status'] = 'error'
                _qa_state['error'] = push_result['error']
                print(f'[qa] Background push failed: {push_result["error"]}')
                return

            now = datetime.datetime.utcnow().isoformat()

            # Update state
            _qa_state.update({
                'status': 'e2e_pushed',
                'e2e_commit_url': push_result.get('commit_url'),
                'version_manifest': version_manifest,
                'nominated_count': nom_count,
                'prod_count': prod_count,
                'total_count': len(version_manifest.get('services', {})),
                'prepared_at': now,
                'prepared_by': gh_user,
                'error': None,
            })

            # Audit trail
            try:
                board_now = _read_board()
                if board_now:
                    board_now['audit_trail'].append({
                        'action': 'qa_e2e_prepared',
                        'branch': 'e2e',
                        'services_count': len(version_manifest.get('services', {})),
                        'nominated': nom_count,
                        'from_prod': prod_count,
                        'commit_url': push_result.get('commit_url'),
                        'by': gh_user,
                        'at': now
                    })
                    _write_board(board_now)
            except Exception as audit_err:
                print(f'[qa] Audit trail update failed (non-fatal): {audit_err}')

            print(f'[qa] Background push completed — {len(version_manifest.get("services", {}))} services pushed to e2e branch')

        except Exception as e:
            _qa_state['status'] = 'error'
            _qa_state['error'] = str(e)
            print(f'[qa] Background push error: {e}')

    # Spawn background greenlet (gevent — non-blocking)
    import gevent
    gevent.spawn(_do_push)

    # Return immediately — frontend polls /api/qa/prepare/status
    return jsonify({
        'status': 'preparing',
        'message': 'Preparing E2E environment — push to GitHub in progress...',
        'nominated_count': nom_count,
        'prod_count': prod_count,
        'total': len(version_manifest.get('services', {})),
    }), 202


@app.route('/api/qa/prepare/status')
def qa_prepare_status():
    """Get the current QA preparation status."""
    return jsonify({
        'status': _qa_state.get('status', 'idle'),
        'e2e_commit_url': _qa_state.get('e2e_commit_url'),
        'nominated_count': _qa_state.get('nominated_count', 0),
        'prod_count': _qa_state.get('prod_count', 0),
        'total_count': _qa_state.get('total_count', 0),
        'prepared_at': _qa_state.get('prepared_at'),
        'prepared_by': _qa_state.get('prepared_by'),
        'error': _qa_state.get('error'),
        # Prod/preprod status
        'prod_status': _qa_state.get('prod_status', 'idle'),
        'prod_commit_url': _qa_state.get('prod_commit_url'),
        'preprod_commit_url': _qa_state.get('preprod_commit_url'),
        'change_ticket': _qa_state.get('change_ticket'),
        'prod_pushed_at': _qa_state.get('prod_pushed_at'),
    })


@app.route('/api/qa/drift-check', methods=['POST'])
def qa_drift_check():
    """Step 4: Compare current board+prod versions against the e2e version.yaml already pushed."""
    if not is_gh_authenticated():
        return jsonify({'error': 'GitHub login required'}), 401

    board = _read_board()
    if not board:
        return jsonify({'error': 'No release board found'}), 404

    try:
        # Build current version manifest from live board + prod
        current_manifest, nom_count, prod_count = _build_version_manifest(board)

        # Fetch the existing e2e version.yaml from GitHub
        e2e_manifest = _fetch_version_yaml_from_branch('e2e')
        if not e2e_manifest:
            return jsonify({
                'error': 'Could not fetch version.yaml from e2e branch. Run "Prepare E2E" first.',
                'drifts': [],
                'status': 'no_e2e'
            }), 404

        # Compare service-by-service
        drifts = []
        e2e_services = e2e_manifest.get('services', {})
        current_services = current_manifest.get('services', {})

        all_services = set(list(e2e_services.keys()) + list(current_services.keys()))
        for svc_name in sorted(all_services):
            e2e_svc = e2e_services.get(svc_name)
            cur_svc = current_services.get(svc_name)

            if not e2e_svc and cur_svc:
                drifts.append({
                    'service': svc_name,
                    'drift_type': 'new',
                    'message': f'New service — not in e2e branch',
                    'current_tag': cur_svc.get('image_tag', ''),
                    'e2e_tag': None,
                    'source': cur_svc.get('source', 'unknown'),
                })
            elif e2e_svc and not cur_svc:
                drifts.append({
                    'service': svc_name,
                    'drift_type': 'removed',
                    'message': f'Removed — was in e2e but no longer present',
                    'current_tag': None,
                    'e2e_tag': e2e_svc.get('image_tag', ''),
                    'source': e2e_svc.get('source', 'unknown'),
                })
            elif e2e_svc and cur_svc:
                e2e_tag = e2e_svc.get('image_tag', '')
                cur_tag = cur_svc.get('image_tag', '')
                if e2e_tag != cur_tag:
                    drifts.append({
                        'service': svc_name,
                        'drift_type': 'version_changed',
                        'message': f'Version drift: {e2e_tag} → {cur_tag}',
                        'current_tag': cur_tag,
                        'e2e_tag': e2e_tag,
                        'source': cur_svc.get('source', 'unknown'),
                    })

        has_drift = len(drifts) > 0

        # Audit trail
        board['audit_trail'].append({
            'action': 'qa_drift_check',
            'drift_count': len(drifts),
            'total_services': len(all_services),
            'by': session.get('github_user', {}).get('login', 'unknown'),
            'at': datetime.datetime.utcnow().isoformat()
        })
        _write_board(board)

        return jsonify({
            'status': 'drift' if has_drift else 'match',
            'drifts': drifts,
            'drift_count': len(drifts),
            'total_services': len(all_services),
            'e2e_generated_at': e2e_manifest.get('generated_at', ''),
        })

    except Exception as e:
        print(f'[qa] Drift check error: {e}')
        return jsonify({'error': str(e)}), 500


@app.route('/api/qa/prepare-prod', methods=['POST'])
def qa_prepare_prod():
    """Step 5: Generate version.yaml with change_ticket and push to prod + preprod branches."""
    global _qa_state

    if not is_gh_authenticated():
        return jsonify({'error': 'GitHub login required'}), 401
    if not QA_DEPLOY_REPO:
        return jsonify({'error': 'QA_DEPLOY_REPO not configured'}), 500

    data = request.json or {}
    change_ticket = (data.get('change_ticket') or '').strip()
    if not change_ticket:
        return jsonify({'error': 'Change ticket is required for production deployment'}), 400

    board = _read_board()
    if not board:
        return jsonify({'error': 'No release board found'}), 404

    _qa_state['prod_status'] = 'pushing'
    _qa_state['change_ticket'] = change_ticket

    try:
        # Build version manifest with ONLY nominated services (not prod versions)
        # Production deployment should only contain services being released
        nominated_services = {}
        for svc_name, svc_data in board.get('services', {}).items():
            nominated_services[svc_name] = {
                'image': svc_data.get('image', ''),
                'image_tag': svc_data.get('image_tag', svc_data.get('version', '')),
                'kind': svc_data.get('kind', 'Deployment'),
                'source': 'board',
                'nominated_by': svc_data.get('nominated_by', ''),
            }

        if not nominated_services:
            _qa_state['prod_status'] = 'error'
            _qa_state['error'] = 'No nominated services on the board'
            return jsonify({'error': 'No nominated services found on the release board'}), 400

        # Build the version manifest (nominated only + change ticket)
        version_manifest = {}
        version_manifest['change_ticket'] = change_ticket
        version_manifest['release_date'] = board.get('release_date', '')
        version_manifest['generated_at'] = datetime.datetime.utcnow().isoformat()
        version_manifest['generated_by'] = session.get('github_user', {}).get('login', 'unknown')
        version_manifest['services'] = nominated_services

        version_yaml = yaml.dump(version_manifest, default_flow_style=False, sort_keys=False)
        release_date = board.get('release_date', '')
        gh_user = session.get('github_user', {}).get('login', 'unknown')
        now = datetime.datetime.utcnow().isoformat()
        commit_msg = f'chore: update version.yaml for release {release_date} [{change_ticket}]'

        # Push to prod branch
        prod_result = _push_version_yaml(version_yaml, branch='prod', commit_message=commit_msg)
        if 'error' in prod_result:
            _qa_state['prod_status'] = 'error'
            _qa_state['error'] = f'prod: {prod_result["error"]}'
            return jsonify({'error': f'Failed to push to prod: {prod_result["error"]}'}), 500

        # Push to preprod branch
        preprod_result = _push_version_yaml(version_yaml, branch='preprod', commit_message=commit_msg)
        if 'error' in preprod_result:
            _qa_state['prod_status'] = 'error'
            _qa_state['error'] = f'preprod: {preprod_result["error"]}'
            return jsonify({
                'error': f'Pushed to prod but failed preprod: {preprod_result["error"]}',
                'prod_commit_url': prod_result.get('commit_url'),
            }), 500

        # Update state
        _qa_state.update({
            'prod_status': 'pushed',
            'prod_commit_url': prod_result.get('commit_url'),
            'preprod_commit_url': preprod_result.get('commit_url'),
            'prod_pushed_at': now,
            'prod_pushed_by': gh_user,
            'error': None,
        })

        # Audit trail
        board['audit_trail'].append({
            'action': 'qa_prod_prepared',
            'change_ticket': change_ticket,
            'branches': ['prod', 'preprod'],
            'services_count': len(nominated_services),
            'nominated_only': True,
            'prod_commit_url': prod_result.get('commit_url'),
            'preprod_commit_url': preprod_result.get('commit_url'),
            'by': gh_user,
            'at': now
        })
        _write_board(board)

        return jsonify({
            'status': 'pushed',
            'change_ticket': change_ticket,
            'prod_commit_url': prod_result.get('commit_url'),
            'preprod_commit_url': preprod_result.get('commit_url'),
            'services': nominated_services,
            'total': len(nominated_services)
        })

    except Exception as e:
        _qa_state['prod_status'] = 'error'
        _qa_state['error'] = str(e)
        print(f'[qa] Prepare-prod error: {e}')
        return jsonify({'error': str(e)}), 500


@app.route('/api/qa/env/services')
def qa_env_services():
    """List services currently running in the QA namespace."""
    qa_ns = request.args.get('namespace', QA_NAMESPACE)

    try:
        # Use local cluster K8s API — QA namespace is on the same cluster
        services = _list_services_from_api(
            client.ApiClient(configuration=client.Configuration()),
            qa_ns,
            '[qa-env]'
        )
        return jsonify({
            'services': services,
            'namespace': qa_ns,
            'count': len(services),
        })
    except Exception as e:
        print(f'[qa] Error listing QA env services: {e}')
        return jsonify({
            'services': [],
            'namespace': qa_ns,
            'count': 0,
            'error': str(e)
        })


@app.route('/api/qa/test/trigger', methods=['POST'])
def qa_test_trigger():
    """Trigger a QA test pipeline (placeholder — pipelines not ready yet)."""
    data = request.json or {}
    test_type = data.get('test_type', 'smoke')  # smoke | e2e | regression

    # If not authenticated or not configured, return a mock/simulated response
    if not is_gh_authenticated() or not QA_TEST_REPO:
        reason = 'GitHub login required' if not is_gh_authenticated() else 'QA_TEST_REPO not configured'
        # Return a simulated success so the UI flow can be demonstrated
        board = _read_board()
        if board:
            board['audit_trail'].append({
                'action': 'qa_test_triggered',
                'test_type': test_type,
                'namespace': QA_NAMESPACE,
                'simulated': True,
                'reason': reason,
                'by': 'local-user',
                'at': datetime.datetime.utcnow().isoformat()
            })
            _write_board(board)
        return jsonify({
            'status': 'simulated',
            'test_type': test_type,
            'message': f'{test_type.upper()} test triggered (simulated — {reason})',
            'html_url': None,
            'run_id': None,
        })

    try:
        owner, repo = QA_TEST_REPO.split('/', 1)
    except ValueError:
        return jsonify({'error': f'Invalid QA_TEST_REPO: "{QA_TEST_REPO}"'}), 500

    gh_user = session.get('github_user', {}).get('login', 'unknown')

    # Resolve the workflow file for this test type
    workflow_file = QA_TEST_WORKFLOWS.get(test_type)
    if not workflow_file:
        return jsonify({'error': f'Unknown test type: {test_type}. Valid: smoke, e2e, regression'}), 400

    try:
        response = _github_post(
            f'/repos/{owner}/{repo}/actions/workflows/{workflow_file}/dispatches',
            {
                'ref': 'main',
                'inputs': {
                    'test_type': test_type,
                    'environment': QA_NAMESPACE,
                }
            }
        )

        if response.status_code == 204:
            # Wait briefly and fetch run
            time.sleep(2)
            run_info = {'run_id': None, 'html_url': None}
            try:
                runs_data = _github_get(
                    f'/repos/{owner}/{repo}/actions/workflows/{workflow_file}/runs',
                    {'per_page': 1, 'event': 'workflow_dispatch'}
                )
                if runs_data.get('workflow_runs'):
                    latest = runs_data['workflow_runs'][0]
                    run_info = {
                        'run_id': latest['id'],
                        'html_url': latest.get('html_url', ''),
                    }
            except Exception as e:
                print(f'[qa-test] Could not fetch run ID: {e}')

            # Audit trail
            board = _read_board()
            if board:
                board['audit_trail'].append({
                    'action': 'qa_test_triggered',
                    'test_type': test_type,
                    'namespace': QA_NAMESPACE,
                    'run_id': run_info.get('run_id'),
                    'by': gh_user,
                    'at': datetime.datetime.utcnow().isoformat()
                })
                _write_board(board)

            return jsonify({
                'status': 'triggered',
                'test_type': test_type,
                'run_id': run_info.get('run_id'),
                'html_url': run_info.get('html_url'),
            })
        else:
            return jsonify({
                'error': f'GitHub returned {response.status_code}: {response.text[:200]}'
            }), response.status_code

    except Exception as e:
        return jsonify({'error': str(e)}), 500


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
            v1 = client.CoreV1Api(api_client=_local_api_client)
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
    """Compare nominated versions against UAT live cluster versions."""
    try:
        board = _read_board()
        if not board or not board.get('services'):
            return "No nominations to check for drift."
        drift_items = []
        namespace = NAMESPACE

        # ── Get the correct K8s API client for the UAT cluster ──
        if DEPLOY_ENV == 'prod':
            uat_ns = os.environ.get('UAT_NAMESPACE', namespace)
            api_client_obj, error = _get_uat_api_client()
            if error:
                return f'Cannot connect to UAT cluster for drift check: {error}'
            apps_v1 = client.AppsV1Api(api_client_obj)
            namespace = uat_ns
            env_label = f'UAT (remote), ns={namespace}'
        else:
            apps_v1 = client.AppsV1Api(api_client=_local_api_client)
            env_label = f'UAT (local), ns={namespace}'

        for svc_name, svc_data in board['services'].items():
            nominated_tag = svc_data.get('image_tag', '')
            kind = svc_data.get('kind', 'Deployment')
            live_tag = ''
            try:
                if kind == 'Deployment':
                    d = _k8s_retry(apps_v1.read_namespaced_deployment, svc_name, namespace)
                    containers = d.spec.template.spec.containers or []
                    live_tag = _extract_image_tag(containers[0].image) if containers else '?'
                elif kind == 'StatefulSet':
                    s = _k8s_retry(apps_v1.read_namespaced_stateful_set, svc_name, namespace)
                    containers = s.spec.template.spec.containers or []
                    live_tag = _extract_image_tag(containers[0].image) if containers else '?'
            except Exception:
                live_tag = 'unknown'
            status = '✅ match' if live_tag == nominated_tag else '⚠️ DRIFT'
            drift_items.append(f"{svc_name} | nominated: {nominated_tag} | live: {live_tag} | {status}")

        header = f"Drift check against {env_label}\n\nService | Nominated | Live (UAT) | Status\n--------|-----------|------------|-------\n"
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
        apps_v1 = client.AppsV1Api(api_client=_local_api_client)
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
        'QA_DEPLOY_REPO': QA_DEPLOY_REPO,
        'QA_NAMESPACE': QA_NAMESPACE,
        'QA_TEST_REPO': QA_TEST_REPO,
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
