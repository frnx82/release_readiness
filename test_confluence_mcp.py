"""
E2E Tests for Confluence MCP Retry Logic, Circuit Breaker, and Health Endpoints
================================================================================

Tests the following changes:
  1. _confluence_mcp_call retry logic with backoff
  2. Circuit breaker (3 consecutive timeouts → skip)
  3. Circuit breaker auto-reset after cooldown
  4. _confluence_mcp_call_once timeout tracking and error recording
  5. _confluence_search tolerance (allows up to 2 timeouts before giving up)
  6. Health endpoint /api/confluence-mcp-health
  7. Reset endpoint /api/confluence-mcp-reset
  8. Diagnostic endpoint includes Step 6 (MCP health state)

Run: python3 -m pytest test_confluence_mcp.py -v
"""

import os
import sys
import json
import time
import unittest
from unittest.mock import patch, MagicMock

# Set required env vars BEFORE importing app
os.environ['CONFLUENCE_MCP_URL'] = 'http://test-confluence-mcp:8080/mcp'
os.environ['CONFLUENCE_PAT_TOKEN'] = 'test-token-12345'
os.environ['CONFLUENCE_EMAIL'] = 'test@example.com'
os.environ['CONFLUENCE_DEFAULT_SPACES'] = 'TEAM1,TEAM2'
os.environ['SSL_VERIFY'] = 'false'
os.environ['DEPLOY_ENV'] = 'test'
os.environ.setdefault('BOARD_DATA_DIR', '/tmp/test-board-data')

import requests

# Import app — this triggers module-level init (K8s failures are OK in test)
print("\n=== Importing app module (K8s warnings are expected) ===")
import app as app_module
print("=== App module imported successfully ===\n")


