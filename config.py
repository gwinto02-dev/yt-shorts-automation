import os
from pathlib import Path
from dotenv import load_dotenv

# Load environment variables from .env if present
load_dotenv()

# Base Paths
BASE_DIR = Path(__file__).resolve().parent
ASSETS_DIR = BASE_DIR / "assets"
IMAGES_DIR = ASSETS_DIR / "images"
MUSIC_DIR = ASSETS_DIR / "music"
FONTS_DIR = ASSETS_DIR / "fonts"
OUTPUT_DIR = BASE_DIR / "output"
DATA_DIR = BASE_DIR / "data"

# File & Data Storage Paths
POLICY_RULES_FILE = BASE_DIR / "policy_rules.json"
SHORTS_HISTORY_FILE = DATA_DIR / "shorts_history.json"
CONCEPT_HISTORY_FILE = DATA_DIR / "concept_history.json"
TITLE_HISTORY_FILE = DATA_DIR / "title_history.json"
VIDEO_TITLE_HISTORY_FILE = DATA_DIR / "video_title_history.json"
IMAGE_HISTORY_FILE = DATA_DIR / "image_history.json"

# Create directories if they do not exist
for path in [ASSETS_DIR, IMAGES_DIR, MUSIC_DIR, FONTS_DIR, OUTPUT_DIR, DATA_DIR]:
    path.mkdir(parents=True, exist_ok=True)

# Guardrails & Free-Tier Limits
MAX_STAGE_RETRIES = 2
# Script generation gets its own (higher) retry budget: Groq calls are cheap/fast
# compared to a full video render, so it's worth trying harder here before we
# either accept a soft-fail script or fall back to the template generator.
SCRIPT_QA_MAX_RETRIES = 4
CONCEPT_COOLDOWN_DAYS = 5
# Lowered from 30: with a pool of only ~50 fresh titles per source and 6
# rotating concepts, a 30-day memory was exhausting the usable pool almost
# entirely (seen in production as 41/42 known titles cooldown-excluded in a
# single run). 15 days still meaningfully avoids near-term repeats while
# roughly doubling how many titles are usable at any given time. Paired with
# the pool-cache merge fix in content_source.py, which grows the underlying
# pool over time instead of resetting it on every fetch.
ANIME_TITLE_COOLDOWN_DAYS = 15
VIDEO_TITLE_COOLDOWN_DAYS = 30
LLM_CALL_WARNING_THRESHOLD = 15  # Warning limit if run exceeds ~80% of daily free allowance
YT_DAILY_QUOTA_ESTIMATE = 1600   # Estimated quota units per video upload (out of 10,000 daily free limit)


# API Endpoints
ANILIST_GRAPHQL_URL = "https://graphql.anilist.co"
JIKAN_API_BASE_URL = "https://api.jikan.moe/v4"
KITSU_API_BASE_URL = "https://kitsu.io/api/edge"

# LLM & API Keys
GROQ_API_KEY = os.getenv("GROQ_API_KEY", "")
GROQ_MODEL = os.getenv("GROQ_MODEL", "openai/gpt-oss-120b")
GROQ_MIN_CALL_INTERVAL = float(os.getenv("GROQ_MIN_CALL_INTERVAL", "8.0"))  # Seconds delay between sequential Groq calls
GROQ_MAX_RETRIES = int(os.getenv("GROQ_MAX_RETRIES", "2"))

GEMINI_API_KEY = os.getenv("GEMINI_API_KEY", "")
OPENAI_API_KEY = os.getenv("OPENAI_API_KEY", "")
GEMINI_MODEL = os.getenv("GEMINI_MODEL", "gemini-3.6-flash")
GEMINI_MIN_CALL_INTERVAL = float(os.getenv("GEMINI_MIN_CALL_INTERVAL", "14.0"))
GEMINI_MAX_RETRIES = int(os.getenv("GEMINI_MAX_RETRIES", "2"))

# YouTube Upload Settings & Guardrails
# HARD CONSTRAINT: Must NEVER default to "public". Allowed values: "private", "scheduled"
YOUTUBE_PRIVACY_STATUS = os.getenv("YOUTUBE_PRIVACY_STATUS", "private").lower()
if YOUTUBE_PRIVACY_STATUS not in ["private", "scheduled"]:
    raise ValueError(
        f"SAFETY GUARDRAIL VIOLATION: Invalid YOUTUBE_PRIVACY_STATUS='{YOUTUBE_PRIVACY_STATUS}'. "
        "Videos must default to 'private' or 'scheduled' only!"
    )

YOUTUBE_CLIENT_ID = os.getenv("YOUTUBE_CLIENT_ID", "")
YOUTUBE_CLIENT_SECRET = os.getenv("YOUTUBE_CLIENT_SECRET", "")
YOUTUBE_REFRESH_TOKEN = os.getenv("YOUTUBE_REFRESH_TOKEN", "")

# Email Settings (Gmail SMTP)
GMAIL_USER = os.getenv("GMAIL_USER", "")
GMAIL_APP_PASSWORD = os.getenv("GMAIL_APP_PASSWORD", "")
NOTIFY_EMAIL = os.getenv("NOTIFY_EMAIL", GMAIL_USER)

# TTS & Video Settings
TTS_VOICE = os.getenv("TTS_VOICE", "en-US-SteffanNeural")
VIDEO_WIDTH = 1080

VIDEO_HEIGHT = 1920
VIDEO_FPS = 30
DEFAULT_BG_MUSIC = MUSIC_DIR / "bg_music.mp3"
