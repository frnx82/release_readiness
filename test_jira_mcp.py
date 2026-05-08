#!/usr/bin/env python3
"""
Test script for Jira MCP Server connectivity.

Tests all available MCP tools:
  1. find_jira_projects
  2. get_jira_project
  3. search_jira_issues  ← key for fix version queries
  4. get_jira_issue
  5. get_jira_issue_subtasks
  6. get_jira_issue_comments
  7. search_jira_recurring_in

Usage:
  export JIRA_MCP_URL=http://jira-mcp-server:8088   # MCP server endpoint
  export JIRA_PAT_TOKEN=your-jira-personal-access-token
  python test_jira_mcp.py

Optional args:
  --fix-version P26.05.08    Test fix version JQL search
  --issue PROJ-123           Test fetching a specific issue
  --project PROJ             Test fetching a specific project
"""

import os
import sys
import json
import uuid
import argparse
import textwrap
import requests

# ── Config ────────────────────────────────────────────────────────────────────
MCP_URL    = os.environ.get('JIRA_MCP_URL', '')
PAT_TOKEN  = os.environ.get('JIRA_PAT_TOKEN', '')
JIRA_EMAIL = os.environ.get('JIRA_EMAIL', '')
SSL_VERIFY = os.environ.get('SSL_VERIFY', 'true').lower() not in ('false', '0', 'no')

if not SSL_VERIFY:
    import urllib3
    urllib3.disable_warnings(urllib3.exceptions.InsecureRequestWarning)

# ── Colors ────────────────────────────────────────────────────────────────────
GREEN  = '\033[92m'
RED    = '\033[91m'
YELLOW = '\033[93m'
CYAN   = '\033[96m'
BOLD   = '\033[1m'
DIM    = '\033[2m'
RESET  = '\033[0m'


def ok(msg):    print(f"  {GREEN}✅ {msg}{RESET}")
def fail(msg):  print(f"  {RED}❌ {msg}{RESET}")
def warn(msg):  print(f"  {YELLOW}⚠️  {msg}{RESET}")
def info(msg):  print(f"  {CYAN}ℹ️  {msg}{RESET}")
def header(msg): print(f"\n{BOLD}{'─'*60}\n  {msg}\n{'─'*60}{RESET}")


def mcp_call(tool_name, arguments, timeout=15, endpoint_url=None, verbose=False):
    """Call an MCP tool via HTTP JSON-RPC."""
    headers = {
        'Content-Type': 'application/json',
        'Accept': 'application/json, text/event-stream',
    }

    # Auth: send both headers (server may expect either one)
    if PAT_TOKEN:
        headers['Jira-Token'] = PAT_TOKEN
        headers['Authorization'] = f'Bearer {PAT_TOKEN}'

    # Also send email if available
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

    url = endpoint_url or MCP_URL

    if verbose:
        print(f"\n     {DIM}── DEBUG REQUEST ──{RESET}")
        print(f"     {DIM}URL: {url}{RESET}")
        safe_headers = {k: (v[:8] + '...' if k in ('Jira-Token', 'Authorization') else v) for k, v in headers.items()}
        print(f"     {DIM}Headers: {json.dumps(safe_headers, indent=2)}{RESET}")
        print(f"     {DIM}Payload: {json.dumps(payload)[:200]}{RESET}")

    try:
        resp = requests.post(url, json=payload, headers=headers,
                             timeout=timeout, verify=SSL_VERIFY)
        if verbose:
            print(f"     {DIM}── DEBUG RESPONSE ──{RESET}")
            print(f"     {DIM}Status: {resp.status_code}{RESET}")
            print(f"     {DIM}Resp Headers: {dict(resp.headers)}{RESET}")
            print(f"     {DIM}Body: {resp.text[:500]}{RESET}")
        return resp
    except Exception as e:
        return e


