import sys
import json
import logging
import tempfile
from pathlib import Path
from unittest.mock import patch, MagicMock

# Add project root to sys.path
BASE_DIR = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(BASE_DIR))

if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8")

import config
from src.script_generator import generate_recommendation_script
from src.qa_checker import (
    check_natural_script_quality,
    check_retention_elements,
    check_structural_variety_qa
)
from src.history_manager import check_originality_against_history, record_short_history

logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(message)s")
logger = logging.getLogger(__name__)

sample_candidates = [
    {
        "id": 1,
        "title": "Frieren: Beyond Journey's End",
        "average_score": 9.3,
        "selection_category": "Must-Watch",
        "genres": ["Fantasy", "Adventure"],
        "verified_facts": {
            "title": "Frieren: Beyond Journey's End",
            "score_numeric": 9.3,
            "verified_score": "9.3/10",
            "studio": "Madhouse",
            "release_year": 2023,
            "genres": ["Fantasy", "Adventure"],
            "lead_characters": ["Frieren", "Fern", "Stark"],
            "synopsis_snippet": "An immortal elf mage reflects on life and mortality long after her hero party defeated the demon king."
        }
    },
    {
        "id": 2,
        "title": "Pluto",
        "average_score": 8.7,
        "selection_category": "Hidden Gem",
        "genres": ["Sci-Fi", "Mystery"],
        "verified_facts": {
            "title": "Pluto",
            "score_numeric": 8.7,
            "verified_score": "8.7/10",
            "studio": "Studio M2",
            "release_year": 2023,
            "genres": ["Sci-Fi", "Mystery"],
            "lead_characters": ["Gesicht", "Atom"],
            "synopsis_snippet": "A detective robot investigates a string of high-profile robot and human murders across a futuristic society."
        }
    },
    {
        "id": 3,
        "title": "Frieren Season 2: El Dorado Arc",
        "average_score": 0.0,
        "selection_category": "Upcoming Spotlight",
        "is_upcoming": True,
        "status": "NOT_YET_RELEASED",
        "genres": ["Fantasy", "Adventure"],
        "verified_facts": {
            "title": "Frieren Season 2: El Dorado Arc",
            "score_numeric": 0.0,
            "verified_score": "N/A",
            "studio": "Madhouse",
            "release_year": 2026,
            "genres": ["Fantasy", "Adventure"],
            "lead_characters": ["Frieren", "Macht", "Denken"],
            "synopsis_snippet": "Confirmed upcoming second season adapting the El Dorado arc focusing on Macht of the Golden Land."
        }
    }
]

concept_info = {
    "name": "Peak Storytelling Spotlight",
    "tagline": "Must-Watch Series & Upcoming Arcs"
}
concept_key = "hidden_gems"

# Explicit fallback mock responses ONLY used when GROQ_API_KEY is not set
MOCK_GROQ_RESPONSES = [
    {
        "script": "What happens when an immortal elf mage reflects on mortality long after her legendary hero party defeated the demon king? Frieren: Beyond Journey's End, animated by Madhouse in 2023, is a stunning 9.3 out of 10 fantasy series that redefines slow-burn storytelling and emotional depth. Next up for mystery lovers, detective robot Gesicht investigates a string of high-profile murders across a futuristic society in Pluto, a dark 8.7 out of 10 sci-fi thriller crafted by Studio M2. Finally, Madhouse officially confirmed Frieren Season 2: El Dorado Arc for 2026, adapting Macht's storyline directly from the manga. If you love rich world-building, start with Frieren tonight — which of these three are you watching first? Drop your pick in the comments below!",
        "video_title": "3 Peak Fantasy Anime You Cannot Miss 📺 #Shorts"
    },
    {
        "script": "Studio M2 spent over two full years perfecting the intricate robotic world and cinematic fight animation of Pluto for a series that many casual anime fans completely missed. Holding an 8.7 out of 10 rating, this dark sci-fi murder mystery follows detective Gesicht as he uncovers a massive conspiracy threatening humanity. Moving over to upcoming announcements, Madhouse officially confirmed Frieren Season 2: El Dorado Arc adapting Macht and Denken's story. Rounding out today's picks, Madhouse delivered the legendary 9.3 out of 10 fantasy epic Frieren: Beyond Journey's End in 2023. Whether you start with Pluto or save Frieren for the weekend, save this Short for your next binge night and stay tuned!",
        "video_title": "Obscure Anime That Deserve Way More Hype 🔥 #Shorts"
    },
    {
        "script": "Right when you think detective Gesicht is solving a routine homicide in Pluto, episode two reveals a terrifying shadowy conspiracy targeting the seven most advanced robots on Earth. Studio M2 produced this compelling 8.7 out of 10 sci-fi mystery series back in 2023. Stepping up next for fantasy lovers, Madhouse brought the legendary 9.3 out of 10 epic Frieren: Beyond Journey's End to life with breathtaking spell animation and emotional storytelling. Finally, Madhouse officially confirmed Frieren Season 2: El Dorado Arc for 2026, covering Denken and Macht's manga battle. Which of these three distinct story premises caught your attention most tonight? Let me know your thoughts in the comments below and save this Short!",
        "video_title": "3 Masterpiece Anime You Haven't Heard Of 🤫 #Shorts"
    }
]

