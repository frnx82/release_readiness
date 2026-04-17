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

def _get_release_date():
    today = datetime.date.today()
    days = (4 - today.weekday()) % 7
    if days == 0 and datetime.datetime.now().hour >= 18:
        days = 7
    return (today + datetime.timedelta(days=days)).isoformat()

def _get_cutoff():
    today = datetime.date.today()
    days = (4 - today.weekday()) % 7
    if days == 0 and datetime.datetime.now().hour >= 18:
        days = 7
    friday = today + datetime.timedelta(days=days)
    cutoff = friday - datetime.timedelta(days=2)  # Wednesday
    return datetime.datetime.combine(cutoff, datetime.time(17, 0)).isoformat()

def _new_board():
    return {
        'release_date': _get_release_date(),
        'cutoff': _get_cutoff(),
        'status': 'open',
        'services': {},
        'audit_trail': [],
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

@app.route('/api/custom_components')
def list_custom_components():
    return jsonify({'components': MOCK_CUSTOM_COMPONENTS, 'count': len(MOCK_CUSTOM_COMPONENTS)})

@app.route('/api/release/current')
def get_current():
    board = _read_board()
    if not board:
        board = _new_board()
        _write_board(board)
    board['is_past_cutoff'] = datetime.datetime.utcnow().isoformat() > board.get('cutoff', '')
    board['nominated_count'] = len(board.get('services', {}))
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
    if board.get('status') in ('locked', 'released'):
        return jsonify({'error': 'Board is locked'}), 403

    now = datetime.datetime.utcnow().isoformat()

    if is_custom:
        # Custom component — version entered manually
        comp = MOCK_CUSTOM_MAP.get(name, {})
        tag = manual_version or 'unknown'
        image = ''
        helm = None
        kind = comp.get('type', 'Custom')
    else:
        # K8s service — version auto-filled from cluster
        svc = MOCK_SERVICE_MAP.get(name, {})
        image = svc.get('image', f'registry.example.com/{name}:unknown')
        tag = svc.get('image_tag', 'unknown')
        helm = svc.get('helm_version')
        kind = svc.get('kind', 'Deployment')

    existing = board['services'].get(name)
    if existing:
        old_tag = existing.get('image_tag', '')
        existing.update({'image': image, 'image_tag': tag, 'helm_version': helm,
                         'notes': notes, 'jira_ids': jira_ids or existing.get('jira_ids', ''),
                         'updated_at': now, 'updated_by': by})
        existing['version_history'].append({'from_tag': old_tag, 'to_tag': tag,
                                            'changed_by': by, 'changed_at': now, 'reason': notes or 'Update'})
        board['audit_trail'].append({'action': 're-nominate', 'service': name,
                                      'from_version': old_tag, 'to_version': tag, 'by': by, 'at': now})
    else:
        board['services'][name] = {
            'name': name, 'kind': kind, 'is_custom': is_custom,
            'image': image, 'image_tag': tag, 'helm_version': helm,
            'nominated_by': by, 'nominated_at': now, 'updated_at': now, 'updated_by': by,
            'notes': notes, 'jira_ids': jira_ids, 'readiness': None, 'readiness_details': None,
            'version_history': [{'from_tag': None, 'to_tag': tag, 'changed_by': by,
                                 'changed_at': now, 'reason': 'Initial nomination'}]
        }
        board['audit_trail'].append({'action': 'nominate', 'service': name,
                                      'version': tag, 'by': by, 'at': now})
    _write_board(board)
    return jsonify({'status': 'ok', 'service': name, 'image_tag': tag})

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
    now = datetime.datetime.utcnow().isoformat()
    board['status'] = 'locked'
    board['finalized_by'] = by
    board['finalized_at'] = now
    board['audit_trail'].append({'action': 'finalize', 'by': by, 'at': now})
    _write_board(board)
    return jsonify({'status': 'locked'})

@app.route('/api/release/complete', methods=['POST'])
def complete():
    data = request.json or {}
    by = data.get('completed_by', 'release-manager')
    board = _read_board()
    if not board:
        return jsonify({'error': 'No board'}), 404
    now = datetime.datetime.utcnow().isoformat()
    board['status'] = 'released'
    board['released_at'] = now
    board['released_by'] = by
    board['audit_trail'].append({'action': 'release', 'by': by, 'at': now})
    _write_board(board)
    return jsonify({'status': 'released'})

@app.route('/api/release/drift')
def drift():
    board = _read_board()
    if not board or not board.get('services'):
        return jsonify({'drift_items': []})
    import random
    items = []
    for name, svc in board['services'].items():
        nom_tag = svc.get('image_tag', '')
        # Simulate some drift
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
    return jsonify({'drift_items': items})

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
        'cutoff': board.get('cutoff'), 'status': board.get('status'),
        'services': [{'name': n, 'image': s.get('image',''), 'image_tag': s.get('image_tag',''),
                       'helm_chart': s.get('helm_version',''), 'nominated_by': s.get('nominated_by',''),
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


@app.route('/api/ai/release_notes', methods=['POST'])
def release_notes():
    board = _read_board()
    if not board or not board.get('services'):
        return jsonify({'error': 'No nominations'}), 400

    # Collect Jira details from mock data
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

    lines = [f"## Release Notes — {board.get('release_date', 'Unknown')}\n",
             f"**{len(board['services'])} services updated:**\n",
             "| Service | Version | Jira Tickets | Helm Chart | Notes |", "|---|---|---|---|---|"]
    for name, s in board['services'].items():
        jira_col = s.get('jira_ids', '') or '—'
        lines.append(f"| {name} | {s.get('image_tag','?')} | {jira_col} | {s.get('helm_version','N/A')} | {s.get('notes','')} |")

    # Add Jira changes detail if any
    if jira_details:
        lines.append("\n## What's Changed\n")
        features, bugs, improvements, tasks = [], [], [], []
        for svc_name, ids in svc_jira_map.items():
            for jid in ids:
                issue = jira_details.get(jid, {})
                entry = f"- **{jid}** ({svc_name}): {issue.get('summary', '?')} [{issue.get('status', '?')}]"
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

    lines.append(f"\n**AI Risk Assessment:** {'🟢 All services look healthy.' if len(board['services']) < 5 else '🟡 Review recommended for larger release scope.'}")
    return jsonify({'notes': '\n'.join(lines), 'gemini_powered': False,
                   'jira_enriched': bool(jira_details),
                   'release_date': board.get('release_date')})

@app.route('/api/release/history')
def history():
    releases = []
    for key, board in _board_store.items():
        releases.append({'release_date': key, 'status': board.get('status','unknown'),
                         'service_count': len(board.get('services',{})),
                         'finalized_by': board.get('finalized_by'), 'created_at': board.get('created_at')})
    releases.sort(key=lambda x: x.get('release_date',''), reverse=True)
    return jsonify({'releases': releases})


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


if __name__ == '__main__':
    port = int(os.getenv('PORT', 8090))
    print(f"\n🚀 Release Readiness Dashboard (MOCK MODE)")
    print(f"   http://localhost:{port}\n")
    app.run(host='0.0.0.0', port=port, debug=True)
