import logging
import json
import random
import re
from typing import List, Dict, Any, Tuple, Optional


import config
from src.llm_tracker import increment_llm_calls
from src.qa_checker import check_natural_script_quality, check_retention_elements
from src.groq_utils import rate_limited_groq_call

from src.history_manager import check_video_title_similarity, get_recent_video_titles

logger = logging.getLogger(__name__)

NATURAL_SYSTEM_PROMPT = """You are a passionate anime storyteller creating a fast-paced, highly engaging 30-40 second YouTube Short narration.
Write like a knowledgeable friend casually recommending anime to fellow fans — focus on specific, concrete details about the actual show (plot hook, character trait, animation style, studio) rather than vague hype adjectives or promo fluff.

CRITICAL RULES:
1. ABSOLUTELY BANNED HYPE, CLICKBAIT & SOFT CLICHÉ PHRASES:
   - Hard banned clickbait: "shatter the way you judge", "leads the charge", "hidden gems", "hidden gem", "you won't believe", "mind-blowing", "game-changer", "will ruin", "next level", "in a world where", "buckle up", "absolute masterpiece", "without further ado", "smash that like button right now", "unpopular opinion but".
   - Soft banned filler clichés (NEVER use these): "stretch its fantasy chops" (or "stretch its [X] chops"), "kinetic flair", "lights up the screen", "brings [X] to life", "sparked conversation", "already generating buzz", "delivers on every front", "packs a punch".
2. WORD COUNT: Script MUST be between 115 and 165 words total.
3. USE SPECIFIC RETRIEVED FACTS: Mention exact scores, studio names, release years, or specific story premises provided in the prompt context.
4. UNRATED / UPCOMING ANIME WITH THIN DATA:
   If an anime has no score (0.0/10 or N/A) or thin data because it's unreleased:
   - Do NOT invent hype, artificial buzz, or vague claims to fill the gap (e.g. AVOID "already sparked conversation", "highly anticipated", "generating buzz", "promising").
   - Instead, anchor the sentence in ONE concrete, verifiable fact you DO have: the studio, the source material (manga/light novel arc), the original show's most memorable detail, or what's confirmed about the plot/setting.
   - It is fine to keep this sentence a little shorter and more matter-of-fact than the others if there's less to say.
5. EXAMPLES OF TARGET TONE (GOOD VS BAD):
   - Example 1 (Hype-filler vs Concrete-fact):
     BAD (vague/robotic): "This highly anticipated sequel is generating buzz and promises kinetic action."
     GOOD (concrete/conversational): "The original run adapted about half the manga, so season 2 finally gets to the arc most fans have been waiting for."
   - Example 2 (Formal-promo-voice vs Casual-friend-voice):
     BAD (formal/promo): "Studio Mappa delivers kinetic flair while bringing Macht's dark fantasy saga to life."
     GOOD (casual/friend): "Mappa animated this one, and they kept the original voice cast for Macht's backstory."
   - Example 3 (Vague-claim vs Specific-detail):
     BAD (vague/claim): "It stretches its fantasy chops and delivers on every front."
     GOOD (specific/detail): "It follows a team of four mages traveling north after defeating the demon king."
6. OPENING HOOK REQUIREMENT (CRITICAL):
   The very first sentence MUST do ONE of these three things:
   - Option A (Specific Question): Pose a specific, curiosity-driving question about the content itself (e.g., "What happens when a disgraced knight gets a second chance at life in a world of high-stakes magic?"). DO NOT use generic "did you know" or "ever wondered".
   - Option B (Surprising Fact): State a surprising or specific concrete fact about one of the three picks (e.g., "Mappa spent over two years animating a single tournament fight for a show almost nobody watched.").
   - Option C (In-Scene Mid-Thought): Open mid-thought or in-scene rather than announcing the video's premise (e.g., "Right when you think this fantasy thriller is a standard revenge story, episode four completely flips the table.").
   NEVER open by announcing the video (e.g., avoid "Here are 3 anime...", "Today we have 3 shows...").
7. CLOSING OUTRO REQUIREMENT (CRITICAL):
   The ending MUST include a concrete callback/reference to a specific detail or title mentioned earlier in the video BEFORE any call-to-action.
   Vary the call-to-action phrasing across generations (e.g., "Which of these three are you watching first?", "Drop your pick in the comments", "Save this for your next binge night"). NEVER use a generic isolated "Subscribe for more".
8. STRUCTURAL VARIETY MANDATE:
   - DO NOT use the same sentence formula for each title.
   - Vary what information comes first for each title (studio vs. rating vs. character vs. plot hook).
   - Use varied natural transition phrases between titles instead of repeating "Number two:" or "Number one on our list is".
9. NO CROSS-SEGMENT REPETITION (CRITICAL): If the opening hook mentions a specific fact, stat, score, or detail about one of the three picks, that exact fact/stat/detail must NOT be restated when that same title's own segment comes up later in the script. Cover a genuinely different detail there — the hook and the title's dedicated segment should never feel like they're saying the same thing twice.
10. Output ONLY raw spoken narration text (or JSON when requested). Do NOT include scene cues, timestamps, or stage directions.
"""

