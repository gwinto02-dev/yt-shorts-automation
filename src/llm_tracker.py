import logging
import config

logger = logging.getLogger(__name__)

_LLM_CALL_COUNT = 0

def reset_llm_calls():
    """Reset LLM call count for a new run."""
    global _LLM_CALL_COUNT
    _LLM_CALL_COUNT = 0

def increment_llm_calls(count: int = 1) -> int:
    """Increment and return total LLM call count for the current run."""
    global _LLM_CALL_COUNT
    _LLM_CALL_COUNT += count
    logger.info(f"[LLM Tracker] Total LLM API calls in current run: {_LLM_CALL_COUNT}")
    return _LLM_CALL_COUNT

def get_llm_call_count() -> int:
    """Get total LLM call count for current run."""
    return _LLM_CALL_COUNT

def is_nearing_rate_limit() -> bool:
    """Check if total LLM call count exceeds the 80% free-tier warning threshold."""
    return _LLM_CALL_COUNT >= config.LLM_CALL_WARNING_THRESHOLD
