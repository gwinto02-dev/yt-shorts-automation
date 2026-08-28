import sys
import unittest
import time
from pathlib import Path
from unittest.mock import MagicMock, patch

# Add project root to sys.path
BASE_DIR = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(BASE_DIR))

import config
from src.groq_utils import (
    parse_retry_delay_from_error,
    is_rate_limit_error,
    rate_limited_groq_call,
    check_groq_quota_preflight
)

class TestGroqUtils(unittest.TestCase):

    def test_config_model_name_consistency(self):
        """Verify central GROQ_MODEL setting is llama-3.3-70b-versatile."""
        self.assertEqual(config.GROQ_MODEL, "llama-3.3-70b-versatile")

    def test_parse_retry_delay_from_error(self):
        """Test retryDelay parsing from various Groq API error string formats."""
        self.assertEqual(parse_retry_delay_from_error("Please try again in 5.2s"), 5.2)
        self.assertEqual(parse_retry_delay_from_error("Rate limit hit, retry after 10 seconds"), 10.0)
        self.assertEqual(parse_retry_delay_from_error('{"error": "rate_limit_exceeded", "retryDelay": "4.5s"}'), 4.5)
        self.assertEqual(parse_retry_delay_from_error("General 500 internal server error"), 0.0)

    def test_is_rate_limit_error(self):
        """Test 429 / rate_limit_exceeded error detection."""
        err_429 = Exception("429: rate_limit_exceeded. Please try again in 5s.")
        self.assertTrue(is_rate_limit_error(err_429))

        err_401 = Exception("401: Unauthorized API key")
        self.assertFalse(is_rate_limit_error(err_401))

    def test_rate_limited_groq_call_spacing(self):
        """Test that rate_limited_groq_call enforces minimum spacing between sequential calls."""
        mock_func = MagicMock(return_value="mock_result")
        
        start = time.time()
        res1 = rate_limited_groq_call(mock_func, min_interval=0.2, max_retries=1)
        res2 = rate_limited_groq_call(mock_func, min_interval=0.2, max_retries=1)
        duration = time.time() - start

        self.assertEqual(res1, "mock_result")
        self.assertEqual(res2, "mock_result")
        self.assertEqual(mock_func.call_count, 2)
        self.assertGreaterEqual(duration, 0.18)

    def test_rate_limited_groq_call_retry_delay_backoff(self):
        """Test backoff retry on 429 error with parsed retryDelay and eventual success."""
        mock_func = MagicMock()
        mock_func.side_effect = [
            Exception("429 rate_limit_exceeded try again in 0.1s"),
            "success_after_retry"
        ]

        with patch("time.sleep") as mock_sleep:
            res = rate_limited_groq_call(mock_func, min_interval=0.0, max_retries=2)
            self.assertEqual(res, "success_after_retry")
            self.assertEqual(mock_func.call_count, 2)
            self.assertTrue(mock_sleep.called)

    def test_rate_limited_groq_call_max_retries_exhausted(self):
        """Test that max retries on 429 raises exception for template fallback."""
        mock_func = MagicMock()
        mock_func.side_effect = Exception("429 rate_limit_exceeded")

        with patch("time.sleep"):
            with self.assertRaises(Exception) as exc_info:
                rate_limited_groq_call(mock_func, min_interval=0.0, max_retries=2)
            self.assertIn("429 rate_limit_exceeded", str(exc_info.exception))
            self.assertEqual(mock_func.call_count, 3)

    def test_check_groq_quota_preflight_no_key(self):
        """Test pre-flight quota check when GROQ_API_KEY is empty."""
        with patch.object(config, "GROQ_API_KEY", ""), patch.object(config, "GEMINI_API_KEY", ""):
            ok, msg = check_groq_quota_preflight()
            self.assertFalse(ok)
            self.assertIn("GROQ_API_KEY is not configured", msg)

    def test_check_groq_quota_preflight_success(self):
        """Test pre-flight quota check when Groq API call succeeds."""
        mock_choice = MagicMock()
        mock_choice.message.content = "OK"
        mock_response = MagicMock()
        mock_response.choices = [mock_choice]
        
        mock_groq_module = MagicMock()
        with patch.dict("sys.modules", {"groq": mock_groq_module}):
            with patch.object(config, "GROQ_API_KEY", "dummy_groq_key"):
                with patch("src.groq_utils.rate_limited_groq_call", return_value=mock_response):
                    ok, msg = check_groq_quota_preflight()
                    self.assertTrue(ok)
                    self.assertIn("operational and ready", msg)

if __name__ == "__main__":
    unittest.main()