class TestConfluenceMCPRetry(unittest.TestCase):
    """Test the retry logic in _confluence_mcp_call."""

    def setUp(self):
        """Reset MCP health state before each test."""
        app_module._confluence_mcp_health = {
            'consecutive_timeouts': 0,
            'last_success': 0,
            'last_failure': 0,
            'last_error': '',
        }

    # ──────────────────────────────────────────────────────────────────────
    # Test 1: Successful call — no retry needed
    # ──────────────────────────────────────────────────────────────────────
    def test_01_successful_call_no_retry(self):
        """A successful MCP call should return immediately without retrying."""
        mock_response = MagicMock()
        mock_response.status_code = 200
        mock_response.text = json.dumps({
            'jsonrpc': '2.0',
            'id': 'test',
            'result': {
                'content': [{'text': json.dumps({'results': [{'id': '1', 'title': 'Test Page'}]})}]
            }
        })
        mock_response.headers = {'Content-Type': 'application/json'}
        mock_response.raise_for_status = MagicMock()

        with patch('requests.post', return_value=mock_response) as mock_post:
            result = app_module._confluence_mcp_call(
                'confluence_search', {'cql': 'type=page', 'limit': 1}, timeout=5, max_retries=2
            )

        self.assertIsNotNone(result, "Successful call should return a result")
        self.assertEqual(mock_post.call_count, 1, "Should not retry on success")
        self.assertEqual(app_module._confluence_mcp_health['consecutive_timeouts'], 0)

    # ──────────────────────────────────────────────────────────────────────
    # Test 2: Timeout then success — retry works
    # ──────────────────────────────────────────────────────────────────────
    def test_02_timeout_then_success_retry(self):
        """If first attempt times out, retry should succeed."""
        mock_success = MagicMock()
        mock_success.status_code = 200
        mock_success.text = json.dumps({
            'jsonrpc': '2.0', 'id': 'test',
            'result': {'content': [{'text': '{"results": [{"id": "1", "title": "Page"}]}'}]}
        })
        mock_success.headers = {'Content-Type': 'application/json'}
        mock_success.raise_for_status = MagicMock()

        call_count = [0]
        def side_effect(*args, **kwargs):
            call_count[0] += 1
            if call_count[0] == 1:
                raise requests.exceptions.Timeout("Connection timed out")
            return mock_success

        with patch('requests.post', side_effect=side_effect):
            result = app_module._confluence_mcp_call(
                'confluence_search', {'cql': 'type=page'}, timeout=5, max_retries=1
            )

        self.assertIsNotNone(result, "Should succeed on retry")
        self.assertEqual(call_count[0], 2, "Should have made 2 attempts")
        self.assertEqual(app_module._confluence_mcp_health['consecutive_timeouts'], 0)

    # ──────────────────────────────────────────────────────────────────────
    # Test 3: All retries fail — returns None, increments timeout counter
    # ──────────────────────────────────────────────────────────────────────
    def test_03_all_retries_fail(self):
        """If all retries fail, should return None and increment consecutive_timeouts."""
        with patch('requests.post', side_effect=requests.exceptions.Timeout("timeout")):
            result = app_module._confluence_mcp_call(
                'confluence_search', {'cql': 'type=page'}, timeout=2, max_retries=1
            )

        self.assertIsNone(result, "Should return None when all retries fail")
        self.assertEqual(app_module._confluence_mcp_health['consecutive_timeouts'], 1)
        self.assertIn('timeout', app_module._confluence_mcp_health['last_error'].lower())

    # ──────────────────────────────────────────────────────────────────────
    # Test 4: Circuit breaker activates after 3 consecutive timeouts
    # ──────────────────────────────────────────────────────────────────────
    def test_04_circuit_breaker_activates(self):
        """After 3 consecutive timeouts, circuit breaker should skip calls."""
        app_module._confluence_mcp_health = {
            'consecutive_timeouts': 3,
            'last_success': 0,
            'last_failure': time.time(),  # Recent failure
            'last_error': 'raw HTTP timeout after 30000ms',
        }

        with patch('requests.post') as mock_post:
            result = app_module._confluence_mcp_call(
                'confluence_search', {'cql': 'type=page'}, timeout=5
            )

        self.assertIsNone(result, "Circuit breaker should return None immediately")
        mock_post.assert_not_called()

    # ──────────────────────────────────────────────────────────────────────
    # Test 5: Circuit breaker resets after cooldown period
    # ──────────────────────────────────────────────────────────────────────
    def test_05_circuit_breaker_resets_after_cooldown(self):
        """Circuit breaker should allow calls after the 2-minute cooldown."""
        app_module._confluence_mcp_health = {
            'consecutive_timeouts': 5,
            'last_success': 0,
            'last_failure': time.time() - 130,  # 130s ago — past 120s cooldown
            'last_error': 'old timeout',
        }

        mock_response = MagicMock()
        mock_response.status_code = 200
        mock_response.text = json.dumps({
            'jsonrpc': '2.0', 'id': 'test',
            'result': {'content': [{'text': '{"results": []}'}]}
        })
        mock_response.headers = {'Content-Type': 'application/json'}
        mock_response.raise_for_status = MagicMock()

        with patch('requests.post', return_value=mock_response) as mock_post:
            result = app_module._confluence_mcp_call(
                'confluence_search', {'cql': 'type=page'}, timeout=5, max_retries=0
            )

        self.assertIsNotNone(result, "Should allow call after cooldown")
        mock_post.assert_called_once()
        self.assertEqual(app_module._confluence_mcp_health['consecutive_timeouts'], 0)

    # ──────────────────────────────────────────────────────────────────────
    # Test 6: Connection error tracking
    # ──────────────────────────────────────────────────────────────────────
    def test_06_connection_error_tracked(self):
        """ConnectionError should be tracked in health state."""
        with patch('requests.post', side_effect=requests.exceptions.ConnectionError("Connection refused")):
            result = app_module._confluence_mcp_call(
                'confluence_search', {'cql': 'type=page'}, timeout=5, max_retries=0
            )

        self.assertIsNone(result)
        self.assertEqual(app_module._confluence_mcp_health['consecutive_timeouts'], 1)
        self.assertIn('connection error', app_module._confluence_mcp_health['last_error'].lower())

    # ──────────────────────────────────────────────────────────────────────
    # Test 7: HTTP error tracking (502)
    # ──────────────────────────────────────────────────────────────────────
    def test_07_http_error_tracked(self):
        """HTTP errors (e.g. 502) should be tracked."""
        mock_response = MagicMock()
        mock_response.status_code = 502
        mock_response.text = 'Bad Gateway'
        mock_response.headers = {'Content-Type': 'text/html'}
        mock_response.raise_for_status.side_effect = requests.exceptions.HTTPError(
            response=mock_response
        )

        with patch('requests.post', return_value=mock_response):
            result = app_module._confluence_mcp_call(
                'confluence_search', {'cql': 'type=page'}, timeout=5, max_retries=0
            )

        self.assertIsNone(result)
        self.assertEqual(app_module._confluence_mcp_health['consecutive_timeouts'], 1)
        self.assertIn('HTTP 502', app_module._confluence_mcp_health['last_error'])

    # ──────────────────────────────────────────────────────────────────────
    # Test 8: max_retries=0 means single attempt only
    # ──────────────────────────────────────────────────────────────────────
    def test_08_no_retries_when_max_zero(self):
        """max_retries=0 should attempt exactly once."""
        call_count = [0]
        def side_effect(*args, **kwargs):
            call_count[0] += 1
            raise requests.exceptions.Timeout("timeout")

        with patch('requests.post', side_effect=side_effect):
            result = app_module._confluence_mcp_call(
                'confluence_search', {'cql': 'type=page'}, timeout=2, max_retries=0
            )

        self.assertIsNone(result)
        self.assertEqual(call_count[0], 1, "max_retries=0 should make exactly 1 attempt")


