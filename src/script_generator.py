import logging
import json
import random
import re
from typing import List, Dict, Any, Tuple


import config
from src.llm_tracker import increment_llm_calls
from src.qa_checker import check_natural_script_quality, check_retention_elements

from src.history_manager import check_video_title_similarity, get_recent_video_titles

logger = logging.getLogger(__name__)

NATURAL_SYSTEM_PROMPT = """You are a passionate anime storyteller creating a fast-paced, highly engaging 30-40 second YouTube Short narration.
Write like a genuine human creator sharing exciting anime recommendations with fellow fans.

CRITICAL RULES:
1. NEVER use generic AI filler phrases: "in a world where", "buckle up", "absolute masterpiece", "without further ado", "smash that like button right now".
2. Word count MUST be between 115 and 165 words.
3. USE THE SPECIFIC RETRIEVED FACTS PROVIDED: Mention exact scores, studio names, release years, or specific story premises provided in the prompt context.
4. UNRATED / UPCOMING ANIME: If score is listed as "Unrated" or missing, DO NOT cite a numerical rating or say "N/A". Use anticipation/hype framing instead (e.g., "highly anticipated", "one to watch", "eagerly awaited").
5. STRUCTURAL VARIETY MANDATE:
   - DO NOT use the same sentence formula for each title (e.g., avoid repeating "Rated X/10, this Y pick produced by Z...").
   - Vary what information comes first for each title (studio vs. rating vs. character vs. plot hook).
   - Use varied natural transition phrases between titles instead of repeating "Number two:" or "Number one on our list is".
6. Output ONLY raw spoken narration text. Do NOT include scene cues, timestamps, or stage directions.
"""

OPENING_STYLE_DIRECTIVES = {
    "QUESTION": "OPENING HOOK STYLE: Ask a compelling, curiosity-driven question directly to the viewer (e.g., 'Ever wondered which anime deserve your attention tonight?').",
    "BOLD_CLAIM": "OPENING HOOK STYLE: Make a bold, high-stakes claim that grabs attention instantly (e.g., 'These three anime will completely ruin ordinary shows for you once you start.').",
    "YOU_WONT_BELIEVE": "OPENING HOOK STYLE: Use a 'you won't believe' / hidden revelation opening style (e.g., 'You won't believe how underrated these three series actually are despite their incredible visuals.').",
    "DIRECT_STATEMENT": "OPENING HOOK STYLE: Use a direct, high-energy statement (e.g., 'Here are three peak anime recommendations you need on your watchlist right now.').",
    "SCENARIO": "OPENING HOOK STYLE: Paint a quick, relatable scenario or hypothetical situation (e.g., 'Picture this: it's Friday night and you need an anime that hooks you from the very first minute.')."
}

CLOSING_STYLE_DIRECTIVES = {
    "QUESTION_TO_VIEWER": "CLOSING OUTRO STYLE: Ask a direct question asking viewers for their opinion (e.g., 'Which of these three are you adding to your watchlist first? Let me know below and subscribe for daily recs!').",
    "DIRECT_RECOMMENDATION": "CLOSING OUTRO STYLE: Provide a direct, confident action recommendation (e.g., 'Start with number one tonight — you won't regret it! Subscribe for more daily anime hidden gems.').",
    "TEASER_TOMORROW": "CLOSING OUTRO STYLE: Use a teaser for upcoming recommendations / bookmark reminder (e.g., 'Save this Short for your next binge night and subscribe so you don't miss tomorrow's spotlight!').",
    "SIMPLE_SIGNOFF": "CLOSING OUTRO STYLE: Use a crisp, passionate creator sign-off (e.g., 'Happy watching, drop your favorite in the comments, and subscribe for daily top-tier anime picks!')."
}

