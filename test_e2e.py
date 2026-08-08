#!/usr/bin/env python3
"""
E2E Test Suite for Release Readiness Dashboard (Mock App)
Tests the full release lifecycle and all API endpoints.
Run with: python3 test_e2e.py
Requires: mock_app.py running on localhost:8090
"""

import requests
import json
import datetime
import os
import sys
import time

BASE = 'http://localhost:8090'
PASS = 0
FAIL = 0
WARN = 0
ERRORS = []

# ─── Helpers ──────────────────────────────────────────────────────────────────

def test(name, condition, detail=''):
    global PASS, FAIL
    if condition:
        PASS += 1
        print(f'  ✅ {name}')
    else:
        FAIL += 1
        msg = f'  ❌ {name}' + (f' — {detail}' if detail else '')
        print(msg)
        ERRORS.append(msg)

def warn(name, detail=''):
    global WARN
    WARN += 1
    print(f'  ⚠️  {name}' + (f' — {detail}' if detail else ''))

def section(title):
    print(f'\n{"═"*60}')
    print(f'  {title}')
    print(f'{"═"*60}')

# Use a session to persist cookies (e.g. GitHub login session)
SESSION = requests.Session()

def get(path):
    return SESSION.get(f'{BASE}{path}', timeout=10)

def post(path, data=None):
    return SESSION.post(f'{BASE}{path}', json=data or {}, timeout=10)

def delete(path, data=None):
    return SESSION.delete(f'{BASE}{path}', json=data or {}, timeout=10)

def ensure_clean_board():
    """Start a fresh, unlocked board for testing."""
    post('/api/release/new_cycle')
    r = get('/api/release/current')
    board = r.json()
    # If auto-locked (past cutoff), unlock it
    if board.get('status') == 'locked':
        post('/api/release/unlock', {'unlocked_by': 'e2e-setup'})


# ═══════════════════════════════════════════════════════════════════════════════
# TEST 1: Health & Connectivity
# ═══════════════════════════════════════════════════════════════════════════════

def test_health():
    section('1. Health & Connectivity')
    
    r = get('/api/ping')
    test('GET /api/ping returns 200', r.status_code == 200)
    test('Ping response is ok', 'ok' in r.text.lower())
    
    r = get('/')
    test('GET / returns 200 (HTML page)', r.status_code == 200)
    test('HTML contains Release Readiness', 'Release Readiness' in r.text)
    
    r = get('/api/auth/status')
    test('GET /api/auth/status returns 200', r.status_code == 200)


# ═══════════════════════════════════════════════════════════════════════════════
# TEST 2: Date & Cutoff Logic (CRITICAL — these were the bug fixes)
# ═══════════════════════════════════════════════════════════════════════════════