class TestConfluenceSearchTolerance(unittest.TestCase):
    """Test that _confluence_search tolerates up to 2 timeouts before giving up."""

    def setUp(self):
        app_module._confluence_mcp_health = {
            'consecutive_timeouts': 0,
            'last_success': 0,
            'last_failure': 0,
            'last_error': '',
        }
        app_module._CONFLUENCE_TOOLS = {'confluence_search': 'Search Confluence'}
        app_module._cache.clear()

    # ──────────────────────────────────────────────────────────────────────
    # Test 9: Search continues after first timeout
    # ──────────────────────────────────────────────────────────────────────
    def test_09_search_continues_after_first_timeout(self):
        """First MCP timeout should NOT abort all remaining CQL strategies."""
        call_count = [0]
        def mock_mcp_call(tool_name, arguments, timeout=30, max_retries=1):
            call_count[0] += 1
            if call_count[0] == 1:
                return None  # First call times out
            return json.dumps({
                'results': [{'id': '42', 'title': 'Found Page', 'space': 'TEAM1'}]
            })

        with patch.object(app_module, '_confluence_mcp_call', side_effect=mock_mcp_call):
            results = app_module._confluence_search('test query', space_key='TEAM1')

        self.assertTrue(len(results) > 0, "Should find results from second strategy even after first timeout")
        self.assertTrue(call_count[0] >= 2, "Should have tried at least 2 strategies")

    # ──────────────────────────────────────────────────────────────────────
    # Test 10: Search stops after 2 timeouts
    # ──────────────────────────────────────────────────────────────────────
    def test_10_search_stops_after_two_timeouts(self):
        """After 2 MCP timeouts, search should stop trying more strategies."""
        call_count = [0]
        def mock_mcp_call(tool_name, arguments, timeout=30, max_retries=1):
            call_count[0] += 1
            return None  # Always timeout

        with patch.object(app_module, '_confluence_mcp_call', side_effect=mock_mcp_call):
            results = app_module._confluence_search('test query', space_key='TEAM1')

        self.assertEqual(len(results), 0, "Should return empty when all strategies time out")
        self.assertEqual(call_count[0], 2,
                         "Should stop after exactly 2 timeouts (MAX_MCP_TIMEOUTS)")

    # ──────────────────────────────────────────────────────────────────────
    # Test 11: Search returns cached results
    # ──────────────────────────────────────────────────────────────────────
    def test_11_search_returns_cached(self):
        """Cached results should be returned without making MCP calls."""
        cached = [{'id': '1', 'title': 'Cached Page'}]
        app_module._cache_set(('confluence_search', 'cached query', None), cached)

        with patch.object(app_module, '_confluence_mcp_call') as mock:
            results = app_module._confluence_search('cached query')

        mock.assert_not_called()
        self.assertEqual(len(results), 1)
        self.assertEqual(results[0]['title'], 'Cached Page')


