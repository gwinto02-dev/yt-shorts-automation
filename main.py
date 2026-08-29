import argparse
import json
import logging
import sys
from pathlib import Path

# Ensure UTF-8 output on Windows console
if sys.platform == "win32":
    try:
        sys.stdout.reconfigure(encoding="utf-8")
        sys.stderr.reconfigure(encoding="utf-8")
    except Exception:
        pass

import config
from src.llm_tracker import reset_llm_calls, get_llm_call_count
from src.history_manager import check_originality_against_history, record_short_history

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
    handlers=[
        logging.StreamHandler(sys.stdout)
    ]
)
logger = logging.getLogger("anime_shorts")

def run_phase_1():
    """Phase 1: Content Source & Concept Selection (5-day cooldown)"""
    logger.info(">>> STARTING PHASE 1: Content Source & Concept Selection")
    from src.content_source import select_candidate_titles
    candidates, concept_key, concept_info = select_candidate_titles(num_candidates=3)
    
    output_file = config.OUTPUT_DIR / "selected_titles.json"
    data_payload = {
        "candidates": candidates,
        "concept_key": concept_key,
        "concept_info": concept_info
    }
    with open(output_file, "w", encoding="utf-8") as f:
        json.dump(data_payload, f, indent=2, ensure_ascii=False)
    logger.info(f"Phase 1 complete! Selected concept '{concept_key}' with {len(candidates)} titles.")
    return data_payload

def run_phase_2(
    candidates=None,
    concept_key="top_recommendations",
    concept_info=None,
    feedback_notes=None,
    target_opening_style=None,
    target_closing_style=None,
    target_transition_style=None,
    avoid_phrases=None,
    recent_hooks=None,
    recent_outros=None
):
    """Phase 2: Script Writing & Natural / Retention QA"""
    logger.info(">>> STARTING PHASE 2: Script Writing & Natural/Retention QA")
    if not candidates:
        selected_file = config.OUTPUT_DIR / "selected_titles.json"
        if not selected_file.exists():
            raise FileNotFoundError("selected_titles.json not found! Run Phase 1 first.")
        with open(selected_file, "r", encoding="utf-8") as f:
            data = json.load(f)
            candidates = data["candidates"]
            concept_key = data.get("concept_key", "top_recommendations")
            concept_info = data.get("concept_info", {})
            
    from src.script_generator import generate_recommendation_script
    script_data = generate_recommendation_script(
        candidates,
        concept_key,
        concept_info,
        feedback_notes=feedback_notes,
        target_opening_style=target_opening_style,
        target_closing_style=target_closing_style,
        target_transition_style=target_transition_style,
        avoid_phrases=avoid_phrases,
        recent_hooks=recent_hooks,
        recent_outros=recent_outros
    )
    script_file = config.OUTPUT_DIR / "script.txt"
    with open(script_file, "w", encoding="utf-8") as f:
        f.write(script_data["full_text"])
    logger.info(f"Phase 2 complete! Script written to {script_file}")
    return script_data


def run_phase_3(candidates=None):
    """Phase 3: Visuals Sourcing & Rights Metadata"""
    logger.info(">>> STARTING PHASE 3: Visuals Sourcing & Rights Metadata")
    if not candidates:
        selected_file = config.OUTPUT_DIR / "selected_titles.json"
        if not selected_file.exists():
            raise FileNotFoundError("selected_titles.json not found! Run Phase 1 first.")
        with open(selected_file, "r", encoding="utf-8") as f:
            data = json.load(f)
            candidates = data["candidates"]

    from src.visuals import fetch_and_save_visuals
    image_paths = fetch_and_save_visuals(candidates)
    logger.info(f"Phase 3 complete! Downloaded {len(image_paths)} cover images.")
    return image_paths

