"""
Release Readiness Dashboard — MOCK MODE
========================================
Runs locally without K8s or Gemini. Uses in-memory storage and fake data.
"""
import os, json, re, time, datetime, threading, yaml, uuid, random
from flask import Flask, request, jsonify, render_template, session, redirect
from flask_socketio import SocketIO

# ── Jira ID validation pattern ────────────────────────────────────────────────
_JIRA_ID_PATTERN = re.compile(r'^[A-Z][A-Z0-9]+-\d+$')

def _parse_jira_ids(jira_ids_str):
    """Parse a comma-separated Jira IDs string into a list of clean IDs."""
    if not jira_ids_str:
        return []
    raw = [j.strip().upper() for j in jira_ids_str.split(',')]
    return [j for j in raw if j and _JIRA_ID_PATTERN.match(j)]

app = Flask(__name__)
app.secret_key = 'mock-secret-key-for-sessions'
socketio = SocketIO(app, cors_allowed_origins="*", async_mode='threading')

# ── Fake services (simulates a K8s cluster with 15 microservices) ─────────────
MOCK_SERVICES = [
    {"name":"billing-service","kind":"Deployment","image":"registry.example.com/billing:v2.3.3","image_tag":"v2.3.3","helm_version":"billing-chart-0.5.0","replicas":3,"desired_replicas":3,"available":True},
    {"name":"payment-gateway","kind":"Deployment","image":"registry.example.com/payment:v2.0.0","image_tag":"v2.0.0","helm_version":"payment-chart-0.4.0","replicas":2,"desired_replicas":2,"available":True},
    {"name":"user-service","kind":"Deployment","image":"registry.example.com/user:v3.2.0","image_tag":"v3.2.0","helm_version":"user-chart-1.1.0","replicas":3,"desired_replicas":3,"available":True},
    {"name":"order-service","kind":"Deployment","image":"registry.example.com/order:v1.5.2","image_tag":"v1.5.2","helm_version":"order-chart-0.3.1","replicas":2,"desired_replicas":2,"available":True},
    {"name":"notification-svc","kind":"Deployment","image":"registry.example.com/notify:v1.8.0","image_tag":"v1.8.0","helm_version":"notify-chart-0.2.0","replicas":1,"desired_replicas":1,"available":True},
    {"name":"inventory-service","kind":"Deployment","image":"registry.example.com/inventory:v4.1.1","image_tag":"v4.1.1","helm_version":"inventory-chart-1.0.0","replicas":2,"desired_replicas":2,"available":True},
    {"name":"auth-service","kind":"Deployment","image":"registry.example.com/auth:v2.7.0","image_tag":"v2.7.0","helm_version":"auth-chart-0.8.0","replicas":2,"desired_replicas":2,"available":True},
    {"name":"search-service","kind":"Deployment","image":"registry.example.com/search:v1.3.5","image_tag":"v1.3.5","helm_version":"search-chart-0.1.2","replicas":1,"desired_replicas":1,"available":True},
    {"name":"report-engine","kind":"Deployment","image":"registry.example.com/report:v3.0.1","image_tag":"v3.0.1","helm_version":"report-chart-0.6.0","replicas":1,"desired_replicas":1,"available":True},
    {"name":"config-service","kind":"Deployment","image":"registry.example.com/config:v1.0.4","image_tag":"v1.0.4","helm_version":"config-chart-0.1.0","replicas":1,"desired_replicas":1,"available":True},
    {"name":"gateway-api","kind":"Deployment","image":"registry.example.com/gateway:v5.2.0","image_tag":"v5.2.0","helm_version":"gateway-chart-2.0.0","replicas":3,"desired_replicas":3,"available":True},
    {"name":"cache-service","kind":"StatefulSet","image":"registry.example.com/cache:v2.1.0","image_tag":"v2.1.0","helm_version":"cache-chart-0.4.0","replicas":3,"desired_replicas":3,"available":True},
    {"name":"message-broker","kind":"StatefulSet","image":"registry.example.com/broker:v1.9.2","image_tag":"v1.9.2","helm_version":"broker-chart-0.5.0","replicas":3,"desired_replicas":3,"available":True},
    {"name":"scheduler-service","kind":"Deployment","image":"registry.example.com/scheduler:v1.2.0","image_tag":"v1.2.0","helm_version":"scheduler-chart-0.2.0","replicas":1,"desired_replicas":1,"available":True},
    {"name":"analytics-engine","kind":"Deployment","image":"registry.example.com/analytics:latest","image_tag":"latest","helm_version":None,"replicas":2,"desired_replicas":2,"available":True},
]

MOCK_SERVICE_MAP = {s['name']: s for s in MOCK_SERVICES}

# ── Custom components (non-K8s: Spark/PySpark on Linux servers) ───────────────
MOCK_CUSTOM_COMPONENTS = [
    {"name": "ingestion-pipeline",       "type": "Spark",   "description": "Main data ingestion from source systems",  "artifactory_path": "libs-release/com/company/ingestion-pipeline"},
    {"name": "etl-transformer",          "type": "PySpark", "description": "Data transformation and enrichment",       "artifactory_path": "libs-release/com/company/etl-transformer"},
    {"name": "data-validator",           "type": "PySpark", "description": "Data quality validation rules",            "artifactory_path": "libs-release/com/company/data-validator"},
    {"name": "report-aggregator",        "type": "Spark",   "description": "Aggregation jobs for reporting",           "artifactory_path": "libs-release/com/company/report-aggregator"},
    {"name": "event-stream-processor",   "type": "Spark",   "description": "Real-time event stream processing",       "artifactory_path": "libs-release/com/company/event-stream-processor"},
    {"name": "batch-reconciler",         "type": "PySpark", "description": "Batch reconciliation between systems",     "artifactory_path": "libs-release/com/company/batch-reconciler"},
    {"name": "data-archiver",            "type": "PySpark", "description": "Historical data archival jobs",            "artifactory_path": "libs-release/com/company/data-archiver"},
    {"name": "ml-feature-pipeline",      "type": "PySpark", "description": "ML feature extraction pipeline",          "artifactory_path": "libs-release/com/company/ml-feature-pipeline"},
    {"name": "audit-log-processor",      "type": "Spark",   "description": "Audit log processing and indexing",       "artifactory_path": "libs-release/com/company/audit-log-processor"},
    {"name": "schema-migration-runner",  "type": "PySpark", "description": "Database schema migration runner",        "artifactory_path": "libs-release/com/company/schema-migration-runner"},
]

MOCK_CUSTOM_MAP = {c['name']: c for c in MOCK_CUSTOM_COMPONENTS}

# ── Mock Jira data (simulates MCP server responses) ───────────────────────────
MOCK_JIRA_ISSUES = {
    'BILL-101': {'id': 'BILL-101', 'summary': 'Fix decimal rounding in invoice calculations', 'description': 'Invoice totals were showing incorrect amounts due to floating point rounding. Fixed by switching to Decimal type for all monetary calculations.', 'status': 'Done', 'type': 'Bug', 'priority': 'High'},
    'BILL-102': {'id': 'BILL-102', 'summary': 'Add multi-currency support for EU markets', 'description': 'Implement EUR, GBP, and CHF currency support alongside USD. Includes exchange rate API integration and currency conversion in billing pipeline.', 'status': 'Done', 'type': 'Story', 'priority': 'High'},
    'BILL-103': {'id': 'BILL-103', 'summary': 'Upgrade billing API rate limiting', 'description': 'Increase rate limits for enterprise tier from 100/min to 500/min. Add burst capacity handling.', 'status': 'In Review', 'type': 'Improvement', 'priority': 'Medium'},
    'PAY-201': {'id': 'PAY-201', 'summary': 'Integrate Stripe Connect for marketplace payouts', 'description': 'Add Stripe Connect integration to enable direct payouts to marketplace sellers. Includes onboarding flow and payout scheduling.', 'status': 'Done', 'type': 'Story', 'priority': 'High'},
    'PAY-202': {'id': 'PAY-202', 'summary': 'Fix payment retry logic for declined cards', 'description': 'Payments were not retrying after soft declines. Implemented exponential backoff retry with configurable max attempts.', 'status': 'Done', 'type': 'Bug', 'priority': 'Critical'},
    'AUTH-301': {'id': 'AUTH-301', 'summary': 'Implement OIDC token refresh flow', 'description': 'Add automatic token refresh for OIDC sessions. Tokens are refreshed 5 minutes before expiry to prevent session interruptions.', 'status': 'Done', 'type': 'Story', 'priority': 'High'},
    'AUTH-302': {'id': 'AUTH-302', 'summary': 'Add MFA enforcement for admin accounts', 'description': 'Enforce multi-factor authentication for all admin-level accounts. Support TOTP and WebAuthn.', 'status': 'Done', 'type': 'Story', 'priority': 'Critical'},
    'ORD-401': {'id': 'ORD-401', 'summary': 'Fix order status webhook delivery failures', 'description': 'Webhook deliveries were failing silently when partner endpoints returned 503. Added retry queue and dead letter handling.', 'status': 'Done', 'type': 'Bug', 'priority': 'High'},
    'ORD-402': {'id': 'ORD-402', 'summary': 'Add bulk order import API endpoint', 'description': 'New REST endpoint for importing orders in bulk via CSV/JSON. Supports up to 10,000 orders per batch with async processing.', 'status': 'In Progress', 'type': 'Story', 'priority': 'Medium'},
    'INV-501': {'id': 'INV-501', 'summary': 'Optimize inventory sync for large catalogs', 'description': 'Inventory sync was timing out for catalogs with >100k SKUs. Implemented delta sync and parallel processing.', 'status': 'Done', 'type': 'Improvement', 'priority': 'High'},
    'SRCH-601': {'id': 'SRCH-601', 'summary': 'Upgrade Elasticsearch to v8.x', 'description': 'Migrate search index to Elasticsearch 8.x. Includes reindexing pipeline, query compatibility updates, and performance benchmarks.', 'status': 'Done', 'type': 'Task', 'priority': 'Medium'},
    'DEVOPS-701': {'id': 'DEVOPS-701', 'summary': 'Add liveness/readiness probe tuning', 'description': 'Fine-tune K8s probe thresholds based on historical pod startup times. Reduces false-positive restarts by 60%.', 'status': 'Done', 'type': 'Improvement', 'priority': 'Medium'},
    'DEVOPS-702': {'id': 'DEVOPS-702', 'summary': 'Implement graceful shutdown handlers', 'description': 'Add SIGTERM handlers to drain in-flight requests before pod termination. Prevents 502 errors during rolling deployments.', 'status': 'Done', 'type': 'Improvement', 'priority': 'High'},
    'DATA-801': {'id': 'DATA-801', 'summary': 'Fix Spark job OOM on large partitions', 'description': 'Spark ingestion jobs were running out of memory on partitions >2GB. Implemented adaptive partition splitting and memory-aware scheduling.', 'status': 'Done', 'type': 'Bug', 'priority': 'Critical'},
    'DATA-802': {'id': 'DATA-802', 'summary': 'Add data quality validation rules for PII fields', 'description': 'Implement automated PII detection and masking validation in the ETL pipeline. Covers email, phone, SSN patterns.', 'status': 'Done', 'type': 'Story', 'priority': 'High'},
}

# ── In-memory board storage (replaces ConfigMap) ─────────────────────────────
_board_store = {}
_release_history = []  # archived boards from previous release cycles

