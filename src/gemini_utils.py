import logging
import re
import time
import random
import threading
from typing import Callable, Any, Tuple

import config

logger = logging.getLogger(__name__)

_gemini_lock = threading.Lock()
_last_gemini_call_time = 0.0

def parse_retry_delay_from_error(error_str: str) -> float:
    """
    Parse retryDelay value (in seconds) from Gemini API error message or response string.
    Examples:
      - 'retryDelay': '55s'
      - retryDelay: 55.2s
      - retry after 55 seconds
    Returns 0.0 if no retryDelay found.
    """
    if not error_str:
        return 0.0

    # Match 'retryDelay': '55s' or "retryDelay": "55.2s" or retryDelay: 55s
    match = re.search(r"['\"]?retryDelay['\"]?\s*:\s*['\"]?(\d+(?:\.\d+)?)s?['\"]?", error_str, re.IGNORECASE)
    if match:
        try:
            return float(match.group(1))
        except ValueError:
            pass

    # Match "retry after 55s" or "retry after 55 seconds"
    match = re.search(r"retry\s+after\s+(\d+(?:\.\d+)?)", error_str, re.IGNORECASE)
    if match:
        try:
            return float(match.group(1))
        except ValueError:
            pass

    return 0.0

def is_rate_limit_error(e: Exception) -> bool:
    """Check if exception is a 429 RESOURCE_EXHAUSTED or rate limit error."""
    err_str = str(e).upper()
    if "429" in err_str or "RESOURCE_EXHAUSTED" in err_str or "QUOTA" in err_str or "RATE LIMIT" in err_str:
        return True
    
    # Check attributes on google.genai / google.api_core exceptions
    code = getattr(e, "code", None)
    status_code = getattr(e, "status_code", None)
    if code in (429, "429", "RESOURCE_EXHAUSTED") or status_code == 429:
        return True
        
    return False

def rate_limited_gemini_call(call_func: Callable[..., Any], *args, **kwargs) -> Any:
    """
    Executes a Gemini API call function with rate-limit aware call spacing (13-15s)
    and exponential backoff with jitter specifically on 429 RESOURCE_EXHAUSTED errors.
    
    Logs execution paths clearly:
      - Real Gemini success
      - Backoff retry success
      - Fallback to template generator trigger
    """
    global _last_gemini_call_time

    max_retries = kwargs.pop("max_retries", config.GEMINI_MAX_RETRIES)
    min_interval = kwargs.pop("min_interval", config.GEMINI_MIN_CALL_INTERVAL)

    attempt = 0
    while attempt <= max_retries:
        # Enforce rate-limit aware call spacing across sequential calls
        with _gemini_lock:
            now = time.time()
            elapsed = now - _last_gemini_call_time
            if elapsed < min_interval and _last_gemini_call_time > 0.0:
                sleep_time = min_interval - elapsed
                logger.info(
                    f"[Gemini Rate Limiter] Spacing API call: sleeping {sleep_time:.2f}s to respect free tier limit (5 req/min)..."
                )
                time.sleep(sleep_time)
            _last_gemini_call_time = time.time()

        try:
            result = call_func(*args, **kwargs)
            if attempt == 0:
                logger.info("[Gemini API SUCCESS] Call succeeded on attempt #1.")
            else:
                logger.info(f"[Gemini API RETRY SUCCESS] Call succeeded on retry attempt #{attempt + 1} after backoff.")
            return result

        except Exception as e:
            if not is_rate_limit_error(e):
                # Non-rate-limit exception (e.g. 403, network, bad prompt) -> fail immediately
                logger.warning(f"[Gemini API ERROR] Non-rate-limit error encountered: {e}")
                raise e

            attempt += 1
            if attempt > max_retries:
                logger.warning(
                    f"[Gemini API FALLBACK] Max retries ({max_retries}) exhausted on 429 RESOURCE_EXHAUSTED error. "
                    f"Falling back to template generator."
                )
                raise e

            err_str = str(e)
            retry_delay = parse_retry_delay_from_error(err_str)
            jitter = random.uniform(0.0, 5.0)

            if retry_delay > 0.0:
                total_wait = retry_delay + jitter
                logger.warning(
                    f"[Gemini 429 Backoff] Parsed API retryDelay of {retry_delay:.1f}s. "
                    f"Waiting {total_wait:.1f}s (with +{jitter:.1f}s jitter) before retry attempt #{attempt + 1}/{max_retries + 1}..."
                )
            else:
                base_wait = 30.0 * (2 ** (attempt - 1))
                total_wait = base_wait + jitter
                logger.warning(
                    f"[Gemini 429 Backoff] 429 RESOURCE_EXHAUSTED detected (attempt #{attempt}/{max_retries}). "
                    f"Exponential backoff: waiting {total_wait:.1f}s before retry..."
                )

            time.sleep(total_wait)


def check_gemini_quota_preflight() -> Tuple[bool, str]:
    """
    Lightweight pre-flight quota check run before the main pipeline starts.
    Makes 1 minimal ping call to verify API key validity, reachability, and rate limit status.
    Returns: (is_available: bool, status_message: str)
    """
    logger.info(">>> RUNNING GEMINI PRE-FLIGHT QUOTA CHECK...")

    if not config.GEMINI_API_KEY:
        msg = "[Pre-flight Quota Check FAIL] GEMINI_API_KEY is not configured in environment."
        logger.warning(msg)
        return False, msg

    try:
        from google import genai
        client = genai.Client(api_key=config.GEMINI_API_KEY)
        
        # Lightweight test call using rate_limited_gemini_call
        response = rate_limited_gemini_call(
            client.models.generate_content,
            model=config.GEMINI_MODEL,
            contents="Say 'OK' if you are active."
        )
        if response and response.text:
            msg = f"[Pre-flight Quota Check SUCCESS] Gemini API is operational and ready (Model: {config.GEMINI_MODEL})."
            logger.info(msg)
            return True, msg
        else:
            msg = f"[Pre-flight Quota Check WARN] Gemini API returned empty response (Model: {config.GEMINI_MODEL})."
            logger.warning(msg)
            return False, msg

    except Exception as e:
        err_str = str(e)
        if is_rate_limit_error(e):
            msg = f"[Pre-flight Quota Check BLOCKED] Gemini API is currently rate-limited (429 RESOURCE_EXHAUSTED): {err_str}"
        elif "403" in err_str or "PERMISSION_DENIED" in err_str:
            msg = f"[Pre-flight Quota Check BLOCKED] Gemini API permission denied (403 PERMISSION_DENIED): {err_str}"
        else:
            msg = f"[Pre-flight Quota Check FAILED] Gemini API call failed: {err_str}"
        logger.error(msg)
        return False, msg