OPENING_STYLE_DIRECTIVES = {
    "SPECIFIC_QUESTION": "OPENING HOOK STYLE (Specific Question): Pose a specific, curiosity-driving question about the content itself (e.g., 'What happens when a disgraced knight gets a second chance at life in a world of high-stakes magic?'). DO NOT use generic 'did you know' or 'ever wondered'.",
    "SURPRISING_FACT": "OPENING HOOK STYLE (Surprising Fact): State a surprising or specific concrete fact about ONE of the three picks (e.g., 'Mappa spent over two years animating a single tournament fight for a show almost nobody watched.'). CRITICAL: When that same title's dedicated segment comes up later in the script, do NOT restate this same fact/stat/score again — cover a DIFFERENT detail about it there (a different plot point, character, or production detail) so the hook and the segment don't feel redundant.",
    "IN_SCENE_MID_THOUGHT": "OPENING HOOK STYLE (In-Scene / Mid-Thought): Open mid-thought or in-scene rather than announcing the video premise (e.g., 'Right when you think this fantasy thriller is a standard revenge story, episode four completely flips the table.')."
}

CLOSING_STYLE_DIRECTIVES = {
    "SPECIFIC_CALLBACK_QUESTION": "CLOSING OUTRO STYLE: Include a concrete callback to one of the 3 featured shows then ask a specific question (e.g., 'If you love dark fantasy, start with [Title #1] tonight — which of these three are you watching first? Drop your pick below!').",
    "SPECIFIC_CALLBACK_BINGE": "CLOSING OUTRO STYLE: Include a concrete callback to one of the 3 featured shows then suggest bookmarking (e.g., 'Whether you start with [Title #1] or save [Title #2] for the weekend, save this Short for your next binge night!').",
    "SPECIFIC_CALLBACK_OPINION": "CLOSING OUTRO STYLE: Include a concrete callback to one of the story premises then ask for viewer thoughts (e.g., 'Which of these three plot twists caught your attention most? Let me know in the comments below!')."
}

TRANSITION_STYLE_DIRECTIVES = {
    "NATURAL_FLOW": "TRANSITIONS: Use smooth, natural conversational transitions (e.g., 'First off...', 'Moving to...', 'Rounding out today's picks...'). DO NOT repeat 'Number two:' or 'Number one on our list is'.",
    "CATEGORICAL_PIVOT": "TRANSITIONS: Pivot between titles based on what makes each unique (e.g., 'If you want pure action...', 'Next up for storyline lovers...', 'Finally, if you need something totally different...').",
    "VARIED_CONNECTORS": "TRANSITIONS: Use varied connective language (e.g., 'Starting with...', 'Then we have...', 'Stepping up next...')."
}

# Mapping table to handle legacy style names seamlessly across historical records and retries
HOOK_STYLE_MAP = {
    "QUESTION": "SPECIFIC_QUESTION",
    "SPECIFIC_QUESTION": "SPECIFIC_QUESTION",
    "BOLD_CLAIM": "SURPRISING_FACT",
    "SURPRISING_FACT": "SURPRISING_FACT",
    "YOU_WONT_BELIEVE": "IN_SCENE_MID_THOUGHT",
    "SCENARIO": "IN_SCENE_MID_THOUGHT",
    "DIRECT_STATEMENT": "IN_SCENE_MID_THOUGHT",
    "IN_SCENE_MID_THOUGHT": "IN_SCENE_MID_THOUGHT"
}

