# Daily Anime Recommendation Shorts Automation (Free-Tier Architecture)

An end-to-end automated YouTube Shorts pipeline that runs daily on **GitHub Actions**, selects candidate anime titles across multiple content concepts, verifies facts and sources, writes natural narration scripts, checks retention and originality against past Shorts history, evaluates YouTube policy and copyright rights compliance, synthesizes neural voiceover, compiles vertical 9:16 Shorts with visual variety, uploads to YouTube strictly as **PRIVATE**, and emails a comprehensive review report.

---

## Free-Tier Safeguards

This system is built to remain **100% free to run** without hitting platform rate limits or surprise billing:

1. **GitHub Actions Minutes (Repo Visibility)**:
   - Ensure this repository is **Public** for **unlimited free GitHub Actions minutes**.
   - Public repository secrets (`GEMINI_API_KEY`, `YOUTUBE_REFRESH_TOKEN`, etc.) remain fully encrypted and secure.
2. **LLM API Calls (Google Gemini Free Tier)**:
   - Tracks total LLM calls per run using `src/llm_tracker.py`.
   - Displays LLM API call count in the daily summary report and issues an email warning if a run exceeds the 80% daily threshold (~15 calls).
3. **Neural TTS Service (Edge-TTS Free Tier)**:
   - Uses Microsoft Edge Neural TTS (`edge-tts`), which provides free high-quality voiceover synthesis without character limits.
   - Built-in retry mechanism and fallback voice handling to prevent crashes on transient network issues.
4. **YouTube Data API v3 Quota**:
   - A single video upload consumes **~1,600 units** of the **10,000 daily free units** provided by Google Cloud.
   - Upload quota usage is tracked and reported in the daily email.

---

## Pipeline Architecture

```text
Research → Concept (5-Day Cooldown) → Fact Check → Script → Natural Script QA (Max 2 Retries) → Retention QA → Policy QA → Visuals → Rights Check → Voice → Audio QA → Editing → Captions → Originality Check (vs History, Max 2 Retries) → Final Video QA → Upload PRIVATE → Review Report Email
```

---

## Key Features & Safety Guardrails

- **Strict YouTube Private Upload Guardrail**: Uploaded videos default strictly to **"Private"**. Any attempt to set visibility to `Public` programmatically is blocked by built-in safety assertions.
- **Natural Script Generation**: Conversational tone free of generic AI tropes ("in a world where", "buckle up", "masterpiece").
- **Content Variety (5-Day Cooldown Rule)**: Supports multiple concept types (`top_recommendations`, `hidden_gems`, `character_spotlight`, `anime_comparison`, `facts_trivia`, `genre_spotlight`). Enforces a 5-day history cooldown so concept types are not repeated too frequently.
- **Originality Checker**: Stores past Shorts history (`data/shorts_history.json`) and flags near-duplicate scripts/hooks before rendering.
- **YouTube Policy Checker**: Documented in `policy_rules.json` to check for repetitive content, reused content, misleading metadata, copyright risks, and AI content disclosure.
- **Copyright & Rights Tracking**: Source and license clearance tracking for all images and audio assets.
- **Visual Variety**: Alternating Ken Burns pan/zoom movements (zoom-in, zoom-out, pan-left, pan-right) without using raw anime video clips or pirated manga scans.
- **Final Video QA**: Aggregated pre-upload check (1080x1920 vertical resolution, black/frozen frames, audio duration, subtitle sync, rights status, policy risk). Any single failure blocks YouTube upload.
- **Bounded Error Recovery**: Maximum 2 retries per QA stage. If a check still fails after retries, execution stops cleanly and sends a diagnostic failure email without uploading broken content.

---

## Project Structure