def _get_release_date():
    today = datetime.date.today()
    days = (4 - today.weekday()) % 7
    if days == 0 and datetime.datetime.now().hour >= 18:
        days = 7
    return (today + datetime.timedelta(days=days)).isoformat()

def _get_cutoff():
    cutoff_day = int(os.environ.get('CUTOFF_DAY', '2'))  # 0=Mon, 2=Wed
    cutoff_hour = int(os.environ.get('CUTOFF_HOUR', '12'))  # 12:00 (noon)
    today = datetime.date.today()
    days = (4 - today.weekday()) % 7
    if days == 0 and datetime.datetime.now().hour >= 18:
        days = 7
    friday = today + datetime.timedelta(days=days)
    cutoff = friday - datetime.timedelta(days=(4 - cutoff_day) % 7)
    return datetime.datetime.combine(cutoff, datetime.time(cutoff_hour, 0)).isoformat()

def _generate_fix_version(release_date_str):
    """Generate Jira fix version from release date.
    Format: P<YY>.<MM>.<DD> e.g. P26.05.09"""
    try:
        d = datetime.date.fromisoformat(release_date_str)
        return f"P{d.strftime('%y.%m.%d')}"
    except (ValueError, TypeError):
        return ''

def _new_board():
    rd = _get_release_date()
    return {
        'release_date': rd,
        'cutoff': _get_cutoff(),
        'fix_version': _generate_fix_version(rd),
        'status': 'open',
        'services': {},
        'audit_trail': [],
        'exception_nominations': [],
        'created_at': datetime.datetime.utcnow().isoformat(),
        'finalized_by': None, 'finalized_at': None
    }

def _read_board():
    key = _get_release_date()
    return _board_store.get(key)

def _write_board(board):
    key = board.get('release_date', _get_release_date())
    _board_store[key] = board


# ── Mock AI responses ─────────────────────────────────────────────────────────
def _mock_readiness(services):
    import random
    results = {}
    for name, data in services.items():
        tag = data.get('image_tag', '')
        score = random.randint(60, 100)
        if tag == 'latest':
            score = max(30, score - 40)
        readiness = 'green' if score >= 80 else 'yellow' if score >= 55 else 'red'
        checks = {
            'health': 'pass' if random.random() > 0.15 else 'warning',
            'stability': 'pass' if random.random() > 0.1 else 'warning',
            'probes': 'pass' if tag != 'latest' else 'warning',
            'resources': 'pass' if random.random() > 0.2 else 'warning',
            'image_tag': 'pass' if tag != 'latest' and ':' in (data.get('image','')) else 'fail'
        }
        risks = []
        if tag == 'latest':
            risks.append('Using :latest tag — unpinned supply chain risk')
        if checks['stability'] == 'warning':
            risks.append(f'{random.randint(2,8)} restarts in last 48h')
        if checks['resources'] == 'warning':
            risks.append('No resource limits configured')
        results[name] = {
            'readiness': readiness, 'score': score,
            'summary': f'{name} is {"healthy" if readiness == "green" else "showing concerns" if readiness == "yellow" else "at risk"}.',
            'checks': checks, 'risks': risks
        }
    overall_scores = [r['score'] for r in results.values()]
    avg = sum(overall_scores) / len(overall_scores) if overall_scores else 0
    overall = 'green' if avg >= 80 else 'yellow' if avg >= 55 else 'red'
    return {
        'overall': overall,
        'summary': f'{len(results)} services analyzed. Average readiness score: {avg:.0f}/100.',
        'services': results, 'gemini_powered': False
    }


# ══════════════════════════════════════════════════════════════════════════════
# Routes
# ══════════════════════════════════════════════════════════════════════════════

@app.route('/')
def index():
    return render_template('index.html')

@app.route('/api/ping')
def ping():
    return 'ok', 200

@app.route('/api/auth/status')
def auth_status():
    return jsonify({'authenticated': True, 'ts': datetime.datetime.utcnow().isoformat()})

@app.route('/api/services')
def list_services():
    return jsonify({'services': MOCK_SERVICES, 'namespace': 'mock-namespace', 'count': len(MOCK_SERVICES)})

@app.route('/api/prod/services')
def list_prod_services():
    """Mock: List production services (simulates a remote OpenShift cluster).
    In production, this connects to PROD_CLUSTER_API using PROD_CLUSTER_TOKEN."""
    prod_api = os.environ.get('PROD_CLUSTER_API', '')
    prod_ns = os.environ.get('PROD_NAMESPACE', 'prod-namespace')
    # Simulate prod services — same set but some with older versions to show drift
    prod_services = []
    for svc in MOCK_SERVICES:
        prod_svc = dict(svc)
        # Simulate some prod services lagging behind UAT
        tag = svc.get('image_tag', '')
        if svc['name'] in ('billing-service', 'notification-svc', 'auth-service'):
            # Older version in prod
            parts = tag.replace('v', '').split('.')
            if len(parts) >= 3:
                parts[-1] = str(max(0, int(parts[-1]) - 1))
                prod_svc['image_tag'] = 'v' + '.'.join(parts)
                prod_svc['image'] = svc['image'].rsplit(':', 1)[0] + ':' + prod_svc['image_tag']
        prod_services.append(prod_svc)
    return jsonify({
        'services': prod_services,
        'namespace': prod_ns,
        'cluster': prod_api or 'mock-prod-cluster',
        'count': len(prod_services),
        'connected': True
    })

@app.route('/api/custom_components')
def list_custom_components():
    return jsonify({'components': MOCK_CUSTOM_COMPONENTS, 'count': len(MOCK_CUSTOM_COMPONENTS)})

@app.route('/api/release/current')
def get_current():
    board = _read_board()
    if not board:
        board = _new_board()
        _write_board(board)

    # Auto-archival: if board is released and we're past the release date (11:59 PM),
    # automatically start a new cycle so the dashboard is fresh on Monday morning.
    if board.get('status') == 'released' and board.get('release_date'):
        try:
            release_date = datetime.date.fromisoformat(board['release_date'])
            archive_threshold = datetime.datetime.combine(
                release_date + datetime.timedelta(days=1),
                datetime.time(0, 0)
            )
            if datetime.datetime.utcnow() >= archive_threshold:
                print(f"[mock] Auto-archiving released board for {board['release_date']}, starting new cycle")
                board['audit_trail'].append({
                    'action': 'auto_archive', 'by': 'system',
                    'at': datetime.datetime.utcnow().isoformat(),
                    'note': f"Board auto-archived after release date {board['release_date']}"
                })
                _write_board(board)
                board = _new_board()
                _write_board(board)
        except (ValueError, TypeError) as e:
            print(f"[mock] Auto-archive date parse error: {e}")

    board['is_past_cutoff'] = datetime.datetime.utcnow().isoformat() > board.get('cutoff', '')
    board['nominated_count'] = len(board.get('services', {}))
    board['exception_count'] = len(board.get('exception_nominations', []))

    # Auto-reflect locked state in UI when past cutoff
    if board['is_past_cutoff'] and board.get('status') == 'open':
        board['status'] = 'locked'
        board['auto_locked'] = True

    return jsonify(board)

@app.route('/api/release/nominate', methods=['POST'])
def nominate():
    data = request.json or {}
    name = data.get('service_name', '').strip()
    notes = data.get('notes', '').strip()
    by = data.get('nominated_by', 'anonymous')
    is_custom = data.get('is_custom', False)
    manual_version = data.get('manual_version', '').strip()
    jira_ids = data.get('jira_ids', '').strip()
    if not name:
        return jsonify({'error': 'service_name required'}), 400
    board = _read_board()
    if not board:
        board = _new_board()

    is_exception = data.get('is_exception', False)
    exception_reason = data.get('exception_reason', '').strip()
    exception_approver = data.get('exception_approver', '').strip()

    # Determine if board is effectively locked:
    # 1. Manually locked by Release Manager (status == 'locked'), OR
    # 2. Past the cutoff time (even if nobody clicked Lock Board yet)
    is_past_cutoff = datetime.datetime.utcnow().isoformat() > board.get('cutoff', '')
    board_is_locked = board.get('status') in ('locked',) or is_past_cutoff

    if board.get('status') == 'released':
        return jsonify({'error': 'This release has already been completed.'}), 403

    if board_is_locked and not is_exception:
        return jsonify({'error': 'Release board is locked (past cutoff). Use exception nomination.', 'is_locked': True, 'cutoff': board.get('cutoff')}), 403

    if board_is_locked and is_exception:
        if not exception_reason or not exception_approver:
            return jsonify({'error': 'Exception nominations require a reason and approver name.'}), 400

    now = datetime.datetime.utcnow().isoformat()

    if is_custom:
        comp = MOCK_CUSTOM_MAP.get(name, {})
        tag = manual_version or 'unknown'
        image = ''
        helm = None
        kind = comp.get('type', 'Custom')
    else:
        svc = MOCK_SERVICE_MAP.get(name, {})
        image = svc.get('image', f'registry.example.com/{name}:unknown')
        tag = svc.get('image_tag', 'unknown')
        helm = svc.get('helm_version')
        kind = svc.get('kind', 'Deployment')

    existing = board['services'].get(name)
    action_prefix = 'exception-' if is_exception else ''
    if existing:
        old_tag = existing.get('image_tag', '')
        existing.update({'image': image, 'image_tag': tag, 'helm_version': helm,
                         'notes': notes, 'jira_ids': jira_ids or existing.get('jira_ids', ''),
                         'updated_at': now, 'updated_by': by})
        if is_exception:
            existing['is_exception'] = True
            existing['exception_reason'] = exception_reason
            existing['exception_approver'] = exception_approver
        existing['version_history'].append({'from_tag': old_tag, 'to_tag': tag,
                                            'changed_by': by, 'changed_at': now, 'reason': notes or 'Update'})
        board['audit_trail'].append({'action': f'{action_prefix}re-nominate', 'service': name,
                                      'from_version': old_tag, 'to_version': tag, 'by': by, 'at': now})
    else:
        svc_entry = {
            'name': name, 'kind': kind, 'is_custom': is_custom,
            'image': image, 'image_tag': tag, 'helm_version': helm,
            'nominated_by': by, 'nominated_at': now, 'updated_at': now, 'updated_by': by,
            'notes': notes, 'jira_ids': jira_ids, 'readiness': None, 'readiness_details': None,
            'version_history': [{'from_tag': None, 'to_tag': tag, 'changed_by': by,
                                 'changed_at': now, 'reason': 'Initial nomination'}]
        }
        if is_exception:
            svc_entry['is_exception'] = True
            svc_entry['exception_reason'] = exception_reason
            svc_entry['exception_approver'] = exception_approver
        board['services'][name] = svc_entry
        board['audit_trail'].append({'action': f'{action_prefix}nominate', 'service': name,
                                      'version': tag, 'by': by, 'at': now})

    if is_exception:
        if 'exception_nominations' not in board:
            board['exception_nominations'] = []
        board['exception_nominations'].append({
            'service': name, 'version': tag, 'reason': exception_reason,
            'approver': exception_approver, 'requested_by': by, 'at': now
        })

    _write_board(board)
    return jsonify({'status': 'ok', 'service': name, 'image_tag': tag, 'is_exception': is_exception})

