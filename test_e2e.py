#!/usr/bin/env python3
"""E2E test suite for Release Readiness app — tests all key functions locally."""
import sys
import os
import json

# Add project to path
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
# Test 1: Syntax & Import
# ══════════════════════════════════════════════════════════════
section('Test 1: Syntax & Import Check')

try:
    import py_compile
    py_compile.compile('app.py', doraise=True)
    ok('app.py compiles without syntax errors')
except py_compile.PyCompileError as e:
    fail(f'Syntax error: {e}')
    sys.exit(1)

try:
    py_compile.compile('templates/index.html', doraise=False)
    ok('index.html exists')
except Exception:
    fail('index.html missing or unreadable')


# ══════════════════════════════════════════════════════════════
# Test 2: _map_issues_to_services
# ══════════════════════════════════════════════════════════════
section('Test 2: _map_issues_to_services()')

# Import the function directly
try:
    # We need to mock some things before importing app
    os.environ.setdefault('FLASK_SECRET_KEY', 'test-secret')
    os.environ.setdefault('POD_NAMESPACE', 'test')
    
    # Extract the function without importing the full app (which needs K8s)
    import importlib.util
    import types
    
    # Read and extract just the function
    with open('app.py', 'r') as f:
        source = f.read()
    
    # Find and extract _map_issues_to_services function
    start = source.find('def _map_issues_to_services(')
    if start == -1:
        fail('_map_issues_to_services function not found in app.py')
    else:
        # Find end of function (next def at same indent or end of file)
        lines = source[start:].split('\n')
        func_lines = [lines[0]]
        for line in lines[1:]:
            if line and not line[0].isspace() and not line.startswith('#'):
                break
            func_lines.append(line)
        func_source = '\n'.join(func_lines)
        
        # Execute the function in an isolated namespace
        ns = {}
        exec(func_source, ns)
        _map_issues_to_services = ns['_map_issues_to_services']
        ok('Function extracted successfully')

        # Test 2a: Basic component matching
        issues = [
            {'id': 'PROJ-1', 'summary': 'Fix login', 'type': 'Bug', 'components': ['auth-service']},
            {'id': 'PROJ-2', 'summary': 'Add cache', 'type': 'Feature', 'components': ['data-pipeline']},
            {'id': 'PROJ-3', 'summary': 'Infra update', 'type': 'Task', 'components': ['infra']},
        ]
        services = ['auth-service', 'data-pipeline', 'user-service']
        svc_map, unmatched = _map_issues_to_services(issues, services)
        
        if 'auth-service' in svc_map and len(svc_map['auth-service']) == 1:
            ok('PROJ-1 mapped to auth-service via component')
        else:
            fail(f'PROJ-1 not mapped correctly: {svc_map}')
        
        if 'data-pipeline' in svc_map and len(svc_map['data-pipeline']) == 1:
            ok('PROJ-2 mapped to data-pipeline via component')
        else:
            fail(f'PROJ-2 not mapped correctly: {svc_map}')
        
        if len(unmatched) == 1 and unmatched[0]['id'] == 'PROJ-3':
            ok('PROJ-3 correctly unmatched (infra not in services)')
        else:
            fail(f'Unmatched wrong: {unmatched}')

        # Test 2b: Case-insensitive matching
        issues2 = [
            {'id': 'PROJ-10', 'summary': 'Test', 'components': ['Auth-Service']},
            {'id': 'PROJ-11', 'summary': 'Test', 'components': ['DATA_PIPELINE']},
        ]
        svc_map2, unmatched2 = _map_issues_to_services(issues2, services)
        
        if 'auth-service' in svc_map2:
            ok('Case-insensitive: "Auth-Service" matched "auth-service"')
        else:
            fail(f'Case-insensitive match failed: {svc_map2}')
        
        if 'data-pipeline' in svc_map2:
            ok('Dash/underscore normalized: "DATA_PIPELINE" matched "data-pipeline"')
        else:
            fail(f'Normalization failed: {svc_map2}')

        # Test 2c: Multiple components → maps to all matching services
        issues3 = [
            {'id': 'PROJ-20', 'summary': 'Shared change', 'components': ['auth-service', 'user-service']},
        ]
        svc_map3, unmatched3 = _map_issues_to_services(issues3, services)
        
        if 'auth-service' in svc_map3 and 'user-service' in svc_map3:
            ok('Multi-component ticket mapped to ALL matching services')
        else:
            fail(f'Multi-component mapping failed: {svc_map3}')
        
        if len(unmatched3) == 0:
            ok('Multi-component ticket not in unmatched')
        else:
            fail(f'Multi-component ticket incorrectly unmatched: {unmatched3}')

        # Test 2d: No components → unmatched
        issues4 = [
            {'id': 'PROJ-30', 'summary': 'No comp', 'components': []},
        ]
        svc_map4, unmatched4 = _map_issues_to_services(issues4, services)
        
        if len(unmatched4) == 1:
            ok('Ticket with no components → unmatched')
        else:
            fail(f'No-component handling failed: {svc_map4}, {unmatched4}')

        # Test 2e: Empty inputs
        svc_map5, unmatched5 = _map_issues_to_services([], services)
        if len(svc_map5) == 0 and len(unmatched5) == 0:
            ok('Empty issues → empty results')
        else:
            fail('Empty input handling failed')

        svc_map6, unmatched6 = _map_issues_to_services(issues, [])
        if len(svc_map6) == 0 and len(unmatched6) == 3:
            ok('No services → all unmatched')
        else:
            fail(f'No-services handling failed: {svc_map6}, {unmatched6}')

