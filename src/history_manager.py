import json
import logging
import re
from datetime import datetime, timedelta
from pathlib import Path
from typing import List, Dict, Any, Optional, Tuple, Set


import config
from src.llm_tracker import increment_llm_calls
from src.groq_utils import rate_limited_groq_call

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

# ==================== SHORTS ORIGINALITY & STRUCTURAL HISTORY ====================

def extract_structural_fingerprint(script_text: str) -> Dict[str, Any]:
    """
    Extract lightweight structural fingerprint from script text.
    Identifies opening style category, closing style category, opening prefix, and closing prefix.
    """
    if not script_text:
        return {
            "opening_style": "IN_SCENE_MID_THOUGHT",
            "opening_prefix": "",
            "closing_style": "SPECIFIC_CALLBACK_OPINION",
            "closing_prefix": "",
            "transition_markers": []
        }

    sentences = [s.strip() for s in re.split(r"[.!?]+", script_text) if s.strip()]
    first_sentence = sentences[0] if sentences else script_text[:80]
    last_sentence = sentences[-1] if sentences else script_text[-80:]
    first_lower = first_sentence.lower()
    last_lower = last_sentence.lower()

    # Determine canonical opening style category
    if "?" in first_sentence or any(w in first_lower for w in ["looking for", "ever wonder", "ready for", "which anime", "need anime", "what happens"]):
        opening_style = "SPECIFIC_QUESTION"
    elif any(w in first_lower for w in ["did you know", "spent", "animated", "produced", "fact", "years", "ruin ordinary", "stop wasting", "absolute tier", "will completely"]):
        opening_style = "SURPRISING_FACT"
    else:
        opening_style = "IN_SCENE_MID_THOUGHT"

    # Determine canonical closing style category
    if "?" in last_sentence or any(w in last_lower for w in ["which of these", "which one", "your thoughts", "let me know", "comment below", "drop your"]):
        closing_style = "SPECIFIC_CALLBACK_QUESTION"
    elif any(w in last_lower for w in ["start with", "binge night", "bookmark", "save this short", "won't regret it"]):
        closing_style = "SPECIFIC_CALLBACK_BINGE"
    else:
        closing_style = "SPECIFIC_CALLBACK_OPINION"

    # Extract normalized prefixes (first 5 words & last 5 words)
    op_words = re.sub(r"[^\w\s]", "", first_lower).split()[:5]
    op_prefix = " ".join(op_words)

    cl_words = re.sub(r"[^\w\s]", "", last_lower).split()[-5:]
    cl_prefix = " ".join(cl_words)

    # Detect transition markers used in script
    transitions_found = []
    lower_full = script_text.lower()
    for marker in ["first off", "next up", "rounding out", "starting strong", "moving over", "finally", "number one", "number two", "then we have"]:
        if marker in lower_full:
            transitions_found.append(marker)

    return {
        "opening_style": opening_style,
        "opening_prefix": op_prefix,
        "closing_style": closing_style,
        "closing_prefix": cl_prefix,
        "transition_markers": transitions_found
    }


def record_short_history(concept_type: str, title: str, hook: str, script: str, video_id: Optional[str] = None):
    """Log details of a newly produced Short to history for future originality and structural checks."""
    history = _load_json_file(config.SHORTS_HISTORY_FILE)
    fp = extract_structural_fingerprint(script)
    history.append({
        "date": datetime.now().isoformat(),
        "date_readable": datetime.now().strftime("%Y-%m-%d"),
        "concept_type": concept_type,
        "title": title,
        "hook": hook,
        "script": script,
        "structural_fingerprint": fp,
        "video_id": video_id or "N/A"
    })
    _save_json_file(config.SHORTS_HISTORY_FILE, history)
    logger.info(f"[HistoryManager] Saved Short to history log: '{title}' ({concept_type}) with structural fingerprint.")

