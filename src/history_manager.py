import json
import logging
from datetime import datetime, timedelta
from pathlib import Path
from typing import List, Dict, Any, Optional

import config
from src.llm_tracker import increment_llm_calls

logger = logging.getLogger(__name__)

def _load_json_file(file_path: Path) -> List[Dict[str, Any]]:
    """Helper to safely load a JSON array file."""
    if not file_path.exists():
        return []
    try:
        with open(file_path, "r", encoding="utf-8") as f:
            data = json.load(f)
            return data if isinstance(data, list) else []
    except Exception as e:
        logger.warning(f"Error reading history file {file_path}: {e}")
        return []

def _save_json_file(file_path: Path, data: List[Dict[str, Any]]):
    """Helper to safely write a JSON array file."""
    try:
        file_path.parent.mkdir(parents=True, exist_ok=True)
        with open(file_path, "w", encoding="utf-8") as f:
            json.dump(data, f, indent=2, ensure_ascii=False)
    except Exception as e:
        logger.error(f"Error writing history file {file_path}: {e}")

# ==================== CONCEPT HISTORY ====================

def get_recent_concepts(days: int = config.CONCEPT_COOLDOWN_DAYS) -> List[str]:
    """Retrieve concept types used within the last `days` days."""
    history = _load_json_file(config.CONCEPT_HISTORY_FILE)
    cutoff_date = datetime.now() - timedelta(days=days)
    
    recent_types = []
    for entry in history:
        date_str = entry.get("date")
        if date_str:
            try:
                entry_date = datetime.fromisoformat(date_str)
                if entry_date >= cutoff_date:
                    recent_types.append(entry.get("concept_type"))
            except ValueError:
                pass
    return recent_types

def is_concept_allowed_by_history(concept_type: str, days: int = config.CONCEPT_COOLDOWN_DAYS) -> bool:
    """Return True if `concept_type` was NOT used within the last `days` days."""
    recent_concepts = get_recent_concepts(days)
    is_allowed = concept_type not in recent_concepts
    if not is_allowed:
        logger.info(f"[HistoryManager] Concept '{concept_type}' was used recently in the last {days} days. Cooling down.")
    return is_allowed

def record_concept_usage(concept_type: str):
    """Record usage of a concept type with current timestamp."""
    history = _load_json_file(config.CONCEPT_HISTORY_FILE)
    history.append({
        "concept_type": concept_type,
        "date": datetime.now().isoformat(),
        "date_readable": datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    })
    _save_json_file(config.CONCEPT_HISTORY_FILE, history)
    logger.info(f"[HistoryManager] Recorded concept usage: '{concept_type}'")

# ==================== SHORTS ORIGINALITY HISTORY ====================

def record_short_history(concept_type: str, title: str, hook: str, script: str, video_id: Optional[str] = None):
    """Log details of a newly produced Short to history for future originality checks."""
    history = _load_json_file(config.SHORTS_HISTORY_FILE)
    history.append({
        "date": datetime.now().isoformat(),
        "date_readable": datetime.now().strftime("%Y-%m-%d"),
        "concept_type": concept_type,
        "title": title,
        "hook": hook,
        "script": script,
        "video_id": video_id or "N/A"
    })
    _save_json_file(config.SHORTS_HISTORY_FILE, history)
    logger.info(f"[HistoryManager] Saved Short to history log: '{title}' ({concept_type})")

def _calculate_jaccard_similarity(text1: str, text2: str) -> float:
    """Compute Jaccard similarity between two text strings based on word sets."""
    words1 = set(text1.lower().split())
    words2 = set(text2.lower().split())
    if not words1 or not words2:
        return 0.0
    intersection = words1.intersection(words2)
    union = words1.union(words2)
    return len(intersection) / len(union)

def check_originality_against_history(new_script: str, new_hook: str, new_title: str) -> Dict[str, Any]:
    """
    Compare new Short script/hook against all past Shorts in history.
    Returns:
      {"pass": bool, "reason": str, "matched_short_title": str or None}
    """
    history = _load_json_file(config.SHORTS_HISTORY_FILE)
    if not history:
        logger.info("[Originality Check] No past Shorts history found. Originality check passed.")
        return {"pass": True, "reason": "No previous Shorts in history to compare against.", "matched_short_title": None}

    # 1. Quick algorithmic check: Jaccard similarity against all past entries
    highest_sim = 0.0
    matched_entry = None

    for entry in history:
        past_script = entry.get("script", "")
        past_hook = entry.get("hook", "")
        
        sim_script = _calculate_jaccard_similarity(new_script, past_script)
        sim_hook = _calculate_jaccard_similarity(new_hook, past_hook)
        max_sim = max(sim_script, sim_hook)
        
        if max_sim > highest_sim:
            highest_sim = max_sim
            matched_entry = entry

    if highest_sim > 0.55 and matched_entry:
        past_title = matched_entry.get("title", "Past Short")
        reason = f"High structural/text similarity ({highest_sim:.0%}) to past Short titled '{past_title}' from {matched_entry.get('date_readable')}."
        logger.warning(f"[Originality Check FAIL] {reason}")
        return {
            "pass": False,
            "reason": reason,
            "matched_short_title": past_title
        }

    # 2. LLM Check for semantic/idea similarity if API key is set
    if config.GEMINI_API_KEY:
        try:
            from google import genai
            client = genai.Client(api_key=config.GEMINI_API_KEY)
            increment_llm_calls()
            
            # Form summary of recent past 10 Shorts
            recent_past = history[-10:]
            past_summaries = []
            for idx, p in enumerate(recent_past, 1):
                past_summaries.append(f"Past Short #{idx} Title: '{p.get('title')}' | Hook: '{p.get('hook')}'")

            prompt = (
                "You are a strict content originality evaluator for YouTube Shorts.\n"
                "Compare the NEW Short proposal against recent past Shorts.\n\n"
                f"NEW Short Title: '{new_title}'\n"
                f"NEW Short Hook: '{new_hook}'\n"
                f"NEW Short Script:\n{new_script}\n\n"
                "RECENT PAST SHORTS:\n" + "\n".join(past_summaries) + "\n\n"
                "Return a JSON object with keys:\n"
                '- "pass": boolean (true if unique and original, false if too similar to a past Short)\n'
                '- "reason": string explaining decision\n'
                '- "matched_short": title of matched past Short if failed, or null if pass\n'
                "Respond ONLY with valid JSON."
            )

            response = client.models.generate_content(
                model='gemini-2.5-flash',
                contents=prompt
            )
            raw_text = response.text.strip()
            # Clean possible markdown formatting
            if raw_text.startswith("```json"):
                raw_text = raw_text[7:]
            if raw_text.endswith("```"):
                raw_text = raw_text[:-3]

            res_json = json.loads(raw_text.strip())
            is_pass = res_json.get("pass", True)
            reason = res_json.get("reason", "Originality confirmed by LLM analysis.")
            matched = res_json.get("matched_short")

            logger.info(f"[Originality Check LLM Result] Pass: {is_pass} | Reason: {reason}")
            return {
                "pass": is_pass,
                "reason": reason,
                "matched_short_title": matched
            }

        except Exception as e:
            logger.warning(f"Gemini LLM Originality check failed: {e}. Falling back to Jaccard result.")

    reason = f"Originality confirmed (Highest text overlap with past Shorts: {highest_sim:.0%})."
    logger.info(f"[Originality Check PASS] {reason}")
    return {
        "pass": True,
        "reason": reason,
        "matched_short_title": None
    }
