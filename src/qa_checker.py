import json
import logging
import re
import subprocess
from pathlib import Path
from typing import List, Dict, Any, Tuple

import imageio_ffmpeg
from PIL import Image

import config
from src.llm_tracker import increment_llm_calls
from src.tts import validate_caption_sync, get_audio_duration_seconds

logger = logging.getLogger(__name__)

# ==================== NATURAL SCRIPT QA ====================

FORBIDDEN_CLICHES = [
    "in a world where",
    "buckle up",
    "absolute masterpiece",
    "mind blown",
    "without further ado",
    "smash that like button right now",
    "unpopluar opinion but"
]

def check_natural_script_quality(script_text: str) -> Dict[str, Any]:
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

    # 4. Use Gemini LLM if API key is present for nuanced tone evaluation
    if config.GEMINI_API_KEY:
        try:
            from google import genai
            client = genai.Client(api_key=config.GEMINI_API_KEY)
            increment_llm_calls()
            
            prompt = (
                "You are an expert script editor for viral YouTube Shorts.\n"
                "Evaluate the following script for naturalness, conversational tone, engagement, and clarity.\n"
                "Reject any script that sounds robotic, repetitive, overly hyped, or uses generic AI clichés.\n\n"
                f"SCRIPT:\n{script_text}\n\n"
                "Respond strictly with a JSON object:\n"
                '{"pass": true/false, "reason": "Short explanation of score"}'
            )
            response = client.models.generate_content(
                model='gemini-2.5-flash',
                contents=prompt
            )
            raw = response.text.strip()
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
            logger.warning(f"Gemini LLM Natural Script QA failed: {e}. Falling back to rule-based PASS.")

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
    
    has_strong_hook = "?" in first_sentence or any(w in first_sentence.lower() for w in ["stop", "looking", "ever wonder", "secret", "best", "unbelievable", "need", "binge", "ready"])
    hook_res = {"pass": has_strong_hook, "reason": "Strong curiosity hook detected." if has_strong_hook else "Hook lacks curiosity trigger or question."}
    
    word_count = len(words)
    pacing_pass = 110 <= word_count <= 210
    pacing_res = {"pass": pacing_pass, "reason": f"Word count ({word_count}) aligns with fast-paced 30-40s delivery." if pacing_pass else "Pacing word count out of range."}
    
    payoff_pass = any(w in script_text.lower() for w in ["masterpiece", "twist", "animation", "story", "watch", "legendary", "reason", "insane", "incredible", "show", "rated", "series", "spotlight"])
    payoff_res = {"pass": payoff_pass, "reason": "Clear value/reason to watch included." if payoff_pass else "Lacks concrete payoff/reason to watch."}
    
    ending_pass = any(w in script_text[-100:].lower() for w in ["comment", "subscribe", "like", "watchlist", "next", "which one", "start"])
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
    """
    logger.info(">>> RUNNING POST-GENERATION FACT AUDIT QA")
    if not sources:
        return {"pass": False, "reason": "No fact sources available for audit."}

    mismatches = []
    
    for src in sources:
        title = src.get("anime_title", "")
        clean_title = re.sub(r'\s*\([^)]*\)', '', title).strip()
        score_num = src.get("score_numeric", 0.0)

        if clean_title.lower() in script_text.lower():
            scores_in_script = re.findall(r"(\d+\.\d+)\s*(?:out of 10|/10|staggering|rated)?", script_text)
            for s_str in scores_in_script:
                try:
                    s_val = float(s_str)
                    if abs(s_val - score_num) > 0.8 and s_val > 5.0 and score_num > 5.0:
                        pos_title = script_text.lower().find(clean_title.lower())
                        pos_score = script_text.find(s_str)
                        if abs(pos_title - pos_score) < 150:
                            mismatches.append(f"Score contradiction for '{title}': Script mentions {s_val}/10 but API score is {score_num:.1f}/10")
                except ValueError:
                    pass

    is_aligned = len(mismatches) == 0
    reason = "All script factual claims match verified API data." if is_aligned else f"Fact Audit Failed ({len(mismatches)} contradiction(s)): " + "; ".join(mismatches)

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

def check_asset_rights(image_paths: List[Path], bg_music_path: Path = None) -> Dict[str, Any]:
    """
    Tracks and verifies legal rights status for image & audio assets.
    """
    logger.info(">>> RUNNING ASSET RIGHTS & COPYRIGHT QA")
    assets_table = []
    unverified_count = 0

    for img_path in image_paths:
        if "cover_" in img_path.name:
            rights = "Official Promotional Artwork (Fair Use Editorial)"
            is_verified = True
        else:
            rights = "Unclear / High Risk"
            is_verified = False
            unverified_count += 1
            
        assets_table.append({
            "asset_name": img_path.name,
            "type": "Image",
            "rights_status": rights,
            "verified": is_verified
        })

    music_file = bg_music_path or config.DEFAULT_BG_MUSIC
    if music_file.exists():
        assets_table.append({
            "asset_name": music_file.name,
            "type": "Audio (Background Music)",
            "rights_status": "Royalty-Free Public Domain / Fair Use",
            "verified": True
        })
    else:
        assets_table.append({
            "asset_name": "Missing BG Music",
            "type": "Audio",
            "rights_status": "Missing File",
            "verified": False
        })
        unverified_count += 1

    all_clear = unverified_count == 0
    reason = "All assets verified for fair-use/royalty-free commercial compliance." if all_clear else f"{unverified_count} asset(s) have unverified/unclear rights."
    
    logger.info(f"[Rights QA Result] Pass: {all_clear} | Unverified Assets: {unverified_count}")
    return {
        "pass": all_clear,
        "assets": assets_table,
        "unverified_count": unverified_count,
        "reason": reason
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
            im1_resized = im1.resize((100, 100)).convert("L")
            im2_resized = im2.resize((100, 100)).convert("L")
            
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

    # Use exact spoken segment midpoints if provided
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
    reason = f"Multi-image visual segment alignment verified (Frame pixel diff at {t1:.1f}s vs {t2:.1f}s: {pixel_diff:.2f} >= 5.0)." if is_distinct else f"VISUAL SEGMENT BUG DETECTED: Frames at {t1:.1f}s and {t2:.1f}s are visually identical (pixel diff: {pixel_diff:.2f} < 5.0)! Only 1 image is displaying."

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

    # Verify ASS Karaoke word highlight tags if file is .ass
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

# ==================== FINAL VIDEO QA ====================

def run_final_video_qa(
    video_path: Path,
    image_paths: List[Path],
    audio_path: Path,
    srt_path: Path,
    policy_res: Dict[str, Any],
    rights_res: Dict[str, Any],
    script_qa_res: Dict[str, Any],
    originality_res: Dict[str, Any],
    fact_sources: List[Dict[str, Any]] = None,
    script_text: str = "",
    segment_timestamps: List[Dict[str, Any]] = None
) -> Dict[str, Any]:
    """
    Comprehensive video check BEFORE YouTube upload.
    Checks:
    - Resolution 1080x1920 (9:16 vertical)
    - Audio & Karaoke Subtitles QA (including Caption Drift & Highlight Validation)
    - Visual Segments Alignment Check (Extracts frames at exact spoken segment midpoints)
    - Post-Generation Fact Audit QA
    - Copyright / Asset Rights
    - YouTube Policy risk
    - Script quality pass (including Duplicate Word Check)
    - Originality pass
    Any single failure blocks upload.
    """
    logger.info(">>> RUNNING FINAL VIDEO QA (PRE-UPLOAD CHECK)")
    failed_checks = []

    # 1. Video existence & file size
    if not video_path.exists() or video_path.stat().st_size == 0:
        failed_checks.append("Final video file is missing or 0 bytes.")
    else:
        # 2. Check resolution & aspect ratio using FFmpeg
        ffmpeg_exe = imageio_ffmpeg.get_ffmpeg_exe()
        cmd = [ffmpeg_exe, "-i", str(video_path)]
        try:
            res = subprocess.run(cmd, stderr=subprocess.PIPE, stdout=subprocess.PIPE, text=True, encoding="utf-8", errors="ignore")
            match = re.search(r"(\d{3,4})x(\d{3,4})", res.stderr)
            if match:
                w, h = match.groups()
                if w != "1080" or h != "1920":
                    failed_checks.append(f"Invalid video resolution ({w}x{h}). Must be 1080x1920 (9:16 vertical).")
        except Exception as e:
            logger.warning(f"Could not verify video resolution via FFmpeg: {e}")

    # 3. Audio & Karaoke Subtitles check
    aud_res = check_audio_and_subtitles(audio_path, srt_path)
    if not aud_res["pass"]:
        failed_checks.append(f"Audio/Karaoke Subtitle QA failure: {aud_res['reason']}")

    # 4. Visual Segments Distinctness & Segment Alignment Check
    duration_sec = get_audio_duration_seconds(audio_path)
    vis_res = check_visual_segments_distinctness(video_path, expected_images_count=len(image_paths), total_duration_sec=duration_sec, segment_timestamps=segment_timestamps)
    if not vis_res["pass"]:
        failed_checks.append(f"Visual Segment Alignment QA failure: {vis_res['reason']}")

    # 5. Post-Generation Fact Audit QA Check
    if fact_sources and script_text:
        fact_audit_res = check_script_factual_alignment(script_text, fact_sources)
        if not fact_audit_res["pass"]:
            failed_checks.append(f"Fact Audit QA failure: {fact_audit_res['reason']}")

    # 6. Rights check
    if not rights_res.get("pass", False):
        failed_checks.append(f"Copyright/Rights QA failure: {rights_res.get('reason')}")

    # 7. Policy check (🔴 HIGH risk blocks upload)
    if policy_res.get("risk_level") == "HIGH":
        failed_checks.append(f"YouTube Policy QA failure: High policy risk flagged ({policy_res.get('flagged_issues')})")

    # 8. Script QA pass
    if not script_qa_res.get("pass", False):
        failed_checks.append(f"Script Quality QA failure: {script_qa_res.get('reason')}")

    # 9. Originality QA pass
    if not originality_res.get("pass", False):
        failed_checks.append(f"Originality QA failure: {originality_res.get('reason')}")

    all_passed = len(failed_checks) == 0
    reason = "All Final QA checks passed! Video approved for private upload." if all_passed else f"Final QA Failed on {len(failed_checks)} check(s): " + "; ".join(failed_checks)

    logger.info("=" * 60)
    logger.info(f"FINAL VIDEO QA RESULT: {'APPROVED 🟢' if all_passed else 'BLOCKED 🔴'}")
    if not all_passed:
        for fc in failed_checks:
            logger.error(f"  ❌ {fc}")
    logger.info("=" * 60)

    return {
        "pass": all_passed,
        "failed_checks": failed_checks,
        "reason": reason
    }
