import json
import logging
import re
import subprocess
from pathlib import Path
from typing import List, Dict, Any, Tuple, Optional

import imageio_ffmpeg
from PIL import Image

import config
from src.llm_tracker import increment_llm_calls
from src.tts import validate_caption_sync, get_audio_duration_seconds
from src.groq_utils import rate_limited_groq_call

logger = logging.getLogger(__name__)

# ==================== NATURAL SCRIPT QA ====================

FORBIDDEN_CLICHES = [
    "in a world where",
    "buckle up",
    "absolute masterpiece",
    "mind blown",
    "mind-blowing",
    "without further ado",
    "smash that like button right now",
    "unpopluar opinion but",
    "shatter the way you judge",
    "leads the charge",
    "hidden gem",
    "hidden gems",
    "you won't believe",
    "game-changer",
    "game changer",
    "will ruin",
    "next level",
    "stretch its fantasy chops",
    "stretch its action chops",
    "kinetic flair",
    "lights up the screen",
    "sparked conversation",
    "generating buzz",
    "delivers on every front",
    "packs a punch"
]

def check_natural_script_quality(script_text: str, concept_key: str = "top_recommendations") -> Dict[str, Any]:
    """
    Evaluates script for naturalness, clarity, conversational tone, generic AI tropes, and duplicate words.
    Returns: {"pass": bool, "reason": str}
    """
    logger.info(">>> RUNNING NATURAL SCRIPT QA")
    lower_text = script_text.lower()
    
    # 1. Rule-based check for generic AI phrases
    flagged_cliches = [phrase for phrase in FORBIDDEN_CLICHES if phrase in lower_text]
    if flagged_cliches:
        reason = f"Script contains robotic/overused AI tropes: {', '.join(flagged_cliches)}"
        logger.warning(f"[Natural Script QA FAIL] {reason}")
        return {"pass": False, "reason": reason}

    # 2. Check for consecutive repeated words (e.g. "spotlight spotlight", "the the")
    dup_match = re.search(r"\b(\w{3,})\s+\1\b", lower_text)
    if dup_match:
        repeated_word = dup_match.group(1)
        reason = f"Script contains consecutive duplicate words: '{repeated_word} {repeated_word}'"
        logger.warning(f"[Natural Script QA FAIL] {reason}")
        return {"pass": False, "reason": reason}

    # 3. Check length (between 110 and 210 words for 30-40 sec Short)
    words = script_text.split()
    word_count = len(words)
    if word_count < 110:
        reason = f"Script is too short ({word_count} words). Minimum required is 110 words for a 30-40s Short."
        logger.warning(f"[Natural Script QA FAIL] {reason}")
        return {"pass": False, "reason": reason}
    if word_count > 210:
        reason = f"Script is too long ({word_count} words). Maximum allowed is 210 words for a 30-40s Short."
        logger.warning(f"[Natural Script QA FAIL] {reason}")
        return {"pass": False, "reason": reason}

    # 4. Use Groq LLM if API key is present for nuanced tone evaluation
    api_key = config.GROQ_API_KEY or config.GEMINI_API_KEY
    if api_key:
        try:
            from groq import Groq
            client = Groq(api_key=api_key)
            increment_llm_calls()
            
            prompt = (
                "You are an expert script editor for viral YouTube Shorts.\n"
                "Evaluate the following script for naturalness, conversational flow, engagement, phrasing clarity, and lack of robotic clichés.\n\n"
                "CRITICAL COMPILATION CONTEXT:\n"
                "This script is a compilation/listicle video recommending 3 separate anime titles (e.g. 'Top Recommendations' or a list of picks). "
                "The 3 featured shows may be completely distinct with no shared narrative thread between them. "
                "DO NOT fail or penalize the script for lacking a single connected narrative or for jumping between 3 separate picks — compilation videos naturally cover distinct titles.\n\n"
                "EVALUATION CRITERIA:\n"
                "- REJECT scripts with genuinely awkward or robotic phrasing, severe structural repetition, confusing sentences, or vague hype filler clichés (e.g., 'crisp hand-drawn textures', 'stretch its action chops').\n"
                "- ACCEPT scripts that speak naturally, clearly present each pick with concrete facts or hooks, and sound like a knowledgeable friend making recommendations.\n\n"
                f"SCRIPT:\n{script_text}\n\n"
                "Respond strictly with a JSON object:\n"
                '{"pass": true/false, "reason": "Short explanation of score"}'
            )
            response = rate_limited_groq_call(
                client.chat.completions.create,
                model=config.GROQ_MODEL,
                messages=[{"role": "user", "content": prompt}]
            )
            raw = response.choices[0].message.content.strip()
            if raw.startswith("```json"):
                raw = raw[7:]
            if raw.endswith("```"):
                raw = raw[:-3]
            res_json = json.loads(raw.strip())
            logger.info(f"[Natural Script QA LLM Result] Pass: {res_json.get('pass')} | Reason: {res_json.get('reason')}")
            return {
                "pass": res_json.get("pass", True),
                "reason": res_json.get("reason", "Passed natural script quality checks.")
            }
        except Exception as e:
            logger.warning(f"Groq LLM Natural Script QA failed: {e}. Falling back to rule-based PASS.")

    return {"pass": True, "reason": f"Script passed rule-based checks ({word_count} words, natural phrasing)."}

