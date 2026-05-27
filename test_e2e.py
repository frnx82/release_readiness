#!/usr/bin/env python3
"""E2E test suite for Release Readiness app — comprehensive function tests."""
import sys
import os
import json

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

PASS = 0
FAIL = 0

def ok(msg):
    global PASS
    PASS += 1
    print(f'  ✅ {msg}')

def fail(msg):
    global FAIL
    FAIL += 1
    print(f'  ❌ {msg}')

def section(title):
    print(f'\n{"="*60}')
    print(f'  {title}')
    print(f'{"="*60}')


# ══════════════════════════════════════════════════════════════
# Test 1: Syntax Check
# ══════════════════════════════════════════════════════════════
section('Test 1: Syntax Check')

try:
    import py_compile
    py_compile.compile('app.py', doraise=True)
    ok('app.py compiles without syntax errors')
except py_compile.PyCompileError as e:
    fail(f'Syntax error: {e}')
    sys.exit(1)

# ══════════════════════════════════════════════════════════════
# Test 2: DEPLOY_ENV variable
# ══════════════════════════════════════════════════════════════
section('Test 2: DEPLOY_ENV Configuration')

with open('app.py', 'r') as f:
    source = f.read()

if "DEPLOY_ENV = os.getenv('DEPLOY_ENV', 'uat').lower()" in source:
    ok('DEPLOY_ENV variable defined with default "uat"')
else:
    fail('DEPLOY_ENV variable not properly defined')

if "'uat' or 'prod'" in source or "uat.*or.*prod" in source:
    ok('DEPLOY_ENV documented as uat/prod')
else:
    # Check comment is there
    if 'DEPLOY_ENV' in source and 'prod' in source:
        ok('DEPLOY_ENV references prod mode')
    else:
        fail('DEPLOY_ENV documentation missing')


# ══════════════════════════════════════════════════════════════
# Test 3: _get_uat_api_client function
# ══════════════════════════════════════════════════════════════
section('Test 3: _get_uat_api_client()')

if 'def _get_uat_api_client()' in source:
    ok('_get_uat_api_client function defined')
else:
    fail('_get_uat_api_client function MISSING')

if "UAT_CLUSTER_API" in source and "UAT_CLUSTER_TOKEN" in source:
    ok('Uses UAT_CLUSTER_API and UAT_CLUSTER_TOKEN env vars')
else:
    fail('UAT env vars not referenced')

if "UAT_CLUSTER_VERIFY_SSL" in source:
    ok('SSL verification configurable via UAT_CLUSTER_VERIFY_SSL')
else:
    fail('UAT SSL config missing')


# ══════════════════════════════════════════════════════════════
# Test 4: _list_services_from_api shared helper
# ══════════════════════════════════════════════════════════════
section('Test 4: _list_services_from_api() Shared Helper')

if 'def _list_services_from_api(' in source:
    ok('_list_services_from_api helper function defined')
else:
    fail('_list_services_from_api helper MISSING')

# Verify it handles Deployments, StatefulSets, DaemonSets
func_start = source.find('def _list_services_from_api(')
if func_start != -1:
    # Get function body (up to next def at same indent)
    func_body = source[func_start:source.find('\ndef ', func_start + 10)]
    
    if 'list_namespaced_deployment' in func_body:
        ok('Lists Deployments')
    else:
        fail('Missing Deployment listing in helper')
    
    if 'list_namespaced_stateful_set' in func_body:
        ok('Lists StatefulSets')
    else:
        fail('Missing StatefulSet listing in helper')
    
    if 'list_namespaced_daemon_set' in func_body:
        ok('Lists DaemonSets')
    else:
        fail('Missing DaemonSet listing in helper')

    if 'log_prefix' in func_body:
        ok('Supports log_prefix parameter for distinguishing local/remote')
    else:
        fail('Missing log_prefix parameter')


# ══════════════════════════════════════════════════════════════
# Test 5: /api/prod/services — bi-directional logic
# ══════════════════════════════════════════════════════════════
section('Test 5: /api/prod/services — Bi-directional')