def run_3x_test():
    print("=" * 80)
    print("STARTING 3-RUN STANDALONE SCRIPT GENERATION & RETRY VERIFICATION TEST")
    print("=" * 80)
    
    # Use isolated test history file to prevent polluting main history or getting false matches
    temp_dir = tempfile.TemporaryDirectory()
    test_shorts_history = Path(temp_dir.name) / "test_shorts_history.json"
    with open(test_shorts_history, "w", encoding="utf-8") as f:
        json.dump([], f)

    results = []
    has_real_key = bool(config.GROQ_API_KEY and config.GROQ_API_KEY != "gsk_test_key")

    retry_style_tests = [
        ("SPECIFIC_QUESTION", "SPECIFIC_CALLBACK_QUESTION"),
        ("SURPRISING_FACT", "SPECIFIC_CALLBACK_BINGE"),
        ("QUESTION", "QUESTION_TO_VIEWER")  # Test legacy key normalization in retry parameter
    ]

    with patch.object(config, "SHORTS_HISTORY_FILE", test_shorts_history):
        if has_real_key:
            print("\n[REAL LIVE API MODE] Valid GROQ_API_KEY detected.")
            print(f"Targeting Groq Model: {config.GROQ_MODEL}")
            print("Making 3 genuine live API calls to generate_recommendation_script() with explicit structural retry parameters...\n")
        else:
            print("\n[WARNING] MOCK MODE — no real API key detected in environment.")
            print("Falling back to curated mock responses for offline environment testing...\n")

        for i in range(1, 4):
            target_op, target_cl = retry_style_tests[i - 1]
            print("\n" + "#" * 60)
            print(f" GENERATION RUN #{i} ({'LIVE GROQ API' if has_real_key else 'MOCK FALLBACK MODE'})")
            print(f" Targeted Retry Parameters: opening='{target_op}', closing='{target_cl}'")
            print("#" * 60)

            if has_real_key:
                # Genuine end-to-end call to the real pipeline function with explicit retry style targets
                gen_output = generate_recommendation_script(
                    candidates=sample_candidates,
                    concept_key=concept_key,
                    concept_info=concept_info,
                    target_opening_style=target_op,
                    target_closing_style=target_cl
                )
            else:
                # Mock path strictly for when no API key is present
                mock_choice = MagicMock()
                mock_choice.message.content = json.dumps(MOCK_GROQ_RESPONSES[i - 1])
                mock_response = MagicMock()
                mock_response.choices = [mock_choice]

                mock_qa_choice = MagicMock()
                mock_qa_choice.message.content = '{"pass": true, "reason": "Passed natural tone check"}'
                mock_qa_response = MagicMock()
                mock_qa_response.choices = [mock_qa_choice]

                mock_orig_choice = MagicMock()
                mock_orig_choice.message.content = '{"pass": true, "reason": "Originality confirmed", "matched_short": null}'
                mock_orig_response = MagicMock()
                mock_orig_response.choices = [mock_orig_choice]
                
                with patch("config.GROQ_API_KEY", "gsk_test_key"), \
                     patch("src.script_generator.rate_limited_groq_call", return_value=mock_response), \
                     patch("src.qa_checker.rate_limited_groq_call", return_value=mock_qa_response), \
                     patch("src.history_manager.rate_limited_groq_call", return_value=mock_orig_response):
                    
                    gen_output = generate_recommendation_script(
                        candidates=sample_candidates,
                        concept_key=concept_key,
                        concept_info=concept_info,
                        target_opening_style=target_op,
                        target_closing_style=target_cl
                    )
            
            script_text = gen_output["full_text"]
            video_title = gen_output["video_title"]
            word_count = gen_output["word_count"]
            
            # Execute actual QA checker functions against the generated text
            if has_real_key:
                natural_qa = check_natural_script_quality(script_text)
                retention_qa = check_retention_elements(script_text)
                orig_qa = check_originality_against_history(script_text, script_text.split('.')[0], video_title)
                struct_qa = check_structural_variety_qa(script_text)
            else:
                mock_qa_choice = MagicMock()
                mock_qa_choice.message.content = '{"pass": true, "reason": "Passed natural tone check"}'
                mock_qa_response = MagicMock()
                mock_qa_response.choices = [mock_qa_choice]

                mock_orig_choice = MagicMock()
                mock_orig_choice.message.content = '{"pass": true, "reason": "Originality confirmed", "matched_short": null}'
                mock_orig_response = MagicMock()
                mock_orig_response.choices = [mock_orig_choice]

                with patch("src.qa_checker.rate_limited_groq_call", return_value=mock_qa_response), \
                     patch("src.history_manager.rate_limited_groq_call", return_value=mock_orig_response):
                    natural_qa = check_natural_script_quality(script_text)
                    retention_qa = check_retention_elements(script_text)
                    orig_qa = check_originality_against_history(script_text, script_text.split('.')[0], video_title)
                    struct_qa = check_structural_variety_qa(script_text)

            print(f"\n--- [RUN #{i} REAL VIDEO TITLE] ---")
            print(video_title)
            print(f"\n--- [RUN #{i} REAL SCRIPT TEXT] ({word_count} words) ---")
            print(script_text)
            print(f"\n--- [RUN #{i} REAL QA RESULTS] ---")
            print(f"1. Natural Script Quality QA : {'PASS ✅' if natural_qa['pass'] else 'FAIL ❌'} | Reason: {natural_qa['reason']}")
            print(f"2. Retention QA             : {'PASS ✅' if retention_qa['pass'] else 'FAIL ❌'} | Reason: {retention_qa['reason']}")
            print(f"3. Originality QA           : {'PASS ✅' if orig_qa['pass'] else 'FAIL ❌'} | Reason: {orig_qa['reason']}")
            print(f"4. Structural Variety QA    : {'PASS ✅' if struct_qa['pass'] else 'FAIL ❌'} | Reason: {struct_qa['reason']}")
            
            # Record generated Short in history log so subsequent runs test anti-repetition against prior runs
            record_short_history("hidden_gems", video_title, script_text.split('.')[0], script_text)

            run_record = {
                "run": i,
                "title": video_title,
                "script": script_text,
                "word_count": word_count,
                "target_op": target_op,
                "target_cl": target_cl,
                "natural_qa": natural_qa,
                "retention_qa": retention_qa,
                "orig_qa": orig_qa,
                "struct_qa": struct_qa,
                "all_pass": natural_qa["pass"] and retention_qa["pass"] and orig_qa["pass"] and struct_qa["pass"]
            }
            results.append(run_record)

    temp_dir.cleanup()

    print("\n" + "=" * 80)
    print("3-RUN SCRIPT GENERATION & RETRY SUMMARY")
    print("=" * 80)
    passed_count = sum(1 for r in results if r["all_pass"])
    print(f"Mode: {'REAL LIVE GROQ API MODE' if has_real_key else 'MOCK FALLBACK MODE'}")
    print(f"Overall Result: {passed_count}/3 Runs Passed All QA Checks\n")

    for r in results:
        status = "ALL QA PASSED 🟢" if r["all_pass"] else "QA FAILED 🔴"
        print(f"Run #{r['run']} (Target Style: {r['target_op']}/{r['target_cl']}): {status}")
        print(f"  Title      : '{r['title']}'")
        print(f"  Word Count : {r['word_count']} words")
        print(f"  Natural QA : {'PASS' if r['natural_qa']['pass'] else 'FAIL'} -> {r['natural_qa']['reason']}")
        print(f"  Retention  : {'PASS' if r['retention_qa']['pass'] else 'FAIL'} -> {r['retention_qa']['reason']}")
        print(f"  Originality: {'PASS' if r['orig_qa']['pass'] else 'FAIL'} -> {r['orig_qa']['reason']}")
        print(f"  Structural : {'PASS' if r['struct_qa']['pass'] else 'FAIL'} -> {r['struct_qa']['reason']}")
        if not r["all_pass"]:
            print("  FAILURES DETAILED:")
            if not r["natural_qa"]["pass"]:
                print(f"    - Natural Script QA Failed: {r['natural_qa']['reason']}")
            if not r["retention_qa"]["pass"]:
                print(f"    - Retention QA Failed: {r['retention_qa']['reason']}")
            if not r["orig_qa"]["pass"]:
                print(f"    - Originality QA Failed: {r['orig_qa']['reason']}")
            if not r["struct_qa"]["pass"]:
                print(f"    - Structural Variety QA Failed: {r['struct_qa']['reason']}")
        print("-" * 60)

    print("=" * 80)
    if passed_count == 3:
        print("FINAL VERDICT: SUCCESS 🟢 3 out of 3 retry runs passed 100% of QA checks!")
    else:
        print(f"FINAL VERDICT: FAILURE 🔴 Only {passed_count}/3 runs passed all QA checks.")
        sys.exit(1)

if __name__ == "__main__":
    run_3x_test()