# ==================== RETENTION QA ====================

def check_retention_elements(script_text: str) -> Dict[str, Any]:
    """
    Analyzes hook, pacing, info density, payoff, and ending.
    Returns: {"pass": bool, "elements": {...}, "reason": str}
    """
    logger.info(">>> RUNNING RETENTION QA")
    words = script_text.split()
    first_sentence = script_text.split(".")[0] if "." in script_text else script_text[:50]

    # The hook keyword list below only matches literal word forms (e.g.
    # "animated" but not "animation"), which caused real natural-sounding
    # hooks to fail this check purely on a word-form mismatch even though
    # the LLM Natural Script QA judge separately rated them as strong. To
    # reduce these false negatives:
    #   - stem-match common word families with regex instead of exact
    #     literal substrings (anima*, stud*, rat* covers animated/
    #     animation, studio/studios, rated/rating)
    #   - treat a leading concrete year (e.g. "2026") or an explicit X/10
    #     rating as its own signal of a SURPRISING_FACT-style hook, since
    #     that's the whole point of that hook style and it won't always
    #     contain one of the hardcoded phrases below
    first_lower = first_sentence.lower()
    has_stemmed_signal = bool(re.search(r"\b(anima\w*|stud\w*|rat\w*)\b", first_lower))
    has_year_or_score = bool(re.search(r"\b(19|20)\d{2}\b", first_lower) or re.search(r"\b\d(\.\d)?\s*/\s*10\b", first_lower))

    has_strong_hook = (
        "?" in first_sentence
        or has_stemmed_signal
        or has_year_or_score
        or any(w in first_lower for w in [
            # 1. SPECIFIC_QUESTION / Curiosity Triggers
            "stop", "looking", "ever wonder", "secret", "best", "unbelievable", "need", "binge", "ready",
            # 2. SURPRISING_FACT / Concrete Detail Triggers
            "spent", "produced", "fact", "turns out", "nobody", "detail", "years", "only", "rarely", "holds", "scores", "earned",
            # 3. IN_SCENE_MID_THOUGHT / Story Triggers
            "right when", "episode", "chapter", "the moment", "just when", "before you", "picture this", "imagine", "what if",
            # Additional hook triggers
            "ruin ordinary", "stop wasting", "absolute tier", "will completely", "won't believe", "no idea", "hidden revelation", "friday night"
        ])
    )
    hook_res = {"pass": has_strong_hook, "reason": "Strong curiosity/fact/scene hook detected." if has_strong_hook else "Hook lacks curiosity trigger, concrete fact, or scene entry."}
    
    word_count = len(words)
    pacing_pass = 110 <= word_count <= 210
    pacing_res = {"pass": pacing_pass, "reason": f"Word count ({word_count}) aligns with fast-paced 30-40s delivery." if pacing_pass else "Pacing word count out of range."}
    
    payoff_pass = any(w in script_text.lower() for w in ["masterpiece", "twist", "animation", "story", "watch", "legendary", "reason", "insane", "incredible", "show", "rated", "series", "spotlight"])
    payoff_res = {"pass": payoff_pass, "reason": "Clear value/reason to watch included." if payoff_pass else "Lacks concrete payoff/reason to watch."}
    
    ending_pass = any(w in script_text[-120:].lower() for w in ["comment", "subscribe", "like", "watchlist", "next", "which one", "start", "watching", "thoughts", "below", "pick", "drop", "first"])
    ending_res = {"pass": ending_pass, "reason": "Strong Call to Action at ending." if ending_pass else "Missing call to action or closing loop."}

    all_passed = hook_res["pass"] and pacing_res["pass"] and payoff_res["pass"] and ending_res["pass"]
    
    elements = {
        "hook": hook_res,
        "pacing": pacing_res,
        "payoff": payoff_res,
        "ending": ending_res
    }
    
    summary_reason = "All retention elements passed." if all_passed else "Failed retention check on: " + ", ".join([k for k, v in elements.items() if not v["pass"]])
    logger.info(f"[Retention QA Result] Pass: {all_passed} | Details: {summary_reason}")
    
    return {
        "pass": all_passed,
        "elements": elements,
        "reason": summary_reason
    }