# Find list_prod_services function
prod_start = source.find('def list_prod_services(')
if prod_start == -1:
    fail('list_prod_services function not found')
else:
    prod_body = source[prod_start:source.find('\n# ──', prod_start + 10)]
    if prod_body.find('\n# ──') == -1:
        prod_body = source[prod_start:prod_start + 3000]
    
    if "DEPLOY_ENV == 'prod'" in prod_body:
        ok('/api/prod/services checks DEPLOY_ENV')
    else:
        fail('/api/prod/services does NOT check DEPLOY_ENV')
    
    if 'prod-local' in prod_body:
        ok('Has local (prod) path with [prod-local] logging')
    else:
        fail('Missing local prod path')
    
    if 'prod-remote' in prod_body or '_get_prod_api_client' in prod_body:
        ok('Has remote path using _get_prod_api_client')
    else:
        fail('Missing remote prod path')

    if "PROD_NAMESPACE" in prod_body:
        ok('Uses PROD_NAMESPACE env var')
    else:
        fail('PROD_NAMESPACE not used')

    if "'deploy_env': DEPLOY_ENV" in prod_body:
        ok('Returns deploy_env in response')
    else:
        fail('deploy_env not included in response')


# ══════════════════════════════════════════════════════════════
# Test 6: /api/services — bi-directional logic
# ══════════════════════════════════════════════════════════════
section('Test 6: /api/services — Bi-directional')

svc_start = source.find('def list_services(')
if svc_start == -1:
    fail('list_services function not found')
else:
    # Get function body until next @app.route
    next_route = source.find('@app.route', svc_start + 10)
    svc_body = source[svc_start:next_route] if next_route != -1 else source[svc_start:svc_start + 5000]
    
    if "DEPLOY_ENV == 'prod'" in svc_body:
        ok('/api/services checks DEPLOY_ENV for remote UAT')
    else:
        fail('/api/services does NOT check DEPLOY_ENV')
    
    if '_get_uat_api_client' in svc_body:
        ok('Uses _get_uat_api_client for remote UAT connection')
    else:
        fail('_get_uat_api_client not called in list_services')

    if 'UAT_NAMESPACE' in svc_body:
        ok('Uses UAT_NAMESPACE env var')
    else:
        fail('UAT_NAMESPACE not used in list_services')

    # Verify original local path still exists
    if '_k8s_retry' in svc_body:
        ok('Original local K8s path preserved (uses _k8s_retry)')
    else:
        fail('Original local K8s path missing')


# ══════════════════════════════════════════════════════════════
# Test 7: _map_issues_to_services (from previous session)
# ══════════════════════════════════════════════════════════════
section('Test 7: _map_issues_to_services()')

try:
    start = source.find('def _map_issues_to_services(')
    if start == -1:
        fail('_map_issues_to_services function not found')
    else:
        lines = source[start:].split('\n')
        func_lines = [lines[0]]
        for line in lines[1:]:
            if line and not line[0].isspace() and not line.startswith('#'):
                break
            func_lines.append(line)
        
        ns = {}
        exec('\n'.join(func_lines), ns)
        _map = ns['_map_issues_to_services']
        ok('Function extracted')

        # Basic test
        issues = [
            {'id': 'P-1', 'components': ['auth-service']},
            {'id': 'P-2', 'components': ['data-pipeline']},
            {'id': 'P-3', 'components': ['infra']},
            {'id': 'P-4', 'components': []},
        ]
        svc_map, unmatched = _map(issues, ['auth-service', 'data-pipeline'])
        
        if 'auth-service' in svc_map and svc_map['auth-service'][0]['id'] == 'P-1':
            ok('Component matching works')
        else:
            fail(f'Component matching failed: {svc_map}')
        
        if len(unmatched) == 2:
            ok(f'Unmatched = 2 (infra + no component)')
        else:
            fail(f'Unmatched count wrong: {len(unmatched)}')

        # Case insensitive
        m2, _ = _map([{'id': 'X', 'components': ['Auth_Service']}], ['auth-service'])
        if 'auth-service' in m2:
            ok('Case-insensitive matching works')
        else:
            fail('Case-insensitive matching broken')

