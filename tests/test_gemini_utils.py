import sys
from pathlib import Path

# Add project root to sys.path
BASE_DIR = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(BASE_DIR))

import time
import pytest
from unittest.mock import MagicMock, patch

import config
from src.gemini_utils import (
    parse_retry_delay_from_error,
    is_rate_limit_error,
    rate_limited_gemini_call,
    check_gemini_quota_preflight
)

def test_config_model_name_consistency():
    """Verify central GEMINI_MODEL setting is gemini-3.6-flash."""
    assert config.GEMINI_MODEL == "gemini-3.6-flash"

def test_parse_retry_delay_from_error():
    """Test retryDelay parsing from various Gemini API error string formats."""
    assert parse_retry_delay_from_error("429 Quota exceeded: 'retryDelay': '55s'") == 55.0
    assert parse_retry_delay_from_error('RESOURCE_EXHAUSTED {"retryDelay": "12.5s"}') == 12.5
    assert parse_retry_delay_from_error("Rate limit hit, please retry after 40 seconds") == 40.0
    assert parse_retry_delay_from_error("General 500 internal server error") == 0.0

def test_is_rate_limit_error():
    """Test 429 / RESOURCE_EXHAUSTED error detection."""
    err_429 = Exception("429 RESOURCE_EXHAUSTED: Quota exceeded for metric: generate_content_free_tier_requests")
    assert is_rate_limit_error(err_429) is True

    err_403 = Exception("403 PERMISSION_DENIED: Access denied")
    assert is_rate_limit_error(err_403) is False

def test_rate_limited_gemini_call_spacing():
    """Test that rate_limited_gemini_call enforces minimum spacing between sequential calls."""
    mock_func = MagicMock(return_value="mock_result")
    
    start = time.time()
    res1 = rate_limited_gemini_call(mock_func, min_interval=0.2, max_retries=1)
    res2 = rate_limited_gemini_call(mock_func, min_interval=0.2, max_retries=1)
    duration = time.time() - start

    assert res1 == "mock_result"
    assert res2 == "mock_result"
    assert mock_func.call_count == 2
    assert duration >= 0.18  # Enforced spacing delay

def test_rate_limited_gemini_call_retry_delay_backoff():
    """Test backoff retry on 429 error with parsed retryDelay and eventual success."""
    mock_func = MagicMock()
    # First call raises 429 with 0.1s retryDelay, second call succeeds
    mock_func.side_effect = [
        Exception("429 RESOURCE_EXHAUSTED 'retryDelay': '0.1s'"),
        "success_after_retry"
    ]

    with patch("time.sleep") as mock_sleep:
        res = rate_limited_gemini_call(mock_func, min_interval=0.0, max_retries=2)
        assert res == "success_after_retry"
        assert mock_func.call_count == 2
        assert mock_sleep.called

def test_rate_limited_gemini_call_max_retries_exhausted():
    """Test that max retries on 429 raises exception for template fallback."""
    mock_func = MagicMock()
    mock_func.side_effect = Exception("429 RESOURCE_EXHAUSTED limit: 5")

    with patch("time.sleep"):
        with pytest.raises(Exception) as exc_info:
            rate_limited_gemini_call(mock_func, min_interval=0.0, max_retries=2)
        assert "429 RESOURCE_EXHAUSTED" in str(exc_info.value)
        assert mock_func.call_count == 3  # Initial + 2 retries

def test_check_gemini_quota_preflight_no_key():
    """Test pre-flight quota check when GEMINI_API_KEY is empty."""
    with patch.object(config, "GEMINI_API_KEY", ""):
        ok, msg = check_gemini_quota_preflight()
        assert ok is False
        assert "GEMINI_API_KEY is not configured" in msg

def test_check_gemini_quota_preflight_success():
    """Test pre-flight quota check when API call succeeds."""
    mock_response = MagicMock()
    mock_response.text = "OK"
    
    with patch.object(config, "GEMINI_API_KEY", "dummy_key"):
        with patch("src.gemini_utils.rate_limited_gemini_call", return_value=mock_response):
            with patch("google.genai.Client"):
                ok, msg = check_gemini_quota_preflight()
                assert ok is True
                assert "operational and ready" in msg