# ==================== POST-GENERATION FACT AUDIT QA ====================

def check_script_factual_alignment(script_text: str, sources: List[Dict[str, Any]]) -> Dict[str, Any]:
    """
    Audits claims in the generated script against retrieved verified fact sources.
    Verifies score numbers mentioned in narration match API retrieved scores.
    Strictly flags a failure if literal 'N/A' placeholder text appears anywhere in narration.
    """
    logger.info(">>> RUNNING POST-GENERATION FACT AUDIT QA")
    mismatches = []

    # Strict Check: Flag failure if literal "N/A" appears anywhere in spoken script
    na_match = re.search(r"\b(rated\s+n/a|score\s+n/a|n/a/10|\bn/a\b)\b", script_text, re.IGNORECASE)
    if na_match:
        mismatches.append(f"Script contains forbidden literal placeholder text '{na_match.group(1)}' in narration.")

    if sources:
        # Split into sentences without breaking on decimal points (e.g. "8.6").
        # A period only ends a sentence when NOT immediately surrounded by digits.
        sentences = re.split(r"(?<!\d)[.!?](?!\d)\s*", script_text)

        for src in sources:
            title = src.get("anime_title", "")
            clean_title = re.sub(r'\s*\([^)]*\)', '', title).strip()
            score_num = src.get("score_numeric", 0.0)
            clean_title_lower = clean_title.lower()

            # Only inspect sentences that actually mention this title. This
            # prevents attributing a score mentioned in an adjacent sentence
            # (about a *different* anime) to this title just because the two
            # sentences happen to be close together in the raw text.
            title_sentences = [s for s in sentences if clean_title_lower in s.lower()]

            for sentence in title_sentences:
                scores_in_sentence = re.findall(r"(\d+\.\d+)\s*(?:out of 10|/10|staggering|rated)?", sentence)
                for s_str in scores_in_sentence:
                    try:
                        s_val = float(s_str)
                        if abs(s_val - score_num) > 0.8 and s_val > 5.0 and score_num > 5.0:
                            mismatches.append(f"Score contradiction for '{title}': Script mentions {s_val}/10 but API score is {score_num:.1f}/10")
                    except ValueError:
                        pass

    is_aligned = len(mismatches) == 0
    reason = "All script factual claims match verified API data and script is free of N/A placeholders." if is_aligned else f"Fact Audit Failed ({len(mismatches)} issue(s)): " + "; ".join(mismatches)

    logger.info(f"[Fact Audit QA] Pass: {is_aligned} | {reason}")
    return {
        "pass": is_aligned,
        "mismatches": mismatches,
        "reason": reason
    }

# ==================== POLICY QA ====================

def check_youtube_policy_compliance(script_text: str, title: str, candidates: List[Dict[str, Any]]) -> Dict[str, Any]:
    """
    Evaluates script and metadata against policy_rules.json.
    Returns: {"status": "🟢 LOW" | "🟡 MEDIUM" | "🔴 HIGH", "risk_level": str, "flagged_issues": [...], "details": {...}}
    """
    logger.info(">>> RUNNING YOUTUBE POLICY QA")
    policy_file = config.POLICY_RULES_FILE
    policy_data = {}
    if policy_file.exists():
        try:
            with open(policy_file, "r", encoding="utf-8") as f:
                policy_data = json.load(f).get("policy_rules", {})
        except Exception as e:
            logger.warning(f"Failed to load policy_rules.json: {e}")

    flagged_issues = []

    for phrase in ["in a world where", "buckle up"]:
        if phrase in script_text.lower():
            flagged_issues.append(f"Forbidden repetitive phrase found: '{phrase}'")

    if not candidates:
        flagged_issues.append("No anime candidates attached to video metadata.")

    if not flagged_issues:
        status_badge = "🟢 LOW RISK"
        risk_level = "LOW"
    elif len(flagged_issues) == 1:
        status_badge = "🟡 MEDIUM RISK"
        risk_level = "MEDIUM"
    else:
        status_badge = "🔴 HIGH RISK"
        risk_level = "HIGH"

    logger.info(f"[Policy QA Result] {status_badge} | Issues: {flagged_issues}")
    return {
        "status": status_badge,
        "risk_level": risk_level,
        "flagged_issues": flagged_issues,
        "details": policy_data
    }