OUTRO_STYLE_MAP = {
    "QUESTION_TO_VIEWER": "SPECIFIC_CALLBACK_QUESTION",
    "SPECIFIC_CALLBACK_QUESTION": "SPECIFIC_CALLBACK_QUESTION",
    "DIRECT_RECOMMENDATION": "SPECIFIC_CALLBACK_BINGE",
    "SPECIFIC_CALLBACK_BINGE": "SPECIFIC_CALLBACK_BINGE",
    "TEASER_TOMORROW": "SPECIFIC_CALLBACK_OPINION",
    "SIMPLE_SIGNOFF": "SPECIFIC_CALLBACK_OPINION",
    "SPECIFIC_CALLBACK_OPINION": "SPECIFIC_CALLBACK_OPINION"
}

def normalize_opening_style(style: Optional[str] = None) -> str:
    """Normalizes opening hook style names (supporting both legacy and canonical keys)."""
    if not style:
        return random.choice(list(OPENING_STYLE_DIRECTIVES.keys()))
    style_upper = str(style).strip().upper()
    if style_upper in HOOK_STYLE_MAP:
        return HOOK_STYLE_MAP[style_upper]
    logger.warning(f"Unknown opening style '{style}' — falling back to 'SPECIFIC_QUESTION' instead of crashing.")
    return "SPECIFIC_QUESTION"

def normalize_closing_style(style: Optional[str] = None) -> str:
    """Normalizes closing outro style names (supporting both legacy and canonical keys)."""
    if not style:
        return random.choice(list(CLOSING_STYLE_DIRECTIVES.keys()))
    style_upper = str(style).strip().upper()
    if style_upper in OUTRO_STYLE_MAP:
        return OUTRO_STYLE_MAP[style_upper]
    logger.warning(f"Unknown closing style '{style}' — falling back to 'SPECIFIC_CALLBACK_QUESTION' instead of crashing.")
    return "SPECIFIC_CALLBACK_QUESTION"

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