def test_dates():
    section('2. Date & Cutoff Logic')
    
    ensure_clean_board()
    r = get('/api/release/current')
    board = r.json()
    
    release_date = board.get('release_date', '')
    cutoff = board.get('cutoff', '')
    
    test('Board has release_date', bool(release_date), f'got: {release_date}')
    test('Board has cutoff', bool(cutoff), f'got: {cutoff}')
    
    # Release date should be a valid ISO date
    try:
        rd = datetime.date.fromisoformat(release_date)
        test('release_date is valid ISO date', True)
        # Release day should be Friday (weekday=4)
        test(f'Release day is Friday (got {rd.strftime("%A")})', rd.weekday() == 4,
             f'{release_date} is {rd.strftime("%A")}')
        # Release date should be in the future (or today if it's Friday)
        test('Release date is today or future', rd >= datetime.date.today(),
             f'release={rd}, today={datetime.date.today()}')
    except ValueError:
        test('release_date is valid ISO date', False, f'got: {release_date}')
    
    # Cutoff should be a valid ISO datetime
    try:
        ct = datetime.datetime.fromisoformat(cutoff)
        test('cutoff is valid ISO datetime', True)
        
        # Cutoff should be BEFORE release date
        cutoff_date = ct.date()
        test('Cutoff date is before release date', cutoff_date < rd,
             f'cutoff={cutoff_date}, release={rd}')
        
        # Cutoff day should be Wednesday (CUTOFF_DAY=2)
        cutoff_day_env = int(os.environ.get('CUTOFF_DAY', '2'))
        expected_day_names = {0: 'Monday', 1: 'Tuesday', 2: 'Wednesday', 3: 'Thursday', 4: 'Friday'}
        test(f'Cutoff day is {expected_day_names.get(cutoff_day_env, "?")} (CUTOFF_DAY={cutoff_day_env})',
             cutoff_date.weekday() == cutoff_day_env,
             f'got {cutoff_date.strftime("%A")} ({cutoff_date})')
        
        # CRITICAL: Cutoff hour should reflect UTC conversion
        # CUTOFF_HOUR=12 local, CUTOFF_TZ_OFFSET=-4 → 16:00 UTC
        tz_offset = int(os.environ.get('CUTOFF_TZ_OFFSET', '-4'))
        cutoff_hour = int(os.environ.get('CUTOFF_HOUR', '12'))
        expected_utc_hour = cutoff_hour - tz_offset
        test(f'Cutoff hour is {expected_utc_hour}:00 UTC (hour {cutoff_hour} + offset {tz_offset} → {expected_utc_hour} UTC)',
             ct.hour == expected_utc_hour,
             f'got {ct.hour}:00 — TIMEZONE BUG if this fails!')
        
    except ValueError:
        test('cutoff is valid ISO datetime', False, f'got: {cutoff}')
    
    # is_past_cutoff should be consistent with current time
    now_utc = datetime.datetime.utcnow()
    expected_past = now_utc.isoformat() > cutoff
    actual_past = board.get('is_past_cutoff', False)
    test('is_past_cutoff matches actual time comparison',
         actual_past == expected_past,
         f'expected={expected_past}, got={actual_past}')
    
    # Fix version format: P<YY>.<MM>.<DD>
    fv = board.get('fix_version', '')
    test('fix_version starts with P', fv.startswith('P'),
         f'got: {fv}')


# ═══════════════════════════════════════════════════════════════════════════════
# TEST 3: Full Release Lifecycle
# ═══════════════════════════════════════════════════════════════════════════════

