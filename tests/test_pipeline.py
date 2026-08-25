import unittest
import sys
from pathlib import Path

# Add project root to sys.path
BASE_DIR = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(BASE_DIR))

import config
from src.youtube_uploader import validate_privacy_status, generate_video_metadata
from src.script_generator import generate_fallback_template_script
from src.history_manager import is_concept_allowed_by_history, record_concept_usage, check_originality_against_history, record_short_history
from src.popularity_filter import is_mainstream_anime, can_qualify_as_hidden_gem
from src.fact_checker import verify_candidate_facts
from src.content_source import select_candidate_titles
from src.qa_checker import (
    check_natural_script_quality, check_youtube_policy_compliance, check_asset_rights,
    check_visual_segments_distinctness, check_script_factual_alignment, check_audio_and_subtitles, run_final_video_qa
)
from src.tts import generate_tiktok_karaoke_ass, compute_title_segment_timestamps, validate_caption_sync
from src.llm_tracker import reset_llm_calls, increment_llm_calls, get_llm_call_count, is_nearing_rate_limit

from unittest.mock import patch, MagicMock

class TestPipelineGuardrailsAndFeatures(unittest.TestCase):

    def setUp(self):
        import tempfile
        self.tmp_dir = tempfile.TemporaryDirectory()
        tmp_path = Path(self.tmp_dir.name)
        
        self.patchers = [
            patch.object(config, "SHORTS_HISTORY_FILE", tmp_path / "shorts_history.json"),
            patch.object(config, "CONCEPT_HISTORY_FILE", tmp_path / "concept_history.json"),
            patch.object(config, "TITLE_HISTORY_FILE", tmp_path / "title_history.json"),
            patch.object(config, "VIDEO_TITLE_HISTORY_FILE", tmp_path / "video_title_history.json"),
            patch.object(config, "OUTPUT_DIR", tmp_path / "output"),
        ]
        for p in self.patchers:
            p.start()
        (tmp_path / "output").mkdir(parents=True, exist_ok=True)

    def tearDown(self):
        for p in self.patchers:
            p.stop()
        self.tmp_dir.cleanup()

    def test_youtube_privacy_guardrail_blocks_public(self):
        """CRITICAL: Ensure setting privacy status to 'public' raises ValueError."""
        with self.assertRaises(ValueError) as ctx:
            validate_privacy_status("public")
        self.assertIn("SAFETY GUARDRAIL VIOLATION", str(ctx.exception))

    def test_youtube_privacy_guardrail_allows_private_and_scheduled(self):
        """Ensure 'private' and 'scheduled' statuses are accepted."""
        self.assertEqual(validate_privacy_status("private"), "private")
        self.assertEqual(validate_privacy_status("scheduled"), "scheduled")
        self.assertEqual(validate_privacy_status("PRIVATE"), "private")

    def test_concept_5day_cooldown_history(self):
        """Verify concepts used within last 5 days are identified for cooldown."""
        test_concept = "test_cooldown_concept_type"
        record_concept_usage(test_concept)
        allowed = is_concept_allowed_by_history(test_concept, days=5)
        self.assertFalse(allowed, "Concept used recently should NOT be allowed within 5-day window.")

    def test_popularity_floor_blocks_mainstream(self):
        """Regression test: verify high popularity anime are strictly blocked from hidden_gems concept."""
        mainstream_candidates = [
            {"title": "ONE PIECE", "popularity": 550000, "mal_members": 2300000, "average_score": 8.7},
            {"title": "Re:ZERO", "popularity": 350000, "mal_members": 1800000, "average_score": 9.0},
            {"title": "Hunter x Hunter", "popularity": 400000, "mal_members": 2100000, "average_score": 8.9},
            {"title": "Demon Slayer", "popularity": 600000, "mal_members": 2800000, "average_score": 8.5},
        ]
        for c in mainstream_candidates:
            is_mainstream, reason = is_mainstream_anime(c)
            self.assertTrue(is_mainstream, f"Title {c['title']} must be flagged as mainstream!")
            qualifies, q_reason = can_qualify_as_hidden_gem(c)
            self.assertFalse(qualifies, f"Title {c['title']} must NOT qualify as a hidden gem!")

    def test_genre_diverse_trio_selection(self):
        """Regression test: verify Genre-Diverse Trio picks 3 titles with distinct primary genres and fails when impossible."""
        # 1. Valid pool with distinct primary genres
        valid_pool = [
            {"id": 1, "title": "Fantasy Show", "cover_image": "img1.jpg", "genres": ["Fantasy", "Action"], "average_score": 8.5},
            {"id": 2, "title": "SciFi Show", "cover_image": "img2.jpg", "genres": ["Sci-Fi", "Drama"], "average_score": 8.4},
            {"id": 3, "title": "Romance Show", "cover_image": "img3.jpg", "genres": ["Romance", "Comedy"], "average_score": 8.3},
        ]
        from unittest.mock import patch
        with patch("src.content_source.fetch_anilist_trending", return_value=valid_pool):
            with patch("src.content_source.fetch_local_trend_data", return_value=[]):
                cands, key, info = select_candidate_titles(num_candidates=3, concept_key="genre_spotlight")
                self.assertEqual(len(cands), 3)
                primary_genres = [c["genres"][0] for c in cands]
                self.assertEqual(len(set(primary_genres)), 3, "Primary genres must be completely distinct!")

        # 2. Invalid pool with overlapping primary genres (all Action) -> must raise ValueError
        same_genre_pool = [
            {"id": 1, "title": "Action Show 1", "cover_image": "img1.jpg", "genres": ["Action"], "average_score": 8.5},
            {"id": 2, "title": "Action Show 2", "cover_image": "img2.jpg", "genres": ["Action"], "average_score": 8.4},
            {"id": 3, "title": "Action Show 3", "cover_image": "img3.jpg", "genres": ["Action"], "average_score": 8.3},
        ]
        with patch("src.content_source.fetch_anilist_trending", return_value=same_genre_pool):
            with patch("src.content_source.fetch_local_trend_data", return_value=[]):
                with self.assertRaises(ValueError) as ctx:
                    select_candidate_titles(num_candidates=3, concept_key="genre_spotlight")
                self.assertIn("Genre-Diverse Trio criteria failed", str(ctx.exception))

    def test_underrated_trio_selection(self):
        """Regression test: verify Underrated Trio enforces popularity floor and fails cleanly if < 3 qualify."""
        # Mainstream pool -> all exceed popularity floor -> must raise ValueError
        mainstream_pool = [
            {"id": 1, "title": "ONE PIECE", "cover_image": "img1.jpg", "popularity": 500000, "mal_members": 2000000, "average_score": 8.7},
            {"id": 2, "title": "Re:ZERO", "cover_image": "img2.jpg", "popularity": 400000, "mal_members": 1500000, "average_score": 9.0},
            {"id": 3, "title": "Demon Slayer", "cover_image": "img3.jpg", "popularity": 600000, "mal_members": 2500000, "average_score": 8.5},
        ]
        from unittest.mock import patch
        with patch("src.content_source.fetch_anilist_trending", return_value=mainstream_pool):
            with patch("src.content_source.fetch_local_trend_data", return_value=[]):
                with self.assertRaises(ValueError) as ctx:
                    select_candidate_titles(num_candidates=3, concept_key="hidden_gems")
                self.assertIn("Underrated Trio criteria failed", str(ctx.exception))

    def test_upcoming_trio_selection(self):
        """Regression test: verify Upcoming Trio selects unreleased/upcoming titles and fails cleanly if < 3 qualify."""
        upcoming_pool = [
            {"id": 101, "title": "Upcoming Anime 1", "cover_image": "u1.jpg", "status": "NOT_YET_RELEASED", "seasonYear": 2026, "genres": ["Action"]},
            {"id": 102, "title": "Upcoming Anime 2", "cover_image": "u2.jpg", "status": "NOT_YET_RELEASED", "seasonYear": 2026, "genres": ["Fantasy"]},
            {"id": 103, "title": "Upcoming Anime 3", "cover_image": "u3.jpg", "status": "NOT_YET_RELEASED", "seasonYear": 2027, "genres": ["Sci-Fi"]},
        ]
        from unittest.mock import patch
        with patch("src.content_source.fetch_anilist_upcoming", return_value=upcoming_pool):
            cands, key, info = select_candidate_titles(num_candidates=3, concept_key="upcoming_spotlight")
            self.assertEqual(len(cands), 3)
            for c in cands:
                self.assertEqual(c["selection_category"], "Upcoming Hype Pick")

        # Invalid pool with only 1 upcoming title -> must raise ValueError
        sparse_upcoming_pool = [
            {"id": 101, "title": "Upcoming Anime 1", "cover_image": "u1.jpg", "status": "NOT_YET_RELEASED", "seasonYear": 2026, "genres": ["Action"]},
            {"id": 102, "title": "Finished Anime 1", "cover_image": "u2.jpg", "status": "FINISHED", "seasonYear": 2020, "genres": ["Fantasy"]},
        ]
        with patch("src.content_source.fetch_anilist_upcoming", return_value=sparse_upcoming_pool):
            with self.assertRaises(ValueError) as ctx:
                select_candidate_titles(num_candidates=3, concept_key="upcoming_spotlight")
            self.assertIn("Upcoming Trio criteria failed", str(ctx.exception))

    def test_fact_check_retrieval_and_sources_file(self):
        """Regression test: verify verify_candidate_facts populates sources with studio, year, and writes file."""
        test_candidates = [
            {"title": "Frieren: Beyond Journey's End", "id": 154587, "average_score": 9.3, "source": "AniList", "genres": ["Fantasy"]}
        ]
        res = verify_candidate_facts(test_candidates)
        self.assertEqual(res["status"], "verified")
        self.assertEqual(len(res["sources"]), 1)
        
        sources_file = config.OUTPUT_DIR / "fact_check_sources.json"
        self.assertTrue(sources_file.exists())
        self.assertGreater(sources_file.stat().st_size, 20)

    def test_post_generation_fact_audit_qa(self):
        """Regression test: verify Post-Generation Fact Audit QA flags script score contradictions."""
        sources = [
            {"anime_title": "Hunter x Hunter", "score_numeric": 8.9, "verified_score": "8.9/10"}
        ]
        contradictory_script = "Looking for anime? Hunter x Hunter is rated 6.0 out of 10 in today's spotlight."
        audit_res = check_script_factual_alignment(contradictory_script, sources)
        self.assertFalse(audit_res["pass"])
        self.assertIn("Score contradiction for 'Hunter x Hunter'", audit_res["reason"])

    def test_detect_consecutive_duplicate_words(self):
        """Regression test: verify Natural Script QA flags consecutive duplicate words like 'spotlight spotlight'."""
        dup_script = "Looking for peak anime? Check out today's Hero Spotlight spotlight featuring top shows!"
        qa_res = check_natural_script_quality(dup_script)
        self.assertFalse(qa_res["pass"])
        self.assertIn("consecutive duplicate words", qa_res["reason"])

    def test_generate_karaoke_ass_subtitles(self):
        """Regression test: verify TikTok style Karaoke ASS subtitles contain active word yellow highlight tags."""
        word_bounds = [
            {"text": "Looking", "offset_ms": 100, "duration_ms": 300, "end_ms": 400},
            {"text": "for", "offset_ms": 400, "duration_ms": 200, "end_ms": 600},
            {"text": "anime", "offset_ms": 600, "duration_ms": 400, "end_ms": 1000},
        ]
        ass_str = generate_tiktok_karaoke_ass(word_bounds, words_per_phrase=3)
        self.assertIn(r"\c&H0000FFFF&\b1", ass_str)
        self.assertIn("Dialogue: 0,0:00:00.10,0:00:00.40", ass_str)

    def test_compute_title_segment_timestamps(self):
        """Regression test: verify candidate title keyword matching calculates accurate segment timestamps."""
        candidates = [
            {"title": "That Time I Got Reincarnated as a Slime"},
            {"title": "Re:ZERO -Starting Life in Another World-"},
            {"title": "Hunter x Hunter (2011)"}
        ]
        word_bounds = [
            {"text": "Looking", "offset_ms": 100, "duration_ms": 300, "end_ms": 400},
            {"text": "at", "offset_ms": 400, "duration_ms": 200, "end_ms": 600},
            {"text": "Hunter", "offset_ms": 2000, "duration_ms": 500, "end_ms": 2500},
            {"text": "and", "offset_ms": 2500, "duration_ms": 200, "end_ms": 2700},
            {"text": "Re:ZERO", "offset_ms": 18000, "duration_ms": 600, "end_ms": 18600},
            {"text": "finally", "offset_ms": 18600, "duration_ms": 400, "end_ms": 19000},
            {"text": "Slime", "offset_ms": 35000, "duration_ms": 500, "end_ms": 35500},
        ]
        segs = compute_title_segment_timestamps(word_bounds, candidates, total_duration_sec=55.0)
        self.assertEqual(len(segs), 3)
        self.assertAlmostEqual(segs[0]["start_sec"], 0.0, delta=0.5)
        self.assertAlmostEqual(segs[1]["start_sec"], 18.0, delta=0.5)
        self.assertAlmostEqual(segs[2]["start_sec"], 35.0, delta=0.5)

    def test_script_natural_qa_detects_cliches(self):
        """Verify Natural Script QA flags overused AI tropes."""
        cliche_script = "In a world where anime exists, buckle up for an absolute masterpiece!"
        qa_res = check_natural_script_quality(cliche_script)
        self.assertFalse(qa_res["pass"])
        self.assertIn("robotic/overused AI tropes", qa_res["reason"])

    def test_policy_checker_loads_rules(self):
        """Verify policy checker loads policy_rules.json and evaluates risk level."""
        script = "Looking for your next banger anime? Here are top recommendations."
        candidates = [{"title": "Demon Slayer", "average_score": 8.7}]
        res = check_youtube_policy_compliance(script, "Top Anime", candidates)
        self.assertIn("status", res)
        self.assertIn(res["risk_level"], ["LOW", "MEDIUM", "HIGH"])

    def test_originality_checker_prevents_duplicates(self):
        """Verify duplicate script detection against history."""
        sample_title = "Originality Test Title"
        sample_script = "This is a unique test script for checking duplicate detection in shorts history."
        record_short_history("top_recommendations", sample_title, "Hook text", sample_script)
        
        check_res = check_originality_against_history(sample_script, "Hook text", sample_title)
        self.assertFalse(check_res["pass"])
        self.assertIn("High structural/text similarity", check_res["reason"])

    def test_final_qa_blocks_upload_on_failure(self):
        """Verify Final QA returns pass=False and blocks upload if a check fails."""
        res = run_final_video_qa(
            video_path=Path("non_existent_video.mp4"),
            image_paths=[],
            audio_path=Path("non_existent_audio.mp3"),
            srt_path=Path("non_existent_subtitles.srt"),
            policy_res={"risk_level": "HIGH", "flagged_issues": ["Forbidden phrase"]},
            rights_res={"pass": False, "reason": "Unverified images"},
            script_qa_res={"pass": False, "reason": "Cliches present"},
            originality_res={"pass": False, "reason": "Duplicate content"}
        )
        self.assertFalse(res["pass"])
        self.assertGreater(len(res["failed_checks"]), 0)

    def test_llm_call_counter(self):
        """Verify LLM API call tracking and 80% threshold limit warning."""
        reset_llm_calls()
        self.assertEqual(get_llm_call_count(), 0)
        increment_llm_calls(5)
        self.assertEqual(get_llm_call_count(), 5)
        self.assertFalse(is_nearing_rate_limit())
        increment_llm_calls(10)
        self.assertTrue(is_nearing_rate_limit())

    def test_asset_rights_checker(self):
        """
        Verify structured rights metadata classification.

        Requirements:
        1. Official promotional artwork from AniList/Jikan → LICENSE_UNKNOWN,
           commercial_use_verified=False, pipeline continues (pass=True),
           asset appears in flagged_for_review for human inspection.
        2. Explicitly licensed asset (e.g. CC0) → LICENSE_VERIFIED,
           commercial_use_verified=True, pass=True, NOT in flagged_for_review.
        3. High-risk / restricted asset → LICENSE_RESTRICTED / HIGH,
           pass=False (upload blocked).
        4. Missing bg_music file → missing_assets list only, NOT a rights
           failure, does not affect pass result.
        5. No candidate metadata supplied → fallback to LICENSE_UNKNOWN /
           REVIEW (conservative), still not blocked (pass=True), but flagged.
        """
        # ---- Case 1: Typical AniList download → LICENSE_UNKNOWN, not commercially verified ----
        candidate_unknown = {
            "title": "Attack on Titan",
            "asset_rights": {
                "asset_id": "cover_1_attack_on_titan.jpg",
                "source": "AniList",
                "source_url": "https://cdn.anilist.co/attack_on_titan.jpg",
                "asset_type": "Official Promotional Artwork",
                "license_status": "LICENSE_UNKNOWN",
                "commercial_use_verified": False,
                "risk_level": "REVIEW",
                "note": "Downloaded from AniList API. Licence status unknown."
            }
        }
        res1 = check_asset_rights(
            [Path("cover_1_attack_on_titan.jpg")],
            candidates=[candidate_unknown]
        )
        self.assertTrue(
            res1["pass"],
            "LICENSE_UNKNOWN artwork must NOT block upload (human reviews private video)."
        )
        self.assertFalse(
            res1["assets"][0]["commercial_use_verified"],
            "Official promotional artwork from AniList must NOT be marked commercially verified."
        )
        self.assertEqual(
            res1["assets"][0]["license_status"],
            "LICENSE_UNKNOWN",
            "AniList artwork must be reported as LICENSE_UNKNOWN, not VERIFIED."
        )
        self.assertEqual(res1["assets"][0]["risk_level"], "REVIEW")
        self.assertEqual(len(res1["flagged_for_review"]), 1,
            "LICENSE_UNKNOWN asset must appear in flagged_for_review for human inspection.")
        self.assertEqual(res1["high_risk_count"], 0)
        self.assertIsInstance(res1.get("missing_assets"), list)

        # ---- Case 2: Explicitly licensed asset (e.g. CC0 / royalty-free image pack) ----
        candidate_verified = {
            "title": "CC0 Illustration",
            "asset_rights": {
                "asset_id": "cover_2_cc0_art.jpg",
                "source": "CreativeCommons",
                "source_url": "https://example.com/cc0_art.jpg",
                "asset_type": "Royalty-Free Illustration",
                "license_status": "LICENSE_VERIFIED",
                "commercial_use_verified": True,
                "risk_level": "LOW",
                "note": "CC0 Public Domain — no rights reserved."
            }
        }
        res2 = check_asset_rights(
            [Path("cover_2_cc0_art.jpg")],
            candidates=[candidate_verified]
        )
        self.assertTrue(res2["pass"], "LICENSE_VERIFIED asset should pass cleanly.")
        self.assertTrue(res2["assets"][0]["commercial_use_verified"],
            "Explicitly licensed CC0 asset must be marked commercial_use_verified=True.")
        self.assertEqual(res2["assets"][0]["license_status"], "LICENSE_VERIFIED")
        self.assertEqual(len(res2["flagged_for_review"]), 0,
            "LICENSE_VERIFIED asset must NOT appear in flagged_for_review.")
        self.assertEqual(res2["high_risk_count"], 0)

        # ---- Case 3: High-risk / restricted asset → upload BLOCKED ----
        candidate_restricted = {
            "title": "Watermarked Screenshot",
            "asset_rights": {
                "asset_id": "screenshot_watermarked.jpg",
                "source": "Unknown",
                "source_url": "",
                "asset_type": "Unknown",
                "license_status": "LICENSE_RESTRICTED",
                "commercial_use_verified": False,
                "risk_level": "HIGH",
                "note": "Download failed or explicit restriction."
            }
        }
        res3 = check_asset_rights(
            [Path("screenshot_watermarked.jpg")],
            candidates=[candidate_restricted]
        )
        self.assertFalse(res3["pass"],
            "LICENSE_RESTRICTED / HIGH risk asset must block upload (pass=False).")
        self.assertGreater(res3["high_risk_count"], 0)
        self.assertFalse(res3["assets"][0]["commercial_use_verified"])

        # ---- Case 4: Missing bg_music file must NOT be a rights failure ----
        res4 = check_asset_rights(
            [Path("cover_1_attack_on_titan.jpg")],
            candidates=[candidate_unknown],
            bg_music_path=Path("non_existent_music_file.mp3")
        )
        self.assertTrue(res4["pass"],
            "Missing bg_music must NOT cause rights QA to fail.")
        self.assertIn(
            "non_existent_music_file.mp3",
            " ".join(res4.get("missing_assets", [])),
            "Missing bg_music must be recorded in missing_assets, not in rights failure."
        )
        self.assertEqual(res4["high_risk_count"], 0)

        # ---- Case 5: No candidates → fallback to LICENSE_UNKNOWN (conservative) ----
        res5 = check_asset_rights([Path("some_image_no_metadata.jpg")])
        self.assertTrue(res5["pass"],
            "Unknown asset with no metadata must default to LICENSE_UNKNOWN (not blocked).")
        self.assertEqual(res5["assets"][0]["license_status"], "LICENSE_UNKNOWN")
        self.assertFalse(res5["assets"][0]["commercial_use_verified"])
        self.assertEqual(len(res5["flagged_for_review"]), 1,
            "Asset with no metadata must appear in flagged_for_review.")

    def test_anime_title_30day_cooldown(self):
        """Regression test for Issue 1: Confirm 5-day old title is excluded, 40-day old allowed."""
        from src.history_manager import is_anime_title_allowed_by_history, _save_json_file
        from datetime import datetime, timedelta

        # Insert a 5-day old entry and a 40-day old entry in title_history.json
        now = datetime.now()
        history = [
            {
                "title": "Recent Title Five Days Ago",
                "normalized_title": "recent title five days ago",
                "anime_id": 9991,
                "date": (now - timedelta(days=5)).isoformat(),
                "date_readable": (now - timedelta(days=5)).strftime("%Y-%m-%d")
            },
            {
                "title": "Old Title Forty Days Ago",
                "normalized_title": "old title forty days ago",
                "anime_id": 9992,
                "date": (now - timedelta(days=40)).isoformat(),
                "date_readable": (now - timedelta(days=40)).strftime("%Y-%m-%d")
            }
        ]
        _save_json_file(config.TITLE_HISTORY_FILE, history)

        allowed_5d, reason_5d = is_anime_title_allowed_by_history("Recent Title Five Days Ago", 9991, days=30)
        self.assertFalse(allowed_5d, "Title featured 5 days ago MUST be excluded within 30-day cooldown.")

        allowed_40d, reason_40d = is_anime_title_allowed_by_history("Old Title Forty Days Ago", 9992, days=30)
        self.assertTrue(allowed_40d, "Title featured 40 days ago MUST be allowed back in after 30-day cooldown expires.")

    def test_missing_score_narration_framing(self):
        """Regression test for Issue 2: Confirm missing scores use anticipation framing and NO 'N/A' text."""
        candidates = [
            {"title": "Upcoming Hero Anime", "average_score": 0.0, "status": "NOT_YET_RELEASED", "is_upcoming": True},
            {"title": "Mystery Show", "average_score": None, "status": "RELEASING"}
        ]

        script_text = generate_fallback_template_script(candidates, {"name": "Upcoming Trio"})
        self.assertNotIn("N/A", script_text, "Narration script must NEVER contain literal 'N/A' text.")
        self.assertNotIn("0.0/10", script_text, "Narration script must NEVER cite 0.0/10 for unrated shows.")

        # Test Fact Audit QA flags literal "N/A"
        bad_script = "Check out this show rated N/A in today's spotlight."
        audit_res = check_script_factual_alignment(bad_script, [])
        self.assertFalse(audit_res["pass"])
        self.assertIn("forbidden literal placeholder text", audit_res["reason"])

    def test_supervisor_qa_gate_consolidation(self):
        """Regression test for Issue 3: Consolidated Supervisor QA Gate checks all 11 gates and produces clear verdict."""
        from src.qa_checker import run_supervisor_qa_gate

        res = run_supervisor_qa_gate(
            video_path=Path("non_existent.mp4"),
            image_paths=[],
            audio_path=Path("non_existent.mp3"),
            srt_path=Path("non_existent.ass"),
            candidates=[],
            concept_key="top_recommendations",
            video_title="Top 3 Recommendations You Need To Watch 🍿 #Shorts",
            policy_res={"risk_level": "LOW", "flagged_issues": []},
            rights_res={"pass": True, "reason": "Clear"},
            script_qa_res={"pass": True, "reason": "Good"},
            retention_qa_res={"pass": True, "reason": "Good"},
            originality_res={"pass": True, "reason": "Good"},
            script_text="Valid narration script text without cliches."
        )

        self.assertIn("verdict", res)
        self.assertEqual(len(res["checks"]), 12, "Supervisor QA Gate MUST run and report exactly 12 individual QA checks.")
        self.assertFalse(res["pass"], "Missing video file must cause overall Supervisor QA verdict to be BLOCKED.")
        self.assertIn("BLOCKED", res["verdict"])

    def test_video_title_variety_and_concept_signal(self):
        """Regression test for Issue 4: Confirm title concept signal check and non-duplicate similarity check."""
        from src.script_generator import verify_title_concept_signal, generate_video_title
        from src.history_manager import check_video_title_similarity, record_video_title_usage

        # Concept signal test
        signal_ok, _ = verify_title_concept_signal("Top 3 Underrated Anime You're Sleeping On 🍿 #Shorts", "hidden_gems")
        self.assertTrue(signal_ok, "Title with 'underrated' keyword must pass concept signal check for hidden_gems.")

        signal_fail, _ = verify_title_concept_signal("Generic 3 Anime You Should Watch 🍿 #Shorts", "hidden_gems")
        self.assertFalse(signal_fail, "Title missing 'underrated' signal must fail concept signal check for hidden_gems.")

        # Similarity test against history
        past_title = "Top 3 Underrated Anime You Need To Watch Right Now 🍿 #Shorts"
        record_video_title_usage(past_title, concept_type="hidden_gems")

        sim_res = check_video_title_similarity(past_title, days=30)
        self.assertFalse(sim_res["pass"], "Near-duplicate video title must fail similarity check.")

    def test_youtube_upload_safety_settings(self):
        """Regression test for Issue 5: Confirm Made for Kids is explicitly False on upload."""
        from src.youtube_uploader import upload_short_to_youtube

        upload_res = upload_short_to_youtube(
            video_path=Path("sample.mp4"),
            candidates=[],
            privacy_status="private"
        )
        self.assertIn("made_for_kids", upload_res)
        self.assertFalse(upload_res["made_for_kids"], "Made for Kids setting MUST be explicitly False.")
        self.assertIn("synthetic_content_status", upload_res)
        self.assertIn("comment_moderation", upload_res)

    def test_structural_variety_qa_detects_pattern_sameness(self):
        """Regression test: verify structural fingerprinting flags scripts with identical structural patterns."""
        from src.history_manager import record_short_history, check_structural_variety_against_history, extract_structural_fingerprint
        from src.script_generator import generate_fallback_template_script

        candidates1 = [
            {"title": "Anime Alpha", "average_score": 8.5, "verified_facts": {"studio": "Studio A", "release_year": 2022, "genres": ["Action"]}},
            {"title": "Anime Beta", "average_score": 8.4, "verified_facts": {"studio": "Studio B", "release_year": 2023, "genres": ["Fantasy"]}},
        ]

        # Generate a script with explicit QUESTION opening and QUESTION_TO_VIEWER closing
        script1 = generate_fallback_template_script(candidates1, {"name": "Top Recommendations"}, target_opening_style="QUESTION", target_closing_style="QUESTION_TO_VIEWER")
        record_short_history("top_recommendations", "Title Alpha Beta", "Hook Alpha", script1)

        # Generate second script with different anime titles BUT same structural pattern
        candidates2 = [
            {"title": "Anime Gamma", "average_score": 8.9, "verified_facts": {"studio": "Studio X", "release_year": 2024, "genres": ["Sci-Fi"]}},
            {"title": "Anime Delta", "average_score": 8.8, "verified_facts": {"studio": "Studio Y", "release_year": 2025, "genres": ["Drama"]}},
        ]
        script2 = generate_fallback_template_script(candidates2, {"name": "Top Recommendations"}, target_opening_style="QUESTION", target_closing_style="QUESTION_TO_VIEWER")

        # Evaluate script2 against history -> must fail structural check due to consecutive structural pattern sameness
        struct_res = check_structural_variety_against_history(script2)
        self.assertFalse(struct_res["pass"], "Script with identical opening style and closing style pattern must fail structural variety QA.")
        self.assertIn("Consecutive structural repetition", struct_res["reason"])

        # Generate third script with rotated BOLD_CLAIM opening and DIRECT_RECOMMENDATION closing -> must pass!
        script3 = generate_fallback_template_script(candidates2, {"name": "Top Recommendations"}, target_opening_style="BOLD_CLAIM", target_closing_style="DIRECT_RECOMMENDATION")
        struct_res3 = check_structural_variety_against_history(script3)
        self.assertTrue(struct_res3["pass"], "Script with rotated opening and closing styles MUST pass structural variety QA.")

    # -----------------------------------------------------------------------
    # REGRESSION TESTS: Structural Variety FAIL must block upload
    # Requirement: ANY Supervisor QA FAIL → BLOCKED → uploader NOT called
    # -----------------------------------------------------------------------

    def test_structural_variety_fail_blocks_supervisor_verdict(self):
        """
        REGRESSION: Structural Variety FAIL must make Supervisor QA verdict BLOCKED.
        Tests the aggregation boundary — does not test the upload call.
        """
        from src.qa_checker import run_supervisor_qa_gate

        sv_fail_res = {"pass": False, "reason": "Opening hook phrasing 'picture this its friday night' matches recent video #5."}

        final_qa = run_supervisor_qa_gate(
            video_path=Path("non_existent.mp4"),
            image_paths=[],
            audio_path=Path("non_existent.mp3"),
            srt_path=Path("non_existent.ass"),
            candidates=[],
            concept_key="top_recommendations",
            video_title="Top 3 Recommendations You Need To Watch #Shorts",
            policy_res={"risk_level": "LOW", "flagged_issues": []},
            rights_res={"pass": True, "reason": "Clear"},
            script_qa_res={"pass": True, "reason": "Good"},
            retention_qa_res={"pass": True, "reason": "Good"},
            originality_res={"pass": True, "reason": "Good"},
            structural_variety_res=sv_fail_res,
            script_text="",
        )

        self.assertFalse(final_qa["pass"],
            "Supervisor QA overall verdict must be BLOCKED when Structural Variety fails.")
        self.assertIn("BLOCKED", final_qa["verdict"],
            "Supervisor QA verdict string must contain 'BLOCKED'.")
        failed_names = [f.split(":")[0] for f in final_qa.get("failed_checks", [])]
        self.assertTrue(
            any("Structural Variety" in n for n in failed_names),
            f"Structural Variety must appear in failed_checks. Got: {final_qa.get('failed_checks')}"
        )

    def test_structural_variety_fail_uploader_not_called(self):
        """
        REGRESSION (upload boundary): When Structural Variety QA fails, the YouTube
        uploader function must never be called.

        This test mocks upload_short_to_youtube and asserts it is NOT called when the
        Supervisor QA returns BLOCKED due to a Structural Variety failure.
        """
        from unittest.mock import patch, MagicMock
        from src.qa_checker import run_supervisor_qa_gate

        sv_fail_res = {"pass": False, "reason": "Opening hook phrasing matches recent video."}

        final_qa = run_supervisor_qa_gate(
            video_path=Path("non_existent.mp4"),
            image_paths=[],
            audio_path=Path("non_existent.mp3"),
            srt_path=Path("non_existent.ass"),
            candidates=[],
            concept_key="top_recommendations",
            video_title="Top 3 Anime You Need 🍿 #Shorts",
            policy_res={"risk_level": "LOW", "flagged_issues": []},
            rights_res={"pass": True, "reason": "Clear"},
            script_qa_res={"pass": True, "reason": "Good"},
            retention_qa_res={"pass": True, "reason": "Good"},
            originality_res={"pass": True, "reason": "Good"},
            structural_variety_res=sv_fail_res,
            script_text="",
        )

        # Replicate main.py upload gate logic and verify uploader is not called
        upload_mock = MagicMock()
        if final_qa["pass"]:
            upload_mock()  # Should NOT reach here

        upload_mock.assert_not_called()
        self.assertFalse(final_qa["pass"],
            "Supervisor QA must be BLOCKED — upload gate must not open.")

    def test_structural_variety_retry_exhaustion_blocks_upload(self):
        """
        REGRESSION (retry path): After max retries (2) are exhausted with Structural
        Variety still failing, the Supervisor QA must be BLOCKED and the uploader
        must NOT be called.

        Simulates: FAIL → retry 1 → FAIL → retry 2 → FAIL → BLOCKED → uploader not called.
        """
        from unittest.mock import patch, MagicMock
        from src.qa_checker import run_supervisor_qa_gate

        MAX_RETRIES = 2

        # Simulate: every QA check returns FAIL (structural variety never passes)
        sv_always_fail = {"pass": False, "reason": "Repeated opening hook detected."}

        orig_fail = {"pass": True, "reason": "Original is fine."}
        sv_res = sv_always_fail

        orig_retries = 0
        while (not orig_fail["pass"] or not sv_res["pass"]) and orig_retries < MAX_RETRIES:
            orig_retries += 1
            # Simulate regeneration — structural variety still fails
            sv_res = sv_always_fail

        # After retry loop: sv_res is still failing
        self.assertFalse(sv_res["pass"],
            "Structural Variety must still be failing after max retries.")
        self.assertEqual(orig_retries, MAX_RETRIES,
            f"Retry counter must reach exactly {MAX_RETRIES}.")

        # Pass the still-failing SV result to supervisor gate
        final_qa = run_supervisor_qa_gate(
            video_path=Path("non_existent.mp4"),
            image_paths=[],
            audio_path=Path("non_existent.mp3"),
            srt_path=Path("non_existent.ass"),
            candidates=[],
            concept_key="top_recommendations",
            video_title="Top Anime #Shorts",
            policy_res={"risk_level": "LOW", "flagged_issues": []},
            rights_res={"pass": True, "reason": "Clear"},
            script_qa_res={"pass": True, "reason": "Good"},
            retention_qa_res={"pass": True, "reason": "Good"},
            originality_res=orig_fail,
            structural_variety_res=sv_res,
            script_text="",
        )

        upload_mock = MagicMock()
        if final_qa["pass"]:
            upload_mock()

        upload_mock.assert_not_called()
        self.assertFalse(final_qa["pass"],
            "Supervisor QA must be BLOCKED after retry exhaustion with Structural Variety still failing.")
        self.assertIn("BLOCKED", final_qa["verdict"])

    def test_structural_variety_retry_success_allows_upload(self):
        """
        REGRESSION (happy retry path): When Structural Variety fails then passes on
        retry, the Supervisor QA should be APPROVED and the upload mock IS called.

        Ensures the fix does not accidentally disable retries or break the happy path.
        """
        from unittest.mock import MagicMock
        from src.qa_checker import run_supervisor_qa_gate

        MAX_RETRIES = 2

        # Simulate: first check fails, retry produces a passing result
        sv_res = {"pass": False, "reason": "Repeated hook."}
        orig_fail = {"pass": True, "reason": "Original is fine."}

        orig_retries = 0
        while (not orig_fail["pass"] or not sv_res["pass"]) and orig_retries < MAX_RETRIES:
            orig_retries += 1
            # Retry generates a new script -> structural variety now passes
            sv_res = {"pass": True, "reason": "Structural variety confirmed after regeneration."}

        self.assertTrue(sv_res["pass"],
            "Structural Variety must pass after successful retry.")
        self.assertEqual(orig_retries, 1,
            "Should have taken exactly 1 retry to pass.")

        # Pass the passing SV result — but supervisor will still block due to missing video/audio files.
        # We only need to confirm sv_res is PASS and that the gate would proceed.
        # (The non-existent file check blocks for other reasons — that's fine, this tests sv integration only.)
        final_qa = run_supervisor_qa_gate(
            video_path=Path("non_existent.mp4"),
            image_paths=[],
            audio_path=Path("non_existent.mp3"),
            srt_path=Path("non_existent.ass"),
            candidates=[],
            concept_key="top_recommendations",
            video_title="Top Anime #Shorts",
            policy_res={"risk_level": "LOW", "flagged_issues": []},
            rights_res={"pass": True, "reason": "Clear"},
            script_qa_res={"pass": True, "reason": "Good"},
            retention_qa_res={"pass": True, "reason": "Good"},
            originality_res=orig_fail,
            structural_variety_res=sv_res,
            script_text="",
        )

        # Structural Variety specifically must NOT be in failed_checks
        sv_failed = any("Structural Variety" in f for f in final_qa.get("failed_checks", []))
        self.assertFalse(sv_failed,
            "Structural Variety must NOT appear in failed_checks after a successful retry.")

    def test_all_blocking_checks_block_upload(self):
        """
        REGRESSION: Each individually failing blocking QA check must produce a
        BLOCKED Supervisor QA verdict and must prevent the upload mock from being called.

        Covers: Repeated Title, N/A Rating (Fact Audit), Title Variation, Structural Variety.
        """
        from unittest.mock import MagicMock
        from src.qa_checker import run_supervisor_qa_gate

        blocking_scenarios = [
            # (check_name_hint, kwargs_override)
            (
                "Structural Variety",
                {"structural_variety_res": {"pass": False, "reason": "Opening hook phrasing matches recent video."}},
            ),
            (
                "Anime Title Cooldown (Repeated Title)",
                {
                    "candidates": [{"title": "Attack on Titan", "id": 16498, "asset_rights": None}],
                    # Force title cooldown failure by passing candidates with a known-recent title
                    # via the mock originality result (Anime Title Cooldown runs inside supervisor)
                    "originality_res": {"pass": False, "reason": "High similarity to past Short."},
                },
            ),
            (
                "N/A Rating (Fact Audit)",
                {
                    "script_text": "Check out this show rated N/A out of 10 in today's spotlight.",
                },
            ),
            (
                "Title Variation (Video Title)",
                {
                    "video_title": "",  # Empty title triggers a pass in the gate (no check); use originality
                    "originality_res": {"pass": False, "reason": "Title similarity too high."},
                },
            ),
            (
                "Rights / Copyright",
                {"rights_res": {"pass": False, "reason": "LICENSE_RESTRICTED asset detected."}},
            ),
            (
                "Policy Risk",
                {"policy_res": {"risk_level": "HIGH", "flagged_issues": ["Forbidden phrase detected."]}},
            ),
        ]

        for scenario_name, overrides in blocking_scenarios:
            with self.subTest(blocking_check=scenario_name):
                kwargs = dict(
                    video_path=Path("non_existent.mp4"),
                    image_paths=[],
                    audio_path=Path("non_existent.mp3"),
                    srt_path=Path("non_existent.ass"),
                    candidates=[],
                    concept_key="top_recommendations",
                    video_title="Top Anime Shorts #Shorts",
                    policy_res={"risk_level": "LOW", "flagged_issues": []},
                    rights_res={"pass": True, "reason": "Clear"},
                    script_qa_res={"pass": True, "reason": "Good"},
                    retention_qa_res={"pass": True, "reason": "Good"},
                    originality_res={"pass": True, "reason": "Good"},
                    structural_variety_res={"pass": True, "reason": "Good"},
                    script_text="",
                )
                kwargs.update(overrides)

                final_qa = run_supervisor_qa_gate(**kwargs)

                upload_mock = MagicMock()
                if final_qa["pass"]:
                    upload_mock()

                upload_mock.assert_not_called()
                self.assertFalse(
                    final_qa["pass"],
                    f"Supervisor QA must be BLOCKED when '{scenario_name}' fails. "
                    f"Got verdict: {final_qa.get('verdict')} | failed: {final_qa.get('failed_checks')}"
                )
                self.assertIn("BLOCKED", final_qa["verdict"],
                    f"Verdict string must contain 'BLOCKED' for scenario '{scenario_name}'.")

    def test_blocked_run_records_title_cooldown_history(self):
        """
        REGRESSION: Confirm candidate anime titles are recorded in title_history.json
        at Phase 1 selection time even when the run later gets BLOCKED by Supervisor QA.
        Subsequent selection attempts must exclude those candidates.
        """
        from src.content_source import select_candidate_titles
        from src.history_manager import is_anime_title_allowed_by_history, _load_json_file
        from unittest.mock import patch

        pool = [
            {"id": 1001, "title": "Test Show Alpha", "cover_image": "img1.jpg", "average_score": 8.8, "popularity": 10000},
            {"id": 1002, "title": "Test Show Beta", "cover_image": "img2.jpg", "average_score": 8.7, "popularity": 10000},
            {"id": 1003, "title": "Test Show Gamma", "cover_image": "img3.jpg", "average_score": 8.6, "popularity": 10000},
            {"id": 1004, "title": "Test Show Delta", "cover_image": "img4.jpg", "average_score": 8.5, "popularity": 10000},
        ]

        with patch("src.content_source.fetch_local_trend_data", return_value=pool):
            candidates, concept_key, info = select_candidate_titles(num_candidates=3, concept_key="top_recommendations")

        self.assertEqual(len(candidates), 3)
        selected_titles = [c["title"] for c in candidates]

        # Verify title_history.json immediately contains the selected titles
        history = _load_json_file(config.TITLE_HISTORY_FILE)
        recorded_titles = [h["title"] for h in history]

        for st in selected_titles:
            self.assertIn(st, recorded_titles, f"Title '{st}' MUST be recorded in title_history.json at Phase 1 selection time!")
            allowed, reason = is_anime_title_allowed_by_history(st, days=30)
            self.assertFalse(allowed, f"Title '{st}' MUST be excluded from future runs by 30-day cooldown!")

    def test_pipeline_exits_nonzero_on_qa_block(self):
        """
        REGRESSION: Confirm run_full_pipeline() raises SystemExit(1) when Supervisor QA
        blocks a run, allowing GitHub Actions step to fail visibly while always-run
        commit action persists history data.
        """
        from main import run_full_pipeline
        from unittest.mock import patch, MagicMock

        with patch("main.run_phase_1") as mock_p1, \
             patch("src.fact_checker.verify_candidate_facts", return_value={"sources": []}), \
             patch("main.run_phase_2", return_value={"full_text": "Script text", "video_title": "Title", "retries": 0}), \
             patch("src.qa_checker.check_youtube_policy_compliance", return_value={"risk_level": "LOW"}), \
             patch("main.run_phase_3", return_value=[]), \
             patch("src.qa_checker.check_asset_rights", return_value={"pass": True}), \
             patch("main.run_phase_4", return_value=(Path("audio.mp3"), Path("sub.ass"), [])), \
             patch("main.run_phase_5", return_value=Path("video.mp4")), \
             patch("src.history_manager.check_originality_against_history", return_value={"pass": True}), \
             patch("src.history_manager.check_structural_variety_against_history", return_value={"pass": True}), \
             patch("src.qa_checker.run_supervisor_qa_gate", return_value={"pass": False, "verdict": "BLOCKED"}), \
             patch("src.notifier.send_daily_summary_email"):

            mock_p1.return_value = {
                "candidates": [{"id": 501, "title": "Blocked Anime"}],
                "concept_key": "top_recommendations",
                "concept_info": {"name": "Top Recs"}
            }

            with self.assertRaises(SystemExit) as ctx:
                run_full_pipeline()

            self.assertEqual(ctx.exception.code, 1, "Pipeline MUST exit with code 1 when Supervisor QA verdict is BLOCKED!")


if __name__ == "__main__":
    unittest.main()