def discover_endpoint():
    """Try multiple endpoint paths to find the working MCP endpoint."""
    base = MCP_URL.rstrip('/')
    # Try: base URL as-is, /mcp, /messages, /rpc
    candidates = [base, f'{base}/mcp', f'{base}/messages', f'{base}/rpc']

    headers = {
        'Content-Type': 'application/json',
        'Accept': 'application/json, text/event-stream',
    }
    if PAT_TOKEN:
        headers['Jira-Token'] = PAT_TOKEN
        headers['Authorization'] = f'Bearer {PAT_TOKEN}'

    payload = {
        'jsonrpc': '2.0',
        'id': str(uuid.uuid4()),
        'method': 'tools/list',
        'params': {}
    }

    for url in candidates:
        try:
            resp = requests.post(url, json=payload, headers=headers,
                                 timeout=10, verify=SSL_VERIFY)
            print(f"  {url}")
            print(f"     HTTP {resp.status_code} | Content-Type: {resp.headers.get('Content-Type', '?')}")
            print(f"     Response (first 300 chars): {resp.text[:300]}")

            if resp.ok:
                try:
                    data = resp.json()
                    tools = data.get('result', {}).get('tools', data.get('tools', []))
                    if tools:
                        ok(f"Found {len(tools)} tools at {url}")
                        for t in tools:
                            name = t.get('name', '?')
                            desc = t.get('description', '')[:60]
                            print(f"       • {GREEN}{name}{RESET}  {DIM}{desc}{RESET}")
                        return url, [t.get('name', '') for t in tools]
                except json.JSONDecodeError:
                    pass
            print()
        except requests.ConnectionError:
            print(f"  {url}  → {RED}Connection refused{RESET}")
        except Exception as e:
            print(f"  {url}  → {RED}{e}{RESET}")

    return None, []


def parse_mcp_response(resp):
    """Parse MCP response — handles JSON, SSE, and text formats."""
    if isinstance(resp, Exception):
        return None, str(resp)

    content_type = resp.headers.get('Content-Type', '')
    raw = resp.text.strip()

    if not raw:
        return None, f"Empty response body (HTTP {resp.status_code})"

    # Try JSON-RPC response
    try:
        data = json.loads(raw)
        if 'result' in data:
            result = data['result']
            # FastMCP wraps results in content array
            if isinstance(result, dict) and 'content' in result:
                content = result['content']
                if isinstance(content, list) and content:
                    text_parts = []
                    for c in content:
                        if isinstance(c, dict) and c.get('text'):
                            text_parts.append(c['text'])
                    if text_parts:
                        combined = '\n'.join(text_parts)
                        try:
                            return json.loads(combined), None
                        except json.JSONDecodeError:
                            return combined, None
            return result, None
        if 'error' in data:
            return None, f"JSON-RPC error: {data['error']}"
        return data, None
    except json.JSONDecodeError:
        pass

    # Try SSE format
    if 'text/event-stream' in content_type or raw.startswith('event:'):
        for line in raw.split('\n'):
            if line.startswith('data:'):
                data_str = line[5:].strip()
                try:
                    return json.loads(data_str), None
                except json.JSONDecodeError:
                    return data_str, None

    return raw, None


def test_connectivity():
    """Test basic MCP server connectivity."""
    header("🔌 Connectivity Test")
    print(f"  MCP URL:    {MCP_URL}")
    print(f"  PAT Token:  {'*' * 8}...{PAT_TOKEN[-4:] if len(PAT_TOKEN) > 4 else '???'}")
    print(f"  Email:      {JIRA_EMAIL or '(not set)'}")
    print(f"  SSL Verify: {SSL_VERIFY}")
    print()

    try:
        resp = requests.get(MCP_URL, timeout=5, verify=SSL_VERIFY)
        ok(f"Server reachable (HTTP {resp.status_code})")
        return True
    except requests.ConnectionError:
        fail(f"Cannot connect to {MCP_URL}")
        return False
    except Exception as e:
        fail(f"Connection error: {e}")
        return False