def test_lifecycle():
    section('3. Release Lifecycle (Nominate → Lock → Unlock → Lock → Release → New Cycle)')
    
    # Start fresh
    ensure_clean_board()
    r = get('/api/release/current')
    board = r.json()
    initial_status = board.get('status')
    print(f'  ℹ️  Board status after reset: {initial_status}')
    
    # 3a. Nominate a K8s service
    print()
    print('  ── 3a. Nominate K8s Service ──')
    r = post('/api/release/nominate', {
        'service_name': 'auth-service',
        'notes': 'E2E test nomination',
        'nominated_by': 'e2e-tester'
    })
    test('Nominate K8s service returns 200', r.status_code == 200, f'status={r.status_code}')
    
    # Verify nomination appears in board
    r = get('/api/release/current')
    board = r.json()
    test('Nominated service appears in board',
         'auth-service' in board.get('services', {}))
    test('Service has correct metadata',
         board.get('services', {}).get('auth-service', {}).get('nominated_by') == 'e2e-tester')
    
    # 3b. Nominate a custom component
    print()
    print('  ── 3b. Nominate Custom Component ──')
    r = post('/api/release/nominate', {
        'service_name': 'ingestion-pipeline',
        'notes': 'Custom component test',
        'nominated_by': 'e2e-tester',
        'is_custom': True,
        'manual_version': 'v3.2.1'
    })
    test('Nominate custom component returns 200', r.status_code == 200)
    
    r = get('/api/release/current')
    board = r.json()
    test('Custom component appears in board',
         'ingestion-pipeline' in board.get('services', {}))
    test('nominated_count is 2', board.get('nominated_count') == 2,
         f'got: {board.get("nominated_count")}')
    
    # 3c. Duplicate nomination should UPSERT (returns 200 — designed behavior)
    print()
    print('  ── 3c. Duplicate Nomination (Upsert) ──')
    r = post('/api/release/nominate', {
        'service_name': 'auth-service',
        'notes': 'Updated nomination',
        'nominated_by': 'e2e-tester-v2'
    })
    test('Re-nomination upserts (returns 200)', r.status_code == 200)
    r = get('/api/release/current')
    board = r.json()
    auth = board.get('services', {}).get('auth-service', {})
    test('Re-nomination updates notes', auth.get('notes') == 'Updated nomination',
         f'got: {auth.get("notes")}')
    test('Count is still 2 (upserted, not added)', board.get('nominated_count') == 2,
         f'got: {board.get("nominated_count")}')
    
    # 3d. Empty service name should fail
    print()
    print('  ── 3d. Empty Service Name ──')
    r = post('/api/release/nominate', {'service_name': '', 'nominated_by': 'e2e-tester'})
    test('Empty service name returns 400', r.status_code == 400, f'status={r.status_code}')
    
    # 3e. Lock board (finalize)
    print()
    print('  ── 3e. Lock Board ──')
    r = post('/api/release/finalize', {'finalized_by': 'release-manager'})
    test('Lock board returns 200', r.status_code == 200,
         f'status={r.status_code}, body={r.text[:100]}')
    
    r = get('/api/release/current')
    board = r.json()
    test('Board status is locked', board.get('status') == 'locked',
         f'got: {board.get("status")}')
    
    # 3f. Double-lock should fail
    print()
    print('  ── 3f. Double Lock ──')
    r = post('/api/release/finalize', {'finalized_by': 'release-manager'})
    test('Double-lock returns 400', r.status_code == 400, f'status={r.status_code}')
    
    # 3g. Regular nomination should be blocked when locked
    print()
    print('  ── 3g. Nomination Blocked When Locked ──')
    r = post('/api/release/nominate', {
        'service_name': 'blocked-service',
        'nominated_by': 'e2e-tester',
        'is_exception': False
    })
    test('Regular nomination blocked when locked (403)', r.status_code == 403,
         f'status={r.status_code}')
    
    # 3h. Exception nomination should work when locked
    print()
    print('  ── 3h. Exception Nomination When Locked ──')
    r = post('/api/release/nominate', {
        'service_name': 'hotfix-service',
        'nominated_by': 'e2e-tester',
        'is_exception': True,
        'exception_reason': 'Critical hotfix',
        'exception_approver': 'vp-engineering'
    })
    test('Exception nomination returns 200 when locked', r.status_code == 200,
         f'status={r.status_code}')
    
    r = get('/api/release/current')
    board = r.json()
    test('Exception service appears in board',
         'hotfix-service' in board.get('services', {}))
    hotfix = board.get('services', {}).get('hotfix-service', {})
    test('Exception flag is set', hotfix.get('is_exception') == True)
    test('Exception approver is recorded', hotfix.get('exception_approver') == 'vp-engineering')
    
    # 3i. Unlock board
    print()
    print('  ── 3i. Unlock Board ──')
    r = post('/api/release/unlock', {'unlocked_by': 'release-manager'})
    test('Unlock returns 200', r.status_code == 200, f'status={r.status_code}')
    
    r = get('/api/release/current')
    board = r.json()
    test('Board status is open after unlock', board.get('status') == 'open',
         f'got: {board.get("status")}')
    
    # 3j. Remove a nomination
    print()
    print('  ── 3j. Remove Nomination ──')
    r = delete('/api/release/remove', {'service_name': 'hotfix-service', 'removed_by': 'e2e-tester'})
    test('Remove nomination returns 200', r.status_code == 200, f'status={r.status_code}')
    
    r = get('/api/release/current')
    board = r.json()
    test('Removed service no longer in board',
         'hotfix-service' not in board.get('services', {}))
    
    # 3k. Rollback a nomination (correct param: target_tag)
    print()
    print('  ── 3k. Rollback Nomination ──')
    # First get current tag
    auth = board.get('services', {}).get('auth-service', {})
    current_tag = auth.get('image_tag', '')
    rollback_tag = 'v1.0.0-rollback'
    r = post('/api/release/rollback', {
        'service_name': 'auth-service',
        'target_tag': rollback_tag,
        'rolled_back_by': 'e2e-tester'
    })
    test('Rollback returns 200', r.status_code == 200, f'status={r.status_code}')
    
    r = get('/api/release/current')
    board = r.json()
    auth_svc = board.get('services', {}).get('auth-service', {})
    test('Rollback updates image_tag', auth_svc.get('image_tag') == rollback_tag,
         f'got: {auth_svc.get("image_tag")}')
    test('Version history records rollback',
         len(auth_svc.get('version_history', [])) >= 2,
         f'history count: {len(auth_svc.get("version_history", []))}')
    
    # 3l. Mark Released
    print()
    print('  ── 3l. Mark Released ──')
    r = post('/api/release/complete', {'released_by': 'release-manager'})
    test('Complete release returns 200', r.status_code == 200, f'status={r.status_code}')
    
    r = get('/api/release/current')
    board = r.json()
    test('Board status is released', board.get('status') == 'released',
         f'got: {board.get("status")}')
    
    # 3m. Nomination on released board should fail
    print()
    print('  ── 3m. Nomination After Release ──')
    r = post('/api/release/nominate', {
        'service_name': 'post-release-svc',
        'nominated_by': 'e2e-tester'
    })
    test('Nomination on released board returns 403', r.status_code == 403,
         f'status={r.status_code}')
    
    # 3n. Lock on released board should fail
    r = post('/api/release/finalize', {'finalized_by': 'test'})
    test('Lock on released board returns 400', r.status_code == 400,
         f'status={r.status_code}')
    
    # 3o. Unlock on released board should fail
    r = post('/api/release/unlock', {'unlocked_by': 'test'})
    test('Unlock released board returns 400', r.status_code == 400,
         f'status={r.status_code}')
    
    # 3p. Start new cycle
    print()
    print('  ── 3p. Start New Cycle ──')
    r = post('/api/release/new_cycle')
    test('New cycle returns 200', r.status_code == 200, f'status={r.status_code}')
    
    r = get('/api/release/current')
    board = r.json()
    test('New board has 0 services',
         len(board.get('services', {})) == 0,
         f'got: {len(board.get("services", {}))}')