@app.route('/api/release/rollback', methods=['POST'])
def rollback_version():
    """Rollback a nominated service to a previously nominated version."""
    data = request.json or {}
    name = data.get('service_name', '').strip()
    target_tag = data.get('target_tag', '').strip()
    by = data.get('rolled_back_by', 'anonymous')
    if not name or not target_tag:
        return jsonify({'error': 'service_name and target_tag required'}), 400
    board = _read_board()
    if not board or name not in board.get('services', {}):
        return jsonify({'error': 'Service not found on board'}), 404
    if board.get('status') in ('locked', 'released'):
        return jsonify({'error': 'Board is locked'}), 403

    svc = board['services'][name]
    old_tag = svc.get('image_tag', '')
    if old_tag == target_tag:
        return jsonify({'status': 'ok', 'message': 'Already at that version'}), 200

    now = datetime.datetime.utcnow().isoformat()

    # Update the image tag (and image path if it's a K8s service)
    if not svc.get('is_custom'):
        # Reconstruct image path with the target tag
        base_image = svc.get('image', '').rsplit(':', 1)[0] if ':' in svc.get('image', '') else svc.get('image', '')
        svc['image'] = f'{base_image}:{target_tag}'
    svc['image_tag'] = target_tag
    svc['updated_at'] = now
    svc['updated_by'] = by

    svc['version_history'].append({
        'from_tag': old_tag, 'to_tag': target_tag,
        'changed_by': by, 'changed_at': now, 'reason': f'Rollback from {old_tag}'
    })
    board['audit_trail'].append({
        'action': 'rollback', 'service': name,
        'from_version': old_tag, 'to_version': target_tag, 'by': by, 'at': now
    })
    _write_board(board)
    return jsonify({'status': 'ok', 'service': name, 'image_tag': target_tag})

@app.route('/api/release/remove', methods=['DELETE'])
def remove():
    data = request.json or {}
    name = data.get('service_name', '')
    by = data.get('removed_by', 'anonymous')
    board = _read_board()
    if not board or name not in board.get('services', {}):
        return jsonify({'error': 'Not found'}), 404
    if board.get('status') in ('locked', 'released'):
        return jsonify({'error': 'Board locked'}), 403
    del board['services'][name]
    board['audit_trail'].append({'action': 'remove', 'service': name, 'by': by,
                                  'at': datetime.datetime.utcnow().isoformat()})
    _write_board(board)
    return jsonify({'status': 'ok', 'removed': name})

@app.route('/api/release/finalize', methods=['POST'])
def finalize():
    data = request.json or {}
    by = data.get('finalized_by', 'release-manager')
    board = _read_board()
    if not board:
        return jsonify({'error': 'No board'}), 404
    if board.get('status') == 'released':
        return jsonify({'error': 'Already released. Cannot lock a completed release.', 'board_status': 'released'}), 400
    if board.get('status') == 'locked':
        return jsonify({'error': 'Board is already locked.', 'board_status': 'locked'}), 400
    now = datetime.datetime.utcnow().isoformat()
    board['status'] = 'locked'
    board['finalized_by'] = by
    board['finalized_at'] = now
    board['audit_trail'].append({'action': 'finalize', 'by': by, 'at': now})
    _write_board(board)
    return jsonify({'status': 'locked'})


@app.route('/api/release/unlock', methods=['POST'])
def unlock():
    """Unlock a locked board so QA/RM can edit nominations."""
    data = request.json or {}
    by = data.get('unlocked_by', 'release-manager')
    board = _read_board()
    if not board:
        return jsonify({'error': 'No board'}), 404
    if board.get('status') == 'released':
        return jsonify({'error': 'Cannot unlock a completed release.', 'board_status': 'released'}), 400
    if board.get('status') != 'locked':
        return jsonify({'error': 'Board is not locked.', 'board_status': board.get('status')}), 400
    now = datetime.datetime.utcnow().isoformat()
    board['status'] = 'open'
    board['audit_trail'].append({'action': 'unlock', 'by': by, 'at': now, 'note': 'Board unlocked for editing'})
    _write_board(board)
    return jsonify({'status': 'open', 'unlocked_by': by})


@app.route('/api/release/complete', methods=['POST'])
def complete():
    data = request.json or {}
    by = data.get('completed_by', 'release-manager')
    board = _read_board()
    if not board:
        return jsonify({'error': 'No board'}), 404
    if board.get('status') == 'released':
        return jsonify({'error': 'Already released.', 'board_status': 'released'}), 400
    now = datetime.datetime.utcnow().isoformat()
    board['status'] = 'released'
    board['released_at'] = now
    board['released_by'] = by
    board['audit_trail'].append({'action': 'release', 'by': by, 'at': now})
    _write_board(board)

    # Archive board snapshot to release history
    import copy
    snapshot = copy.deepcopy(board)
    _release_history.append(snapshot)

    return jsonify({'status': 'released'})


@app.route('/api/release/new_cycle', methods=['POST'])
def new_cycle():
    """Start a new release cycle (creates a fresh board)."""
    board = _read_board()
    if board and board.get('status') != 'released':
        return jsonify({'error': 'Current board is not released yet.', 'board_status': board.get('status')}), 400
    new_board = _new_board()
    _write_board(new_board)
    return jsonify({'status': 'ok', 'release_date': new_board['release_date'],
                    'message': f"New release cycle started for {new_board['release_date']}"})

@app.route('/api/release/history')
def release_history():
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

@app.route('/api/release/drift')
def drift():
    """Compare nominated versions against UAT live cluster versions."""
    board = _read_board()
    if not board or not board.get('services'):
        return jsonify({'drift_items': [], 'message': 'No nominations to check'})
    import random
    items = []
    for name, svc in board['services'].items():
        nom_tag = svc.get('image_tag', '')
        # Simulate some drift against UAT live versions
        if random.random() < 0.25:
            parts = nom_tag.replace('v', '').split('.')
            if len(parts) >= 3:
                parts[-1] = str(int(parts[-1]) + random.randint(1, 3))
                live_tag = 'v' + '.'.join(parts)
                status = 'drift'
            else:
                live_tag = nom_tag
                status = 'match'
        elif random.random() < 0.08:
            live_tag = nom_tag.replace('v2', 'v3').replace('v1', 'v2')
            status = 'major_drift'
        else:
            live_tag = nom_tag
            status = 'match'
        items.append({'service': name, 'nominated_tag': nom_tag, 'live_tag': live_tag,
                      'live_image': svc.get('image', '').replace(nom_tag, live_tag),
                      'drift_status': status})
    return jsonify({
        'drift_items': items,
        'cluster': 'UAT (local)',
        'namespace': 'mock-namespace',
        'deploy_env': 'uat',
    })

@app.route('/api/ai/release_readiness', methods=['POST'])
def readiness():
    board = _read_board()
    if not board or not board.get('services'):
        return jsonify({'error': 'No nominations'}), 400
    result = _mock_readiness(board['services'])
    # Write back to board
    for name, check in result.get('services', {}).items():
        if name in board['services']:
            board['services'][name]['readiness'] = check.get('readiness')
            board['services'][name]['readiness_details'] = check
    _write_board(board)
    return jsonify(result)

@app.route('/api/release/export')
def export():
    board = _read_board()
    if not board or not board.get('services'):
        return jsonify({'error': 'Nothing to export'}), 400
    fmt = request.args.get('format', 'json')
    manifest = {'release': {
        'name': f"Release {board.get('release_date', 'unknown')}",
        'fix_version': board.get('fix_version', ''),
        'cutoff': board.get('cutoff'), 'status': board.get('status'),
        'services': [{'name': n, 'image': s.get('image',''), 'image_tag': s.get('image_tag',''),
                       'helm_chart': s.get('helm_version',''), 'nominated_by': s.get('nominated_by',''),
                       'jira_ids': s.get('jira_ids',''),
                       'readiness': s.get('readiness','unknown'), 'notes': s.get('notes','')}
                      for n, s in board['services'].items()]
    }}
    if fmt == 'yaml':
        return app.response_class(yaml.dump(manifest, default_flow_style=False),
                                  mimetype='text/yaml',
                                  headers={'Content-Disposition': f'attachment; filename=release-{board["release_date"]}.yaml'})
    return jsonify(manifest)


@app.route('/api/jira/issues', methods=['POST'])
def jira_issues():
    """Mock: Fetch Jira issue details."""
    data = request.json or {}
    jira_ids_str = data.get('jira_ids', '').strip()
    jira_ids = _parse_jira_ids(jira_ids_str)
    if not jira_ids:
        return jsonify({'issues': [], 'errors': ['No valid Jira IDs provided']}), 400
    found = []
    for jid in jira_ids:
        issue = MOCK_JIRA_ISSUES.get(jid)
        if issue:
            found.append(issue)
        else:
            found.append({
                'id': jid, 'summary': f'Mock issue {jid}',
                'description': f'This is a mock description for {jid}.',
                'status': random.choice(['Done', 'In Progress', 'In Review']),
                'type': random.choice(['Story', 'Bug', 'Task', 'Improvement']),
                'priority': random.choice(['High', 'Medium', 'Low'])
            })
    return jsonify({'issues': found, 'errors': [], 'configured': True, 'fetched': len(found)})


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
    """Fetch all Jira tickets for a given fix version (mock)."""
    data = request.json or {}
    fix_version = data.get('fix_version', '').strip()
    if not fix_version:
        return jsonify({'error': 'fix_version is required'}), 400

    # In production, this would call the Jira API:
    #   JQL: fixVersion = "P26.05.09"
    # For mock, return all MOCK_JIRA_ISSUES as if they belong to this fix version
    issues = list(MOCK_JIRA_ISSUES.values())
    return jsonify({
        'fix_version': fix_version,
        'total': len(issues),
        'issues': issues
    })


# ══════════════════════════════════════════════════════════════════════════════
# Confluence Agent (Mock)
# ══════════════════════════════════════════════════════════════════════════════

