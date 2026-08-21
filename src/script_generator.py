import logging
import json
import random
import re
from typing import List, Dict, Any, Tuple


import config
from src.llm_tracker import increment_llm_calls
from src.qa_checker import check_natural_script_quality, check_retention_elements

logger = logging.getLogger(__name__)

NATURAL_SYSTEM_PROMPT = """You are a passionate anime storyteller creating a fast-paced, highly engaging 30-40 second YouTube Short narration.
Write like a genuine human creator sharing exciting anime recommendations with fellow fans.

CRITICAL RULES:
1. NEVER use generic AI filler phrases: "in a world where", "buckle up", "absolute masterpiece", "without further ado", "smash that like button right now".
2. Word count MUST be between 115 and 165 words.
3. USE THE SPECIFIC RETRIEVED FACTS PROVIDED: Mention exact scores, studio names, release years, or specific story premises provided in the prompt context.
4. Structure:
   - Hook: Instant curiosity trigger (question or bold claim)
   - Body: Specific, factual reasons to watch each anime (mention exact plot hooks, lead characters, studio visuals, or scores)
   - Outro: Natural question to viewers asking for their opinion + smooth sub reminder
5. Output ONLY raw spoken narration text. Do NOT include scene cues, timestamps, or stage directions.
"""

def generate_script_with_gemini(candidates: List[Dict[str, Any]], concept_info: Dict[str, Any], feedback_notes: str = None) -> str:
    """Generate natural script using Google Gemini API with explicit verified facts context."""
    if not config.GEMINI_API_KEY:
        raise ValueError("GEMINI_API_KEY is not set in environment.")

    logger.info("Generating script using Google Gemini API with verified facts...")
    increment_llm_calls()
    try:
        from google import genai
        client = genai.Client(api_key=config.GEMINI_API_KEY)
        
        prompt_content = f"Concept: {concept_info.get('name')} - {concept_info.get('tagline')}\n"
        prompt_content += "VERIFIED FACTUAL METADATA FOR FEATURED ANIME:\n"
        
        for idx, item in enumerate(reversed(candidates), 1):
            vf = item.get("verified_facts", {})
            title = vf.get("title") or item.get("title")
            score = vf.get("verified_score") or f"{item.get('average_score', 8.5)}/10"
            studio = vf.get("studio", "N/A")
            year = vf.get("release_year", "N/A")
            genres = ", ".join(vf.get("genres", item.get("genres", [])))
            chars = ", ".join(vf.get("lead_characters", []))
            synopsis = vf.get("synopsis_snippet", (item.get("synopsis") or "")[:150])
            
            prompt_content += (
                f"#{idx}: {title}\n"
                f"  - Verified Score: {score}\n"
                f"  - Animation Studio: {studio}\n"
                f"  - Release Year: {year}\n"
                f"  - Genres: {genres}\n"
                f"  - Key Characters: {chars}\n"
                f"  - Story Premise: {synopsis}\n"
            )

        if feedback_notes:
            prompt_content += f"\nPREVIOUS DRAFT FEEDBACK (REWRITE REQUIRED):\n{feedback_notes}\nPlease fix these issues and rewrite into a smooth, fact-based conversational script."

        response = client.models.generate_content(
            model='gemini-2.5-flash',
            contents=f"{NATURAL_SYSTEM_PROMPT}\n\n{prompt_content}"
        )
        return response.text.strip()
    except Exception as e:
        logger.warning(f"Gemini API script generation failed: {e}")
        raise e