TRANSITION_STYLE_DIRECTIVES = {
    "NATURAL_FLOW": "TRANSITIONS: Use smooth, natural conversational transitions (e.g., 'First off...', 'Moving to...', 'Rounding out today's picks...'). DO NOT repeat 'Number two:' or 'Number one on our list is'.",
    "CATEGORICAL_PIVOT": "TRANSITIONS: Pivot between titles based on what makes each unique (e.g., 'If you want pure action...', 'Next up for storyline lovers...', 'Finally, if you need something totally different...').",
    "VARIED_CONNECTORS": "TRANSITIONS: Use varied connective language (e.g., 'Starting with...', 'Then we have...', 'Stepping up next...')."
}

CONCEPT_SIGNALS = {
    "hidden_gems": ["underrated", "hidden gem", "sleeping on", "obscure", "under the radar"],
    "upcoming_spotlight": ["upcoming", "airing soon", "unreleased", "anticipated", "future", "2026", "coming soon"],
    "genre_spotlight": ["genre", "distinct", "variety", "diverse"],
    "top_recommendations": ["top", "recommendation", "must watch", "best", "peak", "need to watch"],
    "character_spotlight": ["character", "mc", "lead", "badass", "hero"],
    "anime_comparison": ["vs", "versus", "comparison", "battle", "head-to-head", "matchup"]
}

VARIED_TITLE_TEMPLATES = {
    "hidden_gems": [
        "Top 3 Underrated Anime You're Sleeping On 🍿 #Shorts",
        "3 Hidden Gem Anime Nobody's Talking About 💎 #Shorts",
        "You Haven't Heard Of These 3 Underrated Anime 🤫 #Shorts",
        "3 Obscure Anime That Deserve Way More Hype 🔥 #Shorts",
        "3 Underrated Masterpieces You Need To Watch 📺 #Shorts"
    ],
    "upcoming_spotlight": [
        "3 Most Anticipated Upcoming Anime Coming Soon 🚀 #Shorts",
        "3 Unreleased Anime Airing Soon You Must Watch ⏳ #Shorts",
        "Upcoming Anime Gems Airing Soon You Need To See 🔥 #Shorts",
        "3 Highly Anticipated Upcoming Anime For Your Watchlist 📅 #Shorts"
    ],
    "genre_spotlight": [
        "3 Peak Anime Across 3 Completely Distinct Genres 🎭 #Shorts",
        "Must-Watch Anime In 3 Different Genres 🎬 #Shorts",
        "3 Incredible Anime Across 3 Unique Genres 🌟 #Shorts"
    ],
    "top_recommendations": [
        "Top 3 Anime Recommendations You Need To Watch 🍿 #Shorts",
        "3 Must-Watch Anime That Will Keep You Hooked 📺 #Shorts",
        "Top Peak Anime You Should Watch Right Now 🔥 #Shorts"
    ],
    "character_spotlight": [
        "3 Anime Featuring The Most Badass MCs 💥 #Shorts",
        "Top 3 Anime With Legendary Main Characters ⚔️ #Shorts"
    ],
    "anime_comparison": [
        "3 Anime Head-to-Head Powerhouse Matchups ⚔️ #Shorts",
        "Battle Of The Masterpieces: Which Anime Should You Watch? 🏆 #Shorts"
    ]
}

def verify_title_concept_signal(title: str, concept_key: str) -> Tuple[bool, str]:
    """Verify that title wording explicitly contains a concept-type signal keyword."""
    signals = CONCEPT_SIGNALS.get(concept_key, CONCEPT_SIGNALS["top_recommendations"])
    lower_title = title.lower()
    
    matched_signal = [s for s in signals if s in lower_title]
    if matched_signal:
        return True, f"Concept signal '{matched_signal[0]}' found in title."
    
    return False, f"Title lacks required concept signal keywords ({', '.join(signals)}) for concept '{concept_key}'."