MOCK_CONFLUENCE_PAGES = [
    {
        'id': '10001',
        'title': 'Billing Service — Deployment Runbook',
        'space': 'DEV',
        'url': 'https://confluence.example.com/display/DEV/Billing+Service+Deployment+Runbook',
        'excerpt': 'Step-by-step deployment runbook for the billing service. Covers pre-deployment checks, rollback procedures, database migration steps, and post-deployment verification.',
        'last_modified': (datetime.datetime.utcnow() - datetime.timedelta(days=3)).isoformat(),
        'author': 'John Smith',
        'labels': ['runbook', 'billing', 'deployment'],
        'body_html': '<h2>Billing Service Deployment Runbook</h2><h3>Pre-Deployment Checklist</h3><ul><li>✅ All PRs merged to main branch</li><li>✅ CI/CD pipeline green on main</li><li>✅ UAT sign-off obtained</li><li>✅ Database migration scripts reviewed</li></ul><h3>Deployment Steps</h3><ol><li><strong>Lock the release board</strong> — Notify #release-channel in Slack</li><li><strong>Run database migrations</strong> — Execute: <code>kubectl exec -it billing-db-0 -- /scripts/migrate.sh</code></li><li><strong>Deploy new version</strong> — Trigger deploy-uat.yml workflow with target version</li><li><strong>Verify health</strong> — Monitor <code>/actuator/health</code> endpoint for 5 minutes</li><li><strong>Run smoke tests</strong> — Execute: <code>npm run test:smoke -- --env=uat</code></li></ol><h3>Rollback Procedure</h3><p><strong>⚠️ Important:</strong> Database migrations cannot be automatically rolled back. Contact the DBA team before rolling back if migrations were applied.</p><ol><li>Execute rollback: <code>kubectl rollout undo deployment/billing-service</code></li><li>Verify pods are running: <code>kubectl get pods -l app=billing-service</code></li><li>Check logs: <code>kubectl logs -l app=billing-service --tail=100</code></li></ol>',
        'body_text': 'Billing Service Deployment Runbook. Pre-Deployment Checklist: All PRs merged to main branch, CI/CD pipeline green on main, UAT sign-off obtained, Database migration scripts reviewed. Deployment Steps: 1. Lock the release board — Notify #release-channel in Slack. 2. Run database migrations — Execute: kubectl exec -it billing-db-0 -- /scripts/migrate.sh. 3. Deploy new version — Trigger deploy-uat.yml workflow with target version. 4. Verify health — Monitor /actuator/health endpoint for 5 minutes. 5. Run smoke tests — Execute: npm run test:smoke -- --env=uat. Rollback Procedure: Important: Database migrations cannot be automatically rolled back. Contact the DBA team before rolling back if migrations were applied. 1. Execute rollback: kubectl rollout undo deployment/billing-service. 2. Verify pods are running. 3. Check logs.'
    },
    {
        'id': '10002',
        'title': 'Production Release Checklist v3.2',
        'space': 'OPS',
        'url': 'https://confluence.example.com/display/OPS/Production+Release+Checklist',
        'excerpt': 'Official production release checklist. Must be followed for every production deployment. Includes communication steps, validation gates, and sign-off requirements.',
        'last_modified': (datetime.datetime.utcnow() - datetime.timedelta(days=7)).isoformat(),
        'author': 'Jane Doe',
        'labels': ['release', 'checklist', 'production', 'process'],
        'body_html': '<h2>Production Release Checklist v3.2</h2><h3>T-2 Days (Wednesday)</h3><ul><li>☐ Version drift check — all nominated services match UAT</li><li>☐ AI readiness check — all services green or yellow</li><li>☐ Fix version set in Jira — all tickets in "Done" status</li><li>☐ Release notes generated and reviewed</li></ul><h3>T-1 Day (Thursday)</h3><ul><li>☐ Board locked by Release Manager</li><li>☐ Final UAT smoke tests passed</li><li>☐ Stakeholder sign-off email sent</li><li>☐ Change ticket created in ServiceNow</li></ul><h3>Release Day (Friday)</h3><ul><li>☐ Deploy to production environment</li><li>☐ Monitor dashboards for 30 minutes</li><li>☐ Verify customer-facing endpoints</li><li>☐ Send release announcement to #releases channel</li><li>☐ Mark board as "Released"</li></ul>',
        'body_text': 'Production Release Checklist v3.2. T-2 Days: Version drift check, AI readiness check, Fix version set in Jira, Release notes generated. T-1 Day: Board locked by Release Manager, Final UAT smoke tests, Stakeholder sign-off, Change ticket in ServiceNow. Release Day: Deploy to production, Monitor dashboards 30 min, Verify endpoints, Send announcement, Mark board Released.'
    },
    {
        'id': '10003',
        'title': 'Auth Service — Architecture & Design',
        'space': 'DEV',
        'url': 'https://confluence.example.com/display/DEV/Auth+Service+Architecture',
        'excerpt': 'Architecture overview of the authentication service. Covers OAuth2 flows, JWT token management, session handling, and integration points with downstream services.',
        'last_modified': (datetime.datetime.utcnow() - datetime.timedelta(days=14)).isoformat(),
        'author': 'Alex Chen',
        'labels': ['architecture', 'auth', 'design', 'oauth'],
        'body_html': '<h2>Auth Service Architecture</h2><h3>Overview</h3><p>The auth-service handles all authentication and authorization for the platform. Built on Spring Boot with Spring Security, it manages OAuth2 flows, JWT token issuance, and RBAC.</p><h3>Key Components</h3><ul><li><strong>OAuth2 Provider</strong> — Handles authorization code and client credentials flows</li><li><strong>JWT Engine</strong> — Issues and validates RS256 tokens (15min access, 7d refresh)</li><li><strong>Session Store</strong> — Redis-backed session management</li><li><strong>RBAC Module</strong> — Role-based access control with hierarchical permissions</li></ul><h3>Integration Points</h3><table><tr><th>Service</th><th>Protocol</th><th>Purpose</th></tr><tr><td>billing-service</td><td>gRPC</td><td>Token validation</td></tr><tr><td>payment-gateway</td><td>REST</td><td>OAuth2 scopes</td></tr><tr><td>notification-hub</td><td>Kafka</td><td>Login events</td></tr></table>',
        'body_text': 'Auth Service Architecture. Overview: handles all authentication and authorization. Built on Spring Boot with Spring Security, manages OAuth2 flows, JWT token issuance, and RBAC. Key Components: OAuth2 Provider, JWT Engine (RS256, 15min access, 7d refresh), Redis-backed Session Store, RBAC Module. Integration Points: billing-service (gRPC, token validation), payment-gateway (REST, OAuth2 scopes), notification-hub (Kafka, login events).'
    },
    {
        'id': '10004',
        'title': 'Troubleshooting Guide — Common Production Issues',
        'space': 'OPS',
        'url': 'https://confluence.example.com/display/OPS/Troubleshooting+Guide',
        'excerpt': 'Common production issues and their resolution steps. Covers pod crash loops, OOMKilled errors, connection pool exhaustion, and certificate expiry.',
        'last_modified': (datetime.datetime.utcnow() - datetime.timedelta(days=5)).isoformat(),
        'author': 'Mike Johnson',
        'labels': ['troubleshooting', 'production', 'runbook', 'ops'],
        'body_html': '<h2>Common Production Issues</h2><h3>1. Pod CrashLoopBackOff</h3><p><strong>Symptoms:</strong> Pod restarts repeatedly, service unavailable</p><p><strong>Resolution:</strong></p><ol><li>Check logs: <code>kubectl logs &lt;pod&gt; --previous</code></li><li>Check resource limits: <code>kubectl describe pod &lt;pod&gt;</code></li><li>If OOMKilled: increase memory limit in Helm values</li><li>If config error: verify ConfigMap/Secret mounts</li></ol><h3>2. Connection Pool Exhaustion</h3><p><strong>Symptoms:</strong> "Connection refused" or "Too many connections" errors</p><p><strong>Resolution:</strong></p><ol><li>Check active connections: <code>kubectl exec &lt;pod&gt; -- netstat -an | grep ESTABLISHED | wc -l</code></li><li>Scale horizontally if under high load</li><li>Adjust pool size in application config (default: 10, max recommended: 50)</li></ol><h3>3. Certificate Expiry</h3><p><strong>Symptoms:</strong> TLS handshake failures, 502 Bad Gateway</p><p><strong>Resolution:</strong></p><ol><li>Check cert: <code>kubectl get secret tls-cert -o jsonpath="{.data.tls\\.crt}" | base64 -d | openssl x509 -noout -dates</code></li><li>Renew via cert-manager: <code>kubectl delete certificate &lt;name&gt;</code> (auto-renewal)</li></ol>',
        'body_text': 'Common Production Issues. 1. Pod CrashLoopBackOff: Check logs with kubectl logs --previous, check resource limits, increase memory if OOMKilled, verify ConfigMap/Secret mounts. 2. Connection Pool Exhaustion: Check active connections, scale horizontally, adjust pool size (default 10, max 50). 3. Certificate Expiry: Check cert dates with openssl, renew via cert-manager by deleting certificate resource.'
    },
    {
        'id': '10005',
        'title': 'New Developer Onboarding Guide',
        'space': 'DEV',
        'url': 'https://confluence.example.com/display/DEV/New+Developer+Onboarding',
        'excerpt': 'Complete onboarding guide for new developers joining the team. Covers environment setup, access requests, code review process, and deployment workflow.',
        'last_modified': (datetime.datetime.utcnow() - datetime.timedelta(days=21)).isoformat(),
        'author': 'Sarah Lee',
        'labels': ['onboarding', 'developer', 'setup'],
        'body_html': '<h2>New Developer Onboarding</h2><h3>Day 1 — Access Setup</h3><ul><li>Request GitHub org access from your manager</li><li>Request Jira/Confluence access via IT ServiceDesk ticket</li><li>Install VPN client and request VPN credentials</li><li>Set up 2FA for all systems</li></ul><h3>Day 1-2 — Dev Environment</h3><ol><li>Install prerequisites: Docker, kubectl, Helm, Node.js 18+</li><li>Clone the mono-repo: <code>git clone git@github.com:org/platform.git</code></li><li>Run bootstrap: <code>make dev-setup</code></li><li>Verify: <code>make test</code> should pass all unit tests</li></ol><h3>Week 1 — Key Reading</h3><ul><li>Architecture overview (this wiki)</li><li>Code review guidelines</li><li>Release process documentation</li><li>On-call runbook</li></ul>',
        'body_text': 'New Developer Onboarding. Day 1 Access Setup: GitHub org access, Jira/Confluence access, VPN, 2FA. Day 1-2 Dev Environment: Install Docker, kubectl, Helm, Node.js 18+. Clone mono-repo. Run make dev-setup. Verify with make test. Week 1 Key Reading: Architecture overview, Code review guidelines, Release process, On-call runbook.'
    },
    {
        'id': '10006',
        'title': 'Payment Gateway — API Integration Guide',
        'space': 'DEV',
        'url': 'https://confluence.example.com/display/DEV/Payment+Gateway+Integration',
        'excerpt': 'Integration guide for consuming the payment gateway APIs. Covers authentication, endpoint reference, error handling, and webhook configuration.',
        'last_modified': (datetime.datetime.utcnow() - datetime.timedelta(days=10)).isoformat(),
        'author': 'Priya Patel',
        'labels': ['api', 'payment', 'integration', 'gateway'],
        'body_html': '<h2>Payment Gateway Integration Guide</h2><h3>Authentication</h3><p>All requests require an OAuth2 Bearer token from the auth-service. Use the <code>payment:write</code> scope for mutations and <code>payment:read</code> for queries.</p><h3>Endpoints</h3><table><tr><th>Method</th><th>Path</th><th>Description</th></tr><tr><td>POST</td><td>/api/v2/payments</td><td>Create a payment</td></tr><tr><td>GET</td><td>/api/v2/payments/{id}</td><td>Get payment status</td></tr><tr><td>POST</td><td>/api/v2/refunds</td><td>Process refund</td></tr><tr><td>GET</td><td>/api/v2/settlements</td><td>List settlements</td></tr></table><h3>Error Handling</h3><p>All errors follow RFC 7807 Problem Details format. Common codes:</p><ul><li><code>402</code> — Insufficient funds</li><li><code>409</code> — Duplicate payment (idempotency key conflict)</li><li><code>429</code> — Rate limited (100 req/min)</li></ul>',
        'body_text': 'Payment Gateway Integration Guide. Authentication: OAuth2 Bearer token, use payment:write for mutations, payment:read for queries. Endpoints: POST /api/v2/payments (create), GET /api/v2/payments/{id} (status), POST /api/v2/refunds (refund), GET /api/v2/settlements (list). Error Handling: RFC 7807 format. 402 Insufficient funds, 409 Duplicate payment, 429 Rate limited (100 req/min).'
    },
    {
        'id': '10007',
        'title': 'Jenkins CI/CD — URLs and Access',
        'space': 'DEV',
        'url': 'https://confluence.example.com/display/DEV/Jenkins+CICD+URLs',
        'excerpt': 'Jenkins server URLs, access setup, and pipeline configuration for all environments.',
        'last_modified': (datetime.datetime.utcnow() - datetime.timedelta(days=2)).isoformat(),
        'author': 'DevOps Team',
        'labels': ['jenkins', 'ci-cd', 'urls', 'devops'],
        'body_html': '<h2>Jenkins CI/CD — URLs and Access</h2><h3>Jenkins Server URLs</h3><table><tr><th>Environment</th><th>URL</th><th>Purpose</th></tr><tr><td>Production</td><td>https://jenkins.company.com</td><td>Production builds & deployments</td></tr><tr><td>UAT</td><td>https://jenkins-uat.company.com</td><td>UAT/Staging builds</td></tr><tr><td>Dev</td><td>https://jenkins-dev.company.com</td><td>Development & feature branch builds</td></tr></table><h3>Access Setup</h3><ol><li>Request access via IT ServiceDesk ticket (category: CI/CD)</li><li>Use your LDAP credentials to log in</li><li>Contact DevOps team for pipeline admin permissions</li></ol><h3>Key Pipelines</h3><ul><li><strong>build-and-test</strong> — Runs on every PR, executes unit/integration tests</li><li><strong>deploy-uat</strong> — Deploys tagged builds to UAT environment</li><li><strong>deploy-prod</strong> — Production deployment (requires approval gate)</li><li><strong>nightly-regression</strong> — Full regression suite, runs at 2 AM EST</li></ul><h3>API Token</h3><p>To trigger builds via API: <code>curl -X POST https://jenkins.company.com/job/deploy-uat/build --user $USER:$API_TOKEN</code></p>',
        'body_text': 'Jenkins CI/CD URLs and Access. Jenkins Server URLs: Production: https://jenkins.company.com (production builds and deployments), UAT: https://jenkins-uat.company.com (UAT/Staging builds), Dev: https://jenkins-dev.company.com (development and feature branch builds). Access Setup: 1. Request access via IT ServiceDesk (category: CI/CD). 2. Use LDAP credentials. 3. Contact DevOps for admin permissions. Key Pipelines: build-and-test (PR builds), deploy-uat (UAT deployment), deploy-prod (production, requires approval), nightly-regression (2 AM EST). API Token: curl -X POST https://jenkins.company.com/job/deploy-uat/build --user $USER:$API_TOKEN'
    },
    {
        'id': '10008',
        'title': 'CI/CD Pipeline Architecture',
        'space': 'DEV',
        'url': 'https://confluence.example.com/display/DEV/CICD+Pipeline+Architecture',
        'excerpt': 'Architecture overview of the CI/CD pipeline including Jenkins, Artifactory, and deployment workflows.',
        'last_modified': (datetime.datetime.utcnow() - datetime.timedelta(days=12)).isoformat(),
        'author': 'DevOps Team',
        'labels': ['ci-cd', 'architecture', 'jenkins', 'artifactory'],
        'body_html': '<h2>CI/CD Pipeline Architecture</h2><h3>Overview</h3><p>Our CI/CD pipeline is built on Jenkins (https://jenkins.company.com) with Artifactory for artifact storage and Helm for Kubernetes deployments.</p><h3>Pipeline Flow</h3><ol><li>Developer pushes code to GitHub</li><li>Jenkins webhook triggers build-and-test pipeline</li><li>Artifacts published to Artifactory (libs-release)</li><li>Docker image built and pushed to registry.example.com</li><li>Helm chart version bumped and deployed to UAT</li></ol><h3>Configuration</h3><p>Jenkins is configured via Jenkinsfile in each repo root. Shared libraries are in the jenkins-shared-lib repo.</p>',
        'body_text': 'CI/CD Pipeline Architecture. Overview: Built on Jenkins (https://jenkins.company.com) with Artifactory for artifact storage and Helm for K8s deployments. Pipeline Flow: 1. Push to GitHub. 2. Jenkins webhook triggers build-and-test. 3. Artifacts to Artifactory. 4. Docker image to registry. 5. Helm deploy to UAT. Configuration: Jenkinsfile in repo root, shared libraries in jenkins-shared-lib repo.'
    },
    {
        'id': '10009',
        'title': 'Monitoring & Alerting Setup',
        'space': 'OPS',
        'url': 'https://confluence.example.com/display/OPS/Monitoring+Alerting+Setup',
        'excerpt': 'Grafana dashboards, Prometheus metrics, and PagerDuty alerting configuration.',
        'last_modified': (datetime.datetime.utcnow() - datetime.timedelta(days=8)).isoformat(),
        'author': 'SRE Team',
        'labels': ['monitoring', 'grafana', 'prometheus', 'alerting'],
        'body_html': '<h2>Monitoring & Alerting</h2><h3>Dashboards</h3><ul><li>Grafana: https://grafana.company.com</li><li>Kibana: https://kibana.company.com</li></ul><h3>Key Metrics</h3><p>All services expose /metrics endpoint for Prometheus scraping. Key metrics: request_duration_seconds, error_rate, active_connections.</p>',
        'body_text': 'Monitoring and Alerting. Dashboards: Grafana at https://grafana.company.com, Kibana at https://kibana.company.com. Key Metrics: All services expose /metrics for Prometheus. Key metrics: request_duration_seconds, error_rate, active_connections.'
    },
]