def test_tool(tool_name, arguments, description=""):
    """Test a single MCP tool and print results."""
    print(f"\n  {BOLD}🔧 {tool_name}{RESET}", end="")
    if description:
        print(f" {DIM}— {description}{RESET}")
    else:
        print()
    print(f"     Args: {json.dumps(arguments)}")

    resp = mcp_call(tool_name, arguments, verbose=True)

    if isinstance(resp, Exception):
        fail(f"Request failed: {resp}")
        return None

    print(f"     HTTP: {resp.status_code} ({resp.headers.get('Content-Type', '?')})")

    result, error = parse_mcp_response(resp)
    if error:
        fail(f"Parse error: {error}")
        print(f"     {DIM}Raw (first 300 chars): {resp.text[:300]}{RESET}")
        return None

    if result is None:
        warn("Empty result")
        return None

    # Pretty print result summary
    if isinstance(result, dict):
        # Check for issues list
        issues = result.get('issues', result.get('result', {}).get('issues', []) if isinstance(result.get('result'), dict) else [])
        if isinstance(issues, list) and issues:
            ok(f"Returned {len(issues)} issues")
            for issue in issues[:5]:
                if isinstance(issue, dict):
                    key = issue.get('key', issue.get('id', '?'))
                    summary = issue.get('summary', issue.get('fields', {}).get('summary', '?'))
                    print(f"       • {CYAN}{key}{RESET}: {summary[:80]}")
            if len(issues) > 5:
                print(f"       {DIM}... and {len(issues) - 5} more{RESET}")
        else:
            ok("Got response")
            # Print first few keys
            preview = json.dumps(result, indent=2)[:400]
            for line in preview.split('\n')[:12]:
                print(f"       {DIM}{line}{RESET}")
    elif isinstance(result, str):
        ok(f"Got text response ({len(result)} chars)")
        for line in result.split('\n')[:5]:
            print(f"       {DIM}{line[:100]}{RESET}")
    elif isinstance(result, list):
        ok(f"Got list with {len(result)} items")
        for item in result[:5]:
            print(f"       {DIM}• {str(item)[:100]}{RESET}")
    else:
        ok(f"Got response: {type(result).__name__}")

    return result


