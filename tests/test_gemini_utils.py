import sys
import unittest
import time
from pathlib import Path
from unittest.mock import MagicMock, patch

# Add project root to sys.path
BASE_DIR = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(BASE_DIR))

import config
from src.gemini_utils import (
    parse_retry_delay_from_error,
    is_rate_limit_error,
    rate_limited_gemini_call,
    check_gemini_quota_preflight
)

class TestGeminiUtils(unittest.TestCase):

    def test_config_model_name_consistency(self):
        """Verify central GEMINI_MODEL setting is gemini-3.6-flash."""
        self.assertEqual(config.GEMINI_MODEL, "gemini-3.6-flash")

    def test_parse_retry_delay_from_error(self):
        """Test retryDelay parsing from various Gemini API error string formats."""
        self.assertEqual(parse_retry_delay_from_error("429 Quota exceeded: 'retryDelay': '55s'"), 55.0)
        self.assertEqual(parse_retry_delay_from_error('RESOURCE_EXHAUSTED {"retryDelay": "12.5s"}'), 12.5)
        self.assertEqual(parse_retry_delay_from_error("Rate limit hit, please retry after 40 seconds"), 40.0)
        self.assertEqual(parse_retry_delay_from_error("General 500 internal server error"), 0.0)

    def test_is_rate_limit_error(self):
        """Test 429 / RESOURCE_EXHAUSTED error detection."""
        err_429 = Exception("429 RESOURCE_EXHAUSTED: Quota exceeded for metric: generate_content_free_tier_requests")
        self.assertTrue(is_rate_limit_error(err_429))

        err_403 = Exception("403 PERMISSION_DENIED: Access denied")
        self.assertFalse(is_rate_limit_error(err_403))

    def test_rate_limited_gemini_call_spacing(self):
        """Test that rate_limited_gemini_call enforces minimum spacing between sequential calls."""
        mock_func = MagicMock(return_value="mock_result")
        
        start = time.time()
        res1 = rate_limited_gemini_call(mock_func, min_interval=0.2, max_retries=1)
        res2 = rate_limited_gemini_call(mock_func, min_interval=0.2, max_retries=1)
        duration = time.time() - start

        self.assertEqual(res1, "mock_result")
        self.assertEqual(res2, "mock_result")
        self.assertEqual(mock_func.call_count, 2)
        self.assertGreaterEqual(duration, 0.18)  # Enforced spacing delay

    def test_rate_limited_gemini_call_retry_delay_backoff(self):
        """Test backoff retry on 429 error with parsed retryDelay and eventual success."""
        mock_func = MagicMock()
        # First call raises 429 with 0.1s retryDelay, second call succeeds
        mock_func.side_effect = [
            Exception("429 RESOURCE_EXHAUSTED 'retryDelay': '0.1s'"),
            "success_after_retry"
        ]

        with patch("time.sleep") as mock_sleep:
            res = rate_limited_gemini_call(mock_func, min_interval=0.0, max_retries=2)
            self.assertEqual(res, "success_after_retry")
            self.assertEqual(mock_func.call_count, 2)
            self.assertTrue(mock_sleep.called)

    def test_rate_limited_gemini_call_max_retries_exhausted(self):
        """Test that max retries on 429 raises exception for template fallback."""
        mock_func = MagicMock()
        mock_func.side_effect = Exception("429 RESOURCE_EXHAUSTED limit: 5")

        with patch("time.sleep"):
            with self.assertRaises(Exception) as exc_info:
                rate_limited_gemini_call(mock_func, min_interval=0.0, max_retries=2)
            self.assertIn("429 RESOURCE_EXHAUSTED", str(exc_info.exception))
            self.assertEqual(mock_func.call_count, 3)  # Initial + 2 retries

    def test_check_gemini_quota_preflight_no_key(self):
        """Test pre-flight quota check when GEMINI_API_KEY is empty."""
        with patch.object(config, "GEMINI_API_KEY", ""):
            ok, msg = check_gemini_quota_preflight()
            self.assertFalse(ok)
            self.assertIn("GEMINI_API_KEY is not configured", msg)

    def test_check_gemini_quota_preflight_success(self):
        """Test pre-flight quota check when API call succeeds."""
        mock_response = MagicMock()
        mock_response.text = "OK"
        
        with patch.object(config, "GEMINI_API_KEY", "dummy_key"):
            with patch("src.gemini_utils.rate_limited_gemini_call", return_value=mock_response):
                with patch("google.genai.Client"):
                    ok, msg = check_gemini_quota_preflight()
                    self.assertTrue(ok)
                    self.assertIn("operational and ready", msg)

if __name__ == "__main__":
    unittest.main()