def generate_script_with_gemini(
    candidates: List[Dict[str, Any]],
    concept_info: Dict[str, Any],
    feedback_notes: str = None,
    target_opening_style: str = None,
    target_closing_style: str = None,
    target_transition_style: str = None,
    avoid_phrases: List[str] = None,
    recent_hooks: List[str] = None,
    recent_outros: List[str] = None
) -> str:
    """Generate natural script using Google Gemini API with explicit structural style directives and anti-repetition constraints."""
    if not config.GEMINI_API_KEY:
        raise ValueError("GEMINI_API_KEY is not set in environment.")

    logger.info("Generating script using Google Gemini API with structural style rotation...")
    increment_llm_calls()
    try:
        from google import genai
        client = genai.Client(api_key=config.GEMINI_API_KEY)
        
        concept_name = concept_info.get('name', '')

        # Select structural style directives
        op_key = target_opening_style or random.choice(list(OPENING_STYLE_DIRECTIVES.keys()))
        cl_key = target_closing_style or random.choice(list(CLOSING_STYLE_DIRECTIVES.keys()))
        tr_key = target_transition_style or random.choice(list(TRANSITION_STYLE_DIRECTIVES.keys()))

        op_dir = OPENING_STYLE_DIRECTIVES[op_key]
        cl_dir = CLOSING_STYLE_DIRECTIVES[cl_key]
        tr_dir = TRANSITION_STYLE_DIRECTIVES[tr_key]

        anti_repetition_directives = ""
        if avoid_phrases:
            phrases_str = ", ".join([f"'{p}'" for p in avoid_phrases if p])
            if phrases_str:
                anti_repetition_directives += f"\n- CRITICAL PHRASE EXCLUSION: Do NOT use or paraphrase any of these exact conflicting phrases: {phrases_str}."
        if recent_hooks:
            hooks_str = "\n".join([f"  * \"{h}\"" for h in recent_hooks[:5] if h])
            if hooks_str:
                anti_repetition_directives += f"\n- PREVIOUS OPENING HOOKS USED RECENTLY (DO NOT REPEAT OR PARAPHRASE THESE):\n{hooks_str}\nWrite a distinctly different opening hook structure."
        if recent_outros:
            outros_str = "\n".join([f"  * \"{o}\"" for o in recent_outros[:5] if o])
            if outros_str:
                anti_repetition_directives += f"\n- PREVIOUS CLOSING OUTROS USED RECENTLY (DO NOT REPEAT OR PARAPHRASE THESE):\n{outros_str}\nWrite a distinctly different closing outro."

        structural_directives = (
            f"REQUIRED STRUCTURAL STYLE INSTRUCTIONS FOR THIS SCRIPT:\n"
            f"- {op_dir}\n"
            f"- {cl_dir}\n"
            f"- {tr_dir}\n"
            f"- PER-TITLE DESCRIPTION VARIETY: Order of details MUST differ for each title. "
            f"Title 1: start with animation studio/visuals then plot premise. "
            f"Title 2: start with lead character/story hook then score/hype. "
            f"Title 3: start with rating/hype then plot hook.{anti_repetition_directives}\n"
        )

        prompt_content = f"Concept: {concept_name} - {concept_info.get('tagline')}\n"
        prompt_content += f"{structural_directives}\n"
        prompt_content += "VERIFIED FACTUAL METADATA FOR FEATURED ANIME:\n"
        
        for idx, item in enumerate(reversed(candidates), 1):
            vf = item.get("verified_facts", {})
            title = vf.get("title") or item.get("title")
            raw_score = item.get("average_score") or vf.get("score_numeric", 0.0)
            verified_score = vf.get("verified_score", "")
            
            is_upcoming = item.get("is_upcoming", False) or item.get("status") == "NOT_YET_RELEASED" or concept_name == "Upcoming Trio"
            score_is_valid = isinstance(raw_score, (int, float)) and raw_score > 0.0 and verified_score not in ["N/A", "0.0/10", ""]

            if score_is_valid:
                score_str = f"{raw_score:.1f}/10"
            else:
                if not is_upcoming:
                    logger.warning(f"[ScriptGenerator WARNING] Non-upcoming anime title '{title}' has missing/invalid score ({raw_score})!")
                score_str = "Unrated / Upcoming (NO numerical score available yet - DO NOT cite a numerical rating or say 'N/A'. Use anticipation/hype framing instead, e.g. 'highly anticipated', 'one to watch', 'eagerly awaited')."

            studio = vf.get("studio", "N/A")
            year = vf.get("release_year", "N/A")
            genres = ", ".join(vf.get("genres", item.get("genres", [])))
            chars = ", ".join(vf.get("lead_characters", []))
            synopsis = vf.get("synopsis_snippet", (item.get("synopsis") or "")[:150])
            
            prompt_content += (
                f"#{idx}: {title}\n"
                f"  - Verified Score: {score_str}\n"
                f"  - Animation Studio: {studio}\n"
                f"  - Release Year: {year}\n"
                f"  - Genres: {genres}\n"
                f"  - Key Characters: {chars}\n"
                f"  - Story Premise: {synopsis}\n"
            )

        if feedback_notes:
            prompt_content += (
                f"\nPREVIOUS DRAFT FEEDBACK (STRUCTURAL REWRITE REQUIRED):\n{feedback_notes}\n"
                f"Please fix these structural issues and rewrite with a completely distinct sentence pattern."
            )

        response = client.models.generate_content(
            model='gemini-2.5-flash',
            contents=f"{NATURAL_SYSTEM_PROMPT}\n\n{prompt_content}"
        )
        return response.text.strip()
    except Exception as e:
        logger.warning(f"Gemini API script generation failed: {e}")
        raise e

