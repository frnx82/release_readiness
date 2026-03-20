"""
Release Readiness Dashboard — MOCK MODE
========================================
Runs locally without K8s or Gemini. Uses in-memory storage and fake data.
"""
import os, json, re, time, datetime, threading, yaml
from flask import Flask, request, jsonify, render_template
from flask_socketio import SocketIO

app = Flask(__name__)
app.secret_key = 'mock-secret'
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
    if not name:
        return jsonify({'error': 'service_name required'}), 400
    board = _read_board()
    if not board:
        board = _new_board()
    if board.get('status') in ('locked', 'released'):
        return jsonify({'error': 'Board is locked'}), 403
    svc = MOCK_SERVICE_MAP.get(name, {})
    image = svc.get('image', f'registry.example.com/{name}:unknown')
    tag = svc.get('image_tag', 'unknown')
    helm = svc.get('helm_version')
    now = datetime.datetime.utcnow().isoformat()

    existing = board['services'].get(name)
    if existing:
        old_tag = existing.get('image_tag', '')
        existing.update({'image': image, 'image_tag': tag, 'helm_version': helm,
                         'notes': notes, 'updated_at': now, 'updated_by': by})
        existing['version_history'].append({'from_tag': old_tag, 'to_tag': tag,
                                            'changed_by': by, 'changed_at': now, 'reason': notes or 'Update'})
        board['audit_trail'].append({'action': 're-nominate', 'service': name,
                                      'from_version': old_tag, 'to_version': tag, 'by': by, 'at': now})
    else:
        board['services'][name] = {
            'name': name, 'kind': svc.get('kind', 'Deployment'),
            'image': image, 'image_tag': tag, 'helm_version': helm,
            'nominated_by': by, 'nominated_at': now, 'updated_at': now, 'updated_by': by,
            'notes': notes, 'readiness': None, 'readiness_details': None,
            'version_history': [{'from_tag': None, 'to_tag': tag, 'changed_by': by,
                                 'changed_at': now, 'reason': 'Initial nomination'}]
        }
        board['audit_trail'].append({'action': 'nominate', 'service': name,
                                      'version': tag, 'by': by, 'at': now})
    _write_board(board)
    return jsonify({'status': 'ok', 'service': name, 'image_tag': tag})

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

@app.route('/api/ai/release_notes', methods=['POST'])
def release_notes():
    board = _read_board()
    if not board or not board.get('services'):
        return jsonify({'error': 'No nominations'}), 400
    lines = [f"## Release Notes — {board.get('release_date', 'Unknown')}\n",
             f"**{len(board['services'])} services updated:**\n",
             "| Service | Version | Helm Chart | Notes |", "|---|---|---|---|"]
    for name, s in board['services'].items():
        lines.append(f"| {name} | {s.get('image_tag','?')} | {s.get('helm_version','N/A')} | {s.get('notes','')} |")
    lines.append(f"\n**AI Risk Assessment:** {'🟢 All services look healthy.' if len(board['services']) < 5 else '🟡 Review recommended for larger release scope.'}")
    return jsonify({'notes': '\n'.join(lines), 'gemini_powered': False, 'release_date': board.get('release_date')})

@app.route('/api/release/history')
def history():
    releases = []
    for key, board in _board_store.items():
        releases.append({'release_date': key, 'status': board.get('status','unknown'),
                         'service_count': len(board.get('services',{})),
                         'finalized_by': board.get('finalized_by'), 'created_at': board.get('created_at')})
    releases.sort(key=lambda x: x.get('release_date',''), reverse=True)
    return jsonify({'releases': releases})


if __name__ == '__main__':
    port = int(os.getenv('PORT', 8090))
    print(f"\n🚀 Release Readiness Dashboard (MOCK MODE)")
    print(f"   http://localhost:{port}\n")
    app.run(host='0.0.0.0', port=port, debug=True)
