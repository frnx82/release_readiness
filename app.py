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
# Jira MCP Server Configuration
# ══════════════════════════════════════════════════════════════════════════════
JIRA_MCP_URL          = os.getenv('JIRA_MCP_URL', '')            # MCP server endpoint URL
JIRA_SERVICE_ACCOUNT  = os.getenv('JIRA_SERVICE_ACCOUNT', '')    # Jira service account username
JIRA_PAT_TOKEN        = os.getenv('JIRA_PAT_TOKEN', '')          # Jira PAT token

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

# Print Jira MCP config status at startup
if JIRA_MCP_URL:
    print(f"[Release Readiness] ✅ Jira MCP server configured — URL: {JIRA_MCP_URL}")
    if JIRA_SERVICE_ACCOUNT:
        print(f"    Service account: {JIRA_SERVICE_ACCOUNT}")
    if JIRA_PAT_TOKEN:
        print(f"    PAT token: {'*' * 8}...configured")
else:
    print("[Release Readiness] ℹ️  No Jira MCP server configured. Jira integration disabled.")
    print("    Set JIRA_MCP_URL + JIRA_SERVICE_ACCOUNT + JIRA_PAT_TOKEN to enable.")
    print("    Or set GITHUB_TOKEN for PAT mode")

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
    Returns the tool result or None on failure.
    """
    if not JIRA_MCP_URL:
        return None
    try:
        headers = {
            'Content-Type': 'application/json',
            'Accept': 'application/json',
        }
        if JIRA_PAT_TOKEN:
            headers['Authorization'] = f'Bearer {JIRA_PAT_TOKEN}'
        if JIRA_SERVICE_ACCOUNT:
            headers['X-Service-Account'] = JIRA_SERVICE_ACCOUNT

        payload = {
            'jsonrpc': '2.0',
            'id': str(uuid.uuid4()),
            'method': 'tools/call',
            'params': {
                'name': tool_name,
                'arguments': arguments
            }
        }
        r = requests.post(JIRA_MCP_URL, json=payload, headers=headers,
                         timeout=timeout, verify=SSL_VERIFY)
        r.raise_for_status()
        result = r.json()

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

        # Parse the response — try JSON first, fall back to text
        try:
            data = json.loads(raw) if isinstance(raw, str) else raw
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
        except (json.JSONDecodeError, AttributeError):
            # Fall back to treating the response as a text description
            issue = {
                'id': jira_id,
                'summary': jira_id,
                'description': str(raw)[:500],
                'status': 'Unknown',
                'type': 'Task',
                'priority': 'Medium',
            }
        results[jira_id] = issue
        _cache_set(cache_key, issue)

    return results


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
RELEASE_CADENCE = os.getenv('RELEASE_CADENCE', 'friday')  # 'friday' or 'custom'
CUTOFF_DAY = int(os.getenv('CUTOFF_DAY', '2'))  # 0=Mon, 2=Wed
CUTOFF_HOUR = int(os.getenv('CUTOFF_HOUR', '17'))  # 17:00


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


def _read_board(release_date=None):
    """Read the release board from its ConfigMap. Returns dict or None."""
    try:
        v1 = client.CoreV1Api()
        cm_name = _board_configmap_name(release_date)
        cm = _k8s_retry(v1.read_namespaced_config_map, cm_name, NAMESPACE)
        data = json.loads(cm.data.get('manifest.json', '{}'))
        return data
    except client.exceptions.ApiException as e:
        if e.status == 404:
            return None
        raise
    except Exception:
        return None


def _write_board(board_data, release_date=None):
    """Write the release board to its ConfigMap. Creates if doesn't exist."""
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


def _new_board():
    """Create an empty release board template."""
    release_date = _get_current_release_date()
    return {
        'release_date': release_date,
        'cutoff': _get_cutoff_datetime(),
        'status': 'open',
        'services': {},
        'audit_trail': [],
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
                'description': parts[2].strip() if len(parts) > 2 else ''
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
    """List all deployable services from the K8s cluster with current versions."""
    namespace = request.args.get('namespace', NAMESPACE)
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
    return jsonify({'components': CUSTOM_COMPONENTS, 'count': len(CUSTOM_COMPONENTS)})


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

    # Enrich with live metadata
    board['is_past_cutoff'] = datetime.datetime.utcnow().isoformat() > board.get('cutoff', '')
    board['nominated_count'] = len(board.get('services', {}))
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

    if board.get('status') == 'locked':
        return jsonify({'error': 'Release board is locked (past cutoff). Contact Release Manager.'}), 403

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
        # K8s service — auto-fill version from live cluster
        namespace = data.get('namespace', NAMESPACE)
        image = ''
        image_tag = ''
        helm_version = None
        kind = 'Deployment'

        try:
            apps_v1 = client.AppsV1Api()
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
            'reason': notes or 'Version update'
        })

        board['audit_trail'].append({
            'action': 're-nominate',
            'service': service_name,
            'from_version': old_tag,
            'to_version': image_tag,
            'by': nominated_by,
            'at': now
        })
    else:
        board.setdefault('services', {})[service_name] = {
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
            'version_history': [{
                'from_tag': None,
                'to_tag': image_tag,
                'changed_by': nominated_by,
                'changed_at': now,
                'reason': 'Initial nomination'
            }]
        }

        board['audit_trail'].append({
            'action': 'nominate',
            'service': service_name,
            'version': image_tag,
            'by': nominated_by,
            'at': now
        })

    _write_board(board)
    return jsonify({'status': 'ok', 'service': service_name, 'image_tag': image_tag})


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