# ═══════════════════════════════════════════════════════════════════════════════
# TEST 4: Audit Trail
# ═══════════════════════════════════════════════════════════════════════════════

def test_audit():
    section('4. Audit Trail')
    
    # Set up a board with some actions
    ensure_clean_board()
    post('/api/release/nominate', {
        'service_name': 'audit-test-svc',
        'nominated_by': 'e2e-tester'
    })
    
    r = get('/api/release/current')
    board = r.json()
    trail = board.get('audit_trail', [])
    
    test('Audit trail is a list', isinstance(trail, list))
    test('Audit trail has entries after nomination', len(trail) > 0, f'count: {len(trail)}')
    
    if trail:
        entry = trail[-1]
        test('Audit entry has action', 'action' in entry)
        test('Audit entry has timestamp (at)', 'at' in entry)
        test('Audit entry has actor (by)', 'by' in entry)
        test('Last audit action is nominate', entry.get('action') == 'nominate',
             f'got: {entry.get("action")}')


# ═══════════════════════════════════════════════════════════════════════════════
# TEST 5: Services & Drift
# ═══════════════════════════════════════════════════════════════════════════════

def test_services():
    section('5. Services & Drift')
    
    # UAT services
    r = get('/api/services')
    test('GET /api/services returns 200', r.status_code == 200)
    data = r.json()
    test('Services response has items', 'items' in data or 'services' in data,
         f'keys: {list(data.keys())}')
    
    # Prod services
    r = get('/api/prod/services')
    test('GET /api/prod/services returns 200', r.status_code == 200)
    
    # Custom components
    r = get('/api/custom_components')
    test('GET /api/custom_components returns 200', r.status_code == 200)
    data = r.json()
    test('Custom components has list', 'components' in data)
    test('Custom components count > 0', data.get('count', 0) > 0,
         f'count: {data.get("count")}')
    
    # Drift
    r = get('/api/release/drift')
    test('GET /api/release/drift returns 200', r.status_code == 200)
    data = r.json()
    test('Drift response has drift_items', 'drift_items' in data,
         f'keys: {list(data.keys())}')


# ═══════════════════════════════════════════════════════════════════════════════
# TEST 6: Fix Version & Jira
# ═══════════════════════════════════════════════════════════════════════════════

