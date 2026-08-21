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

def run_phase_2(candidates=None, concept_key="top_recommendations", concept_info=None):
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
    script_data = generate_recommendation_script(candidates, concept_key, concept_info)
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

def run_phase_6(video_path=None, candidates=None, script_data=None):
    """Phase 6: YouTube Upload (Private Guardrail)"""
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

    from src.youtube_uploader import upload_short_to_youtube
    upload_res = upload_short_to_youtube(video_path=video_path, candidates=candidates, privacy_status="private")
    logger.info(f"Phase 6 complete! Result: {upload_res}")
    return upload_res

def run_full_pipeline():
    """Runs full pipeline end-to-end with QA evaluators, retries, and upload safety."""
    logger.info("=========================================================")
    logger.info("=== STARTING YOUTUBE SHORTS AUTOMATION FULL PIPELINE ===")
    logger.info("=========================================================")
    
    reset_llm_calls()
    retry_counts = {}

    # Step 1: Concept & Candidate Selection (5-day cooldown)
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

    # Step 6: Policy QA
    from src.qa_checker import check_youtube_policy_compliance, check_asset_rights, run_final_video_qa
    from src.youtube_uploader import generate_video_metadata
    metadata = generate_video_metadata(candidates)
    policy_res = check_youtube_policy_compliance(script_data["full_text"], metadata["title"], candidates)

    # Step 7: Visuals Sourcing & Rights Check
    image_paths = run_phase_3(candidates)
    rights_res = check_asset_rights(image_paths)

    # Step 8: Voice (TTS) & Subtitles
    audio_path, subtitles_path, segment_timestamps = run_phase_4(script_data, candidates=candidates)

    # Step 9: Video Assembly with Visual Variety & Segment Alignment
    video_path = run_phase_5(image_paths, audio_path, subtitles_path, concept_key, candidates, segment_timestamps=segment_timestamps)

    # Step 10: Originality Check against Past Shorts History (Bounded Retries)
    first_sentence = script_data["full_text"].split(".")[0] if "." in script_data["full_text"] else script_data["full_text"][:50]
    orig_retries = 0
    originality_res = check_originality_against_history(script_data["full_text"], first_sentence, metadata["title"])

    while not originality_res["pass"] and orig_retries < config.MAX_STAGE_RETRIES:
        orig_retries += 1
        logger.warning(f"[Originality QA Retry {orig_retries}/{config.MAX_STAGE_RETRIES}] Script too similar to history. Regenerating script...")
        script_data = run_phase_2(candidates, concept_key, concept_info)
        audio_path, subtitles_path, segment_timestamps = run_phase_4(script_data, candidates=candidates)
        video_path = run_phase_5(image_paths, audio_path, subtitles_path, concept_key, candidates, segment_timestamps=segment_timestamps)
        originality_res = check_originality_against_history(script_data["full_text"], first_sentence, metadata["title"])

    retry_counts["originality_qa"] = orig_retries

    # Step 11: Final Video QA Pre-Upload Check
    final_qa_res = run_final_video_qa(
        video_path=video_path,
        image_paths=image_paths,
        audio_path=audio_path,
        srt_path=subtitles_path,
        policy_res=policy_res,
        rights_res=rights_res,
        script_qa_res=script_data.get("script_qa_res", {"pass": True}),
        originality_res=originality_res,
        fact_sources=fact_check_res.get("sources"),
        script_text=script_data.get("full_text", ""),
        segment_timestamps=segment_timestamps
    )



    # Step 12: Upload to YouTube PRIVATE ONLY if Final QA Passes
    upload_res = None
    if final_qa_res["pass"]:
        upload_res = run_phase_6(video_path, candidates, script_data)
        record_short_history(
            concept_type=concept_key,
            title=metadata["title"],
            hook=first_sentence,
            script=script_data["full_text"],
            video_id=upload_res.get("video_id")
        )
    else:
        logger.error("❌ FINAL QA FAILED! YouTube upload BLOCKED to prevent uploading partial/defective video.")

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

    logger.info("=== FULL PIPELINE COMPLETED SUCCESSFULLY ===")

def main():
    parser = argparse.ArgumentParser(description="Daily Anime Recommendation Shorts Automation")
    parser.add_argument("--phase", type=int, choices=[1, 2, 3, 4, 5, 6, 7], help="Run a specific phase (1-7)")
    parser.add_argument("--all", action="store_true", help="Run full pipeline end-to-end")
    args = parser.parse_args()

    if args.phase:
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