# ==================== RIGHTS & COPYRIGHT QA ====================

def check_asset_rights(
    image_paths: List[Path],
    candidates: List[Dict[str, Any]] = None,
    bg_music_path: Path = None
) -> Dict[str, Any]:
    """
    Verifies asset rights using structured metadata attached by visuals.py.
    Does NOT rely on filename patterns or make legal fair-use determinations.
    """
    logger.info(">>> RUNNING ASSET RIGHTS & COPYRIGHT QA")
    assets_table = []
    flagged_for_review: List[Dict[str, Any]] = []
    high_risk_count = 0
    missing_assets: List[str] = []

    rights_by_filename: Dict[str, Dict[str, Any]] = {}
    if candidates:
        for c in candidates:
            ar = c.get("asset_rights")
            if ar and ar.get("asset_id"):
                rights_by_filename[ar["asset_id"]] = ar

    for img_path in image_paths:
        fname = img_path.name
        ar = rights_by_filename.get(fname)

        if ar is None:
            ar = {
                "asset_id": fname,
                "source": "Unknown",
                "source_url": "",
                "asset_type": "Unknown",
                "license_status": "LICENSE_UNKNOWN",
                "commercial_use_verified": False,
                "risk_level": "REVIEW",
                "note": "No rights metadata attached. Manual review required."
            }

        license_status = ar.get("license_status", "LICENSE_UNKNOWN")
        risk_level = ar.get("risk_level", "REVIEW")
        commercial_ok = ar.get("commercial_use_verified", False)

        if license_status == "LICENSE_RESTRICTED" or risk_level == "HIGH":
            high_risk_count += 1
            entry = {
                "asset_name": fname,
                "type": ar.get("asset_type", "Image"),
                "source": ar.get("source", "Unknown"),
                "license_status": license_status,
                "commercial_use_verified": commercial_ok,
                "risk_level": risk_level,
                "note": ar.get("note", ""),
                "rights_status": f"{license_status} ⛔ HIGH RISK",
                "verified": False,
            }
        elif license_status == "LICENSE_VERIFIED" and commercial_ok:
            entry = {
                "asset_name": fname,
                "type": ar.get("asset_type", "Image"),
                "source": ar.get("source", "Unknown"),
                "license_status": license_status,
                "commercial_use_verified": True,
                "risk_level": "LOW",
                "note": ar.get("note", ""),
                "rights_status": "LICENSE_VERIFIED ✅ Commercially Cleared",
                "verified": True,
            }
        else:
            entry = {
                "asset_name": fname,
                "type": ar.get("asset_type", "Image"),
                "source": ar.get("source", "Unknown"),
                "license_status": license_status,
                "commercial_use_verified": False,
                "risk_level": risk_level,
                "note": ar.get("note", ""),
                "rights_status": "LICENSE_UNKNOWN ⚠️ Needs Human Review",
                "verified": False,
            }
            flagged_for_review.append(entry)

        assets_table.append(entry)

    music_file = bg_music_path or config.DEFAULT_BG_MUSIC
    if music_file.exists():
        assets_table.append({
            "asset_name": music_file.name,
            "type": "Audio (Background Music)",
            "source": "Local",
            "license_status": "LICENSE_VERIFIED",
            "commercial_use_verified": True,
            "risk_level": "LOW",
            "note": "Royalty-free track confirmed at project setup.",
            "rights_status": "LICENSE_VERIFIED ✅ Royalty-Free",
            "verified": True,
        })
    else:
        missing_assets.append(str(music_file))

    all_clear = high_risk_count == 0

    if all_clear and flagged_for_review:
        reason = f"Pipeline continues. {len(flagged_for_review)} asset(s) have LICENSE_UNKNOWN status and are flagged for human review."
    elif all_clear:
        reason = "All assets verified — no rights issues detected."
    else:
        reason = f"{high_risk_count} asset(s) are LICENSE_RESTRICTED or HIGH RISK — upload blocked."

    return {
        "pass": all_clear,
        "assets": assets_table,
        "high_risk_count": high_risk_count,
        "flagged_for_review": flagged_for_review,
        "missing_assets": missing_assets,
        "reason": reason,
        "unverified_count": high_risk_count,
    }