def generate_script_with_groq(
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
    """Generate natural script using Groq API with explicit structural style directives and anti-repetition constraints."""
    api_key = config.GROQ_API_KEY or config.GEMINI_API_KEY
    if not api_key:
        raise ValueError("GROQ_API_KEY is not set in environment.")

    logger.info("Generating script using Groq API with structural style rotation...")
    increment_llm_calls()
    try:
        from groq import Groq
        client = Groq(api_key=api_key)
        
        concept_name = concept_info.get('name', '')

        # Select structural style directives
        op_key = normalize_opening_style(target_opening_style)
        cl_key = normalize_closing_style(target_closing_style)
        tr_key = target_transition_style if target_transition_style in TRANSITION_STYLE_DIRECTIVES else random.choice(list(TRANSITION_STYLE_DIRECTIVES.keys()))

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

        angle_info = concept_info.get("selected_angle") or {}
        angle_label = angle_info.get("label") or concept_info.get("angle_label", "")
        angle_instruction = angle_info.get("instruction") or concept_info.get("angle_instruction", "")

        angle_directive = ""
        if angle_label and angle_instruction:
            angle_directive = (
                f"\nCRITICAL FRAMING ANGLE FOR THIS VIDEO:\n"
                f"- ANGLE: '{angle_label}'\n"
                f"- INSTRUCTION: {angle_instruction}\n"
                f"Build the hook, the reasoning for each pick, and the overall narrative around this specific angle — NOT just a generic 'here are 3 picks' framing.\n"
            )

        prompt_content = f"Concept: {concept_name} - {concept_info.get('tagline')}\n"
        if angle_directive:
            prompt_content += f"{angle_directive}\n"
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

        response = rate_limited_groq_call(
            client.chat.completions.create,
            model=config.GROQ_MODEL,
            messages=[{"role": "user", "content": f"{NATURAL_SYSTEM_PROMPT}\n\n{prompt_content}"}]
        )
        return response.choices[0].message.content.strip()
    except Exception as e:
        logger.warning(f"Groq API script generation failed: {e}")
        raise e

generate_script_with_gemini = generate_script_with_groq

def generate_script_and_title_with_groq(
    candidates: List[Dict[str, Any]],
    concept_key: str,
    concept_info: Dict[str, Any],
    feedback_notes: str = None,
    target_opening_style: str = None,
    target_closing_style: str = None,
    target_transition_style: str = None,
    avoid_phrases: List[str] = None,
    recent_hooks: List[str] = None,
    recent_outros: List[str] = None
) -> Tuple[str, str]:
    """
    Consolidated single Groq API call that generates BOTH the spoken script narration
    and concept-aligned video title in structured JSON format.
    """
    api_key = config.GROQ_API_KEY or config.GEMINI_API_KEY
    if not api_key:
        raise ValueError("GROQ_API_KEY is not set in environment.")

    logger.info("Generating script AND title in single consolidated Groq API call...")
    increment_llm_calls()
    try:
        from groq import Groq
        client = Groq(api_key=api_key)
        
        recent_titles = get_recent_video_titles(days=config.VIDEO_TITLE_COOLDOWN_DAYS)
        recent_past_str = "\n".join([f"- {t.get('title')}" for t in recent_titles[-5:]])
        candidate_titles = ", ".join([c.get("title", "") for c in candidates])
        concept_name = concept_info.get('name', '')

        # Select structural style directives
        op_key = normalize_opening_style(target_opening_style)
        cl_key = normalize_closing_style(target_closing_style)
        tr_key = target_transition_style if target_transition_style in TRANSITION_STYLE_DIRECTIVES else random.choice(list(TRANSITION_STYLE_DIRECTIVES.keys()))

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

        angle_info = concept_info.get("selected_angle") or {}
        angle_label = angle_info.get("label") or concept_info.get("angle_label", "")
        angle_instruction = angle_info.get("instruction") or concept_info.get("angle_instruction", "")

        angle_directive = ""
        if angle_label and angle_instruction:
            angle_directive = (
                f"\nCRITICAL FRAMING ANGLE FOR THIS VIDEO:\n"
                f"- ANGLE: '{angle_label}'\n"
                f"- INSTRUCTION: {angle_instruction}\n"
                f"Build the hook, the reasoning for each pick, and the overall narrative around this specific angle — NOT just a generic 'here are 3 picks' framing.\n"
            )

        prompt_content = f"Concept: {concept_name} - {concept_info.get('tagline')}\n"
        if angle_directive:
            prompt_content += f"{angle_directive}\n"
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
                f"Please fix these structural issues and rewrite with a completely distinct sentence pattern.\n"
            )

        title_directives = (
            f"\nVIDEO TITLE INSTRUCTIONS:\n"
            f"Also generate an engaging YouTube Short title (max 90 characters) for this video.\n"
            f"Concept Type: {concept_name} (Key: {concept_key})\n"
            f"Featured Titles: {candidate_titles}\n"
            f"Requirements: 1) Must include concept signal wording (e.g. 'underrated', 'hidden gem', 'upcoming'). "
            f"2) 1 emoji & '#Shorts' at end. 3) Avoid similarity with recent titles: {recent_past_str}\n"
        )

        full_prompt = (
            f"{NATURAL_SYSTEM_PROMPT}\n\n{prompt_content}\n{title_directives}\n"
            f"OUTPUT FORMAT REQUIREMENT:\n"
            f"Respond STRICTLY with a single valid JSON object containing keys 'script' and 'video_title':\n"
            f'{{\n  "script": "spoken narration text...",\n  "video_title": "Video Title 🍿 #Shorts"\n}}'
        )

        response = rate_limited_groq_call(
            client.chat.completions.create,
            model=config.GROQ_MODEL,
            messages=[{"role": "user", "content": full_prompt}]
        )

        raw = (response.choices[0].message.content or "").strip()
        if not raw:
            # Reasoning-style Groq models (e.g. openai/gpt-oss-120b) can occasionally spend their
            # whole token budget on internal reasoning and return an empty final content field,
            # which previously hit json.loads("") -> "Expecting value: line 1 column 1 (char 0)".
            # Fail fast with a clear message so the caller's fallback path triggers immediately
            # instead of burning a retry attempt on an opaque JSON error.
            raise ValueError("Groq returned empty content for the consolidated script+title JSON call (likely reasoning-token exhaustion).")
        if raw.startswith("```json"):
            raw = raw[7:]
        if raw.startswith("```"):
            raw = raw[3:]
        if raw.endswith("```"):
            raw = raw[:-3]

        parsed = json.loads(raw.strip())
        script_text = parsed.get("script", "").strip()
        video_title = parsed.get("video_title", "").strip().replace('"', '')

        if script_text and video_title:
            return script_text, video_title
        raise ValueError("Combined Groq JSON output missing 'script' or 'video_title' field.")

    except Exception as e:
        logger.warning(f"Consolidated Groq script+title call failed/unparsed: {e}")
        raise e