def _calculate_jaccard_similarity(text1: str, text2: str) -> float:
    """Compute Jaccard similarity between two text strings based on word sets."""
    words1 = set(text1.lower().split())
    words2 = set(text2.lower().split())
    if not words1 or not words2:
        return 0.0
    intersection = words1.intersection(words2)
    union = words1.union(words2)
    return len(intersection) / len(union)


def get_recent_hooks_and_outros(days: int = 30, limit: int = 5) -> Dict[str, Any]:
    """
    Retrieve opening hooks, closing outros, and structural styles used in recent Shorts history.
    Used to pass explicit anti-repetition guidance into script regeneration prompts.
    """
    history = _load_json_file(config.SHORTS_HISTORY_FILE)
    cutoff = datetime.now() - timedelta(days=days)
    recent_entries = []
    for item in reversed(history):
        d_str = item.get("date")
        if d_str:
            try:
                dt = datetime.fromisoformat(d_str)
                if dt >= cutoff:
                    recent_entries.append(item)
            except ValueError:
                recent_entries.append(item)
        else:
            recent_entries.append(item)
        if len(recent_entries) >= limit:
            break

    recent_hooks = []
    recent_outros = []
    opening_styles = []
    closing_styles = []

    for entry in recent_entries:
        hook = entry.get("hook")
        script = entry.get("script", "")
        fp = entry.get("structural_fingerprint") or extract_structural_fingerprint(script)
        
        if hook:
            recent_hooks.append(hook)
        elif script:
            first_sent = [s.strip() for s in re.split(r"[.!?]+", script) if s.strip()]
            if first_sent:
                recent_hooks.append(first_sent[0])
                
        if fp.get("closing_prefix"):
            recent_outros.append(fp["closing_prefix"])
        elif script:
            last_sent = [s.strip() for s in re.split(r"[.!?]+", script) if s.strip()]
            if last_sent:
                recent_outros.append(last_sent[-1])
                
        if fp.get("opening_style"):
            opening_styles.append(fp["opening_style"])
        if fp.get("closing_style"):
            closing_styles.append(fp["closing_style"])

    return {
        "hooks": recent_hooks,
        "outros": recent_outros,
        "opening_styles": opening_styles,
        "closing_styles": closing_styles
    }