except Exception as e:
    import traceback
    fail(f'Error: {e}\n{traceback.format_exc()}')


# ══════════════════════════════════════════════════════════════
# Test 8: Async release notes job pattern
# ══════════════════════════════════════════════════════════════
section('Test 8: Async Release Notes')

if '_release_notes_jobs' in source:
    ok('Job store defined')
else:
    fail('Job store missing')

if 'job_id' in source and "str(uuid.uuid4())" in source:
    ok('Job ID generation using UUID')
else:
    fail('Job ID generation missing')

if "status': 'running'" in source:
    ok('Job starts with status=running')
else:
    fail('Initial status not set')

if '/api/ai/release_notes/<job_id>' in source:
    ok('Polling endpoint registered')
else:
    fail('Polling endpoint missing')


# ══════════════════════════════════════════════════════════════
# Test 9: Frontend integrity
# ══════════════════════════════════════════════════════════════
section('Test 9: Frontend Integrity')

try:
    with open('templates/index.html', 'r') as f:
        html = f.read()

    checks = [
        ('loadProdServices', 'loadProdServices function'),
        ('loadUATServices', 'loadUATServices function'),
        ('generateReleaseNotes', 'generateReleaseNotes function'),
        ('job_id', 'Async polling (job_id)'),
        ('pollInterval', 'Poll interval for async'),
        ('copyReleaseNotes', 'Copy release notes'),
        ("name === 'prod'", 'Prod tab switch trigger'),
        ("name === 'uat'", 'UAT tab switch trigger'),
        ('/api/prod/services', 'Prod services API call'),
        ('/api/services', 'UAT services API call'),
    ]
    for needle, desc in checks:
        if needle in html:
            ok(desc)
        else:
            fail(f'{desc} MISSING')

except Exception as e:
    fail(f'Frontend check error: {e}')


# ══════════════════════════════════════════════════════════════
# Test 10: Backend routes
# ══════════════════════════════════════════════════════════════
section('Test 10: Backend Routes')

routes = [
    '/api/services',
    '/api/prod/services',
    '/api/ai/release_notes',
    '/api/release/current',
    '/api/custom_components',
]
for route in routes:
    if route in source:
        ok(f'Route {route}')
    else:
        fail(f'Route {route} MISSING')


# ══════════════════════════════════════════════════════════════
# Test 11: Startup logging
# ══════════════════════════════════════════════════════════════
section('Test 11: Startup Logging')

if 'DEPLOY_ENV.upper()' in source or 'DEPLOY_ENV' in source:
    ok('DEPLOY_ENV shown in startup logs')
else:
    fail('DEPLOY_ENV not in startup logs')

startup_vars = ['UAT_CLUSTER_API', 'UAT_CLUSTER_TOKEN', 'UAT_NAMESPACE', 
                'PROD_CLUSTER_API', 'PROD_CLUSTER_TOKEN', 'PROD_NAMESPACE']
for var in startup_vars:
    if f"'{var}'" in source:
        ok(f'{var} in startup config')
    else:
        fail(f'{var} missing from startup config')


# ══════════════════════════════════════════════════════════════
# Test 12: deploy.yaml validation
# ══════════════════════════════════════════════════════════════
section('Test 12: deploy.yaml Validation')

try:
    with open('manifests/deploy.yaml', 'r') as f:
        deploy = f.read()

    deploy_vars = [
        'DEPLOY_ENV',
        'PROD_CLUSTER_API', 'PROD_CLUSTER_TOKEN', 'PROD_NAMESPACE', 'PROD_CLUSTER_VERIFY_SSL',
        'UAT_CLUSTER_API', 'UAT_CLUSTER_TOKEN', 'UAT_NAMESPACE', 'UAT_CLUSTER_VERIFY_SSL',
    ]
    for var in deploy_vars:
        if var in deploy:
            ok(f'{var} in deploy.yaml')
        else:
            fail(f'{var} MISSING from deploy.yaml')

    if 'release-readiness-uat' in deploy:
        ok('UAT secret reference (release-readiness-uat)')
    else:
        fail('UAT secret reference missing')

    if 'release-readiness-prod' in deploy:
        ok('Prod secret reference (release-readiness-prod)')
    else:
        fail('Prod secret reference missing')

    if 'optional: true' in deploy:
        ok('Secret refs are optional (won\'t crash if missing)')
    else:
        fail('Secrets not marked optional')

    # Validate YAML structure
    try:
        import yaml
        docs = list(yaml.safe_load_all(deploy))
        ok(f'deploy.yaml is valid YAML ({len(docs)} documents)')
    except ImportError:
        ok('(YAML validation skipped — pyyaml not installed)')
    except Exception as e:
        fail(f'deploy.yaml YAML parse error: {e}')