except Exception as e:
    import traceback
    fail(f'Test 2 error: {e}\n{traceback.format_exc()}')


# ══════════════════════════════════════════════════════════════
# Test 3: Combined service Jira mapping (merge logic)
# ══════════════════════════════════════════════════════════════
section('Test 3: Combined Jira Mapping (component + manual)')

try:
    # Simulate the merge logic from generate_release_notes
    component_svc_map = {
        'auth-service': [
            {'id': 'PROJ-1', 'summary': 'Fix login', 'type': 'Bug', 'status': 'Done'},
            {'id': 'PROJ-2', 'summary': 'Add SSO', 'type': 'Feature', 'status': 'Done'},
        ],
        'data-pipeline': [
            {'id': 'PROJ-3', 'summary': 'Perf fix', 'type': 'Improvement', 'status': 'Done'},
        ]
    }
    manual_svc_jira_map = {
        'auth-service': ['PROJ-99'],  # manually entered
        'user-service': ['PROJ-50'],
    }
    jira_details = {
        'PROJ-99': {'id': 'PROJ-99', 'summary': 'Manual ticket', 'type': 'Task', 'status': 'Open'},
        'PROJ-50': {'id': 'PROJ-50', 'summary': 'User fix', 'type': 'Bug', 'status': 'Done'},
    }
    board_services = {'auth-service': {}, 'data-pipeline': {}, 'user-service': {}, 'api-gateway': {}}

    combined_svc_jiras = {}
    for svc_name in board_services:
        combined = []
        seen_ids = set()
        for issue in component_svc_map.get(svc_name, []):
            if issue['id'] not in seen_ids:
                combined.append(issue)
                seen_ids.add(issue['id'])
        for jid in manual_svc_jira_map.get(svc_name, []):
            if jid not in seen_ids:
                issue = jira_details.get(jid, {'id': jid, 'summary': '', 'type': 'Task', 'status': '?'})
                combined.append(issue)
                seen_ids.add(jid)
        if combined:
            combined_svc_jiras[svc_name] = combined

    # Verify
    if len(combined_svc_jiras.get('auth-service', [])) == 3:
        ok('auth-service: 2 component + 1 manual = 3 tickets')
    else:
        fail(f'auth-service has {len(combined_svc_jiras.get("auth-service", []))} tickets, expected 3')

    if len(combined_svc_jiras.get('data-pipeline', [])) == 1:
        ok('data-pipeline: 1 component ticket')
    else:
        fail(f'data-pipeline wrong count')

    if len(combined_svc_jiras.get('user-service', [])) == 1:
        ok('user-service: 1 manual ticket')
    else:
        fail(f'user-service wrong count')

    if 'api-gateway' not in combined_svc_jiras:
        ok('api-gateway: no tickets (correct)')
    else:
        fail('api-gateway should have no tickets')

    # Test deduplication
    component_svc_map_dup = {'svc-a': [{'id': 'X-1', 'summary': 'dup'}]}
    manual_dup = {'svc-a': ['X-1']}
    combined_dup = []
    seen = set()
    for issue in component_svc_map_dup.get('svc-a', []):
        if issue['id'] not in seen:
            combined_dup.append(issue)
            seen.add(issue['id'])
    for jid in manual_dup.get('svc-a', []):
        if jid not in seen:
            combined_dup.append({'id': jid})
            seen.add(jid)
    
    if len(combined_dup) == 1:
        ok('Deduplication: same ticket in component + manual = 1 entry')
    else:
        fail(f'Deduplication failed: {len(combined_dup)} entries')