def check_structural_variety_against_history(script_text: str, days: int = 30, limit: int = 7) -> Dict[str, Any]:
    """
    Evaluates script structural variety against recent videos history (last limit entries).
    Flags failure if structural pattern (opening category+prefix, closing category+prefix, or consecutive style repetition) matches recent Shorts.
    """
    logger.info(">>> RUNNING STRUCTURAL VARIETY QA (FINGERPRINT CHECK)")
    new_fp = extract_structural_fingerprint(script_text)
    history = _load_json_file(config.SHORTS_HISTORY_FILE)

    if not history:
        logger.info("[Structural Variety QA PASS] History is empty. Script passed.")
        return {
            "pass": True,
            "reason": "No past history entries to compare against.",
            "fingerprint": new_fp,
            "matched_issues": [],
            "forbidden_phrases": [],
            "forbidden_opening_styles": [],
            "forbidden_closing_styles": [],
            "recent_hooks": [],
            "recent_outros": []
        }

    # Inspect recent past entries
    cutoff = datetime.now() - timedelta(days=days)
    recent_entries = []
    for item in reversed(history):
        d_str = item.get("date")
        if d_str:
            try:
                dt = datetime.fromisoformat(d_str)
                if dt >= cutoff:
                    recent_entries.append(item)
            except ValueError:
                recent_entries.append(item)
        else:
            recent_entries.append(item)
        if len(recent_entries) >= limit:
            break

    matched_issues = []
    forbidden_phrases = []
    forbidden_opening_styles = []
    forbidden_closing_styles = []
    recent_hooks = []
    recent_outros = []

    for entry in recent_entries:
        hook = entry.get("hook")
        script = entry.get("script", "")
        fp = entry.get("structural_fingerprint") or extract_structural_fingerprint(script)
        if hook:
            recent_hooks.append(hook)
        if fp.get("closing_prefix"):
            recent_outros.append(fp["closing_prefix"])
        if fp.get("opening_style") and fp["opening_style"] not in forbidden_opening_styles:
            forbidden_opening_styles.append(fp["opening_style"])
        if fp.get("closing_style") and fp["closing_style"] not in forbidden_closing_styles:
            forbidden_closing_styles.append(fp["closing_style"])

    # Check 1: Consecutive structural pattern sameness (opening style AND closing style match immediate previous)
    if recent_entries:
        prev_entry = recent_entries[0]
        prev_fp = prev_entry.get("structural_fingerprint") or extract_structural_fingerprint(prev_entry.get("script", ""))
        
        if new_fp["opening_style"] == prev_fp["opening_style"] and new_fp["closing_style"] == prev_fp["closing_style"]:
            matched_issues.append(
                f"Consecutive structural repetition with previous video (Opening: {new_fp['opening_style']}, Outro: {new_fp['closing_style']})."
            )

    # Check 2: Opening style and opening prefix match a recent script in last 5-7 videos
    for idx, prev in enumerate(recent_entries, 1):
        prev_fp = prev.get("structural_fingerprint") or extract_structural_fingerprint(prev.get("script", ""))
        
        # Opening prefix sameness (Jaccard on first 5 words > 0.70 or same prefix)
        if new_fp["opening_prefix"] and prev_fp["opening_prefix"]:
            if new_fp["opening_prefix"] == prev_fp["opening_prefix"] or _calculate_jaccard_similarity(new_fp["opening_prefix"], prev_fp["opening_prefix"]) > 0.70:
                matched_issues.append(
                    f"Opening hook phrasing '{new_fp['opening_prefix']}' matches recent video #{idx} ('{prev.get('title')}')."
                )
                forbidden_phrases.append(new_fp["opening_prefix"])

        # Closing prefix sameness
        if new_fp["closing_prefix"] and prev_fp["closing_prefix"]:
            if new_fp["closing_prefix"] == prev_fp["closing_prefix"] or _calculate_jaccard_similarity(new_fp["closing_prefix"], prev_fp["closing_prefix"]) > 0.70:
                matched_issues.append(
                    f"Closing outro phrasing '{new_fp['closing_prefix']}' matches recent video #{idx} ('{prev.get('title')}')."
                )
                forbidden_phrases.append(new_fp["closing_prefix"])

    is_pass = len(matched_issues) == 0
    reason = (
        f"Structural variety confirmed across last {len(recent_entries)} videos (Opening: {new_fp['opening_style']}, Outro: {new_fp['closing_style']})."
        if is_pass
        else f"Structural Variety Failed: " + "; ".join(matched_issues)
    )

    logger.info(f"[Structural Variety QA] Pass: {is_pass} | {reason}")
    return {
        "pass": is_pass,
        "reason": reason,
        "fingerprint": new_fp,
        "matched_issues": matched_issues,
        "forbidden_phrases": forbidden_phrases,
        "forbidden_opening_styles": forbidden_opening_styles,
        "forbidden_closing_styles": forbidden_closing_styles,
        "recent_hooks": recent_hooks,
        "recent_outros": recent_outros
    }


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
    api_key = config.GROQ_API_KEY or config.GEMINI_API_KEY
    if api_key:
        try:
            from groq import Groq
            client = Groq(api_key=api_key)
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

            response = rate_limited_groq_call(
                client.chat.completions.create,
                model=config.GROQ_MODEL,
                messages=[{"role": "user", "content": prompt}]
            )
            raw_text = response.choices[0].message.content.strip()
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
            logger.warning(f"Groq LLM Originality check failed: {e}. Falling back to Jaccard result.")

    reason = f"Originality confirmed (Highest text overlap with past Shorts: {highest_sim:.0%})."
    logger.info(f"[Originality Check PASS] {reason}")
    return {
        "pass": True,
        "reason": reason,
        "matched_short_title": None
    }