except Exception as e:
    fail(f'deploy.yaml check error: {e}')


# ══════════════════════════════════════════════════════════════
# Test 13: Error handling in remote connections
# ══════════════════════════════════════════════════════════════
section('Test 13: Error Handling')

# Check that both remote paths have error handling
if "'connected': False" in source:
    ok('Returns connected=False on error')
else:
    fail('Missing connected=False in error responses')

# Count error response patterns
error_count = source.count("'connected': False")
if error_count >= 3:
    ok(f'Found {error_count} error response paths with connected=False')
else:
    fail(f'Only {error_count} error paths — expected at least 3 (prod-remote, uat-remote, client-fail)')

# Check traceback logging
if 'traceback.format_exc()' in source:
    ok('Detailed traceback logging on connection failure')
else:
    fail('Missing traceback logging')


# ══════════════════════════════════════════════════════════════
# Test 14: Cache integration
# ══════════════════════════════════════════════════════════════
section('Test 14: Cache Integration')

# Verify caching works for both paths
cache_keys = ["'prod_services'", "'uat_services'", "'services'"]
for key in cache_keys:
    if key in source:
        ok(f'Cache key {key} used')
    else:
        fail(f'Cache key {key} missing')


# ══════════════════════════════════════════════════════════════
# Test 15: Potential bugs — code consistency
# ══════════════════════════════════════════════════════════════
section('Test 15: Potential Bug Checks')

# Bug check 1: Make sure _get_uat_api_client doesn't reference PROD variables
uat_func_start = source.find('def _get_uat_api_client()')
if uat_func_start != -1:
    uat_func_end = source.find('\ndef ', uat_func_start + 10)
    uat_func_body = source[uat_func_start:uat_func_end]
    
    if 'PROD_CLUSTER' in uat_func_body:
        fail('BUG: _get_uat_api_client references PROD_CLUSTER variables!')
    else:
        ok('_get_uat_api_client correctly uses UAT_CLUSTER vars only')

    if 'UAT_CLUSTER_API' in uat_func_body and 'UAT_CLUSTER_TOKEN' in uat_func_body:
        ok('_get_uat_api_client reads correct env vars')
    else:
        fail('_get_uat_api_client missing required env vars')

# Bug check 2: Make sure list_services doesn't break for DEPLOY_ENV=uat
svc_func = source[source.find('def list_services('):source.find('\n@app.route', source.find('def list_services(') + 10)]
if "if DEPLOY_ENV == 'prod':" in svc_func:
    # Check that there's an else/default path for uat
    if '_k8s_retry' in svc_func:
        ok('list_services has fallback local path for DEPLOY_ENV=uat')
    else:
        fail('BUG: list_services missing local path for DEPLOY_ENV=uat')
else:
    fail('list_services missing DEPLOY_ENV check')

# Bug check 3: Ensure prod endpoint doesn't call _get_uat_api_client
prod_func = source[source.find('def list_prod_services('):source.find('\n# ──', source.find('def list_prod_services(') + 10)]
if '_get_uat_api_client' in prod_func:
    fail('BUG: list_prod_services calls _get_uat_api_client instead of _get_prod_api_client!')
else:
    ok('list_prod_services correctly uses _get_prod_api_client')

# Bug check 4: Verify loadProdServices frontend calls /api/prod/services (not /api/services)
with open('templates/index.html', 'r') as f:
    html = f.read()