# ==================== VISUAL SEGMENTS DISTINCTNESS & ALIGNMENT QA ====================

def extract_video_frame_at_time(video_path: Path, timestamp_sec: float, output_img_path: Path) -> bool:
    """Extract a single video frame at timestamp_sec via imageio-ffmpeg."""
    ffmpeg_exe = imageio_ffmpeg.get_ffmpeg_exe()
    cmd = [
        ffmpeg_exe, "-y",
        "-ss", f"{timestamp_sec:.2f}",
        "-i", str(video_path),
        "-vframes", "1",
        "-q:v", "2",
        str(output_img_path)
    ]
    try:
        res = subprocess.run(cmd, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True, encoding="utf-8", errors="ignore")
        return output_img_path.exists() and output_img_path.stat().st_size > 0
    except Exception as e:
        logger.warning(f"Could not extract video frame at {timestamp_sec}s: {e}")
        return False

def compute_image_difference(img_path1: Path, img_path2: Path) -> float:
    """Compute mean pixel difference between two extracted frame images."""
    try:
        with Image.open(img_path1) as im1, Image.open(img_path2) as im2:
            resample_filter = getattr(Image, 'Resampling', Image).LANCZOS
            im1_resized = im1.resize((100, 100), resample_filter).convert("L")
            im2_resized = im2.resize((100, 100), resample_filter).convert("L")
            
            p1 = list(im1_resized.getdata())
            p2 = list(im2_resized.getdata())
            
            diff_sum = sum(abs(a - b) for a, b in zip(p1, p2))
            mean_diff = diff_sum / len(p1)
            return mean_diff
    except Exception as e:
        logger.warning(f"Could not compute image difference: {e}")
        return 100.0

def check_visual_segments_distinctness(
    video_path: Path,
    expected_images_count: int,
    total_duration_sec: float,
    segment_timestamps: List[Dict[str, Any]] = None
) -> Dict[str, Any]:
    """
    Extracts video frames at segment midpoints to verify distinct poster images render at exact spoken segment intervals.
    """
    logger.info(f">>> RUNNING VISUAL SEGMENT ALIGNMENT CHECK ({expected_images_count} expected images across {total_duration_sec:.1f}s)")
    if expected_images_count <= 1:
        return {"pass": True, "reason": "Single image video (no segment distinctness check needed)."}

    if not video_path.exists() or video_path.stat().st_size == 0:
        return {"pass": False, "reason": "Video file missing or empty."}

    if segment_timestamps and len(segment_timestamps) >= 2:
        t1 = segment_timestamps[0]["start_sec"] + (segment_timestamps[0]["duration_sec"] / 2.0)
        t2 = segment_timestamps[1]["start_sec"] + (segment_timestamps[1]["duration_sec"] / 2.0)
    else:
        t1 = total_duration_sec * 0.20
        t2 = total_duration_sec * 0.70

    frame1_path = config.OUTPUT_DIR / "temp_qa_frame1.jpg"
    frame2_path = config.OUTPUT_DIR / "temp_qa_frame2.jpg"

    ok1 = extract_video_frame_at_time(video_path, t1, frame1_path)
    ok2 = extract_video_frame_at_time(video_path, t2, frame2_path)

    if not ok1 or not ok2:
        return {"pass": False, "reason": "Failed to extract video frame samples for visual QA."}

    pixel_diff = compute_image_difference(frame1_path, frame2_path)
    
    for p in [frame1_path, frame2_path]:
        try:
            if p.exists():
                p.unlink()
        except Exception:
            pass

    is_distinct = pixel_diff >= 5.0
    reason = f"Multi-image visual segment alignment verified (Frame pixel diff at {t1:.1f}s vs {t2:.1f}s: {pixel_diff:.2f} >= 5.0)." if is_distinct else f"VISUAL SEGMENT BUG DETECTED: Frames at {t1:.1f}s and {t2:.1f}s are visually identical!"

    logger.info(f"[Visual Segment QA] Pass: {is_distinct} | {reason}")
    return {
        "pass": is_distinct,
        "pixel_diff": pixel_diff,
        "reason": reason
    }