class TestHealthEndpoint(unittest.TestCase):
    """Test the /api/confluence-mcp-health endpoint."""

    def setUp(self):
        app_module.app.config['TESTING'] = True
        self.client = app_module.app.test_client()
        app_module._confluence_mcp_health = {
            'consecutive_timeouts': 0,
            'last_success': 0,
            'last_failure': 0,
            'last_error': '',
        }

    # ──────────────────────────────────────────────────────────────────────
    # Test 12: Health endpoint shows healthy state
    # ──────────────────────────────────────────────────────────────────────
    def test_12_health_healthy(self):
        """Health endpoint should show 'healthy' when no timeouts."""
        app_module._confluence_mcp_health['last_success'] = time.time()

        resp = self.client.get('/api/confluence-mcp-health')
        data = resp.get_json()

        self.assertEqual(resp.status_code, 200)
        self.assertEqual(data['status'], 'healthy')
        self.assertEqual(data['consecutive_timeouts'], 0)
        self.assertFalse(data['circuit_breaker']['is_open'])

    # ──────────────────────────────────────────────────────────────────────
    # Test 13: Health endpoint shows degraded state
    # ──────────────────────────────────────────────────────────────────────
    def test_13_health_degraded(self):
        """Health endpoint should show 'degraded' with 1-2 timeouts."""
        app_module._confluence_mcp_health = {
            'consecutive_timeouts': 2,
            'last_success': time.time() - 60,
            'last_failure': time.time() - 10,
            'last_error': 'raw HTTP timeout after 30000ms',
        }

        resp = self.client.get('/api/confluence-mcp-health')
        data = resp.get_json()

        self.assertEqual(data['status'], 'degraded')
        self.assertEqual(data['consecutive_timeouts'], 2)
        self.assertFalse(data['circuit_breaker']['is_open'])
        self.assertTrue(len(data['tips']) > 0)

    # ──────────────────────────────────────────────────────────────────────
    # Test 14: Health endpoint shows circuit_open state
    # ──────────────────────────────────────────────────────────────────────
    def test_14_health_circuit_open(self):
        """Health endpoint should show 'circuit_open' with 3+ recent timeouts."""
        app_module._confluence_mcp_health = {
            'consecutive_timeouts': 5,
            'last_success': time.time() - 300,
            'last_failure': time.time() - 10,
            'last_error': 'connection error after 5000ms: Connection refused',
        }

        resp = self.client.get('/api/confluence-mcp-health')
        data = resp.get_json()

        self.assertEqual(data['status'], 'circuit_open')
        self.assertTrue(data['circuit_breaker']['is_open'])
        self.assertEqual(data['consecutive_timeouts'], 5)
        self.assertTrue(len(data['tips']) >= 3)
        tips_text = ' '.join(data['tips'])
        self.assertIn('confluence-mcp-reset', tips_text)

    # ──────────────────────────────────────────────────────────────────────
    # Test 15: Health shows not_configured
    # ──────────────────────────────────────────────────────────────────────
    def test_15_health_not_configured(self):
        """Health should show 'not_configured' when CONFLUENCE_MCP_URL is empty."""
        original = app_module.CONFLUENCE_MCP_URL
        try:
            app_module.CONFLUENCE_MCP_URL = ''
            resp = self.client.get('/api/confluence-mcp-health')
            data = resp.get_json()
            self.assertEqual(data['status'], 'not_configured')
        finally:
            app_module.CONFLUENCE_MCP_URL = original


