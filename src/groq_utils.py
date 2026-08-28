import logging
import re
import time
import random
import threading
from typing import Callable, Any, Tuple

import config

logger = logging.getLogger(__name__)

_groq_lock = threading.Lock()
_last_groq_call_time = 0.0

def parse_retry_delay_from_error(error_str: str) -> float:
    """
    Parse retryDelay value (in seconds) from Groq API error message or response string.
    Examples:
      - Please try again in 5.2s
      - retry after 5.0s
      - 'retryDelay': '5.0s'
      - in 4.5s
    Returns 0.0 if no retryDelay found.
    """
    if not error_str:
        return 0.0

    # Match 'try again in 5.2s' or 'try again in 5.2'
    match = re.search(r"try\s+again\s+in\s+(\d+(?:\.\d+)?)s?", error_str, re.IGNORECASE)
    if match:
        try:
            return float(match.group(1))
        except ValueError:
            pass

    # Match 'retry after 5.0s' or 'retry after 5'
    match = re.search(r"retry\s+after\s+(\d+(?:\.\d+)?)s?", error_str, re.IGNORECASE)
    if match:
        try:
            return float(match.group(1))
        except ValueError:
            pass

    # Match 'retryDelay': '5.0s'
    match = re.search(r"['\"]?retryDelay['\"]?\s*:\s*['\"]?(\d+(?:\.\d+)?)s?['\"]?", error_str, re.IGNORECASE)
    if match:
        try:
            return float(match.group(1))
        except ValueError:
            pass

    # Match 'in 5.2s'
    match = re.search(r"\bin\s+(\d+(?:\.\d+)?)s\b", error_str, re.IGNORECASE)
    if match:
        try:
            return float(match.group(1))
        except ValueError:
            pass

    return 0.0

def is_rate_limit_error(e: Exception) -> bool:
    """Check if exception is a 429 rate limit or quota exceeded error."""
    err_str = str(e).upper()
    if "429" in err_str or "RATE_LIMIT" in err_str or "RATE LIMIT" in err_str or "RESOURCE_EXHAUSTED" in err_str or "TPM" in err_str or "RPM" in err_str:
        return True
    
    code = getattr(e, "code", None)
    status_code = getattr(e, "status_code", None)
    if code in (429, "429", "RESOURCE_EXHAUSTED") or status_code == 429:
        return True

    # Check groq SDK specific RateLimitError if imported
    try:
        from groq import RateLimitError
        if isinstance(e, RateLimitError):
            return True
    except ImportError:
        pass

    return False

def rate_limited_groq_call(call_func: Callable[..., Any], *args, **kwargs) -> Any:
    """
    Executes a Groq API call function with rate-limit aware call spacing
    and exponential backoff with jitter on 429 rate limit errors.
    
    Logs execution paths clearly:
      - Real Groq success
      - Backoff retry success
      - Fallback to template generator trigger
    """
    global _last_groq_call_time

    max_retries = kwargs.pop("max_retries", config.GROQ_MAX_RETRIES)
    min_interval = kwargs.pop("min_interval", config.GROQ_MIN_CALL_INTERVAL)

    attempt = 0
    while attempt <= max_retries:
        # Enforce rate-limit aware call spacing across sequential calls
        with _groq_lock:
            now = time.time()
            elapsed = now - _last_groq_call_time
            if elapsed < min_interval and _last_groq_call_time > 0.0:
                sleep_time = min_interval - elapsed
                logger.info(
                    f"[Groq Rate Limiter] Spacing API call: sleeping {sleep_time:.2f}s to respect rate limit..."
                )
                time.sleep(sleep_time)
            _last_groq_call_time = time.time()

        try:
            result = call_func(*args, **kwargs)
            if attempt == 0:
                logger.info("[Groq API SUCCESS] Call succeeded on attempt #1.")
            else:
                logger.info(f"[Groq API RETRY SUCCESS] Call succeeded on retry attempt #{attempt + 1} after backoff.")
            return result

        except Exception as e:
            if not is_rate_limit_error(e):
                logger.warning(f"[Groq API ERROR] Non-rate-limit error encountered: {e}")
                raise e

            attempt += 1
            if attempt > max_retries:
                logger.warning(
                    f"[Groq API FALLBACK] Max retries ({max_retries}) exhausted on 429 rate limit error. "
                    f"Falling back to template generator."
                )
                raise e

            err_str = str(e)
            retry_delay = parse_retry_delay_from_error(err_str)
            jitter = random.uniform(0.0, 3.0)

            if retry_delay > 0.0:
                total_wait = retry_delay + jitter
                logger.warning(
                    f"[Groq 429 Backoff] Parsed API retryDelay of {retry_delay:.1f}s. "
                    f"Waiting {total_wait:.1f}s (with +{jitter:.1f}s jitter) before retry attempt #{attempt + 1}/{max_retries + 1}..."
                )
            else:
                base_wait = 10.0 * (2 ** (attempt - 1))
                total_wait = base_wait + jitter
                logger.warning(
                    f"[Groq 429 Backoff] Rate limit detected (attempt #{attempt}/{max_retries}). "
                    f"Exponential backoff: waiting {total_wait:.1f}s before retry..."
                )

            time.sleep(total_wait)


def check_groq_quota_preflight() -> Tuple[bool, str]:
    """
    Lightweight pre-flight quota check run before the main pipeline starts.
    Makes 1 minimal ping call via Groq API to verify API key validity and reachability.
    Returns: (is_available: bool, status_message: str)
    """
    logger.info(">>> RUNNING GROQ PRE-FLIGHT QUOTA CHECK...")

    if not config.GROQ_API_KEY:
        msg = "[Pre-flight Quota Check FAIL] GROQ_API_KEY is not configured in environment."
        logger.warning(msg)
        return False, msg

    try:
        from groq import Groq
        client = Groq(api_key=config.GROQ_API_KEY)
        
        response = rate_limited_groq_call(
            client.chat.completions.create,
            model=config.GROQ_MODEL,
            messages=[{"role": "user", "content": "Say 'OK' if active."}]
        )
        if response and response.choices and response.choices[0].message.content:
            msg = f"[Pre-flight Quota Check SUCCESS] Groq API is operational and ready (Model: {config.GROQ_MODEL})."
            logger.info(msg)
            return True, msg
        else:
            msg = f"[Pre-flight Quota Check WARN] Groq API returned empty response (Model: {config.GROQ_MODEL})."
            logger.warning(msg)
            return False, msg

    except Exception as e:
        err_str = str(e)
        if is_rate_limit_error(e):
            msg = f"[Pre-flight Quota Check BLOCKED] Groq API is currently rate-limited (429): {err_str}"
        elif "401" in err_str or "UNAUTHORIZED" in err_str or "API key" in err_str:
            msg = f"[Pre-flight Quota Check BLOCKED] Groq API key unauthorized (401): {err_str}"
        else:
            msg = f"[Pre-flight Quota Check FAILED] Groq API call failed: {err_str}"
        logger.error(msg)
        return False, msg