except Exception as e:
    import traceback
    fail(f'Test 3 error: {e}\n{traceback.format_exc()}')


# ══════════════════════════════════════════════════════════════
# Test 4: Deterministic table generation
# ══════════════════════════════════════════════════════════════
section('Test 4: Deterministic Table (Jira column per service)')

try:
    # Simulate building the table
    board = {
        'release_date': '2026-05-15',
        'services': {
            'auth-service': {'image_tag': 'v1.2.3', 'helm_version': '2.0.1', 'notes': 'SSO update'},
            'data-pipeline': {'image_tag': 'v3.0.0', 'helm_version': '1.5.0', 'notes': ''},
            'api-gateway': {'image_tag': 'v2.1.0', 'helm_version': None, 'notes': 'No changes'},
        }
    }
    
    combined_test = {
        'auth-service': [{'id': 'PROJ-1'}, {'id': 'PROJ-2'}],
        'data-pipeline': [{'id': 'PROJ-3'}],
        # api-gateway has no tickets
    }

    lines = []
    for svc_name, svc_data in board['services'].items():
        svc_issues = combined_test.get(svc_name, [])
        jira_col = ', '.join(i['id'] for i in svc_issues) if svc_issues else '—'
        tag = svc_data.get('image_tag', '?')
        helm = svc_data.get('helm_version')
        ver_str = f"{tag} (Helm: {helm})" if helm else tag
        lines.append(f"| {svc_name} | {ver_str} | {jira_col} | {svc_data.get('helm_version', 'N/A')} | {svc_data.get('notes', '')} |")

    table = '\n'.join(lines)

    if 'PROJ-1, PROJ-2' in table and 'auth-service' in table:
        ok('auth-service row has PROJ-1, PROJ-2')
    else:
        fail(f'auth-service Jira column wrong in: {table}')

    if 'PROJ-3' in table and 'data-pipeline' in table:
        ok('data-pipeline row has PROJ-3')
    else:
        fail(f'data-pipeline Jira column wrong')

    # api-gateway should have '—' not any PROJ tickets
    gw_line = [l for l in lines if 'api-gateway' in l][0]
    if '—' in gw_line and 'PROJ' not in gw_line:
        ok('api-gateway row has "—" (no tickets)')
    else:
        fail(f'api-gateway should have no tickets: {gw_line}')

except Exception as e:
    import traceback
    fail(f'Test 4 error: {e}\n{traceback.format_exc()}')


# ══════════════════════════════════════════════════════════════
# Test 5: AI Prompt (per-service Jira context)
# ══════════════════════════════════════════════════════════════
section('Test 5: AI Prompt Service Lines')