def generate_fallback_template_script(
    candidates: List[Dict[str, Any]],
    concept_info: Dict[str, Any],
    target_opening_style: str = None,
    target_closing_style: str = None
) -> str:
    """High quality fallback script generator grounded in specific retrieved facts with dynamic structural rotation."""
    logger.info("Generating script using natural template fallback generator with structural rotation...")
    count = len(candidates)
    reversed_candidates = list(reversed(candidates))
    concept_name = concept_info.get("name", "Recommendations")
    clean_concept_name = re.sub(r"\s+spotlight$", "", concept_name, flags=re.IGNORECASE).strip()

    op_style = target_opening_style or random.choice(["QUESTION", "BOLD_CLAIM", "YOU_WONT_BELIEVE", "DIRECT_STATEMENT", "SCENARIO"])
    cl_style = target_closing_style or random.choice(["QUESTION_TO_VIEWER", "DIRECT_RECOMMENDATION", "TEASER_TOMORROW", "SIMPLE_SIGNOFF"])
    
    # Opening Hooks by Style
    if op_style == "QUESTION":
        hook = f"Looking for peak anime that will actually blow your mind? Here are {count} incredible shows in today's {clean_concept_name} spotlight."
    elif op_style == "BOLD_CLAIM":
        hook = f"These {count} anime will ruin ordinary shows for you. Here are today's {clean_concept_name} recommendations."
    elif op_style == "YOU_WONT_BELIEVE":
        hook = f"You won't believe how incredible these {count} anime actually are. Welcome to today's {clean_concept_name} lineup."
    elif op_style == "SCENARIO":
        hook = f"Picture this: it's Friday night and you need an anime that hooks you instantly. Here are {count} top picks."
    else: # DIRECT_STATEMENT
        hook = f"Here are {count} must-watch anime recommendations you need on your watchlist right now."

    lines = [hook]
    
    # Per-Title Description Phrases with Rotated Ordering
    transitions = [
        ["First off", "Next up", "Rounding out today's picks"],
        ["Starting strong with", "Moving over to", "Finally"],
        ["If you want intense storytelling", "Then we have", "To top it all off"]
    ]
    chosen_trans = random.choice(transitions)

    for idx, item in enumerate(reversed_candidates, 1):
        vf = item.get("verified_facts", {})
        title = vf.get("title") or item['title']
        raw_score = item.get("average_score") or vf.get("score_numeric", 0.0)
        verified_score = vf.get("verified_score", "")
        
        is_upcoming = item.get("is_upcoming", False) or item.get("status") == "NOT_YET_RELEASED" or concept_name == "Upcoming Trio"
        score_is_valid = isinstance(raw_score, (int, float)) and raw_score > 0.0 and verified_score not in ["N/A", "0.0/10", ""]

        studio = vf.get("studio") if vf.get("studio") != "N/A" else "top animation teams"
        year = f"in {vf.get('release_year')}" if vf.get("release_year") and vf.get("release_year") != "N/A" else ""
        genres = ", ".join(vf.get("genres", item.get("genres", ['Action']))[:2])
        category = item.get("selection_category", "Must-Watch")
        
        t_prefix = chosen_trans[idx - 1] if idx <= len(chosen_trans) else f"Number {count - idx + 1}"

        if idx == 1:
            # Structure A: Start with Studio & Category -> Title -> Score/Hype
            if score_is_valid:
                lines.append(f"{t_prefix}, animated by {studio} {year}, we have {title}. Rated {raw_score:.1f}/10, this {category} delivers unforgettable {genres} moments.")
            else:
                lines.append(f"{t_prefix}, animated by {studio} {year}, we have {title}. Standing as a highly anticipated {category}, this delivers unforgettable {genres} moments.")
        elif idx == 2:
            # Structure B: Start with Title -> Character/Story Premise -> Rating/Hype -> Studio
            if score_is_valid:
                lines.append(f"{t_prefix}, {title} follows an intense journey that hooks you instantly. Holding a {raw_score:.1f}/10 rating, {studio} crafted this into a peak {genres} series.")
            else:
                lines.append(f"{t_prefix}, {title} follows an intense journey that hooks you instantly. Recognized as one to watch, {studio} crafted this into a peak {genres} series.")
        else:
            # Structure C: Start with Score/Hype -> Title -> Plot Hook
            if score_is_valid:
                lines.append(f"{t_prefix}, holding a stellar {raw_score:.1f}/10 score, is {title}. It's a sensational {genres} pick that stays with you long after the credits roll.")
            else:
                lines.append(f"{t_prefix}, standing as an eagerly awaited upcoming pick, is {title}. It's a sensational {genres} show that stays with you long after the credits roll.")

    # Closings by Style
    if cl_style == "QUESTION_TO_VIEWER":
        outro = "Which of these three are you adding to your watchlist first? Drop your thoughts below and hit subscribe!"
    elif cl_style == "DIRECT_RECOMMENDATION":
        outro = "Start with number one tonight — you won't regret it. Subscribe for daily peak anime recommendations!"
    elif cl_style == "TEASER_TOMORROW":
        outro = "Save this Short for binge night and subscribe so you don't miss tomorrow's daily anime spotlight!"
    else: # SIMPLE_SIGNOFF
        outro = "Happy watching! Subscribe for daily top-tier anime picks and leave your favorite show in the comments."

    lines.append(outro)
    return " ".join(lines)