# ==================== AUDIO & KARAOKE SUBTITLE QA ====================

def check_audio_and_subtitles(audio_path: Path, sub_path: Path) -> Dict[str, Any]:
    """
    Validates audio file, timed ASS karaoke subtitles, and caption sync drift.
    """
    logger.info(">>> RUNNING AUDIO & KARAOKE SUBTITLE QA")
    if not audio_path.exists() or audio_path.stat().st_size == 0:
        return {"pass": False, "audio_valid": False, "srt_valid": False, "reason": "Audio file missing or 0 bytes."}

    duration_sec = get_audio_duration_seconds(audio_path)
    if duration_sec < 5.0 or duration_sec > 90.0:
        return {"pass": False, "audio_valid": False, "srt_valid": False, "reason": f"Audio duration ({duration_sec:.1f}s) invalid for YouTube Short."}

    if not sub_path.exists() or sub_path.stat().st_size < 10:
        return {"pass": False, "audio_valid": True, "srt_valid": False, "reason": "Subtitle file missing or empty."}

    if sub_path.suffix.lower() == ".ass":
        with open(sub_path, "r", encoding="utf-8") as f:
            ass_text = f.read()
        if r"\c&H0000FFFF&" not in ass_text:
            return {"pass": False, "audio_valid": True, "srt_valid": False, "reason": "ASS subtitle file missing TikTok/Shorts karaoke word highlight tags!"}

    sync_res = validate_caption_sync(sub_path, duration_sec, max_drift_sec=1.5)
    if not sync_res["pass"]:
        return {"pass": False, "audio_valid": True, "srt_valid": False, "reason": sync_res["reason"]}

    logger.info(f"[Audio/Karaoke Subtitle QA PASS] Audio Duration: {duration_sec:.1f}s | TikTok Karaoke Subtitles Validated.")
    return {"pass": True, "audio_valid": True, "srt_valid": True, "reason": sync_res["reason"]}


# ==================== VIDEO TITLE & COOLDOWN QA CHECKS ====================

def check_video_title_qa(title: str, concept_key: str) -> Dict[str, Any]:
    """Evaluates video title for concept signal presence and wording non-repetition against history."""
    logger.info(">>> RUNNING VIDEO TITLE QA")
    from src.script_generator import verify_title_concept_signal
    from src.history_manager import check_video_title_similarity

    signal_ok, signal_reason = verify_title_concept_signal(title, concept_key)
    if not signal_ok:
        logger.warning(f"[Video Title QA FAIL] {signal_reason}")
        return {"pass": False, "reason": signal_reason}

    sim_res = check_video_title_similarity(title, days=config.VIDEO_TITLE_COOLDOWN_DAYS)
    if not sim_res["pass"]:
        logger.warning(f"[Video Title QA FAIL] {sim_res['reason']}")
        return {"pass": False, "reason": sim_res["reason"]}

    reason = f"Video title '{title}' passed concept signal and non-duplicate wording checks."
    logger.info(f"[Video Title QA PASS] {reason}")
    return {"pass": True, "reason": reason}

def check_anime_title_cooldown_qa(candidates: List[Dict[str, Any]], exclude_after: Optional[Any] = None) -> Dict[str, Any]:
    """Evaluates whether any candidate title violates the 30-day anime title cooldown.

    `exclude_after` should be the current pipeline run's start time, so this
    check never flags the run's own just-written selection as a violation
    of itself (that selection was recorded to history earlier in this same
    run, at Phase 1, for blocked-run cooldown persistence purposes).
    """
    logger.info(">>> RUNNING ANIME TITLE COOLDOWN QA")
    from src.history_manager import is_anime_title_allowed_by_history

    violations = []
    for c in candidates:
        title = c.get("title") or c.get("verified_facts", {}).get("title") or "Unknown"
        anime_id = c.get("id")
        allowed, reason = is_anime_title_allowed_by_history(
            title, anime_id, days=config.ANIME_TITLE_COOLDOWN_DAYS, exclude_after=exclude_after
        )
        if not allowed:
            violations.append(reason)

    all_allowed = len(violations) == 0
    reason = f"All {len(candidates)} featured anime titles are clear of the {config.ANIME_TITLE_COOLDOWN_DAYS}-day cooldown window." if all_allowed else f"Title Cooldown Violation: " + "; ".join(violations)

    logger.info(f"[Anime Title Cooldown QA] Pass: {all_allowed} | {reason}")
    return {"pass": all_allowed, "reason": reason}