def run_phase_4(script_data=None, candidates=None):
    """Phase 4: Voiceover (TTS) & Subtitles"""
    logger.info(">>> STARTING PHASE 4: Voiceover (TTS) & Subtitles")
    if not script_data:
        script_file = config.OUTPUT_DIR / "script.txt"
        if not script_file.exists():
            raise FileNotFoundError("script.txt not found! Run Phase 2 first.")
        with open(script_file, "r", encoding="utf-8") as f:
            script_text = f.read()
        script_data = {"full_text": script_text}

    from src.tts import generate_narration_and_subtitles
    audio_path, subtitles_path, segment_timestamps = generate_narration_and_subtitles(script_data["full_text"], candidates=candidates)
    logger.info(f"Phase 4 complete! Audio: {audio_path}, Subtitles: {subtitles_path}")
    return audio_path, subtitles_path, segment_timestamps

def run_phase_5(image_paths=None, audio_path=None, subtitles_path=None, concept_key="top_recommendations", candidates=None, segment_timestamps=None):
    """Phase 5: Video Assembly with Visual Variety"""
    logger.info(">>> STARTING PHASE 5: Video Assembly")
    if not image_paths:
        from src.visuals import get_cached_image_paths
        image_paths = get_cached_image_paths()
    if not audio_path:
        audio_path = config.OUTPUT_DIR / "narration.mp3"
    if not subtitles_path:
        subtitles_path = config.OUTPUT_DIR / "subtitles.ass"

    from src.video_editor import assemble_short_video
    output_video = assemble_short_video(
        image_paths=image_paths,
        audio_path=audio_path,
        subtitles_path=subtitles_path,
        output_path=config.OUTPUT_DIR / "final_short.mp4",
        concept_key=concept_key,
        candidates=candidates,
        segment_timestamps=segment_timestamps
    )
    logger.info(f"Phase 5 complete! Final video output: {output_video}")
    return output_video

def run_phase_6(video_path=None, candidates=None, script_data=None, custom_title=None, final_qa_verdict=None):
    """Phase 6: YouTube Upload with pre-upload validation and safe error reporting."""
    logger.info(">>> STARTING PHASE 6: YouTube Upload (Private Guardrail)")
    if not video_path:
        video_path = config.OUTPUT_DIR / "final_short.mp4"
    if not candidates:
        selected_file = config.OUTPUT_DIR / "selected_titles.json"
        if selected_file.exists():
            with open(selected_file, "r", encoding="utf-8") as f:
                data = json.load(f)
                candidates = data.get("candidates", [])
        else:
            candidates = []

    if not custom_title and isinstance(script_data, dict):
        custom_title = script_data.get("video_title")

    from src.youtube_uploader import upload_short_to_youtube
    upload_res = upload_short_to_youtube(
        video_path=video_path,
        candidates=candidates,
        privacy_status="private",
        custom_title=custom_title,
        final_qa_verdict=final_qa_verdict
    )

    if upload_res.get("success") or upload_res.get("status") == "dry_run_success":
        logger.info(f"Phase 6 complete! Upload status: {upload_res.get('youtube_url') or 'Dry-run success'}")
    else:
        logger.warning(
            f"Phase 6: Upload not completed. "
            f"error_type={upload_res.get('error_type')} | "
            f"api_reason={upload_res.get('api_reason')} | "
            f"upload_attempted={upload_res.get('upload_attempted')} | "
            f"video_preserved={upload_res.get('video_preserved')}"
        )
    return upload_res