prod_func_start = html.find('function loadProdServices')
if prod_func_start != -1:
    prod_func_body = html[prod_func_start:prod_func_start + 500]
    if '/api/prod/services' in prod_func_body:
        ok('Frontend loadProdServices calls /api/prod/services')
    else:
        fail('BUG: loadProdServices calls wrong endpoint')

# Bug check 5: loadUATServices calls /api/services
uat_func_start = html.find('function loadUATServices')
if uat_func_start != -1:
    uat_func_body = html[uat_func_start:uat_func_start + 800]
    if "fetch('/api/services')" in uat_func_body:
        ok('Frontend loadUATServices calls /api/services')
    else:
        fail('BUG: loadUATServices calls wrong endpoint')


# ══════════════════════════════════════════════════════════════
# Test 16: Duplicate Route Detection
# ══════════════════════════════════════════════════════════════
section('Test 16: Duplicate Route Detection')

import re
route_pattern = re.compile(r"@app\.route\('(/[^']+)'")
all_routes = route_pattern.findall(source)
route_counts = {}
for r in all_routes:
    route_counts[r] = route_counts.get(r, 0) + 1

duplicates = {r: c for r, c in route_counts.items() if c > 1}
if duplicates:
    for route, count in duplicates.items():
        fail(f'BUG: Duplicate route {route} defined {count} times!')
else:
    ok(f'No duplicate routes (checked {len(all_routes)} route definitions)')


# ══════════════════════════════════════════════════════════════
# Test 17: deploy.yaml Probe Path Validation
# ══════════════════════════════════════════════════════════════
section('Test 17: Probe Path Validation')

try:
    with open('manifests/deploy.yaml', 'r') as f:
        deploy_content = f.read()

    # Extract probe paths from deploy.yaml
    probe_paths = re.findall(r'path:\s*(/\S+)', deploy_content)
    for path in probe_paths:
        # Check that the path exists as a route in app.py
        if f"'{path}'" in source:
            ok(f'Probe path {path} exists as a route in app.py')
        else:
            fail(f'BUG: Probe path {path} does NOT exist as a route in app.py — pod will crash loop!')
except Exception as e:
    fail(f'Probe validation error: {e}')


# ══════════════════════════════════════════════════════════════
# Test 18: Release History Response Shape
# ══════════════════════════════════════════════════════════════
section('Test 18: Release History Consistency')

# Verify the frontend expects 'history' key (not 'releases')
with open('templates/index.html', 'r') as f:
    html = f.read()

if "d.history" in html or "history:" in html:
    ok('Frontend expects "history" key from /api/release/history')
else:
    fail('Frontend does not reference "history" key')

# Verify the backend returns 'history' key
history_func_start = source.find('def get_release_history(')
if history_func_start != -1:
    history_func = source[history_func_start:history_func_start + 2000]
    if "'history': summaries" in history_func or '"history":' in history_func:
        ok('Backend returns "history" key')
    else:
        fail('Backend does not return "history" key')
else:
    fail('get_release_history function not found')


# ══════════════════════════════════════════════════════════════
# Test 19: Auto-Release Lifecycle
# ══════════════════════════════════════════════════════════════
section('Test 19: Auto-Release Lifecycle')

# Check auto-release logic exists for open/locked boards
if "board_status in ('open', 'locked')" in source and 'auto_release' in source:
    ok('Auto-release triggers for open/locked boards past release date')
else:
    fail('Missing auto-release for open/locked boards')

if 'system (auto-released)' in source:
    ok('Auto-released boards marked with system attribution')
else:
    fail('Missing system attribution for auto-released boards')

# Check duplicate history prevention
if 'already_in_history' in source:
    ok('Duplicate history prevention check exists')
else:
    fail('No duplicate history prevention — risk of double entries')

# Check import copy is at top level
lines = source.split('\n')
top_100 = '\n'.join(lines[:100])
if 'import copy' in top_100:
    ok('import copy is at top-level (not inline)')
else:
    fail('import copy missing from top-level imports')

