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

class TestPipelineGuardrailsAndFeatures(unittest.TestCase):

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
        """Verify rights status tagging for downloaded assets."""
        res = check_asset_rights([Path("assets/images/cover_1_test.jpg")])
        self.assertIn("assets", res)
        self.assertTrue(res["pass"])

if __name__ == "__main__":
    unittest.main()