# ── Keyword extraction (same as app.py) ───────────────────────────────────────
_STOP_WORDS = frozenset({
    'what', 'where', 'when', 'which', 'who', 'whom', 'whose', 'why', 'how',
    'a', 'an', 'the', 'this', 'that', 'these', 'those',
    'i', 'me', 'my', 'we', 'our', 'you', 'your', 'he', 'she', 'it', 'they', 'them',
    'is', 'are', 'was', 'were', 'be', 'been', 'being',
    'do', 'does', 'did', 'doing',
    'have', 'has', 'had', 'having',
    'can', 'could', 'will', 'would', 'shall', 'should', 'may', 'might', 'must',
    'in', 'on', 'at', 'to', 'for', 'of', 'with', 'by', 'from', 'about',
    'and', 'or', 'but', 'not', 'if', 'so', 'as', 'up', 'out',
    'please', 'tell', 'find', 'get', 'give', 'show', 'need', 'want', 'know',
})

def _extract_search_keywords(query):
    """Extract meaningful search keywords from a natural-language question."""
    tokens = re.findall(r'[a-zA-Z0-9_\-\.]+', query.lower())
    keywords = [t for t in tokens if t not in _STOP_WORDS and len(t) > 1]
    if not keywords:
        keywords = [t for t in tokens if len(t) > 1]
    return keywords


@app.route('/api/confluence/search', methods=['POST'])
def confluence_search():
    """Mock: Search Confluence pages with keyword extraction + title-first matching."""
    data = request.json or {}
    query = data.get('query', '').strip()
    space_key = data.get('space_key', '').strip().upper()
    ai_summary = data.get('ai_summary', True)

    if not query:
        return jsonify({'results': [], 'ai_summary': None, 'error': 'No query provided'}), 400

    # Extract keywords from natural-language query
    keywords = _extract_search_keywords(query)
    print(f'[mock-confluence] Query: "{query}" → keywords: {keywords}')

    # Multi-strategy search: title match first (weighted higher), then content match
    results = []
    seen_ids = set()
    for page in MOCK_CONFLUENCE_PAGES:
        if space_key and page['space'] != space_key:
            continue

        title_lower = page['title'].lower()
        content_lower = f"{page['excerpt']} {page.get('body_text', '')} {' '.join(page['labels'])}".lower()

        title_score = 0
        text_score = 0
        for kw in keywords:
            if kw in title_lower:
                title_score += 5  # Title matches weighted heavily
            if kw in [l.lower() for l in page['labels']]:
                title_score += 3  # Label matches also high value
            if kw in content_lower:
                text_score += 1  # Content match

        total_score = title_score + text_score
        if total_score > 0 and page['id'] not in seen_ids:
            seen_ids.add(page['id'])
            results.append({**page, '_score': total_score, '_title_score': title_score})

    # Sort by total score (title matches dominate)
    results.sort(key=lambda x: (x['_title_score'], x['_score']), reverse=True)
    for r in results:
        del r['_score']
        del r['_title_score']

    # Strategy 3: Browse ALL pages fallback — if keyword search returned few results,
    # add remaining pages in the space so AI can scan their titles
    if len(results) < 5:
        print(f'[mock-confluence] Only {len(results)} keyword matches — browsing all pages in space')
        for page in MOCK_CONFLUENCE_PAGES:
            if space_key and page['space'] != space_key:
                continue
            if page['id'] not in seen_ids:
                seen_ids.add(page['id'])
                results.append({**page, '_browse_result': True})
        print(f'[mock-confluence] After browse-all: {len(results)} total pages')

    keyword_results = [r for r in results if not r.get('_browse_result')]
    browse_results = [r for r in results if r.get('_browse_result')]
    print(f'[mock-confluence] {len(keyword_results)} keyword matches + {len(browse_results)} browse results')

    # Generate mock AI summary — reads actual page content
    summary = None
    if ai_summary and results:
        keyword_titles = [r['title'] for r in keyword_results]
        browse_titles = [r['title'] for r in browse_results]

        # Read top 5 pages' body_text for the summary (keyword results first)
        pages_to_read = keyword_results[:5]
        if len(pages_to_read) < 5 and browse_results:
            pages_to_read += browse_results[:5 - len(pages_to_read)]
        pages_content = []
        for r in pages_to_read:
            body = r.get('body_text', r.get('excerpt', ''))
            if body:
                pages_content.append(f"Page: {r['title']}\n{body[:3000]}")

        # Build a context-aware summary from actual page content
        query_lower = query.lower()
        kw_str = ' '.join(keywords)

        # Show result counts in summary
        total_found = len(results)
        summary = f"Based on **{len(keyword_results)} matching page{'s' if len(keyword_results) != 1 else ''}**"
        if browse_results:
            summary += f" + **{len(browse_results)} other page{'s' if len(browse_results) != 1 else ''}** in the space"
        summary += f" in Confluence:\n\n"

        # Look for URLs/specific data in page content matching keywords
        found_specific = False
        if 'jenkins' in kw_str and 'url' in kw_str:
            for r in results:
                body = r.get('body_text', '')
                if 'jenkins' in body.lower() and ('http' in body.lower() or 'url' in r['title'].lower()):
                    summary += f"### 🔗 Jenkins URLs\n\n"
                    summary += f"From **{r['title']}**:\n\n"
                    summary += "| Environment | URL |\n|---|---|\n"
                    summary += "| Production | `https://jenkins.company.com` |\n"
                    summary += "| UAT | `https://jenkins-uat.company.com` |\n"
                    summary += "| Dev | `https://jenkins-dev.company.com` |\n\n"
                    summary += "**Access**: Use LDAP credentials. Request access via IT ServiceDesk (category: CI/CD).\n\n"
                    summary += "**API**: `curl -X POST https://jenkins.company.com/job/<pipeline>/build --user $USER:$API_TOKEN`\n\n"
                    found_specific = True
                    break
        elif 'runbook' in kw_str or 'rollback' in kw_str or 'deployment' in kw_str:
            summary += "### 📋 Deployment & Rollback Procedure\n\n"
            summary += "1. **Pre-deployment**: Verify all PRs are merged, CI/CD is green, and UAT sign-off is obtained\n"
            summary += "2. **Run database migrations**: `kubectl exec -it billing-db-0 -- /scripts/migrate.sh`\n"
            summary += "3. **Deploy**: Trigger the deploy workflow with the target version\n"
            summary += "4. **Verify health**: Monitor `/actuator/health` for 5 minutes\n"
            summary += "5. **Rollback** (if needed): `kubectl rollout undo deployment/<service-name>`\n\n"
            summary += "> ⚠️ **Warning**: Database migrations cannot be automatically rolled back — contact the DBA team first.\n\n"
            found_specific = True
        elif 'grafana' in kw_str or 'monitoring' in kw_str:
            for r in results:
                if 'monitoring' in r['title'].lower() or 'grafana' in r.get('body_text', '').lower():
                    summary += f"### 📊 Monitoring URLs\n\n"
                    summary += f"From **{r['title']}**:\n\n"
                    summary += "- **Grafana**: `https://grafana.company.com`\n"
                    summary += "- **Kibana**: `https://kibana.company.com`\n\n"
                    found_specific = True
                    break
        elif 'troubleshoot' in kw_str:
            summary += "### 🔧 Common Issues & Resolutions\n\n"
            summary += "**Pod CrashLoopBackOff**: Check logs with `kubectl logs <pod> --previous`, verify resource limits.\n\n"
            summary += "**Connection Pool Exhaustion**: Scale horizontally, adjust pool size (default: 10, max: 50).\n\n"
            found_specific = True
        elif 'architecture' in kw_str or 'design' in kw_str:
            summary += "### 🏗️ Architecture Overview\n\n"
            summary += "The platform uses a **microservices architecture** with key services:\n\n"
            summary += "- **auth-service**: OAuth2 + JWT (Spring Boot)\n"
            summary += "- **payment-gateway**: REST APIs with OAuth2 scopes\n"
            summary += "- **billing-service**: Core billing with DB migrations\n\n"
            found_specific = True
        elif 'onboarding' in kw_str:
            summary += "### 🎓 Getting Started\n\n"
            summary += "**Day 1**: Request GitHub, Jira/Confluence, and VPN access. Set up 2FA.\n\n"
            summary += "**Day 1-2**: Install Docker, kubectl, Helm, Node.js 18+. Clone mono-repo, run `make dev-setup`.\n\n"
            found_specific = True
        elif 'release' in kw_str or 'checklist' in kw_str:
            summary += "### 📋 Release Process\n\n"
            summary += "**T-2 Days**: Version drift check, AI readiness, fix version in Jira\n\n"
            summary += "**T-1 Day**: Lock board, UAT smoke tests, stakeholder sign-off\n\n"
            summary += "**Release Day**: Deploy to prod, monitor 30 min, send announcement\n\n"
            found_specific = True

        if not found_specific:
            # Generic: show top page titles with excerpts
            for r in keyword_results[:3] if keyword_results else results[:3]:
                summary += f"- **{r['title']}** — {r.get('excerpt', '')[:100]}\n"
            summary += "\nClick 👁 Preview on any page card below for the full content.\n"

        summary += f"\n*Sources: {', '.join(keyword_titles[:5])}*"
        if browse_titles:
            summary += f"\n\n📂 *Other pages in this space: {', '.join(browse_titles[:5])}*"

    return jsonify({
        'results': results[:20],
        'ai_summary': summary,
        'query': query,
        'total': len(results),
        'configured': True
    })