generate_script_and_title_with_gemini = generate_script_and_title_with_groq

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

    def _name_without_trailing(word: str) -> str:
        """Strip a specific trailing word (e.g. 'spotlight', 'recommendations',
        'lineup') from concept_name, case-insensitively, if concept_name
        already ends with it. This prevents accidental duplicate words like
        'Top Recommendations recommendations' when a hook template appends
        that same word again after the concept name — computed per-template,
        since different hook styles append different trailing words, so no
        single shared cleaned name can safely serve all of them at once."""
        return re.sub(rf"\s+{re.escape(word)}$", "", concept_name, flags=re.IGNORECASE).strip()

    op_style = normalize_opening_style(target_opening_style)
    cl_style = normalize_closing_style(target_closing_style)
    
    first_title = reversed_candidates[0].get("verified_facts", {}).get("title") or reversed_candidates[0].get("title", "our top pick")

    # Opening Hooks by Style (Supporting both legacy and new style keys)
    if op_style in ["QUESTION", "SPECIFIC_QUESTION"]:
        hook = f"What happens when three standout anime slip right past most fans' watchlists? Today we examine {count} incredible series worth your attention."
    elif op_style in ["BOLD_CLAIM", "SURPRISING_FACT"]:
        hook = f"Did you know {reversed_candidates[0].get('verified_facts', {}).get('studio', 'top animation studios')} spent years perfecting the visuals for a series many fans overlooked? These shows will completely ruin ordinary series for you."
    else: # YOU_WONT_BELIEVE, SCENARIO, IN_SCENE_MID_THOUGHT, DIRECT_STATEMENT
        hook = f"Right when you think you've seen every top-tier anime story, these {count} distinct series completely change the game."

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
                lines.append(f"{t_prefix}, {title} follows an intense journey that hooks you instantly. Holding a {raw_score:.1f}/10 rating, {studio} crafted this into a standout {genres} series.")
            else:
                lines.append(f"{t_prefix}, {title} follows an intense journey that hooks you instantly. Recognized as one to watch, {studio} crafted this into a standout {genres} series.")
        else:
            # Structure C: Start with Score/Hype -> Title -> Plot Hook
            if score_is_valid:
                lines.append(f"{t_prefix}, holding a stellar {raw_score:.1f}/10 score, is {title}. It's a sensational {genres} pick that stays with you long after the credits roll.")
            else:
                lines.append(f"{t_prefix}, standing as an eagerly awaited upcoming pick, is {title}. It's a sensational {genres} show that stays with you long after the credits roll.")

    # Closings by Style (Supporting both legacy and new style keys)
    if cl_style in ["QUESTION_TO_VIEWER", "SPECIFIC_CALLBACK_QUESTION"]:
        outro = f"If you love great storytelling, start with {first_title} tonight — which of these three are you watching first? Drop your thoughts below!"
    elif cl_style in ["DIRECT_RECOMMENDATION", "SPECIFIC_CALLBACK_BINGE"]:
        outro = f"Start with {first_title} tonight — you won't regret it! Bookmark this Short for your next binge night and subscribe!"
    else: # TEASER_TOMORROW, SIMPLE_SIGNOFF, SPECIFIC_CALLBACK_OPINION
        outro = f"Which of these three premises caught your attention most? Let me know in the comments below!"

    lines.append(outro)
    return " ".join(lines)