def generate_video_title(candidates: List[Dict[str, Any]], concept_key: str, concept_info: Dict[str, Any]) -> str:
    """
    Generate dynamic video title reflecting concept signal and avoiding near-duplicate wording across videos.
    """
    recent_titles = get_recent_video_titles(days=config.VIDEO_TITLE_COOLDOWN_DAYS)
    retries = 0
    proposed_title = ""

    while retries <= config.MAX_STAGE_RETRIES:
        if config.GEMINI_API_KEY and retries < 2:
            try:
                from google import genai
                client = genai.Client(api_key=config.GEMINI_API_KEY)
                increment_llm_calls()
                
                recent_past_str = "\n".join([f"- {t.get('title')}" for t in recent_titles[-5:]])
                candidate_titles = ", ".join([c.get("title", "") for c in candidates])
                concept_name = concept_info.get("name", "Top Recommendations")

                prompt = (
                    f"Create an engaging YouTube Short title (max 90 characters) for an anime video.\n"
                    f"Concept Type: {concept_name} (Key: {concept_key})\n"
                    f"Featured Anime Titles: {candidate_titles}\n\n"
                    "CRITICAL REQUIREMENTS:\n"
                    f"1. MUST explicitly include concept-type wording (e.g. for hidden_gems include 'underrated' or 'hidden gem'; for upcoming include 'upcoming' or 'airing soon').\n"
                    "2. MUST sound distinct and phrased differently from recent past titles.\n"
                    "3. Include 1 emoji and '#Shorts' at the end.\n\n"
                    f"RECENT PAST TITLES TO AVOID SIMILARITY WITH:\n{recent_past_str}\n\n"
                    "Output ONLY the plain title text, nothing else."
                )

                response = client.models.generate_content(
                    model='gemini-2.5-flash',
                    contents=prompt
                )
                proposed_title = response.text.strip().replace('"', '')
            except Exception as e:
                logger.warning(f"LLM video title generation failed: {e}. Using template fallback.")
                proposed_title = ""

        if not proposed_title:
            templates = VARIED_TITLE_TEMPLATES.get(concept_key, VARIED_TITLE_TEMPLATES["top_recommendations"])
            proposed_title = random.choice(templates)

        # Validate concept signal presence
        signal_ok, signal_reason = verify_title_concept_signal(proposed_title, concept_key)
        # Validate non-repetition against recent title history
        sim_res = check_video_title_similarity(proposed_title, days=config.VIDEO_TITLE_COOLDOWN_DAYS)

        if signal_ok and sim_res["pass"]:
            logger.info(f"[Video Title SUCCESS] Generated valid title: '{proposed_title}' (Concept signal verified, non-duplicate)")
            return proposed_title

        logger.warning(f"[Video Title Retry #{retries + 1}] Title '{proposed_title}' failed check: Signal OK={signal_ok}, Similarity Pass={sim_res['pass']}. Retrying...")
        retries += 1

    # Fallback to guaranteed template matching concept signal
    fallback_templates = VARIED_TITLE_TEMPLATES.get(concept_key, VARIED_TITLE_TEMPLATES["top_recommendations"])
    chosen_fallback = fallback_templates[0]
    logger.info(f"[Video Title Fallback] Using standard concept-aligned title: '{chosen_fallback}'")
    return chosen_fallback