def run_full_pipeline():
    """Runs full pipeline end-to-end with QA evaluators, retries, and upload safety."""
    logger.info("=========================================================")
    logger.info("=== STARTING YOUTUBE SHORTS AUTOMATION FULL PIPELINE ===")
    logger.info("=========================================================")

    # Step 0: Pre-flight Groq API Quota & Reachability Check
    if config.GROQ_API_KEY or config.GEMINI_API_KEY:
        from src.groq_utils import check_groq_quota_preflight
        quota_ok, quota_msg = check_groq_quota_preflight()
        if not quota_ok:
            logger.error(f"❌ PIPELINE ABORTED: Pre-flight Groq API Check Failed!\nReason: {quota_msg}")
            sys.exit(1)
    else:
        logger.info("[Pre-flight Quota Check] GROQ_API_KEY not set. Operating in template fallback mode.")

    # Capture the run's start time BEFORE any history writes happen (Phase 1
    # records the selected titles to title_history.json for cooldown
    # persistence purposes, even on runs that later get blocked). The
    # Supervisor QA's cooldown check later in this same run must exclude
    # entries written at or after this timestamp, so it never flags this
    # run's own just-written selection as a violation of itself.
    from datetime import datetime
    run_start_time = datetime.now()

    reset_llm_calls()
    retry_counts = {}

    from src.history_manager import record_anime_titles_usage, record_video_title_usage

    # Step 1: Concept & Candidate Selection (5-day concept cooldown & 30-day title cooldown)
    p1_data = run_phase_1()
    candidates = p1_data["candidates"]
    concept_key = p1_data["concept_key"]
    concept_info = p1_data["concept_info"]

    # Step 2: Fact Checking
    from src.fact_checker import verify_candidate_facts
    fact_check_res = verify_candidate_facts(candidates)

    # Step 3 & 4 & 5: Script Generation + Natural & Retention QA (Bounded Retries)
    script_data = run_phase_2(candidates, concept_key, concept_info)
    retry_counts["script_qa"] = script_data.get("retries", 0)
    video_title = script_data.get("video_title", "Top Anime Short")

    script_text = script_data.get("full_text", "")
    script_qa_res = script_data.get("script_qa_res", {"pass": True, "reason": "Not run"})
    retention_qa_res = script_data.get("retention_qa_res", {"pass": True, "reason": "Not run"})
    word_count = script_data.get("word_count", len(script_text.split()) if script_text else 0)

    script_is_invalid = (
        not script_text
        or not script_text.strip()
        or word_count < 110
        or not script_qa_res.get("pass", True)
        or not retention_qa_res.get("pass", True)
    )

    if script_is_invalid:
        fail_reason = (
            "Script text is empty or zero-length." if not script_text or not script_text.strip()
            else f"Script word count ({word_count} words) is below minimum required 110 words." if word_count < 110
            else script_qa_res.get("reason") if not script_qa_res.get("pass", True)
            else retention_qa_res.get("reason", "Failed script quality check")
        )
        logger.error(
            f"❌ PIPELINE ABORTED: Script generation failed QA after all retries — script is empty or invalid. "
            f"Reason: {fail_reason}. Blocking before Phase 4 (TTS)."
        )

        final_qa_res = {
            "pass": False,
            "verdict": "FAIL - BLOCKED BY SCRIPT QA GATE",
            "failing_evaluators": ["Natural Script Quality QA" if not script_qa_res.get("pass", True) else "Retention QA"],
            "reasons": [fail_reason],
            "details": {}
        }

        # Send daily summary email notification report before exiting
        from src.notifier import send_daily_summary_email
        send_daily_summary_email(
            candidates=candidates,
            script_data=script_data,
            concept_info=concept_info,
            fact_check_res=fact_check_res,
            policy_res={"pass": False, "reason": "Pipeline aborted due to script QA failure"},
            rights_res={"pass": False, "reason": "Not evaluated due to early abort"},
            originality_res={"pass": False, "reason": "Not evaluated due to early abort"},
            final_qa_res=final_qa_res,
            upload_res={"success": False, "message": f"Pipeline aborted: {fail_reason}"},
            retry_counts=retry_counts
        )

        logger.error("=== FULL PIPELINE FINISHED: BLOCKED BY SCRIPT QA GATE ===")
        sys.exit(1)

    # Step 6: Policy QA
    from src.qa_checker import check_youtube_policy_compliance, check_asset_rights, run_supervisor_qa_gate
    policy_res = check_youtube_policy_compliance(script_data["full_text"], video_title, candidates)

    # Step 7: Visuals Sourcing & Rights Check
    image_paths = run_phase_3(candidates)
    rights_res = check_asset_rights(image_paths, candidates=candidates)

    # Step 8: Voice (TTS) & Subtitles
    audio_path, subtitles_path, segment_timestamps = run_phase_4(script_data, candidates=candidates)

    # Step 9: Video Assembly with Visual Variety & Segment Alignment
    video_path = run_phase_5(image_paths, audio_path, subtitles_path, concept_key, candidates, segment_timestamps=segment_timestamps)

    # Step 10: Content Originality QA & Structural Variety QA against Past Shorts History (Bounded Retries)
    from src.history_manager import check_structural_variety_against_history, get_recent_hooks_and_outros
    first_sentence = script_data["full_text"].split(".")[0] if "." in script_data["full_text"] else script_data["full_text"][:50]
    orig_retries = 0
    
    originality_res = check_originality_against_history(script_data["full_text"], first_sentence, video_title)
    structural_variety_res = check_structural_variety_against_history(script_data["full_text"])

    ALL_OPENINGS = ["SPECIFIC_QUESTION", "SURPRISING_FACT", "IN_SCENE_MID_THOUGHT"]
    ALL_CLOSINGS = ["SPECIFIC_CALLBACK_QUESTION", "SPECIFIC_CALLBACK_BINGE", "SPECIFIC_CALLBACK_OPINION"]

    while (not originality_res["pass"] or not structural_variety_res["pass"]) and orig_retries < config.MAX_STAGE_RETRIES:
        orig_retries += 1
        fail_reason = originality_res.get("reason") if not originality_res["pass"] else structural_variety_res.get("reason")
        logger.warning(f"[Script Variety QA Retry {orig_retries}/{config.MAX_STAGE_RETRIES}] {fail_reason}. Regenerating script with distinct structural style...")
        
        recent_info = get_recent_hooks_and_outros(days=30, limit=5)
        forbidden_phrases = list(structural_variety_res.get("forbidden_phrases", []))
        
        fp = structural_variety_res.get("fingerprint", {})
        if fp.get("opening_prefix"):
            forbidden_phrases.append(fp["opening_prefix"])
        if fp.get("closing_prefix"):
            forbidden_phrases.append(fp["closing_prefix"])

        forbidden_ops = set(recent_info.get("opening_styles", []) + ([fp.get("opening_style")] if fp.get("opening_style") else []))
        forbidden_cls = set(recent_info.get("closing_styles", []) + ([fp.get("closing_style")] if fp.get("closing_style") else []))

        avail_ops = [s for s in ALL_OPENINGS if s not in forbidden_ops] or ALL_OPENINGS
        avail_cls = [s for s in ALL_CLOSINGS if s not in forbidden_cls] or ALL_CLOSINGS

        target_op = avail_ops[(orig_retries - 1) % len(avail_ops)]
        target_cl = avail_cls[(orig_retries - 1) % len(avail_cls)]

        feedback_notes = (
            f"REWRITE REQUIRED: {fail_reason}. "
            f"Do NOT use phrase(s): {', '.join(forbidden_phrases)}. "
            f"Target Opening Style: {target_op}. Target Closing Style: {target_cl}."
        )

        script_data = run_phase_2(
            candidates,
            concept_key,
            concept_info,
            feedback_notes=feedback_notes,
            target_opening_style=target_op,
            target_closing_style=target_cl,
            avoid_phrases=forbidden_phrases,
            recent_hooks=recent_info.get("hooks", []),
            recent_outros=recent_info.get("outros", [])
        )
        video_title = script_data.get("video_title", video_title)
        first_sentence = script_data["full_text"].split(".")[0] if "." in script_data["full_text"] else script_data["full_text"][:50]
        
        audio_path, subtitles_path, segment_timestamps = run_phase_4(script_data, candidates=candidates)
        video_path = run_phase_5(image_paths, audio_path, subtitles_path, concept_key, candidates, segment_timestamps=segment_timestamps)
        
        originality_res = check_originality_against_history(script_data["full_text"], first_sentence, video_title)
        structural_variety_res = check_structural_variety_against_history(script_data["full_text"])

    retry_counts["originality_qa"] = orig_retries

    # Log explicitly if retries were exhausted with QA still failing
    if not originality_res["pass"] or not structural_variety_res["pass"]:
        failing_check = "Originality" if not originality_res["pass"] else "Structural Variety"
        failing_reason = originality_res.get("reason") if not originality_res["pass"] else structural_variety_res.get("reason")
        logger.error(
            f"[{failing_check} QA] Maximum retries ({config.MAX_STAGE_RETRIES}) exhausted — QA still FAILING. "
            f"Reason: {failing_reason}. "
            "Supervisor QA Gate will block upload."
        )

    # Step 11: Consolidated Supervisor QA Gate Pre-Upload Check
    final_qa_res = run_supervisor_qa_gate(
        video_path=video_path,
        image_paths=image_paths,
        audio_path=audio_path,
        srt_path=subtitles_path,
        candidates=candidates,
        concept_key=concept_key,
        video_title=video_title,
        policy_res=policy_res,
        rights_res=rights_res,
        script_qa_res=script_data.get("script_qa_res", {"pass": True}),
        retention_qa_res=script_data.get("retention_qa_res", {"pass": True}),
        originality_res=originality_res,
        structural_variety_res=structural_variety_res,
        fact_sources=fact_check_res.get("sources"),
        script_text=script_data.get("full_text", ""),
        segment_timestamps=segment_timestamps,
        run_start_time=run_start_time
    )


    # Step 12: Upload to YouTube PRIVATE ONLY if Supervisor QA Passes
    upload_res = None
    if final_qa_res["pass"]:
        upload_res = run_phase_6(video_path, candidates, script_data, custom_title=video_title, final_qa_verdict=True)
        if upload_res.get("success") or upload_res.get("status") == "dry_run_success":
            record_short_history(
                concept_type=concept_key,
                title=video_title,
                hook=first_sentence,
                script=script_data["full_text"],
                video_id=upload_res.get("video_id"),
                concept_angle=concept_info.get("selected_angle")
            )
            record_anime_titles_usage(candidates, concept_type=concept_key)
            record_video_title_usage(video_title, concept_type=concept_key)
        else:
            logger.warning(
                f"Upload did not succeed — history not recorded. "
                f"Reason: {upload_res.get('message', 'unknown')}"
            )
    else:
        logger.error("❌ SUPERVISOR QA GATE FAILED! YouTube upload BLOCKED to prevent uploading broken/non-compliant content.")

    # Step 13: Daily Review Summary Email Report
    from src.notifier import send_daily_summary_email
    send_daily_summary_email(
        candidates=candidates,
        script_data=script_data,
        concept_info=concept_info,
        fact_check_res=fact_check_res,
        policy_res=policy_res,
        rights_res=rights_res,
        originality_res=originality_res,
        final_qa_res=final_qa_res,
        upload_res=upload_res,
        retry_counts=retry_counts
    )

    if not final_qa_res["pass"]:
        logger.error("=== FULL PIPELINE FINISHED: BLOCKED BY SUPERVISOR QA ===")
        sys.exit(1)

    logger.info("=== FULL PIPELINE COMPLETED SUCCESSFULLY ===")

def main():
    parser = argparse.ArgumentParser(description="Daily Anime Recommendation Shorts Automation")
    parser.add_argument("--phase", type=int, choices=[1, 2, 3, 4, 5, 6, 7], help="Run a specific phase (1-7)")
    parser.add_argument("--all", action="store_true", help="Run full pipeline end-to-end")
    parser.add_argument("--check-quota", action="store_true", help="Run pre-flight Groq API quota check and exit")
    args = parser.parse_args()

    if args.check_quota:
        from src.groq_utils import check_groq_quota_preflight
        ok, msg = check_groq_quota_preflight()
        sys.exit(0 if ok else 1)
    elif args.phase:
        phase_map = {
            1: run_phase_1,
            2: run_phase_2,
            3: run_phase_3,
            4: run_phase_4,
            5: run_phase_5,
            6: run_phase_6
        }
        phase_map[args.phase]()
    elif args.all:
        run_full_pipeline()
    else:
        parser.print_help()

if __name__ == "__main__":
    main()