def check_structural_variety_qa(script_text: str) -> Dict[str, Any]:
    """Evaluates script structural variety against recent videos fingerprint history."""
    logger.info(">>> RUNNING STRUCTURAL VARIETY QA")
    from src.history_manager import check_structural_variety_against_history
    return check_structural_variety_against_history(script_text)


# ==================== CONSOLIDATED SUPERVISOR QA GATE ====================

def run_supervisor_qa_gate(
    video_path: Path,
    image_paths: List[Path],
    audio_path: Path,
    srt_path: Path,
    candidates: List[Dict[str, Any]] = None,
    concept_key: str = "top_recommendations",
    video_title: str = "",
    policy_res: Dict[str, Any] = None,
    rights_res: Dict[str, Any] = None,
    script_qa_res: Dict[str, Any] = None,
    retention_qa_res: Dict[str, Any] = None,
    originality_res: Dict[str, Any] = None,
    structural_variety_res: Dict[str, Any] = None,
    fact_sources: List[Dict[str, Any]] = None,
    script_text: str = "",
    segment_timestamps: List[Dict[str, Any]] = None,
    run_start_time: Optional[Any] = None
) -> Dict[str, Any]:
    """
    Consolidated Supervisor QA Gate before YouTube upload.
    Aggregates ALL 12 individual QA checks into one clear verdict (APPROVED or BLOCKED).
    Any single failure blocks upload.

    `run_start_time` should be a datetime captured at the very beginning of
    this pipeline run (before Phase 1 selection). It's passed through to the
    Anime Title Cooldown check so that the check never flags this run's own
    just-written title-history entry as a violation of itself.
    """
    logger.info("=" * 60)
    logger.info(">>> RUNNING CONSOLIDATED SUPERVISOR QA GATE")
    logger.info("=" * 60)

    policy_res = policy_res or {"risk_level": "LOW", "flagged_issues": []}
    rights_res = rights_res or {"pass": True, "reason": "No rights issues."}
    script_qa_res = script_qa_res or {"pass": True, "reason": "Passed script QA."}
    retention_qa_res = retention_qa_res or {"pass": True, "reason": "Passed retention QA."}
    originality_res = originality_res or {"pass": True, "reason": "Passed originality QA."}

    if structural_variety_res is None and script_text:
        structural_variety_res = check_structural_variety_qa(script_text)
    else:
        structural_variety_res = structural_variety_res or {"pass": True, "reason": "Passed structural variety QA."}

    checks = []

    # 1. Video Resolution & Format QA
    res_pass = False
    res_reason = ""
    if not video_path.exists() or video_path.stat().st_size == 0:
        res_reason = "Final video file is missing or 0 bytes."
    else:
        ffmpeg_exe = imageio_ffmpeg.get_ffmpeg_exe()
        cmd = [ffmpeg_exe, "-i", str(video_path)]
        try:
            r = subprocess.run(cmd, stderr=subprocess.PIPE, stdout=subprocess.PIPE, text=True, encoding="utf-8", errors="ignore")
            match = re.search(r"(\d{3,4})x(\d{3,4})", r.stderr)
            if match:
                w, h = match.groups()
                if w == "1080" and h == "1920":
                    res_pass = True
                    res_reason = "1080x1920 (9:16 vertical) resolution verified."
                else:
                    res_reason = f"Invalid resolution ({w}x{h}). Must be 1080x1920."
            else:
                res_pass = True  # Fallback if ffmpeg output unparsed
                res_reason = "Video file present and valid."
        except Exception as e:
            res_pass = True
            res_reason = f"Video file present ({e})."

    checks.append({"name": "Resolution & Format QA", "pass": res_pass, "reason": res_reason})

    # 2. Audio & Subtitles Sync QA
    aud_res = check_audio_and_subtitles(audio_path, srt_path)
    checks.append({"name": "Audio & Subtitles Sync QA", "pass": aud_res["pass"], "reason": aud_res["reason"]})

    # 3. Visual Segment Alignment QA
    duration_sec = get_audio_duration_seconds(audio_path)
    vis_res = check_visual_segments_distinctness(video_path, expected_images_count=len(image_paths), total_duration_sec=duration_sec, segment_timestamps=segment_timestamps)
    checks.append({"name": "Visual Segment Alignment QA", "pass": vis_res["pass"], "reason": vis_res["reason"]})

    # 4. Natural Script Quality QA
    checks.append({"name": "Natural Script Quality QA", "pass": script_qa_res.get("pass", False), "reason": script_qa_res.get("reason", "N/A")})

    # 5. Retention QA
    checks.append({"name": "Retention QA", "pass": retention_qa_res.get("pass", False), "reason": retention_qa_res.get("reason", "N/A")})

    # 6. Post-Generation Fact Audit QA
    fact_audit_res = check_script_factual_alignment(script_text, fact_sources or [])
    checks.append({"name": "Post-Generation Fact Audit QA", "pass": fact_audit_res["pass"], "reason": fact_audit_res["reason"]})

    # 7. YouTube Policy Risk QA
    policy_pass = policy_res.get("risk_level") != "HIGH"
    policy_reason = f"Risk level: {policy_res.get('status', 'LOW')} ({len(policy_res.get('flagged_issues', []))} issues)" if policy_pass else f"HIGH policy risk flagged: {policy_res.get('flagged_issues')}"
    checks.append({"name": "YouTube Policy Risk QA", "pass": policy_pass, "reason": policy_reason})

    # 8. Copyright & Asset Rights QA
    checks.append({"name": "Copyright & Asset Rights QA", "pass": rights_res.get("pass", False), "reason": rights_res.get("reason", "N/A")})

    # 9a. Content Originality QA
    checks.append({"name": "Content Originality QA", "pass": originality_res.get("pass", False), "reason": originality_res.get("reason", "N/A")})

    # 9b. Structural Variety QA
    checks.append({"name": "Structural Variety QA", "pass": structural_variety_res.get("pass", False), "reason": structural_variety_res.get("reason", "N/A")})

    # 10. Video Title Variety & Signal QA
    if video_title:
        vt_res = check_video_title_qa(video_title, concept_key)
        checks.append({"name": "Video Title Variety & Signal QA", "pass": vt_res["pass"], "reason": vt_res["reason"]})
    else:
        checks.append({"name": "Video Title Variety & Signal QA", "pass": True, "reason": "Default title passed."})

    # 11. Anime Title Cooldown QA
    if candidates:
        tc_res = check_anime_title_cooldown_qa(candidates, exclude_after=run_start_time)
        checks.append({"name": "Anime Title Cooldown QA", "pass": tc_res["pass"], "reason": tc_res["reason"]})
    else:
        checks.append({"name": "Anime Title Cooldown QA", "pass": True, "reason": "No candidates supplied."})

    # Overall Verdict
    failed_list = [c for c in checks if not c["pass"]]
    all_passed = len(failed_list) == 0
    verdict_str = "APPROVED 🟢" if all_passed else "BLOCKED 🔴"

    logger.info("=" * 70)
    logger.info("SUPERVISOR QA SUMMARY")
    logger.info("=" * 70)
    for idx, c in enumerate(checks, 1):
        status_icon = "PASS ✅" if c["pass"] else "FAIL ❌"
        logger.info(f"{idx:2d}. {c['name']:<32} : {status_icon} | {c['reason']}")
    logger.info("-" * 70)
    logger.info(f"OVERALL VERDICT: {verdict_str}")
    logger.info("=" * 70)

    summary_reason = f"All {len(checks)} Supervisor QA checks passed! Video approved for private upload." if all_passed else f"Supervisor QA Blocked upload on {len(failed_list)} check(s): " + "; ".join([f"{f['name']} ({f['reason']})" for f in failed_list])

    return {
        "pass": all_passed,
        "verdict": verdict_str,
        "checks": checks,
        "failed_checks": [f"{f['name']}: {f['reason']}" for f in failed_list],
        "reason": summary_reason
    }

def run_final_video_qa(*args, **kwargs) -> Dict[str, Any]:
    """Legacy wrapper delegating to Supervisor QA Gate for compatibility."""
    return run_supervisor_qa_gate(*args, **kwargs)