def generate_recommendation_script(
    candidates: List[Dict[str, Any]],
    concept_key: str = "top_recommendations",
    concept_info: Dict[str, Any] = None,
    feedback_notes: str = None,
    target_opening_style: str = None,
    target_closing_style: str = None,
    target_transition_style: str = None,
    avoid_phrases: List[str] = None,
    recent_hooks: List[str] = None,
    recent_outros: List[str] = None
) -> Dict[str, Any]:
    """
    Generates script and dynamic video title with automated QA retry loop.
    """
    if not concept_info:
        concept_info = {"name": "Top Recommendations", "tagline": "Must-Watch Anime"}

    retries = 0
    script_text = ""

    script_qa_res = {"pass": False, "reason": "Not run"}
    retention_qa_res = {"pass": False, "reason": "Not run"}

    while retries <= config.MAX_STAGE_RETRIES:
        if retries > 0:
            logger.info(f"[Script Rewrite Retry {retries}/{config.MAX_STAGE_RETRIES}] Rewriting script based on QA feedback...")

        # Generate draft
        if config.GEMINI_API_KEY:
            try:
                script_text = generate_script_with_gemini(
                    candidates,
                    concept_info,
                    feedback_notes=feedback_notes,
                    target_opening_style=target_opening_style,
                    target_closing_style=target_closing_style,
                    target_transition_style=target_transition_style,
                    avoid_phrases=avoid_phrases,
                    recent_hooks=recent_hooks,
                    recent_outros=recent_outros
                )
            except Exception:
                script_text = generate_fallback_template_script(
                    candidates,
                    concept_info,
                    target_opening_style=target_opening_style,
                    target_closing_style=target_closing_style
                )
        else:
            script_text = generate_fallback_template_script(
                candidates,
                concept_info,
                target_opening_style=target_opening_style,
                target_closing_style=target_closing_style
            )

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

    # Generate concept-aligned, varied video title
    video_title = generate_video_title(candidates, concept_key, concept_info)

    word_count = len(script_text.split())
    logger.info("-" * 50)
    logger.info(f"FINAL VIDEO TITLE: {video_title}")
    logger.info(f"FINAL SCRIPT ({word_count} words | Retries: {retries}):")
    logger.info(script_text)
    logger.info("-" * 50)

    return {
        "full_text": script_text,
        "video_title": video_title,
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