def test_jira():
    section('6. Fix Version & Jira')
    
    ensure_clean_board()
    r = get('/api/release/current')
    fv = r.json().get('fix_version', '')
    
    # Update fix version
    r = post('/api/release/fix_version', {'fix_version': 'P26.08.14-test'})
    test('Update fix version returns 200', r.status_code == 200, f'status={r.status_code}')
    
    # Verify fix version was updated
    r = get('/api/release/current')
    test('Fix version updated', r.json().get('fix_version') == 'P26.08.14-test',
         f'got: {r.json().get("fix_version")}')
    
    # Query Jira by fix version
    r = post('/api/release/jira_by_fix_version', {'fix_version': fv})
    test('Jira by fix_version returns 200', r.status_code == 200, f'status={r.status_code}')
    data = r.json()
    test('Jira response has issues', 'issues' in data, f'keys: {list(data.keys())}')
    
    # Jira issues search (needs jira_ids, not jql)
    r = post('/api/jira/issues', {'jira_ids': 'TEST-123,TEST-456'})
    test('Jira issues search returns 200', r.status_code == 200, f'status={r.status_code}')


# ═══════════════════════════════════════════════════════════════════════════════
# TEST 7: Export
# ═══════════════════════════════════════════════════════════════════════════════

def test_export():
    section('7. Export')
    
    # Ensure board has nominated services
    ensure_clean_board()
    post('/api/release/nominate', {
        'service_name': 'export-test-svc',
        'nominated_by': 'e2e-tester'
    })
    
    # JSON export
    r = get('/api/release/export')
    test('Export returns 200', r.status_code == 200)
    data = r.json()
    test('Export has release key', 'release' in data,
         f'keys: {list(data.keys())}')
    release = data.get('release', {})
    test('Export release has services', 'services' in release,
         f'release keys: {list(release.keys())}' if isinstance(release, dict) else 'not a dict')
    
    # YAML export
    r = get('/api/release/export?format=yaml')
    test('YAML export returns 200', r.status_code == 200)


# ═══════════════════════════════════════════════════════════════════════════════
# TEST 8: History & Exceptions
# ═══════════════════════════════════════════════════════════════════════════════

def test_history():
    section('8. History & Exceptions')
    
    r = get('/api/release/history')
    test('GET /api/release/history returns 200', r.status_code == 200)
    data = r.json()
    test('History response is list or has releases',
         isinstance(data, list) or 'releases' in data or 'history' in data)
    
    r = get('/api/release/exceptions')
    test('GET /api/release/exceptions returns 200', r.status_code == 200)


# ═══════════════════════════════════════════════════════════════════════════════
# TEST 9: AI Readiness
# ═══════════════════════════════════════════════════════════════════════════════

def test_ai():
    section('9. AI Readiness & Chat')
    
    r = post('/api/ai/release_readiness')
    test('AI readiness check returns 200', r.status_code == 200, f'status={r.status_code}')
    data = r.json()
    test('AI readiness has summary or assessment',
         any(k in data for k in ['summary', 'assessment', 'status', 'readiness']),
         f'keys: {list(data.keys())}')
    
    # AI chat
    r = post('/api/ai/converse', {'message': 'What is the current release status?'})
    test('AI converse returns 200', r.status_code == 200, f'status={r.status_code}')
    data = r.json()
    test('AI response has reply', 'reply' in data or 'response' in data or 'message' in data,
         f'keys: {list(data.keys())}')
    
    # Reset chat
    r = post('/api/ai/converse/reset')
    test('AI chat reset returns 200', r.status_code == 200)


# ═══════════════════════════════════════════════════════════════════════════════
# TEST 10: Confluence
# ═══════════════════════════════════════════════════════════════════════════════

def test_confluence():
    section('10. Confluence Integration')
    
    r = get('/api/confluence/status')
    test('Confluence status returns 200', r.status_code == 200)
    
    r = post('/api/confluence/search', {'query': 'release notes'})
    test('Confluence search returns 200', r.status_code == 200)
    data = r.json()
    test('Search results have pages', 'results' in data or 'pages' in data,
         f'keys: {list(data.keys())}')
    
    # Get specific page
    r = get('/api/confluence/page/10001')
    test('Confluence page returns 200', r.status_code == 200)
    data = r.json()
    test('Page has content', 'title' in data or 'body' in data or 'content' in data,
         f'keys: {list(data.keys())}')
    
    # Labels
    r = post('/api/confluence/labels', {'page_id': '10001', 'labels': ['test-label']})
    test('Confluence labels returns 200', r.status_code == 200)