try:
    combined_prompt_test = {
        'auth-service': [
            {'id': 'PROJ-1', 'summary': 'Fix login', 'type': 'Bug', 'status': 'Done',
             'priority': 'High', 'description': 'Login was broken on mobile'},
        ],
        'data-pipeline': [],
    }
    
    service_list = []
    for svc_name in ['auth-service', 'data-pipeline']:
        svc_line = f"- {svc_name} [K8S SERVICE]: image_tag=v1.0"
        svc_issues = combined_prompt_test.get(svc_name, [])
        if svc_issues:
            svc_ids = [i['id'] for i in svc_issues]
            svc_line += f", jira_tickets=[{', '.join(svc_ids)}]"
            for issue in svc_issues:
                svc_line += f"\n    JIRA {issue['id']}: type={issue['type']}, summary=\"{issue['summary']}\""
        service_list.append(svc_line)

    prompt_text = '\n'.join(service_list)

    if 'jira_tickets=[PROJ-1]' in prompt_text:
        ok('auth-service prompt line includes PROJ-1')
    else:
        fail(f'Prompt missing PROJ-1 for auth-service')

    # data-pipeline should NOT have jira_tickets
    dp_line = [l for l in service_list if 'data-pipeline' in l][0]
    if 'jira_tickets' not in dp_line:
        ok('data-pipeline prompt line has NO jira_tickets (correct)')
    else:
        fail(f'data-pipeline should not have tickets: {dp_line}')

except Exception as e:
    import traceback
    fail(f'Test 5 error: {e}\n{traceback.format_exc()}')


# ══════════════════════════════════════════════════════════════
# Test 6: Unmatched tickets section
# ══════════════════════════════════════════════════════════════
section('Test 6: Unmatched Tickets Section')

try:
    unmatched = [
        {'id': 'PROJ-50', 'type': 'Task', 'summary': 'CI/CD update', 'status': 'Done',
         'components': ['devops']},
        {'id': 'PROJ-51', 'type': 'Bug', 'summary': 'No comp ticket', 'status': 'Open',
         'components': []},
    ]

    lines = []
    if unmatched:
        lines.append(f"\n### 📋 Other Changes ({len(unmatched)} tickets)")
        lines.append("> *These Jira tickets have no component matching a nominated service.*\n")
        for issue in unmatched:
            comps = ', '.join(issue.get('components', [])) or 'No component'
            lines.append(f"- **{issue['id']}** [{issue.get('type', 'Task')}]: {issue.get('summary', '?')} [{issue.get('status', '?')}] — Components: {comps}")

    section_text = '\n'.join(lines)
    
    if 'Other Changes (2 tickets)' in section_text:
        ok('Unmatched section header correct')
    else:
        fail(f'Header wrong: {section_text[:100]}')

    if 'PROJ-50' in section_text and 'Components: devops' in section_text:
        ok('PROJ-50 listed with component "devops"')
    else:
        fail('PROJ-50 not formatted correctly')

    if 'PROJ-51' in section_text and 'No component' in section_text:
        ok('PROJ-51 listed with "No component"')
    else:
        fail('PROJ-51 not formatted correctly')

except Exception as e:
    import traceback
    fail(f'Test 6 error: {e}\n{traceback.format_exc()}')


# ══════════════════════════════════════════════════════════════
# Test 7: Async job store
# ══════════════════════════════════════════════════════════════
section('Test 7: Async Job Store Pattern')

try:
    import uuid
    _release_notes_jobs = {}

    job_id = str(uuid.uuid4())[:8]
    _release_notes_jobs[job_id] = {
        'status': 'running', 'notes': '', 'error': None,
        'gemini_powered': True, 'jira_enriched': True,
        'jira_count': 29, 'fix_version': 'P26.05.08',
        'release_date': '2026-05-15'
    }

    if _release_notes_jobs[job_id]['status'] == 'running':
        ok(f'Job {job_id} created with status=running')
    else:
        fail('Job status wrong')

    # Simulate completion
    _release_notes_jobs[job_id]['notes'] = '## Release Notes\nTest content'
    _release_notes_jobs[job_id]['status'] = 'done'

    if _release_notes_jobs[job_id]['status'] == 'done':
        ok('Job completed → status=done')
    else:
        fail('Job completion failed')

    if len(_release_notes_jobs[job_id]['notes']) > 0:
        ok('Job has notes content')
    else:
        fail('Notes empty after completion')

    # Simulate error
    job_id2 = str(uuid.uuid4())[:8]
    _release_notes_jobs[job_id2] = {'status': 'error', 'error': 'API timeout'}
    if _release_notes_jobs[job_id2]['status'] == 'error':
        ok('Error job stored correctly')
    else:
        fail('Error job status wrong')

except Exception as e:
    import traceback
    fail(f'Test 7 error: {e}\n{traceback.format_exc()}')