class TestResetEndpoint(unittest.TestCase):
    """Test the /api/confluence-mcp-reset endpoint."""

    def setUp(self):
        app_module.app.config['TESTING'] = True
        self.client = app_module.app.test_client()

    # ──────────────────────────────────────────────────────────────────────
    # Test 16: Reset clears circuit breaker
    # ──────────────────────────────────────────────────────────────────────
    def test_16_reset_clears_circuit_breaker(self):
        """POST /api/confluence-mcp-reset should clear all health counters."""
        app_module._confluence_mcp_health = {
            'consecutive_timeouts': 5,
            'last_success': time.time() - 300,
            'last_failure': time.time() - 10,
            'last_error': 'raw HTTP timeout',
        }

        resp = self.client.post('/api/confluence-mcp-reset')
        data = resp.get_json()

        self.assertEqual(resp.status_code, 200)
        self.assertEqual(data['status'], 'reset')
        self.assertEqual(data['previous_state']['consecutive_timeouts'], 5)
        self.assertEqual(app_module._confluence_mcp_health['consecutive_timeouts'], 0)
        self.assertEqual(app_module._confluence_mcp_health['last_error'], '')

    # ──────────────────────────────────────────────────────────────────────
    # Test 17: Reset then health shows healthy
    # ──────────────────────────────────────────────────────────────────────
    def test_17_reset_then_health_is_healthy(self):
        """After reset, health endpoint should show healthy status."""
        app_module._confluence_mcp_health = {
            'consecutive_timeouts': 10,
            'last_success': 0,
            'last_failure': time.time(),
            'last_error': 'circuit was open',
        }

        self.client.post('/api/confluence-mcp-reset')
        resp = self.client.get('/api/confluence-mcp-health')
        data = resp.get_json()

        self.assertEqual(data['status'], 'healthy')
        self.assertFalse(data['circuit_breaker']['is_open'])

    # ──────────────────────────────────────────────────────────────────────
    # Test 18: Reset allows MCP calls to resume
    # ──────────────────────────────────────────────────────────────────────
    def test_18_reset_allows_calls_to_resume(self):
        """After resetting, MCP calls should be attempted again."""
        app_module._confluence_mcp_health = {
            'consecutive_timeouts': 5,
            'last_success': 0,
            'last_failure': time.time(),
            'last_error': 'timeout',
        }

        # Circuit breaker should block
        with patch('requests.post') as mock_post:
            result = app_module._confluence_mcp_call(
                'confluence_search', {'cql': 'type=page'}, timeout=5, max_retries=0
            )
            self.assertIsNone(result)
            mock_post.assert_not_called()

        # Reset
        self.client.post('/api/confluence-mcp-reset')

        # Now calls should go through
        mock_response = MagicMock()
        mock_response.status_code = 200
        mock_response.text = json.dumps({
            'jsonrpc': '2.0', 'id': 'test',
            'result': {'content': [{'text': '{"results": []}'}]}
        })
        mock_response.headers = {'Content-Type': 'application/json'}
        mock_response.raise_for_status = MagicMock()

        with patch('requests.post', return_value=mock_response) as mock_post:
            result = app_module._confluence_mcp_call(
                'confluence_search', {'cql': 'type=page'}, timeout=5, max_retries=0
            )
            self.assertIsNotNone(result, "Call should succeed after reset")
            mock_post.assert_called_once()


class TestDiagnosticEndpoint(unittest.TestCase):
    """Test the /api/confluence-diag endpoint includes health state."""

    def setUp(self):
        app_module.app.config['TESTING'] = True
        self.client = app_module.app.test_client()
        app_module._confluence_mcp_health = {
            'consecutive_timeouts': 2,
            'last_success': time.time() - 60,
            'last_failure': time.time() - 5,
            'last_error': 'raw HTTP timeout after 15000ms',
        }

    # ──────────────────────────────────────────────────────────────────────
    # Test 19: Diagnostic includes Step 6 MCP health
    # ──────────────────────────────────────────────────────────────────────
    def test_19_diagnostic_includes_mcp_health(self):
        """Diagnostic endpoint should include step6_mcp_health."""
        with patch('socket.getaddrinfo', return_value=[
            (2, 1, 6, '', ('127.0.0.1', 8080))
        ]), patch('socket.create_connection') as mock_sock, \
             patch('requests.post', side_effect=requests.exceptions.Timeout("diag timeout")), \
             patch('requests.get', side_effect=requests.exceptions.Timeout("diag timeout")):

            mock_sock_instance = MagicMock()
            mock_sock.return_value = mock_sock_instance

            resp = self.client.get('/api/confluence-diag')
            data = resp.get_json()

        self.assertIn('step6_mcp_health', data)
        # Step 5 calls _confluence_mcp_call which times out → increments from 2 to 3
        self.assertGreaterEqual(data['step6_mcp_health']['consecutive_timeouts'], 2)
        self.assertIn('timeout', data['step6_mcp_health']['last_error'].lower())