def generate_fallback_template_script(candidates: List[Dict[str, Any]], concept_info: Dict[str, Any]) -> str:
    """High quality fallback script generator grounded in specific retrieved facts."""
    logger.info("Generating script using natural template fallback generator with retrieved facts...")
    count = len(candidates)
    reversed_candidates = list(reversed(candidates))
    concept_name = concept_info.get("name", "Recommendations")
    clean_concept_name = re.sub(r"\s+spotlight$", "", concept_name, flags=re.IGNORECASE).strip()
    
    hooks = [
        f"Looking for peak anime content? Here are {count} incredible shows featured in today's {clean_concept_name} spotlight.",
        f"If you need anime that will actually keep you hooked, here are {count} top tier shows you should watch today.",
        f"Ready for your next anime binge? Check out these {count} powerful recommendations."
    ]
    
    lines = [random.choice(hooks)]
    
    for idx, item in enumerate(reversed_candidates, 1):
        rank_num = count - idx + 1
        vf = item.get("verified_facts", {})
        title = vf.get("title") or item['title']
        score = vf.get("verified_score") or f"{item.get('average_score', 8.5):.1f}/10"
        studio = vf.get("studio") if vf.get("studio") != "N/A" else "top animation teams"
        year = f"in {vf.get('release_year')}" if vf.get("release_year") and vf.get("release_year") != "N/A" else ""
        genres = ", ".join(vf.get("genres", item.get("genres", ['Action']))[:2])
        category = item.get("selection_category", "Must-Watch")
        
        if rank_num == 1:
            lines.append(
                f"Number one on our list is {title}. Rated a staggering {score}, this acclaimed {category} produced by {studio} {year} delivers incredible {genres} storytelling that will blow you away."
            )
        elif rank_num == 2:
            lines.append(
                f"Number two: {title}. Rated {score}, this phenomenal {genres} series crafted by {studio} delivers unforgettable character moments and intense plot twists you can't skip."
            )
        else:
            lines.append(
                f"Also featured in today's spotlight is {title}, an outstanding {genres} pick rated {score} that stays with you long after the final episode."
            )
            
    lines.append("Which of these incredible anime will you start watching first? Drop your thoughts in the comments below, hit that like button, and subscribe for daily anime recommendations!")
    return " ".join(lines)

def generate_recommendation_script(
    candidates: List[Dict[str, Any]],
    concept_key: str = "top_recommendations",
    concept_info: Dict[str, Any] = None
) -> Dict[str, Any]:
    """
    Generates script with automated Natural QA & Retention QA rewrite loop (max 2 retries).
    """
    if not concept_info:
        concept_info = {"name": "Top Recommendations", "tagline": "Must-Watch Anime"}

    retries = 0
    feedback_notes = None
    script_text = ""
    script_qa_res = {"pass": False, "reason": "Not run"}
    retention_qa_res = {"pass": False, "reason": "Not run"}

    while retries <= config.MAX_STAGE_RETRIES:
        if retries > 0:
            logger.info(f"[Script Rewrite Retry {retries}/{config.MAX_STAGE_RETRIES}] Rewriting script based on QA feedback...")

        # Generate draft
        if config.GEMINI_API_KEY:
            try:
                script_text = generate_script_with_gemini(candidates, concept_info, feedback_notes)
            except Exception:
                script_text = generate_fallback_template_script(candidates, concept_info)
        else:
            script_text = generate_fallback_template_script(candidates, concept_info)

        # Run QA checks
        script_qa_res = check_natural_script_quality(script_text)
        retention_qa_res = check_retention_elements(script_text)

        if script_qa_res["pass"] and retention_qa_res["pass"]:
            logger.info(f"[Script Generation SUCCESS] Script passed all natural & retention QA checks on try #{retries + 1}!")
            break

        # Accumulate feedback for rewrite
        feedback_parts = []
        if not script_qa_res["pass"]:
            feedback_parts.append(f"Natural Script QA: {script_qa_res['reason']}")
        if not retention_qa_res["pass"]:
            feedback_parts.append(f"Retention QA: {retention_qa_res['reason']}")

        feedback_notes = "; ".join(feedback_parts)
        retries += 1

    word_count = len(script_text.split())
    logger.info("-" * 50)
    logger.info(f"FINAL SCRIPT ({word_count} words | Retries: {retries}):")
    logger.info(script_text)
    logger.info("-" * 50)

    return {
        "full_text": script_text,
        "word_count": word_count,
        "candidates": candidates,
        "concept_key": concept_key,
        "concept_info": concept_info,
        "retries": min(retries, config.MAX_STAGE_RETRIES),
        "script_qa_res": script_qa_res,
        "retention_qa_res": retention_qa_res
    }

if __name__ == "__main__":
    test_file = config.OUTPUT_DIR / "selected_titles.json"
    if test_file.exists():
        with open(test_file, "r", encoding="utf-8") as f:
            data = json.load(f)
        res = generate_recommendation_script(data["candidates"], data.get("concept_key"), data.get("concept_info"))
        print("Generated script:", res)