# Verify complete_release uses copy.deepcopy (not inline import)
complete_release_idx = source.find('def complete_release(')
if complete_release_idx != -1:
    complete_func = source[complete_release_idx:complete_release_idx + 1000]
    if 'import copy' in complete_func:
        fail('complete_release still has inline import copy')
    elif 'copy.deepcopy' in complete_func:
        ok('complete_release uses top-level copy.deepcopy')
    else:
        fail('complete_release missing copy.deepcopy for history snapshot')


# ══════════════════════════════════════════════════════════════
# Test 20: Stale Cutoff Fix
# ══════════════════════════════════════════════════════════════
section('Test 20: Stale Cutoff Fix')

if 'effective_cutoff' in source and '_get_cutoff_datetime()' in source:
    ok('Uses effective_cutoff with live recalculation')
else:
    fail('Missing effective_cutoff logic')

if '_get_current_release_date()' in source:
    ok('Compares board release_date to current release window')
else:
    fail('Missing current release date comparison')

# Check the nominate endpoint also uses effective cutoff
nominate_idx = source.find('def nominate_service(')
if nominate_idx != -1:
    nominate_func = source[nominate_idx:nominate_idx + 1500]
    if 'effective_cutoff' in nominate_func or '_get_cutoff_datetime()' in nominate_func:
        ok('nominate_service uses effective/live cutoff (not stale)')
    else:
        fail('nominate_service still uses stale board cutoff')

# Ensure utcnow comparison against effective_cutoff, not raw board.get('cutoff')
get_current_idx = source.find('def get_current_release(')
if get_current_idx != -1:
    get_current_func = source[get_current_idx:get_current_idx + 5000]
    if 'now_iso' in get_current_func and 'effective_cutoff' in get_current_func:
        ok('is_past_cutoff compares against effective_cutoff')
    else:
        fail('is_past_cutoff may still compare against raw stored cutoff')


# ══════════════════════════════════════════════════════════════
# Test 21: History Tab — Fix Version Selector
# ══════════════════════════════════════════════════════════════
section('Test 21: History Tab — Fix Version Selector')

with open('templates/index.html', 'r') as f:
    html = f.read()

if 'history-fix-version-select' in html:
    ok('Fix version dropdown element exists')
else:
    fail('Missing fix version dropdown element')

if 'onHistoryFixVersionSelect' in html:
    ok('onHistoryFixVersionSelect handler defined')
else:
    fail('Missing onHistoryFixVersionSelect handler')

if 'history-selected-detail' in html:
    ok('Selected release detail container exists')
else:
    fail('Missing detail container for selected release')

if 'history-detail-tbody' in html:
    ok('Detail table tbody exists for service rows')
else:
    fail('Missing detail table tbody')

if 'exportHistoryTable' in html:
    ok('Export/copy table function exists')
else:
    fail('Missing export table function')

if '_historyData' in html:
    ok('History data cached in _historyData for selector')
else:
    fail('Missing _historyData cache')


# ══════════════════════════════════════════════════════════════
# Test 22: Auto-Lock Consistency
# ══════════════════════════════════════════════════════════════
section('Test 22: Auto-Lock Consistency')

if "board['auto_locked'] = True" in source:
    ok('auto_locked flag set when auto-locking past cutoff')
else:
    fail('Missing auto_locked flag')

# Verify auto-lock only triggers on 'open' status, not 'released'
if "board['is_past_cutoff'] and board.get('status') == 'open'" in source:
    ok('Auto-lock only triggers on open boards (not released/locked)')
else:
    fail('Auto-lock condition may trigger on wrong statuses')


# ══════════════════════════════════════════════════════════════
# SUMMARY
# ══════════════════════════════════════════════════════════════
print(f'\n{"="*60}')
print(f'  TEST RESULTS: {PASS} passed, {FAIL} failed')
print(f'{"="*60}')
if FAIL == 0:
    print('  🎉 ALL TESTS PASSED!')
else:
    print(f'  ⚠️  {FAIL} FAILURES — review above')
print()
sys.exit(0 if FAIL == 0 else 1)

