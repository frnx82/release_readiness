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
import yaml
from flask import Flask, request, jsonify, render_template
from flask_socketio import SocketIO
from kubernetes import client, config

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
            project = os.environ.get('GOOGLE_CLOUD_PROJECT') or os.environ.get('GEMINI_PROJECT_ID')
            location = os.environ.get('GOOGLE_CLOUD_LOCATION', 'us-central1')
            if project:
                _model_client = genai.Client(project=project, location=location)
            else:
                api_key = os.environ.get('GEMINI_API_KEY', '')
                if api_key:
                    _model_client = genai.Client(api_key=api_key)
            if _model_client:
                print(f"[gemini] Model client initialised (project={project}, model={GEMINI_MODEL})")
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


# ── Flask app ─────────────────────────────────────────────────────────────────
app = Flask(__name__)
app.secret_key = os.environ.get('FLASK_SECRET_KEY', 'release-readiness-default-secret')

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

    if not service_name:
        return jsonify({'error': 'service_name is required'}), 400

    board = _read_board()
    if not board:
        board = _new_board()

    if board.get('status') == 'locked':
        return jsonify({'error': 'Release board is locked (past cutoff). Contact Release Manager.'}), 403

    if board.get('status') == 'released':
        return jsonify({'error': 'This release has already been completed.'}), 403

    # Auto-fill version from live cluster
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

    now = datetime.datetime.utcnow().isoformat()

    # Check if re-nomination
    existing = board.get('services', {}).get(service_name)
    if existing:
        old_tag = existing.get('image_tag', '')
        existing['image'] = image
        existing['image_tag'] = image_tag
        existing['helm_version'] = helm_version
        existing['notes'] = notes
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
            'image': image,
            'image_tag': image_tag,
            'helm_version': helm_version,
            'nominated_by': nominated_by,
            'nominated_at': now,
            'updated_at': now,
            'updated_by': nominated_by,
            'notes': notes,
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


@app.route('/api/ai/release_notes', methods=['POST'])
def generate_release_notes():
    """Generate AI-powered release notes for Teams/Jira."""
    board = _read_board()
    if not board or not board.get('services'):
        return jsonify({'error': 'No nominations to generate notes for'}), 400

    if not get_model():
        # Deterministic fallback
        lines = [f"## Release Notes — {board.get('release_date', 'Unknown')}\n"]
        lines.append(f"**{len(board['services'])} services updated:**\n")
        lines.append("| Service | Version | Notes |")
        lines.append("|---|---|---|")
        for svc_name, svc_data in board['services'].items():
            lines.append(f"| {svc_name} | {svc_data.get('image_tag', '?')} | {svc_data.get('notes', '')} |")
        return jsonify({'notes': '\n'.join(lines), 'gemini_powered': False})

    service_list = []
    for svc_name, svc_data in board['services'].items():
        service_list.append(f"- {svc_name}: tag={svc_data.get('image_tag','?')}, "
                           f"helm={svc_data.get('helm_version','N/A')}, "
                           f"readiness={svc_data.get('readiness','?')}, "
                           f"notes=\"{svc_data.get('notes','')}\"")

    prompt = f"""Generate professional release notes for the operations team.

Release Date: {board.get('release_date', 'Unknown')}
Status: {board.get('status', 'open')}

Nominated services:
{chr(10).join(service_list)}

Format as a markdown table with columns: Service, Version, Change Type (Patch/Minor/Major based on version), Notes.
Include an AI Risk Assessment summary at the end.
Return ONLY the markdown text, no JSON wrapping.
"""

    try:
        response = gemini_generate_with_retry(prompt)
        notes = response.text if response else 'AI unavailable'
        return jsonify({'notes': notes, 'gemini_powered': True,
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


# ██████████████████████████████████████████████████████████████████████████████
if __name__ == '__main__':
    socketio.run(app, host='0.0.0.0', port=int(os.getenv('PORT', 8080)), debug=True)
