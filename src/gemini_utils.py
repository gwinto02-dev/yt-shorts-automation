"""
Backwards-compatible wrapper module forwarding legacy Gemini function names to Groq utility functions.
"""
from src.groq_utils import (
    parse_retry_delay_from_error,
    is_rate_limit_error,
    rate_limited_groq_call as rate_limited_gemini_call,
    check_groq_quota_preflight as check_gemini_quota_preflight
)