def main():
    global MCP_URL
    parser = argparse.ArgumentParser(
        description='Test Jira MCP Server connectivity and tools',
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog=textwrap.dedent("""
        Examples:
          python test_jira_mcp.py
          python test_jira_mcp.py --fix-version P26.05.08
          python test_jira_mcp.py --issue PROJ-123
          python test_jira_mcp.py --project PROJ
          python test_jira_mcp.py --all
        """)
    )
    parser.add_argument('--fix-version', help='Test fix version JQL search (e.g. P26.05.08)')
    parser.add_argument('--issue', help='Test fetching a specific Jira issue (e.g. PROJ-123)')
    parser.add_argument('--project', help='Test fetching a specific project (e.g. PROJ)')
    parser.add_argument('--jql', help='Test custom JQL query')
    parser.add_argument('--all', action='store_true', help='Run all tests')
    args = parser.parse_args()

    print(f"\n{BOLD}{'═'*60}")
    print(f"  🧪 Jira MCP Server Connectivity Test")
    print(f"{'═'*60}{RESET}")

    # Validate config
    if not MCP_URL:
        fail("JIRA_MCP_URL not set!")
        print(f"  {DIM}Set: export JIRA_MCP_URL=http://your-mcp-server:8088{RESET}")
        sys.exit(1)
    if not PAT_TOKEN:
        fail("JIRA_PAT_TOKEN not set!")
        print(f"  {DIM}Set: export JIRA_PAT_TOKEN=your-personal-access-token{RESET}")
        sys.exit(1)

    # Test 1: Connectivity
    if not test_connectivity():
        sys.exit(1)

    # Test 2: Discover endpoint + available tools
    header("🔍 Endpoint & Tool Discovery")
    info(f"Base URL: {MCP_URL}")
    info("Trying: base, /mcp, /messages, /rpc")
    print()
    working_url, tool_names = discover_endpoint()

    if working_url:
        ok(f"Working endpoint: {working_url}")
        MCP_URL = working_url
    else:
        warn("Could not discover tools via tools/list")
        info("Will try tool calls directly against base URL")

    # Test 3: Tool Tests
    header("📋 Tool Tests")

    if args.all:
        test_tool('find_jira_projects', {}, 'List all accessible Jira projects')

    # Test 3: Get specific project
    if args.project or args.all:
        project_key = args.project or 'PROJ'
        test_tool('get_jira_project', {'project_key': project_key},
                  f'Get project details for {project_key}')

    # Test 4: Get specific issue
    if args.issue or args.all:
        issue_key = args.issue or 'PROJ-1'
        result = test_tool('get_jira_issue', {'issue_key': issue_key},
                          f'Fetch issue {issue_key}')

        if result and (args.issue or args.all):
            # Also test subtasks and comments
            test_tool('get_jira_issue_subtasks', {'issue_key': issue_key},
                      f'Get subtasks of {issue_key}')
            test_tool('get_jira_issue_comments', {'issue_key': issue_key},
                      f'Get comments on {issue_key}')

    # Test 5: Fix version search (THE KEY TEST)
    if args.fix_version or args.all:
        fix_version = args.fix_version or 'P26.05.08'
        header(f"🏷️ Fix Version Search: {fix_version}")
        jql = f'fixVersion = "{fix_version}" ORDER BY issuetype ASC'
        print(f"  JQL: {CYAN}{jql}{RESET}")
        result = test_tool('search_jira_issues', {'jql': jql, 'max_results': 50},
                          f'Search by fix version {fix_version}')

        if result:
            # Try to extract and display issue details
            issues = []
            if isinstance(result, dict):
                issues = result.get('issues', [])
            elif isinstance(result, list):
                issues = result

            if issues:
                print(f"\n  {BOLD}📝 Fix Version Issues Detail:{RESET}")
                for i, issue in enumerate(issues[:10]):
                    if isinstance(issue, dict):
                        key = issue.get('key', issue.get('id', '?'))
                        fields = issue.get('fields', issue)
                        summary = fields.get('summary', '?')
                        desc = (fields.get('description') or '')[:150]
                        itype = fields.get('issuetype', {})
                        type_name = itype.get('name', '?') if isinstance(itype, dict) else str(itype)
                        status = fields.get('status', {})
                        status_name = status.get('name', '?') if isinstance(status, dict) else str(status)
                        print(f"\n  {CYAN}{key}{RESET} [{type_name}] — {status_name}")
                        print(f"    Summary: {summary}")
                        if desc:
                            print(f"    Description: {DIM}{desc}...{RESET}")

    # Test 6: Custom JQL
    if args.jql:
        header(f"🔍 Custom JQL Search")
        print(f"  JQL: {CYAN}{args.jql}{RESET}")
        test_tool('search_jira_issues', {'jql': args.jql, 'max_results': 20},
                  'Custom JQL query')

    # If no specific tests, run a basic search
    if not any([args.fix_version, args.issue, args.project, args.jql, args.all]):
        header("🔍 Quick Search Test (search_jira_issues)")
        info("Running a basic search to verify the search tool works...")
        info("Use --fix-version, --issue, --project, or --all for more tests")
        print()

        # Test the search tool with a simple JQL
        result = test_tool('search_jira_issues',
                          {'jql': 'updated >= -7d ORDER BY updated DESC', 'max_results': 5},
                          'Recently updated issues (last 7 days)')

        # Also test get_jira_issue with a dummy to verify the tool exists
        if result and isinstance(result, dict):
            issues = result.get('issues', [])
            if issues and isinstance(issues[0], dict):
                first_key = issues[0].get('key', '')
                if first_key:
                    test_tool('get_jira_issue', {'issue_key': first_key},
                              f'Verify single issue fetch: {first_key}')

    # Summary
    header("📊 Summary")
    print(f"""
  MCP Server:  {GREEN}{MCP_URL}{RESET}
  Auth Header: Jira-Token: ****...{PAT_TOKEN[-4:] if len(PAT_TOKEN) > 4 else '???'}

  {BOLD}Available tools on your MCP server:{RESET}
    • find_jira_projects      — List all projects
    • get_jira_project         — Get project by key
    • {GREEN}search_jira_issues{RESET}      — {BOLD}JQL search (fix version queries){RESET}
    • get_jira_issue           — Get single issue by key
    • get_jira_issue_subtasks  — Get issue subtasks
    • get_jira_issue_comments  — Get issue comments
    • search_jira_recurring_in — Search recurring issues

  {BOLD}For Release Readiness integration:{RESET}
    The app needs: JIRA_MCP_URL + JIRA_PAT_TOKEN
    Tool used for fix version: search_jira_issues
    Auth header sent: Jira-Token (not Authorization: Bearer)
""")


if __name__ == '__main__':
    main()