class TestEdgeCases(unittest.TestCase):
    """Test edge cases and boundary conditions."""

    def setUp(self):
        app_module._confluence_mcp_health = {
            'consecutive_timeouts': 0,
            'last_success': 0,
            'last_failure': 0,
            'last_error': '',
        }

    # ──────────────────────────────────────────────────────────────────────
    # Test 20: MCP URL not configured returns None
    # ──────────────────────────────────────────────────────────────────────
    def test_20_no_mcp_url_returns_none(self):
        """When CONFLUENCE_MCP_URL is empty, should return None cleanly."""
        original = app_module.CONFLUENCE_MCP_URL
        try:
            app_module.CONFLUENCE_MCP_URL = ''
            result = app_module._confluence_mcp_call(
                'confluence_search', {'cql': 'type=page'}, timeout=5
            )
            self.assertIsNone(result)
            self.assertEqual(app_module._confluence_mcp_health['consecutive_timeouts'], 0)
        finally:
            app_module.CONFLUENCE_MCP_URL = original

    # ──────────────────────────────────────────────────────────────────────
    # Test 21: Circuit breaker at exact threshold
    # ──────────────────────────────────────────────────────────────────────
    def test_21_circuit_breaker_at_exact_threshold(self):
        """Circuit breaker should activate at exactly 3 consecutive timeouts."""
        app_module._confluence_mcp_health = {
            'consecutive_timeouts': 2,
            'last_success': 0,
            'last_failure': time.time(),
            'last_error': 'timeout',
        }

        # Should still try (2 < 3)
        with patch('requests.post', side_effect=requests.exceptions.Timeout("timeout")):
            app_module._confluence_mcp_call(
                'confluence_search', {'cql': 'type=page'}, timeout=2, max_retries=0
            )

        self.assertEqual(app_module._confluence_mcp_health['consecutive_timeouts'], 3)

        # Now at exactly 3 — should be blocked
        with patch('requests.post') as mock_post:
            result = app_module._confluence_mcp_call(
                'confluence_search', {'cql': 'type=page'}, timeout=2, max_retries=0
            )
            self.assertIsNone(result)
            mock_post.assert_not_called()

    # ──────────────────────────────────────────────────────────────────────
    # Test 22: SSE response parsing
    # ──────────────────────────────────────────────────────────────────────
    def test_22_sse_response_parsing(self):
        """MCP call should correctly parse SSE responses."""
        sse_body = (
            'event: message\n'
            'data: {"jsonrpc":"2.0","id":"test","result":{"content":[{"text":"{\\"results\\":[{\\"id\\":\\"99\\",\\"title\\":\\"SSE Page\\"}]}"}]}}\n'
            '\n'
        )
        mock_response = MagicMock()
        mock_response.status_code = 200
        mock_response.text = sse_body
        mock_response.headers = {'Content-Type': 'text/event-stream'}
        mock_response.raise_for_status = MagicMock()

        with patch('requests.post', return_value=mock_response):
            result = app_module._confluence_mcp_call(
                'confluence_search', {'cql': 'type=page'}, timeout=5, max_retries=0
            )

        self.assertIsNotNone(result)
        self.assertEqual(app_module._confluence_mcp_health['consecutive_timeouts'], 0)

    # ──────────────────────────────────────────────────────────────────────
    # Test 23: Keyword extraction
    # ──────────────────────────────────────────────────────────────────────
    def test_23_keyword_extraction(self):
        """_extract_search_keywords should strip stop words correctly."""
        fn = app_module._extract_search_keywords
        self.assertEqual(fn('what is the jenkins url'), 'jenkins url')
        self.assertEqual(fn('how do I deploy to production'), 'deploy production')
        self.assertEqual(fn('where can I find the runbook for payments'), 'runbook payments')
        result = fn('how do I')
        self.assertTrue(len(result) > 0)

    # ──────────────────────────────────────────────────────────────────────
    # Test 24: Consecutive timeouts accumulate correctly
    # ──────────────────────────────────────────────────────────────────────
    def test_24_consecutive_timeouts_accumulate(self):
        """Each failed call should increment consecutive_timeouts by 1."""
        with patch('requests.post', side_effect=requests.exceptions.Timeout("timeout")):
            for i in range(3):
                app_module._confluence_mcp_call(
                    'confluence_search', {'cql': 'type=page'}, timeout=2, max_retries=0
                )
                expected = i + 1
                actual = app_module._confluence_mcp_health['consecutive_timeouts']
                self.assertEqual(actual, expected,
                    f"After {expected} failures, consecutive_timeouts should be {expected}, got {actual}")

    # ──────────────────────────────────────────────────────────────────────
    # Test 25: Technical keyword preservation
    # ──────────────────────────────────────────────────────────────────────
    def test_25_technical_terms_preserved(self):
        """Technical terms should be preserved in keyword extraction."""
        fn = app_module._extract_search_keywords
        result = fn('openshift egress static ips configuration')
        self.assertIn('openshift', result)
        self.assertIn('egress', result)
        self.assertIn('configuration', result)


if __name__ == '__main__':
    unittest.main(verbosity=2, buffer=False)