def generate_video_title(candidates: List[Dict[str, Any]], concept_key: str, concept_info: Dict[str, Any]) -> str:
    """
    Generate dynamic video title reflecting concept signal and avoiding near-duplicate wording across videos.
    """
    recent_titles = get_recent_video_titles(days=config.VIDEO_TITLE_COOLDOWN_DAYS)
    retries = 0
    proposed_title = ""

    api_key = config.GROQ_API_KEY or config.GEMINI_API_KEY
    while retries <= config.MAX_STAGE_RETRIES:
        if api_key and retries < 2:
            try:
                from groq import Groq
                client = Groq(api_key=api_key)
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

                response = rate_limited_groq_call(
                    client.chat.completions.create,
                    model=config.GROQ_MODEL,
                    messages=[{"role": "user", "content": prompt}]
                )
                proposed_title = response.choices[0].message.content.strip().replace('"', '')
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

    if recent_hooks is None or recent_outros is None:
        try:
            from src.history_manager import get_recent_hooks_and_outros
            hist_data = get_recent_hooks_and_outros(days=30, limit=5)
            if recent_hooks is None:
                recent_hooks = hist_data.get("hooks", [])
            if recent_outros is None:
                recent_outros = hist_data.get("outros", [])
        except Exception as e:
            logger.warning(f"Could not auto-load recent history hooks/outros: {e}")

    if target_opening_style is None or target_closing_style is None:
        try:
            from src.history_manager import extract_structural_fingerprint
            recent_op_styles = [extract_structural_fingerprint(h).get("opening_style") for h in (recent_hooks or []) if h]
            recent_cl_styles = [extract_structural_fingerprint(o).get("closing_style") for o in (recent_outros or []) if o]
            
            avail_op = [k for k in OPENING_STYLE_DIRECTIVES.keys() if k not in recent_op_styles] or list(OPENING_STYLE_DIRECTIVES.keys())
            avail_cl = [k for k in CLOSING_STYLE_DIRECTIVES.keys() if k not in recent_cl_styles] or list(CLOSING_STYLE_DIRECTIVES.keys())
            
            if target_opening_style is None:
                target_opening_style = random.choice(avail_op)
            if target_closing_style is None:
                target_closing_style = random.choice(avail_cl)
        except Exception as e:
            logger.warning(f"Could not rotate style keys from history: {e}")

    retries = 0
    script_text = ""

    script_qa_res = {"pass": False, "reason": "Not run"}
    retention_qa_res = {"pass": False, "reason": "Not run"}

    combined_video_title = None
    has_api_key = bool(config.GROQ_API_KEY or config.GEMINI_API_KEY)

    while retries <= config.MAX_STAGE_RETRIES:
        if retries > 0:
            logger.info(f"[Script Rewrite Retry {retries}/{config.MAX_STAGE_RETRIES}] Rewriting script based on QA feedback...")

        # Generate draft
        if has_api_key:
            try:
                script_text, combined_video_title = generate_script_and_title_with_groq(
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
            except Exception:
                try:
                    script_text = generate_script_with_groq(
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

        # CRITICAL FIX: previously the specific phrases flagged by Natural Script QA were only
        # ever mentioned inside the free-text feedback_notes blob, never added to avoid_phrases —
        # so the hard "CRITICAL PHRASE EXCLUSION" directive (which demonstrably works elsewhere,
        # e.g. the structural-variety retry loop in main.py) never kicked in for this loop, and
        # the model kept regenerating the same category of vague-hype phrasing across all retries.
        # Now we accumulate every flagged phrase across retries into avoid_phrases so each rewrite
        # is explicitly forbidden from repeating what already failed.
        newly_flagged = script_qa_res.get("flagged_phrases") or []
        if newly_flagged:
            avoid_phrases = list(avoid_phrases or [])
            for p in newly_flagged:
                if p and p.lower() not in [existing.lower() for existing in avoid_phrases]:
                    avoid_phrases.append(p)
            logger.info(f"[Script Rewrite] Adding flagged phrase(s) to hard exclusion list: {newly_flagged}")

        retries += 1

    if not script_text or not script_text.strip():
        logger.warning("[ScriptGenerator] Script text is empty after retries. Triggering natural template fallback generator...")
        script_text = generate_fallback_template_script(
            candidates,
            concept_info,
            target_opening_style=target_opening_style,
            target_closing_style=target_closing_style
        )

    # Use combined title if obtained and valid, else generate video title
    if combined_video_title:
        signal_ok, _ = verify_title_concept_signal(combined_video_title, concept_key)
        sim_res = check_video_title_similarity(combined_video_title, days=config.VIDEO_TITLE_COOLDOWN_DAYS)
        if signal_ok and sim_res["pass"]:
            video_title = combined_video_title
        else:
            video_title = generate_video_title(candidates, concept_key, concept_info)
    else:
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