# ═══════════════════════════════════════════════════════════════════════════════
# TEST 11: GitHub Auth
# ═══════════════════════════════════════════════════════════════════════════════

def test_github():
    section('11. GitHub Auth')
    
    r = get('/api/github/status')
    test('GitHub status returns 200', r.status_code == 200)
    
    # Login
    r = post('/api/github/login', {'code': 'test-code'})
    test('GitHub login returns 200', r.status_code == 200)
    
    # Check status after login
    r = get('/api/github/status')
    data = r.json()
    test('GitHub shows authenticated after login',
         data.get('logged_in') == True,
         f'data: {data}')
    
    # Logout
    r = post('/api/github/logout')
    test('GitHub logout returns 200', r.status_code == 200)


# ═══════════════════════════════════════════════════════════════════════════════
# TEST 12: Deploy (requires GitHub login)
# ═══════════════════════════════════════════════════════════════════════════════

def test_deploy():
    section('12. Deploy Workflows')
    
    r = get('/api/deploy/workflows')
    test('Deploy workflows returns 200', r.status_code == 200)
    data = r.json()
    test('Workflows response has data', 'workflows' in data,
         f'keys: {list(data.keys())}')
    
    r = get('/api/deploy/history')
    test('Deploy history returns 200', r.status_code == 200)
    
    # Login first (deploy requires auth)
    post('/api/github/login', {'code': 'test-code'})
    
    # Trigger deploy
    r = post('/api/deploy/trigger', {
        'workflow_id': 'deploy-uat',
        'service': 'auth-service',
        'ref': 'main'
    })
    test('Deploy trigger returns 200 (after login)', r.status_code == 200,
         f'status={r.status_code}, body={r.text[:100]}')
    data = r.json()
    run_id = data.get('run_id', '')
    
    if run_id:
        test('Deploy response has run_id', True)
        r = get(f'/api/deploy/status/{run_id}')
        test('Deploy status returns 200', r.status_code == 200)
    
    # Cleanup
    post('/api/github/logout')


# ═══════════════════════════════════════════════════════════════════════════════
# TEST 13: QA Preparation
# ═══════════════════════════════════════════════════════════════════════════════

def test_qa():
    section('13. QA Preparation')
    
    # Set up: need nominated services
    ensure_clean_board()
    post('/api/release/nominate', {
        'service_name': 'qa-test-svc',
        'nominated_by': 'e2e-tester'
    })
    
    r = post('/api/qa/prepare')
    test('QA prepare returns 200', r.status_code == 200, f'status={r.status_code}')
    
    r = get('/api/qa/prepare/status')
    test('QA prepare status returns 200', r.status_code == 200)
    
    r = get('/api/qa/env/services')
    test('QA env services returns 200', r.status_code == 200)
    
    r = post('/api/qa/drift-check')
    test('QA drift-check returns 200', r.status_code == 200)
    
    r = post('/api/qa/prepare-prod', {'change_ticket': 'CHG-12345'})
    # This may return 400 if QA state hasn't reached 'e2e_pushed' — that's valid behavior
    test('QA prepare-prod returns 200 or 400 (state check)',
         r.status_code in (200, 400), f'status={r.status_code}')


# ═══════════════════════════════════════════════════════════════════════════════
# TEST 14: Release Notes (AI)
# ═══════════════════════════════════════════════════════════════════════════════

def test_release_notes():
    section('14. AI Release Notes')
    
    r = post('/api/ai/release_notes')
    test('AI release notes returns 200', r.status_code == 200, f'status={r.status_code}')
    data = r.json()
    
    job_id = data.get('job_id', '')
    if job_id:
        test('Release notes response has job_id', True)
        r = get(f'/api/ai/release_notes/{job_id}')
        test('Release notes poll returns 200', r.status_code == 200)
    else:
        test('Release notes has content', 'notes' in data or 'markdown' in data or 'content' in data,
             f'keys: {list(data.keys())}')


# ═══════════════════════════════════════════════════════════════════════════════
# TEST 15: Artifactory
# ═══════════════════════════════════════════════════════════════════════════════