@app.route('/api/confluence/page/<page_id>')
def confluence_page(page_id):
    """Mock: Get full page content."""
    for page in MOCK_CONFLUENCE_PAGES:
        if page['id'] == page_id:
            return jsonify(page)
    return jsonify({'error': 'Page not found'}), 404


@app.route('/api/confluence/labels', methods=['POST'])
def confluence_by_labels():
    """Mock: Search by labels."""
    data = request.json or {}
    labels = data.get('labels', [])
    space_key = data.get('space_key', '').strip().upper()

    results = []
    for page in MOCK_CONFLUENCE_PAGES:
        page_labels = [l.lower() for l in page.get('labels', [])]
        if any(l.lower() in page_labels for l in labels):
            if not space_key or page['space'] == space_key:
                results.append(page)
    return jsonify({'results': results, 'total': len(results)})


@app.route('/api/confluence/status')
def confluence_status():
    """Mock: Confluence integration status."""
    return jsonify({
        'configured': True,
        'mcp_url': True,
        'rest_url': False,
        'spaces': ['DEV', 'OPS', 'REL'],
    })


@app.route('/api/ai/release_notes', methods=['POST'])
def release_notes():
    board = _read_board()
    if not board or not board.get('services'):
        return jsonify({'error': 'No nominations'}), 400

    fix_version = board.get('fix_version', '')

    # ── Step 1: Fetch Jira tickets by fix version ────────────────────────
    fix_version_issues = []
    if fix_version:
        # In production: call Jira API with JQL fixVersion = "{fix_version}"
        # In mock: use all mock issues
        fix_version_issues = list(MOCK_JIRA_ISSUES.values())

    # ── Step 2: Also collect per-nomination Jira IDs (manual entries) ────
    svc_jira_map = {}
    jira_details = {}
    for name, s in board['services'].items():
        ids = _parse_jira_ids(s.get('jira_ids', ''))
        if ids:
            svc_jira_map[name] = ids
            for jid in ids:
                if jid in MOCK_JIRA_ISSUES:
                    jira_details[jid] = MOCK_JIRA_ISSUES[jid]
                else:
                    jira_details[jid] = {
                        'id': jid, 'summary': f'Mock issue {jid}',
                        'description': f'Description for {jid}',
                        'status': 'Done', 'type': 'Task', 'priority': 'Medium'
                    }

    # ── Step 3: Build release notes ──────────────────────────────────────
    lines = [f"## Release Notes — {board.get('release_date', 'Unknown')}"]
    if fix_version:
        lines.append(f"**Fix Version:** `{fix_version}`\n")
    lines.append(f"**{len(board['services'])} services nominated for release:**\n")
    lines.append("| Service | Version | Jira Tickets | Helm Chart | Notes |")
    lines.append("|---|---|---|---|---|")
    for name, s in board['services'].items():
        jira_col = s.get('jira_ids', '') or '—'
        lines.append(f"| {name} | {s.get('image_tag','?')} | {jira_col} | {s.get('helm_version','N/A')} | {s.get('notes','')} |")

    # ── Step 4: "What's Changed" from fix version Jira tickets ───────────
    all_issues = {}
    # Add fix version issues
    for issue in fix_version_issues:
        all_issues[issue['id']] = issue
    # Add per-nomination issues
    for jid, issue in jira_details.items():
        all_issues[jid] = issue

    if all_issues:
        lines.append("\n## What's Changed\n")
        if fix_version:
            lines.append(f"*Jira tickets from fix version `{fix_version}` ({len(fix_version_issues)} tickets):*\n")
        features, bugs, improvements, tasks = [], [], [], []
        for jid, issue in all_issues.items():
            desc_preview = (issue.get('description', '') or '')[:120]
            if len(issue.get('description', '')) > 120:
                desc_preview += '...'
            entry = f"- **{jid}**: {issue.get('summary', '?')} [{issue.get('status', '?')}]\n  > {desc_preview}"
            itype = issue.get('type', 'Task')
            if itype in ('Story', 'Feature'):
                features.append(entry)
            elif itype == 'Bug':
                bugs.append(entry)
            elif itype == 'Improvement':
                improvements.append(entry)
            else:
                tasks.append(entry)
        if features:
            lines.append("### 🆕 New Features")
            lines.extend(features)
        if bugs:
            lines.append("\n### 🐛 Bug Fixes")
            lines.extend(bugs)
        if improvements:
            lines.append("\n### ⚡ Improvements")
            lines.extend(improvements)
        if tasks:
            lines.append("\n### 🔧 Maintenance")
            lines.extend(tasks)

    # ── Step 5: Post-cutoff exception warning ────────────────────────────
    exc_services = [n for n, s in board['services'].items() if s.get('is_exception')]
    if exc_services:
        lines.append("\n## ⚠️ Post-Cutoff Changes\n")
        lines.append("> The following services were nominated **after the cutoff deadline** as exceptions.\n")
        for n in exc_services:
            s = board['services'][n]
            lines.append(f"- **{n}** (`{s.get('image_tag', '?')}`) — Approved by: {s.get('exception_approver', '?')}, Reason: {s.get('exception_reason', '?')}")

    lines.append(f"\n**AI Risk Assessment:** {'🟢 All services look healthy.' if len(board['services']) < 5 else '🟡 Review recommended for larger release scope.'}")

    total_jira = len(all_issues)
    return jsonify({
        'notes': '\n'.join(lines),
        'gemini_powered': False,
        'jira_enriched': total_jira > 0,
        'jira_count': total_jira,
        'fix_version': fix_version,
        'release_date': board.get('release_date')
    })

@app.route('/api/release/history')
def history():
    releases = []
    for key, board in _board_store.items():
        releases.append({'release_date': key, 'status': board.get('status','unknown'),
                         'service_count': len(board.get('services',{})),
                         'finalized_by': board.get('finalized_by'), 'created_at': board.get('created_at')})
    releases.sort(key=lambda x: x.get('release_date',''), reverse=True)
    return jsonify({'releases': releases})

@app.route('/api/release/exceptions')
def release_exceptions():
    """Exception nomination analytics."""
    board = _read_board()
    if not board:
        return jsonify({'total': 0, 'exceptions': [], 'by_requester': {}, 'by_approver': {}})
    exceptions = board.get('exception_nominations', [])
    by_req, by_app = {}, {}
    for e in exceptions:
        by_req[e.get('requested_by', '?')] = by_req.get(e.get('requested_by', '?'), 0) + 1
        by_app[e.get('approver', '?')] = by_app.get(e.get('approver', '?'), 0) + 1
    return jsonify({
        'total': len(exceptions), 'exceptions': exceptions,
        'by_requester': by_req, 'by_approver': by_app,
        'release_date': board.get('release_date')
    })


# ══════════════════════════════════════════════════════════════════════════════
# Artifactory Versions (Mock)
# ══════════════════════════════════════════════════════════════════════════════

def _mock_artifactory_versions(component_name):
    """Generate realistic mock Artifactory versions for a component."""
    import hashlib
    # Use component name as seed for deterministic but varied versions
    seed = int(hashlib.md5(component_name.encode()).hexdigest()[:8], 16)
    now = datetime.datetime.now()
    versions = []
    major = 2 + (seed % 3)
    minor = 8 + (seed % 5)
    for i in range(12):
        patch = 12 - i + (seed % 3)
        ver = f'{major}.{minor}.{patch}'
        days_ago = i * 7 + (seed % 4)
        date = (now - datetime.timedelta(days=days_ago)).strftime('%Y-%m-%dT%H:%M:%S')
        size_mb = round(15 + (seed % 40) + random.uniform(-2, 2), 1)
        if i == 0:
            freshness = 'current_week'
        elif i < 3:
            freshness = 'previous_week'
        else:
            freshness = 'stale'
        versions.append({
            'version': ver,
            'date': date,
            'size_mb': size_mb,
            'freshness': freshness,
            'download_count': max(1, 150 - (i * 15) + random.randint(-5, 5)),
            'path': f'libs-release/com/company/{component_name}/{ver}/{component_name}-{ver}.jar'
        })
    return versions

@app.route('/api/artifactory/versions/<component_name>')
def get_artifactory_versions(component_name):
    """Fetch available versions from Artifactory for a custom component (mock)."""
    comp = MOCK_CUSTOM_MAP.get(component_name)
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

    versions = _mock_artifactory_versions(component_name)

    return jsonify({
        'component': component_name,
        'artifactory_configured': True,
        'artifactory_path': art_path,
        'version_count': len(versions),
        'versions': versions
    })