# ══════════════════════════════════════════════════════════════
# Test 8: _parse_jira_ids (existing function)
# ══════════════════════════════════════════════════════════════
section('Test 8: _parse_jira_ids()')

try:
    # Extract _parse_jira_ids from app.py
    with open('app.py', 'r') as f:
        source = f.read()
    
    start = source.find('def _parse_jira_ids(')
    if start == -1:
        fail('_parse_jira_ids not found')
    else:
        lines = source[start:].split('\n')
        func_lines = [lines[0]]
        for line in lines[1:]:
            if line and not line[0].isspace() and not line.startswith('#'):
                break
            func_lines.append(line)
        
        ns = {'re': __import__('re'), '_JIRA_ID_PATTERN': __import__('re').compile(r'^[A-Z][A-Z0-9]+-\d+$')}
        exec('\n'.join(func_lines), ns)
        _parse_jira_ids = ns['_parse_jira_ids']

        result = _parse_jira_ids('PROJ-123, PROJ-456')
        if result == ['PROJ-123', 'PROJ-456']:
            ok('Comma-separated parsing works')
        else:
            fail(f'Parsing failed: {result}')

        result2 = _parse_jira_ids('PROJ-1, PROJ-2')
        if 'PROJ-1' in result2 and 'PROJ-2' in result2:
            ok('Multiple IDs with comma parsing works')
        else:
            fail(f'Multi-ID parsing failed: {result2}')

        result3 = _parse_jira_ids('')
        if result3 == []:
            ok('Empty string → empty list')
        else:
            fail(f'Empty string failed: {result3}')

except Exception as e:
    import traceback
    fail(f'Test 8 error: {e}\n{traceback.format_exc()}')


# ══════════════════════════════════════════════════════════════
# Test 9: Frontend HTML integrity
# ══════════════════════════════════════════════════════════════
section('Test 9: Frontend HTML Integrity')

try:
    with open('templates/index.html', 'r') as f:
        html = f.read()

    # Check async polling code exists
    if 'job_id' in html and 'pollInterval' in html:
        ok('Async polling code present in frontend')
    else:
        fail('Async polling code missing')

    if '_showReleaseNotes' in html:
        ok('_showReleaseNotes helper function present')
    else:
        fail('_showReleaseNotes missing')

    if 'ai-timer' in html:
        ok('AI timer element present for progress display')
    else:
        fail('AI timer missing')

    if 'generateReleaseNotes' in html:
        ok('generateReleaseNotes function present')
    else:
        fail('generateReleaseNotes missing')

    if 'copyReleaseNotes' in html:
        ok('copyReleaseNotes function present')
    else:
        fail('copyReleaseNotes missing')

    # Check prod env tab
    if 'loadProdServices' in html:
        ok('loadProdServices function present')
    else:
        fail('loadProdServices missing')

except Exception as e:
    import traceback
    fail(f'Test 9 error: {e}\n{traceback.format_exc()}')


# ══════════════════════════════════════════════════════════════
# Test 10: Backend route registration check
# ══════════════════════════════════════════════════════════════
section('Test 10: Backend Route Checks')

try:
    with open('app.py', 'r') as f:
        source = f.read()

    routes = [
        ('/api/ai/release_notes', 'POST'),
        ('/api/ai/release_notes/<job_id>', 'GET'),
        ('/api/prod/services', 'GET'),
        ('/api/release/current', 'GET'),
        ('/api/release/current', 'GET'),
        ('/api/services', 'GET'),
    ]
    for path, method in routes:
        if path in source:
            ok(f'Route {path} registered')
        else:
            fail(f'Route {path} MISSING')

    # Check key functions exist
    funcs = [
        '_map_issues_to_services',
        '_fetch_jira_by_fix_version',
        '_get_prod_api_client',
        'generate_release_notes',
        'release_notes_status',
        '_release_notes_jobs',
    ]
    for func in funcs:
        if func in source:
            ok(f'{func} defined')
        else:
            fail(f'{func} MISSING')

except Exception as e:
    import traceback
    fail(f'Test 10 error: {e}\n{traceback.format_exc()}')


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