def test_artifactory():
    section('15. Artifactory Versions')
    
    # Use a known component name from MOCK_CUSTOM_COMPONENTS
    r = get('/api/artifactory/versions/ingestion-pipeline')
    test('Artifactory versions returns 200', r.status_code == 200, f'status={r.status_code}')
    data = r.json()
    test('Versions response has versions list', 'versions' in data,
         f'keys: {list(data.keys())}')
    test('At least one version returned', len(data.get('versions', [])) > 0,
         f'count: {len(data.get("versions", []))}')
    
    # Unknown component should 404
    r = get('/api/artifactory/versions/unknown-component')
    test('Unknown component returns 404', r.status_code == 404)


# ═══════════════════════════════════════════════════════════════════════════════
# TEST 16: Edge Cases
# ═══════════════════════════════════════════════════════════════════════════════

def test_edge_cases():
    section('16. Edge Cases')
    
    # Nonexistent API
    r = get('/api/nonexistent')
    test('Nonexistent endpoint returns 404', r.status_code == 404)
    
    # Remove nonexistent service
    ensure_clean_board()
    r = delete('/api/release/remove', {'service_name': 'does-not-exist', 'removed_by': 'test'})
    test('Remove nonexistent service returns 404', r.status_code == 404,
         f'status={r.status_code}')
    
    # Rollback nonexistent service (returns 400 for missing fields or 404)
    r = post('/api/release/rollback', {
        'service_name': 'does-not-exist',
        'target_tag': 'v1.0.0',
        'rolled_back_by': 'test'
    })
    test('Rollback nonexistent service returns 404', r.status_code == 404,
         f'status={r.status_code}')
    
    # Unlock already-open board
    ensure_clean_board()
    r = post('/api/release/unlock', {'unlocked_by': 'test'})
    test('Unlock open board returns 400', r.status_code == 400,
         f'status={r.status_code}')
    
    # Complete release on open board should work (marks it released)
    ensure_clean_board()
    post('/api/release/nominate', {'service_name': 'edge-svc', 'nominated_by': 'test'})
    r = post('/api/release/complete', {'released_by': 'test'})
    test('Release from open board returns 200', r.status_code == 200,
         f'status={r.status_code}')


# ═══════════════════════════════════════════════════════════════════════════════
# MAIN
# ═══════════════════════════════════════════════════════════════════════════════

if __name__ == '__main__':
    print()
    print('🚀 Release Readiness Dashboard — E2E Test Suite')
    print(f'   Target: {BASE}')
    print(f'   Time:   {datetime.datetime.now().strftime("%Y-%m-%d %H:%M:%S")}')
    print(f'   UTC:    {datetime.datetime.utcnow().strftime("%Y-%m-%d %H:%M:%S")}')
    
    # Check server is up
    try:
        r = requests.get(f'{BASE}/api/ping', timeout=3)
        if r.status_code != 200:
            print(f'\n❌ Server at {BASE} returned {r.status_code}. Is mock_app.py running?')
            sys.exit(1)
    except requests.ConnectionError:
        print(f'\n❌ Cannot connect to {BASE}. Start mock_app.py first.')
        sys.exit(1)
    
    # Run all test suites
    suites = [
        test_health, test_dates, test_lifecycle, test_audit,
        test_services, test_jira, test_export, test_history,
        test_ai, test_confluence, test_github, test_deploy,
        test_qa, test_release_notes, test_artifactory, test_edge_cases
    ]
    for suite in suites:
        try:
            suite()
        except Exception as e:
            FAIL += 1
            msg = f'  💥 {suite.__name__} CRASHED: {e}'
            print(msg)
            ERRORS.append(msg)
    
    # Summary
    section('RESULTS')
    total = PASS + FAIL
    print(f'  ✅ Passed: {PASS}/{total}')
    print(f'  ❌ Failed: {FAIL}/{total}')
    if WARN:
        print(f'  ⚠️  Warnings: {WARN}')
    
    if ERRORS:
        print(f'\n  ── Failed Tests ──')
        for e in ERRORS:
            print(e)
    
    print()
    if FAIL == 0:
        print('  🎉 ALL TESTS PASSED!')
    else:
        print(f'  ⚠️  {FAIL} test(s) failed — see above for details')
    
    print()
    sys.exit(0 if FAIL == 0 else 1)