```text
yt-automation-project/
├── .github/
│   └── workflows/
│       └── daily_anime_short.yml   # Scheduled daily GitHub Actions workflow
├── assets/
│   ├── images/                     # Downloaded official cover artwork
│   └── music/                      # Royalty-free background music track
├── data/
│   ├── concept_history.json        # 5-day concept cooldown log
│   └── shorts_history.json         # History log of previous Shorts scripts & hooks
├── output/                         # Rendered artifacts (script.txt, narration.mp3, final_short.mp4, fact_check_sources.json)
├── policy_rules.json               # Configurable YouTube policy rules & guidelines
├── scripts/
│   ├── generate_bg_music.py       # Helper to generate ambient background music
│   └── setup_youtube_oauth.py     # One-time YouTube OAuth token setup tool
├── src/
│   ├── content_source.py           # Phase 1: Candidate selector & 5-day concept cooldown
│   ├── fact_checker.py             # Phase 2a: Fact verification & source citations
│   ├── script_generator.py         # Phase 2b: Natural script generator & rewrite loop
│   ├── qa_checker.py               # QA Evaluators (Natural, Retention, Policy, Rights, Originality, Final QA)
│   ├── history_manager.py          # History log manager & similarity checker
│   ├── llm_tracker.py              # LLM call tracking & rate limit monitor
│   ├── visuals.py                  # Phase 3: Cover downloader & rights tagger
│   ├── tts.py                      # Phase 4: Edge-TTS voiceover & caption generator
│   ├── video_editor.py             # Phase 5: FFmpeg vertical compiler with visual variety
│   ├── youtube_uploader.py         # Phase 6: YouTube Data API uploader (Private forced)
│   └── notifier.py                 # Phase 7: Rich HTML summary report notifier
├── tests/
│   └── test_pipeline.py            # Automated unit & guardrail tests
├── config.py                       # Configuration, constants & safety guardrails
├── main.py                         # CLI entry point
├── requirements.txt                # Python dependencies
└── README.md                       # Project documentation
```

---

## Setup & Authorization Guide

### 1. Local Environment Setup

1. Clone the repository:
   ```bash
   git clone <repo-url>
   cd "yt automation project"
   ```

2. Install dependencies:
   ```bash
   python -m pip install -r requirements.txt
   ```

3. Run automated tests:
   ```bash
   python -m unittest discover -s tests -p "test_*.py"
   ```

---

### 2. GitHub Secrets Configuration

Add the following environment secrets under **Settings > Secrets and variables > Actions**:

| Secret Name | Description | Required? |
|---|---|---|
| `GEMINI_API_KEY` | Google Gemini API Key for LLM script writing & QA checks | Optional (template used if missing) |
| `YOUTUBE_CLIENT_ID` | OAuth Client ID from Google Cloud Console | Required for Upload |
| `YOUTUBE_CLIENT_SECRET` | OAuth Client Secret from Google Cloud Console | Required for Upload |
| `YOUTUBE_REFRESH_TOKEN` | Refresh Token generated from `setup_youtube_oauth.py` | Required for Upload |
| `GMAIL_USER` | Sender Gmail address for review summary emails | Required for Email |
| `GMAIL_APP_PASSWORD` | Gmail App Password (16 characters) | Required for Email |
| `NOTIFY_EMAIL` | Recipient email address for daily reports | Optional |

---

## Testing & Manual Execution

### Test Full Pipeline Locally
```bash
python main.py --all
```

---

## Operational Workflow

1. The GitHub Actions workflow executes on schedule daily at 00:00 UTC.
2. The video is rendered and uploaded directly to YouTube with visibility forced to **Private**.
3. You receive a daily summary email containing:
   - Video concept & type
   - Script quality pass/fail status
   - Fact-check status & sources
   - Originality pass/fail vs history
   - Policy risk level (🟢/🟡/🔴)
   - Copyright / Rights clearance table
   - Stage retry counts & total LLM API calls used
   - Free-tier safeguards status dashboard
   - Direct link to review the private video in **YouTube Studio**
4. Watch the private video in YouTube Studio and manually change visibility to **Public** whenever ready.