# ==================== SPECIFIC ANIME TITLE HISTORY & COOLDOWN ====================

def _normalize_title_text(text: str) -> str:
    """Helper to normalize title text for accurate comparison."""
    if not text:
        return ""
    import re
    cleaned = re.sub(r"[^\w\s]", "", text.lower())
    return " ".join(cleaned.split())

def record_anime_titles_usage(candidates: List[Dict[str, Any]], concept_type: str = "general"):
    """Record all featured anime titles from candidate list into title_history.json with deduplication guard."""
    history = _load_json_file(config.TITLE_HISTORY_FILE)
    now = datetime.now()
    now_iso = now.isoformat()
    now_str = now.strftime("%Y-%m-%d %H:%M:%S")

    # Build set of recently recorded titles (within last 10 minutes) to prevent duplicates in single run
    recent_cutoff = now - timedelta(minutes=10)
    recently_recorded = set()
    for entry in history:
        date_str = entry.get("date")
        if date_str:
            try:
                dt = datetime.fromisoformat(date_str)
                if dt >= recent_cutoff:
                    norm = entry.get("normalized_title") or _normalize_title_text(entry.get("title", ""))
                    if norm:
                        recently_recorded.add(norm)
                    if entry.get("anime_id"):
                        recently_recorded.add(str(entry["anime_id"]))
            except ValueError:
                pass

    added_count = 0
    for item in candidates:
        title = item.get("title") or item.get("verified_facts", {}).get("title") or "Unknown Title"
        norm_title = _normalize_title_text(title)
        anime_id = item.get("id")

        if norm_title in recently_recorded or (anime_id and str(anime_id) in recently_recorded):
            logger.debug(f"[HistoryManager] Title '{title}' already recorded in recent 10m window, skipping duplicate log.")
            continue

        history.append({
            "title": title,
            "normalized_title": norm_title,
            "anime_id": anime_id,
            "concept_type": concept_type,
            "date": now_iso,
            "date_readable": now_str
        })
        recently_recorded.add(norm_title)
        if anime_id:
            recently_recorded.add(str(anime_id))
        added_count += 1

    if added_count > 0:
        _save_json_file(config.TITLE_HISTORY_FILE, history)
        logger.info(f"[HistoryManager] Recorded {added_count} anime title(s) to title_history.json (Concept: {concept_type})")

def get_recent_anime_titles(
    days: int = config.ANIME_TITLE_COOLDOWN_DAYS,
    exclude_after: Optional[datetime] = None
) -> List[Dict[str, Any]]:
    """Retrieve anime titles featured in videos within the last `days` days.

    If `exclude_after` is provided, entries written at or after that
    timestamp are excluded — this prevents a run from seeing its own
    just-written selection as a "past" entry when checking cooldown later
    in the same execution.
    """
    history = _load_json_file(config.TITLE_HISTORY_FILE)
    cutoff_date = datetime.now() - timedelta(days=days)

    recent_entries = []
    for entry in history:
        date_str = entry.get("date")
        if date_str:
            try:
                entry_date = datetime.fromisoformat(date_str)
                if entry_date >= cutoff_date:
                    if exclude_after is not None and entry_date >= exclude_after:
                        continue
                    recent_entries.append(entry)
            except ValueError:
                pass
    return recent_entries