# ══════════════════════════════════════════════════════════════════════════════
# Async Release Notes Job (Mock)
# ══════════════════════════════════════════════════════════════════════════════
_release_notes_jobs = {}

@app.route('/api/ai/release_notes/<job_id>')
def release_notes_status(job_id):
    """Poll for async release notes generation status."""
    job = _release_notes_jobs.get(job_id)
    if not job:
        # In mock mode, return a synthetic 'done' response if job not found
        # This handles cases where the frontend polls before the job is registered
        return jsonify({'status': 'done', 'notes': '(Mock release notes — job expired)'})
    if job['status'] == 'running':
        return jsonify({'status': 'running'})
    elif job['status'] == 'error':
        return jsonify({'status': 'error', 'error': job['error']}), 500
    else:
        result = {k: v for k, v in job.items() if k != 'status'}
        result['status'] = 'done'
        return jsonify(result)


# ══════════════════════════════════════════════════════════════════════════════
# GitHub OAuth (Mock) + Deploy
# ══════════════════════════════════════════════════════════════════════════════
_github_sessions = {}    # session_id -> user info
_deploy_runs = {}        # run_id -> run details

@app.route('/login')
def login_page():
    """Mock OAuth — auto-login and redirect home."""
    session['github_user'] = {
        'login': 'mock-user',
        'name': 'Mock User',
        'avatar_url': 'https://github.com/identicons/mock.png',
        'logged_in': True
    }
    return redirect('/')

@app.route('/logout')
def logout_page():
    """Clear session and redirect home."""
    session.pop('github_user', None)
    return redirect('/')

@app.route('/api/github/login', methods=['POST'])
def github_login():
    """Mock GitHub OAuth — instantly logs in as the provided user."""
    data = request.json or {}
    username = data.get('username', 'rajesh')
    session['github_user'] = {
        'login': username,
        'name': username.title(),
        'avatar_url': f'https://github.com/{username}.png',
        'logged_in': True
    }
    return jsonify({'status': 'ok', 'user': session['github_user']})

@app.route('/api/github/status')
def github_status():
    """Check if user is logged into GitHub."""
    user = session.get('github_user')
    if user and user.get('logged_in'):
        return jsonify({'logged_in': True, 'user': user})
    return jsonify({'logged_in': False})

@app.route('/api/github/logout', methods=['POST'])
def github_logout():
    session.pop('github_user', None)
    return jsonify({'status': 'ok'})

def _simulate_workflow_run(run_id):
    """Background thread: progresses a workflow run through stages."""
    time.sleep(2)  # queued for 2s
    if run_id in _deploy_runs:
        _deploy_runs[run_id]['status'] = 'in_progress'
        _deploy_runs[run_id]['steps'] = [
            {'name': 'Checkout code', 'status': 'completed'},
            {'name': 'Build & Push Image', 'status': 'in_progress'},
            {'name': 'Deploy to Environment', 'status': 'pending'},
            {'name': 'Health Check', 'status': 'pending'}
        ]
    time.sleep(3)  # build for 3s
    if run_id in _deploy_runs:
        _deploy_runs[run_id]['steps'][1]['status'] = 'completed'
        _deploy_runs[run_id]['steps'][2]['status'] = 'in_progress'
    time.sleep(3)  # deploy for 3s
    if run_id in _deploy_runs:
        _deploy_runs[run_id]['steps'][2]['status'] = 'completed'
        _deploy_runs[run_id]['steps'][3]['status'] = 'in_progress'
    time.sleep(2)  # health check for 2s
    if run_id in _deploy_runs:
        # 85% chance success, 15% failure
        success = random.random() > 0.15
        _deploy_runs[run_id]['steps'][3]['status'] = 'completed' if success else 'failed'
        _deploy_runs[run_id]['status'] = 'completed' if success else 'failure'
        _deploy_runs[run_id]['conclusion'] = 'success' if success else 'failure'
        _deploy_runs[run_id]['completed_at'] = datetime.datetime.utcnow().isoformat()

@app.route('/api/deploy/workflows')
def deploy_workflows():
    """Mock: return sample deployment workflows."""
    workflows = [
        {
            'id': 101, 'name': 'Deploy Service to UAT', 'file': 'deploy-uat.yml',
            'state': 'active', 'last_conclusion': 'success', 'last_run_ago': '2h ago',
            'duration': '3m 42s', 'last_run_by': 'rajesh', 'branch': 'main',
            'dispatch_inputs': [
                {'name': 'service', 'type': 'choice', 'description': 'Service to deploy', 'default': '', 'required': True,
                 'options': ['billing-service', 'auth-service', 'payment-gateway', 'report-engine', 'notification-hub']},
                {'name': 'version', 'type': 'string', 'description': 'Image tag / version', 'default': '', 'required': True},
                {'name': 'environment', 'type': 'choice', 'description': 'Target environment', 'default': 'uat', 'required': True,
                 'options': ['uat', 'staging', 'production']},
            ]
        },
        {
            'id': 102, 'name': 'Deploy Helm Chart', 'file': 'deploy-helm.yml',
            'state': 'active', 'last_conclusion': 'success', 'last_run_ago': '5h ago',
            'duration': '5m 18s', 'last_run_by': 'dev-team', 'branch': 'main',
            'dispatch_inputs': [
                {'name': 'chart_name', 'type': 'string', 'description': 'Helm chart name', 'default': '', 'required': True},
                {'name': 'chart_version', 'type': 'string', 'description': 'Chart version', 'default': '', 'required': True},
                {'name': 'namespace', 'type': 'string', 'description': 'Target namespace', 'default': 'uat', 'required': True},
            ]
        },
        {
            'id': 103, 'name': 'Rollback Service', 'file': 'rollback.yml',
            'state': 'active', 'last_conclusion': 'failure', 'last_run_ago': '1d ago',
            'duration': '1m 12s', 'last_run_by': 'ops-lead', 'branch': 'main',
            'dispatch_inputs': [
                {'name': 'service', 'type': 'string', 'description': 'Service to rollback', 'default': '', 'required': True},
                {'name': 'target_version', 'type': 'string', 'description': 'Version to rollback to', 'default': '', 'required': True},
            ]
        },
        {
            'id': 104, 'name': 'Run Integration Tests', 'file': 'integration-tests.yml',
            'state': 'active', 'last_conclusion': 'success', 'last_run_ago': '30m ago',
            'duration': '8m 55s', 'last_run_by': 'rajesh', 'branch': 'main',
            'dispatch_inputs': [
                {'name': 'test_suite', 'type': 'choice', 'description': 'Which test suite', 'default': 'all', 'required': False,
                 'options': ['all', 'smoke', 'regression', 'security']},
            ]
        },
    ]
    return jsonify({'workflows': workflows, 'repo': 'org/app-deployment'})

@app.route('/api/deploy/trigger', methods=['POST'])
def deploy_trigger():
    """Trigger a mock GitHub Actions workflow_dispatch."""
    gh_user = session.get('github_user')
    if not gh_user or not gh_user.get('logged_in'):
        return jsonify({'error': 'GitHub login required'}), 401

    data = request.json or {}
    workflow_id = data.get('workflow_id', '')
    inputs = data.get('inputs', {})

    # Force environment to UAT
    if 'environment' in inputs:
        inputs['environment'] = 'uat'

    if not workflow_id:
        return jsonify({'error': 'workflow_id required'}), 400

    run_id = str(uuid.uuid4())[:8]
    now = datetime.datetime.utcnow().isoformat()

    run = {
        'run_id': run_id,
        'inputs': inputs,
        'environment': 'uat',
        'status': 'queued',
        'conclusion': None,
        'triggered_by': gh_user['login'],
        'triggered_at': now,
        'completed_at': None,
        'workflow_id': workflow_id,
        'repo': 'org/app-deployment',
        'html_url': f'https://github.com/org/app-deployment/actions/runs/{run_id}',
        'steps': [
            {'name': 'Checkout code', 'status': 'pending'},
            {'name': 'Build & Push Image', 'status': 'pending'},
            {'name': 'Deploy to Environment', 'status': 'pending'},
            {'name': 'Health Check', 'status': 'pending'}
        ]
    }
    _deploy_runs[run_id] = run

    # Add to board audit trail
    board = _read_board()
    if board:
        board['audit_trail'].append({
            'action': 'deploy_triggered',
            'service': inputs.get('service', ''),
            'version': inputs.get('version', ''),
            'environment': 'uat',
            'workflow_id': workflow_id,
            'run_id': run_id, 'by': gh_user['login'], 'at': now
        })
        _write_board(board)

    # Start background simulation
    t = threading.Thread(target=_simulate_workflow_run, args=(run_id,), daemon=True)
    t.start()

    return jsonify({'status': 'ok', 'run': run})

@app.route('/api/deploy/status/<run_id>')
def deploy_status(run_id):
    """Poll status of a specific workflow run."""
    run = _deploy_runs.get(run_id)
    if not run:
        return jsonify({'error': 'Run not found'}), 404
    return jsonify(run)

@app.route('/api/deploy/history')
def deploy_history():
    """List all deploy runs, newest first."""
    runs = sorted(_deploy_runs.values(), key=lambda r: r.get('triggered_at', ''), reverse=True)
    return jsonify({'runs': runs, 'count': len(runs)})


# ══════════════════════════════════════════════════════════════════════════════
# AI Release Chatbot (Mock)
# ══════════════════════════════════════════════════════════════════════════════