@app.route('/api/release/finalize', methods=['POST'])
def finalize_release():
    """Lock the release board (cutoff)."""
    data = request.json or {}
    finalized_by = data.get('finalized_by', 'release-manager')

    board = _read_board()
    if not board:
        return jsonify({'error': 'No active release board'}), 404

    if board.get('status') == 'released':
        return jsonify({'error': 'Already released'}), 400

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


@app.route('/api/release/complete', methods=['POST'])
def complete_release():
    """Mark the release as completed."""
    data = request.json or {}
    completed_by = data.get('completed_by', 'release-manager')

    board = _read_board()
    if not board:
        return jsonify({'error': 'No active release board'}), 404

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
    return jsonify({'status': 'released'})


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


@app.route('/api/ai/release_notes', methods=['POST'])
def generate_release_notes():
    """Generate AI-powered release notes for Teams/Jira.

    When Jira IDs are associated with nominated services, fetches ticket
    details via the Jira MCP server and includes them in the AI prompt
    for richer, change-aware release notes.
    """
    board = _read_board()
    if not board or not board.get('services'):
        return jsonify({'error': 'No nominations to generate notes for'}), 400

    # ── Collect and fetch Jira ticket details ──
    all_jira_ids = []
    svc_jira_map = {}  # service_name → [jira_ids]
    for svc_name, svc_data in board['services'].items():
        ids = _parse_jira_ids(svc_data.get('jira_ids', ''))
        if ids:
            svc_jira_map[svc_name] = ids
            all_jira_ids.extend(ids)

    jira_details = {}
    if all_jira_ids:
        jira_details = _fetch_jira_issues(list(set(all_jira_ids)))

    if not get_model():
        # Deterministic fallback
        lines = [f"## Release Notes — {board.get('release_date', 'Unknown')}\n"]
        lines.append(f"**{len(board['services'])} services updated:**\n")
        lines.append("| Service | Version | Jira Tickets | Notes |")
        lines.append("|---|---|---|---|")
        for svc_name, svc_data in board['services'].items():
            jira_col = svc_data.get('jira_ids', '') or '—'
            lines.append(f"| {svc_name} | {svc_data.get('image_tag', '?')} | {jira_col} | {svc_data.get('notes', '')} |")

        # If we have Jira details, append a changes summary
        if jira_details:
            lines.append(f"\n## Changes Detail\n")
            for svc_name, ids in svc_jira_map.items():
                lines.append(f"\n### {svc_name}")
                for jid in ids:
                    issue = jira_details.get(jid, {})
                    if issue:
                        lines.append(f"- **{jid}** ({issue.get('type', 'Task')}): "
                                    f"{issue.get('summary', 'No summary')} [{issue.get('status', '?')}]")
                    else:
                        lines.append(f"- **{jid}**: (details unavailable)")

        return jsonify({'notes': '\n'.join(lines), 'gemini_powered': False,
                       'jira_enriched': bool(jira_details)})

    # ── Build AI prompt with Jira context ──
    service_list = []
    for svc_name, svc_data in board['services'].items():
        svc_line = (f"- {svc_name}: tag={svc_data.get('image_tag','?')}, "
                    f"helm={svc_data.get('helm_version','N/A')}, "
                    f"readiness={svc_data.get('readiness','?')}, "
                    f"notes=\"{svc_data.get('notes','')}\"")

        # Append Jira ticket details for this service
        svc_ids = svc_jira_map.get(svc_name, [])
        if svc_ids:
            svc_line += f", jira_tickets=[{', '.join(svc_ids)}]"
            for jid in svc_ids:
                issue = jira_details.get(jid, {})
                if issue:
                    desc = (issue.get('description', '') or '')[:300]
                    svc_line += (f"\n    JIRA {jid}: type={issue.get('type','Task')}, "
                                f"status={issue.get('status','?')}, "
                                f"summary=\"{issue.get('summary','')}\", "
                                f"description=\"{desc}\"")
        service_list.append(svc_line)

    jira_instruction = ""
    if jira_details:
        jira_instruction = """\n\nJira tickets are associated with each service. Use the Jira ticket summaries and
descriptions to explain WHAT actually changed in each service. Organize changes by:
- 🆕 New Features
- 🐛 Bug Fixes
- ⚡ Improvements
- 🔧 Maintenance
"""

    prompt = f"""Generate professional release notes for the operations team.

Release Date: {board.get('release_date', 'Unknown')}
Status: {board.get('status', 'open')}

Nominated services:
{chr(10).join(service_list)}
{jira_instruction}
Format the output as markdown with:
1. An executive summary of the release (2-3 sentences)
2. A table with columns: Service | Version | Jira Tickets | Change Summary | Risk Level
3. {'A "What\'s Changed" section organized by change type based on the Jira tickets' if jira_details else 'A brief summary of upcoming changes based on service notes'}
4. An AI Risk Assessment summary at the end

Return ONLY the markdown text, no JSON wrapping.
"""

    try:
        response = gemini_generate_with_retry(prompt)
        notes = response.text if response else 'AI unavailable'
        return jsonify({'notes': notes, 'gemini_powered': True,
                       'jira_enriched': bool(jira_details),
                       'jira_count': len(jira_details),
                       'release_date': board.get('release_date')})
    except Exception as e:
        return jsonify({'error': str(e), 'notes': '', 'gemini_powered': False}), 500


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
    socketio.run(app, host='0.0.0.0', port=int(os.getenv('PORT', 8080)), debug=True)