def is_anime_title_allowed_by_history(
    title: str,
    anime_id: Optional[int] = None,
    days: int = config.ANIME_TITLE_COOLDOWN_DAYS,
    exclude_after: Optional[datetime] = None
) -> Tuple[bool, str]:
    """
    Returns (allowed: bool, reason: str).
    Checks whether a specific anime title (or ID) was featured within the last `days` days.

    `exclude_after` should be set to the current pipeline run's start time,
    so this check only considers genuinely prior runs — not the entry this
    same run may have already written to history moments earlier.
    """
    recent_entries = get_recent_anime_titles(days, exclude_after=exclude_after)
    norm_new = _normalize_title_text(title)

    for entry in recent_entries:
        past_title = entry.get("title", "")
        past_norm = entry.get("normalized_title") or _normalize_title_text(past_title)
        past_id = entry.get("anime_id")
        past_date = entry.get("date_readable", "recent date")

        # Match by ID or exact/normalized title match
        if (anime_id and past_id and int(anime_id) == int(past_id)) or (norm_new and norm_new == past_norm):
            reason = f"Anime title '{title}' was already featured on {past_date} (within {days}-day cooldown window)."
            logger.info(f"[HistoryManager EXCLUDE] {reason}")
            return False, reason

    return True, f"Anime title '{title}' is clear of the {days}-day cooldown window."


# ==================== VIDEO TITLE HISTORY & VARIETY CHECK ====================

def record_video_title_usage(title: str, concept_type: str = "general"):
    """Record produced video title to video_title_history.json."""
    history = _load_json_file(config.VIDEO_TITLE_HISTORY_FILE)
    history.append({
        "title": title,
        "normalized_title": _normalize_title_text(title),
        "concept_type": concept_type,
        "date": datetime.now().isoformat(),
        "date_readable": datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    })
    _save_json_file(config.VIDEO_TITLE_HISTORY_FILE, history)
    logger.info(f"[HistoryManager] Recorded video title to history: '{title}'")

def get_recent_video_titles(days: int = config.VIDEO_TITLE_COOLDOWN_DAYS) -> List[Dict[str, Any]]:
    """Retrieve video titles used within the last `days` days."""
    history = _load_json_file(config.VIDEO_TITLE_HISTORY_FILE)
    
    # Fallback to shorts_history.json if video_title_history.json has few entries
    if not history:
        shorts_hist = _load_json_file(config.SHORTS_HISTORY_FILE)
        for s in shorts_hist:
            if s.get("title"):
                history.append({
                    "title": s["title"],
                    "normalized_title": _normalize_title_text(s["title"]),
                    "concept_type": s.get("concept_type", "general"),
                    "date": s.get("date", datetime.now().isoformat()),
                    "date_readable": s.get("date_readable", "")
                })

    cutoff_date = datetime.now() - timedelta(days=days)
    recent = []
    for entry in history:
        date_str = entry.get("date")
        if date_str:
            try:
                entry_date = datetime.fromisoformat(date_str)
                if entry_date >= cutoff_date:
                    recent.append(entry)
            except ValueError:
                pass
    return recent

def check_video_title_similarity(new_title: str, days: int = config.VIDEO_TITLE_COOLDOWN_DAYS) -> Dict[str, Any]:
    """
    Checks if `new_title` is too similar in wording/structure to a recent video title.
    Returns: {"pass": bool, "reason": str, "matched_title": str or None}
    """
    recent_entries = get_recent_video_titles(days)
    if not recent_entries:
        return {"pass": True, "reason": "No recent video titles to compare against.", "matched_title": None}

    norm_new = _normalize_title_text(new_title)
    highest_sim = 0.0
    matched_title = None

    for entry in recent_entries:
        past_title = entry.get("title", "")
        norm_past = entry.get("normalized_title") or _normalize_title_text(past_title)
        
        sim = _calculate_jaccard_similarity(norm_new, norm_past)
        if sim > highest_sim:
            highest_sim = sim
            matched_title = past_title

    if highest_sim > 0.55 and matched_title:
        reason = f"Video title '{new_title}' is too similar ({highest_sim:.0%} overlap) to recent title '{matched_title}'."
        logger.warning(f"[Video Title Similarity FAIL] {reason}")
        return {
            "pass": False,
            "reason": reason,
            "matched_title": matched_title
        }

    return {
        "pass": True,
        "reason": f"Video title is unique (Highest phrasing overlap: {highest_sim:.0%}).",
        "matched_title": None
    }