@app.route('/api/ai/converse', methods=['POST'])
def ai_converse():
    """Mock AI chatbot — returns contextual responses based on keywords."""
    data = request.json or {}
    message = data.get('message', '').lower().strip()

    if not message:
        return jsonify({'error': 'No message provided'}), 400

    # Read current board for contextual answers
    board = _read_board() or _new_board()
    services = board.get('services', {})
    svc_names = list(services.keys())
    svc_count = len(svc_names)

    # ── Keyword-based mock responses ──────────────────────────────────────
    if any(w in message for w in ['what', 'release', 'board', 'going', 'friday', 'included']):
        if svc_count == 0:
            reply = ("📋 **No services nominated yet** for this release.\n\n"
                     f"Release date: **{board.get('release_date', '?')}**\n"
                     f"Board status: **{board.get('status', 'open')}**\n\n"
                     "Use the Board tab to nominate services.")
        else:
            svc_lines = []
            for name, svc in services.items():
                jira_col = svc.get('jira_ids', '') or '\u2014'
                svc_lines.append(f"| {name} | `{svc.get('image_tag', '?')}` | {jira_col} | {svc.get('nominated_by', '?')} |")
            table = "| Service | Version | Jira | Nominated By |\n|---------|---------|------|-------------|\n" + '\n'.join(svc_lines)
            reply = (f"📋 **Release Board Summary**\n\n"
                     f"- **Release Date:** {board.get('release_date', '?')}\n"
                     f"- **Status:** {board.get('status', 'open')}\n"
                     f"- **Services:** {svc_count} nominated\n"
                     f"- **Cutoff:** {board.get('cutoff', '?')}\n\n"
                     f"{table}")

    elif any(w in message for w in ['drift', 'version', 'mismatch']):
        if svc_count == 0:
            reply = "🔀 No services nominated — nothing to check for drift."
        else:
            lines = []
            for name, svc in services.items():
                match = random.choice(['✅ Match', '⚠️ Drift'])
                lines.append(f"| {name} | `{svc.get('image_tag', '?')}` | `{svc.get('image_tag', '?')}` | {match} |")
            table = "| Service | Nominated | Live | Status |\n|---------|-----------|------|--------|\n" + '\n'.join(lines)
            reply = f"🔀 **Version Drift Check**\n\n{table}\n\n💡 *Run the full drift check from the Version Drift tab for live data.*"

    elif any(w in message for w in ['readiness', 'health', 'score', 'ready', 'green', 'red']):
        if svc_count == 0:
            reply = "🤖 No services nominated — run a readiness check after nominating services."
        else:
            lines = []
            for name in svc_names:
                score = random.randint(70, 99)
                status = '🟢 Green' if score >= 80 else '🟡 Yellow'
                lines.append(f"| {name} | {score}/100 | {status} |")
            table = "| Service | Score | Status |\n|---------|-------|--------|\n" + '\n'.join(lines)
            reply = f"🤖 **AI Readiness Summary**\n\n{table}\n\n💡 *Run the full check from the AI Readiness tab for actual Gemini analysis.*"

    elif any(w in message for w in ['audit', 'trail', 'history', 'log', 'who', 'when']):
        trail = board.get('audit_trail', [])
        if not trail:
            reply = "📜 **Audit trail is empty** — no actions recorded yet."
        else:
            lines = []
            for e in list(reversed(trail))[:10]:
                detail = e.get('service', '')
                if e.get('image_tag'):
                    detail += f" → `{e['image_tag']}`"
                lines.append(f"| {e.get('action', '?')} | {e.get('by', '?')} | {e.get('at', '?')[:16]} | {detail} |")
            table = "| Action | By | When | Details |\n|--------|----|----- |--------|\n" + '\n'.join(lines)
            reply = f"📜 **Recent Audit Trail**\n\n{table}"

    elif any(w in message for w in ['uat', 'cluster', 'running', 'deployed', 'live', 'namespace']):
        lines = []
        for s in MOCK_SERVICES[:8]:
            lines.append(f"| {s['name']} | {s['kind']} | `{s['image_tag']}` | {s['replicas']}/{s['desired_replicas']} |")
        table = "| Service | Kind | Image Tag | Ready |\n|---------|------|-----------|-------|\n" + '\n'.join(lines)
        reply = f"🖥️ **UAT Namespace — Live Services**\n\n{table}\n\n*Showing 8 of {len(MOCK_SERVICES)} services.*"

    elif any(name in message for name in svc_names):
        # User asked about a specific service
        matched = [n for n in svc_names if n in message][0]
        svc = services[matched]
        reply = (f"🔍 **Service: {matched}**\n\n"
                 f"| Field | Value |\n|-------|-------|\n"
                 f"| Image Tag | `{svc.get('image_tag', '?')}` |\n"
                 f"| Helm Chart | `{svc.get('helm_chart_version', 'n/a')}` |\n"
                 f"| Kind | {svc.get('kind', 'Deployment')} |\n"
                 f"| Jira Tickets | {svc.get('jira_ids', '') or 'None'} |\n"
                 f"| Nominated By | {svc.get('nominated_by', '?')} |\n"
                 f"| Nominated At | {svc.get('nominated_at', '?')} |\n"
                 f"| Notes | {svc.get('notes', '—')} |\n\n"
                 f"✅ **Status:** Healthy — all pods running")

    elif any(w in message for w in ['help', 'can you', 'what can']):
        reply = ("💬 **I can help you with:**\n\n"
                 "- **\"What's in this release?\"** — shows all nominated services\n"
                 "- **\"Check drift\"** — compares nominated vs live versions\n"
                 "- **\"Show readiness scores\"** — AI health check results\n"
                 "- **\"Show audit trail\"** — who did what and when\n"
                 "- **\"What's running in UAT?\"** — live cluster services\n"
                 "- **\"Is billing-service included?\"** — specific service status\n\n"
                 "Just ask in natural language! 🚀")

    else:
        reply = (f"🤖 I understand you're asking about: *\"{data.get('message', '')}\"*\n\n"
                 f"Here's what I can see:\n"
                 f"- **{svc_count}** services on the board\n"
                 f"- Board status: **{board.get('status', 'open')}**\n"
                 f"- Release date: **{board.get('release_date', '?')}**\n\n"
                 f"Try asking something specific like:\n"
                 f"- \"What services are nominated?\"\n"
                 f"- \"Check for version drift\"\n"
                 f"- \"Show readiness scores\"")

    return jsonify({'reply': reply, 'session_id': request.headers.get('X-Session-Id', 'default')})


@app.route('/api/ai/converse/reset', methods=['POST'])
def ai_converse_reset():
    """Clear chat session (no-op in mock mode)."""
    return jsonify({'status': 'cleared', 'session_id': request.headers.get('X-Session-Id', 'default')})


# ══════════════════════════════════════════════════════════════════════════════
# QA Tab — E2E Environment Preparation (Mock)
# ══════════════════════════════════════════════════════════════════════════════
_qa_state = {
    'status': None,          # None, 'e2e_pushed', etc.
    'e2e_commit_url': None,
    'nominated_count': 0,
    'prod_count': 0,
    'total_count': 0,
    'services': {},
    'prod_status': None,
    'prod_commit_url': None,
    'preprod_commit_url': None,
    'change_ticket': None,
    'generated_at': None,
}


@app.route('/api/qa/prepare', methods=['POST'])
def qa_prepare():
    """Mock: Generate version.yaml and push to e2e branch."""
    board = _read_board()
    if not board:
        return jsonify({'error': 'No active release board'}), 400

    nominated = board.get('services', {})
    if not nominated:
        return jsonify({'error': 'No services nominated on the board'}), 400

    # Build version manifest: nominated services + remaining prod services
    services = {}
    for name, svc in nominated.items():
        services[name] = {
            'image': svc.get('image', f'registry.example.com/{name}:{svc.get("image_tag", "latest")}'),
            'image_tag': svc.get('image_tag', 'latest'),
            'kind': svc.get('kind', 'Deployment'),
            'helm_version': svc.get('helm_chart_version', svc.get('helm_version', '')),
            'source': 'board',
        }

    # Add prod services not on the board
    prod_count = 0
    for svc in MOCK_SERVICES:
        if svc['name'] not in services:
            services[svc['name']] = {
                'image': svc.get('image', ''),
                'image_tag': svc.get('image_tag', 'latest'),
                'kind': svc.get('kind', 'Deployment'),
                'helm_version': svc.get('helm_version', ''),
                'source': 'prod',
            }
            prod_count += 1

    now = datetime.datetime.utcnow().isoformat()
    commit_hash = uuid.uuid4().hex[:7]
    commit_url = f'https://github.com/org/app-deployment/commit/{commit_hash}'

    # Save QA state
    _qa_state['status'] = 'e2e_pushed'
    _qa_state['e2e_commit_url'] = commit_url
    _qa_state['nominated_count'] = len(nominated)
    _qa_state['prod_count'] = prod_count
    _qa_state['total_count'] = len(services)
    _qa_state['services'] = services
    _qa_state['generated_at'] = now

    # Audit trail
    board.setdefault('audit_trail', []).append({
        'action': 'qa_e2e_prepared',
        'by': session.get('github_user', {}).get('login', 'mock-user'),
        'at': now,
        'service': f'{len(services)} services',
    })
    _write_board(board)

    return jsonify({
        'status': 'ok',
        'commit_url': commit_url,
        'nominated_count': len(nominated),
        'prod_count': prod_count,
        'total': len(services),
        'services': services,
        'generated_at': now,
    })


@app.route('/api/qa/prepare/status')
def qa_prepare_status():
    """Mock: Check if QA prepare has already been run."""
    return jsonify(_qa_state)


@app.route('/api/qa/drift-check', methods=['POST'])
def qa_drift_check():
    """Mock: Compare current board+prod against E2E version.yaml."""
    if _qa_state['status'] != 'e2e_pushed':
        return jsonify({'error': 'E2E environment not prepared yet. Run "Prepare E2E" first.'}), 400

    e2e_services = _qa_state.get('services', {})
    board = _read_board() or _new_board()
    nominated = board.get('services', {})

    drifts = []
    # Simulate some drifts randomly
    for name, e2e_svc in e2e_services.items():
        if name in nominated:
            board_tag = nominated[name].get('image_tag', 'latest')
            e2e_tag = e2e_svc.get('image_tag', 'latest')
            # 15% chance of drift
            if random.random() < 0.15:
                new_tag = e2e_tag.rsplit('.', 1)
                if len(new_tag) == 2 and new_tag[1].isdigit():
                    drifted_tag = f"{new_tag[0]}.{int(new_tag[1]) + 1}"
                else:
                    drifted_tag = e2e_tag + '-hotfix'
                drifts.append({
                    'service': name,
                    'e2e_tag': e2e_tag,
                    'current_tag': drifted_tag,
                    'drift_type': 'version_changed',
                })

    return jsonify({
        'status': 'drift' if drifts else 'match',
        'drift_count': len(drifts),
        'total_services': len(e2e_services),
        'drifts': drifts,
        'e2e_generated_at': _qa_state.get('generated_at'),
    })


@app.route('/api/qa/prepare-prod', methods=['POST'])
def qa_prepare_prod():
    """Mock: Push version.yaml to prod + preprod branches."""
    data = request.json or {}
    change_ticket = data.get('change_ticket', '').strip()
    if not change_ticket:
        return jsonify({'error': 'Change ticket is required'}), 400

    if _qa_state['status'] != 'e2e_pushed':
        return jsonify({'error': 'E2E environment not prepared yet'}), 400

    prod_hash = uuid.uuid4().hex[:7]
    preprod_hash = uuid.uuid4().hex[:7]
    prod_url = f'https://github.com/org/app-deployment/commit/{prod_hash}'
    preprod_url = f'https://github.com/org/app-deployment/commit/{preprod_hash}'

    _qa_state['prod_status'] = 'pushed'
    _qa_state['prod_commit_url'] = prod_url
    _qa_state['preprod_commit_url'] = preprod_url
    _qa_state['change_ticket'] = change_ticket

    return jsonify({
        'status': 'ok',
        'prod_commit_url': prod_url,
        'preprod_commit_url': preprod_url,
        'total': _qa_state['total_count'],
        'change_ticket': change_ticket,
    })


@app.route('/api/qa/env/services')
def qa_env_services():
    """Mock: List services running in the QA namespace."""
    services = []
    for svc in MOCK_SERVICES:
        services.append({
            'name': svc['name'],
            'kind': svc.get('kind', 'Deployment'),
            'image_tag': svc.get('image_tag', 'latest'),
            'replicas': svc.get('replicas', 1),
            'desired_replicas': svc.get('desired_replicas', 1),
            'available': svc.get('available', True),
        })
    return jsonify({
        'services': services,
        'namespace': 'qa-e2e',
        'count': len(services),
    })


@app.route('/api/qa/test/trigger', methods=['POST'])
def qa_test_trigger():
    """Mock: Trigger a test pipeline (smoke/e2e/regression)."""
    data = request.json or {}
    test_type = data.get('test_type', 'smoke')

    run_id = uuid.uuid4().hex[:8]
    return jsonify({
        'status': 'triggered',
        'test_type': test_type,
        'run_id': run_id,
        'html_url': f'https://github.com/org/app-deployment/actions/runs/{run_id}',
    })


if __name__ == '__main__':
    port = int(os.getenv('PORT', 8090))
    print(f"\n🚀 Release Readiness Dashboard (MOCK MODE)")
    print(f"   http://localhost:{port}\n")
    app.run(host='0.0.0.0', port=port, debug=True)
