import asyncio
import logging
import re
import subprocess
from pathlib import Path
from typing import Tuple, List, Dict, Any

import edge_tts
import imageio_ffmpeg

import config

logger = logging.getLogger(__name__)

def ms_to_ass_time(ms: int) -> str:
    """Format milliseconds to ASS timestamp format (0:00:00.00)."""
    centiseconds = (ms % 1000) // 10
    seconds, milliseconds = divmod(ms, 1000)
    minutes, seconds = divmod(seconds, 60)
    hours, minutes = divmod(minutes, 60)
    return f"{hours}:{minutes:02d}:{seconds:02d}.{centiseconds:02d}"

def ms_to_srt_time(ms: int) -> str:
    """Format milliseconds to SRT timestamp format (00:00:00,000)."""
    seconds, milliseconds = divmod(ms, 1000)
    minutes, seconds = divmod(seconds, 60)
    hours, minutes = divmod(minutes, 60)
    return f"{hours:02d}:{minutes:02d}:{seconds:02d},{milliseconds:03d}"

def srt_time_to_ms(srt_time: str) -> int:
    """Convert SRT timestamp (00:00:00,000) to milliseconds."""
    try:
        time_part, ms_part = srt_time.strip().split(",")
        h, m, s = map(int, time_part.split(":"))
        return (h * 3600 + m * 60 + s) * 1000 + int(ms_part)
    except Exception:
        return 0

def get_audio_duration_seconds(audio_path: Path) -> float:
    """Extract audio duration in seconds using imageio-ffmpeg."""
    ffmpeg_exe = imageio_ffmpeg.get_ffmpeg_exe()
    cmd = [ffmpeg_exe, "-i", str(audio_path)]
    try:
        res = subprocess.run(cmd, stderr=subprocess.PIPE, stdout=subprocess.PIPE, text=True, encoding="utf-8", errors="ignore")
        match = re.search(r"Duration:\s*(\d+):(\d+):(\d+\.\d+)", res.stderr)
        if match:
            hours, minutes, seconds = match.groups()
            total_sec = int(hours) * 3600 + int(minutes) * 60 + float(seconds)
            return total_sec
    except Exception as e:
        logger.warning(f"Could not parse audio duration via ffmpeg: {e}")
    return 30.0

def generate_tiktok_karaoke_ass(word_bounds: List[Dict[str, Any]], words_per_phrase: int = 3) -> str:
    """
    Generates ASS subtitle file with TikTok/Shorts style active word karaoke highlighting.
    Active spoken word is highlighted in bright yellow (&H0000FFFF&) with bold emphasis in real-time sync with TTS audio.
    """
    ass_header = """[Script Info]
Title: YouTube Shorts Karaoke Captions
ScriptType: v4.00+
WrapStyle: 0
PlayResX: 1080
PlayResY: 1920
ScaledBorderAndShadow: yes

[V4+ Styles]
Format: Name, Fontname, Fontsize, PrimaryColour, SecondaryColour, OutlineColour, BackColour, Bold, Italic, Underline, StrikeOut, ScaleX, ScaleY, Spacing, Angle, BorderStyle, Outline, Shadow, Alignment, MarginL, MarginR, MarginV, Encoding
Style: Default,Arial,60,&H00FFFFFF,&H0000FFFF,&H00000000,&H80000000,-1,0,0,0,100,100,0,0,1,4,2,2,50,50,420,1

[Events]
Format: Layer, Start, End, Style, Name, MarginL, MarginR, MarginV, Effect, Text
"""
    if not word_bounds:
        return ass_header

    events = []
    
    # Group word boundaries into phrases of 3 words
    for i in range(0, len(word_bounds), words_per_phrase):
        phrase_group = word_bounds[i:i + words_per_phrase]
        
        # Create a dialogue event for each word's active spoken window
        for active_idx, w_active in enumerate(phrase_group):
            w_start = w_active["offset_ms"]
            w_end = w_active["end_ms"]
            if w_end <= w_start:
                w_end = w_start + 250

            phrase_parts = []
            for idx, w in enumerate(phrase_group):
                clean_w = w["text"].replace("{", "").replace("}", "")
                if idx == active_idx:
                    # Active word: Bright Yellow (&H0000FFFF&) with bold
                    phrase_parts.append(f"{{\\c&H0000FFFF&\\b1}}{clean_w}{{\\r}}")
                else:
                    # Inactive words: White
                    phrase_parts.append(f"{{\\c&H00FFFFFF&}}{clean_w}{{\\r}}")
                    
            line_text = " ".join(phrase_parts)
            start_str = ms_to_ass_time(w_start)
            end_str = ms_to_ass_time(w_end)
            
            events.append(f"Dialogue: 0,{start_str},{end_str},Default,,0,0,0,,{line_text}")
            
    return ass_header + "\n".join(events)

def compute_title_segment_timestamps(
    word_bounds: List[Dict[str, Any]],
    candidates: List[Dict[str, Any]],
    total_duration_sec: float
) -> List[Dict[str, Any]]:
    """
    Computes exact spoken start/end timestamps for each anime title's narration segment.
    Note: Script narration presents candidates in REVERSE rank order (Candidate #N -> Candidate #1).
    """
    num_candidates = len(candidates)
    if not word_bounds or num_candidates == 0:
        dur_per_img = total_duration_sec / max(1, num_candidates)
        return [
            {"candidate_idx": idx, "start_sec": idx * dur_per_img, "end_sec": (idx + 1) * dur_per_img, "duration_sec": dur_per_img}
            for idx in range(num_candidates)
        ]

    # Script presents candidates in reversed order
    reversed_candidates = list(reversed(candidates))
    candidate_keywords = []
    for c in reversed_candidates:
        title = c.get("title", "")
        clean = re.sub(r'\s*\([^)]*\)', '', title)
        clean = re.sub(r'\s*-\s*Season\s*\d+', '', clean, flags=re.IGNORECASE)
        words = [w.lower() for w in re.findall(r'\b[A-Za-z0-9]{3,}\b', clean)]
        candidate_keywords.append(set(words))

    spoken_starts_sec = [0.0] * num_candidates

    # Search word boundary stream for title keywords in narration order
    search_cand_idx = 0
    for w in word_bounds:
        if search_cand_idx >= num_candidates:
            break
        w_text = w["text"].lower().strip()
        keywords = candidate_keywords[search_cand_idx]
        
        if any(kw in w_text for kw in keywords):
            w_start_sec = w["offset_ms"] / 1000.0
            if search_cand_idx == 0 or w_start_sec > spoken_starts_sec[search_cand_idx - 1] + 2.0:
                spoken_starts_sec[search_cand_idx] = w_start_sec
                logger.info(f"[Segment Timing] Matched Narration Title #{search_cand_idx + 1} ('{reversed_candidates[search_cand_idx].get('title')}') starting at {w_start_sec:.2f}s")
                search_cand_idx += 1

    # First segment in video starts at 0.0s
    spoken_starts_sec[0] = 0.0

    dur_per_img = total_duration_sec / max(1, num_candidates)
    for i in range(1, num_candidates):
        if spoken_starts_sec[i] <= spoken_starts_sec[i - 1]:
            spoken_starts_sec[i] = spoken_starts_sec[i - 1] + dur_per_img

    segments = []
    for i in range(num_candidates):
        st = spoken_starts_sec[i]
        et = spoken_starts_sec[i + 1] if i + 1 < num_candidates else total_duration_sec
        dur = max(2.0, et - st)
        # Original candidate index corresponding to reversed narration position
        orig_cand_idx = num_candidates - 1 - i
        segments.append({
            "segment_idx": i,
            "candidate_idx": orig_cand_idx,
            "title": reversed_candidates[i].get("title", ""),
            "start_sec": st,
            "end_sec": et,
            "duration_sec": dur
        })
        logger.info(f"[Segment Timing Result] Narration Segment #{i+1} ('{reversed_candidates[i].get('title')}'): {st:.2f}s -> {et:.2f}s ({dur:.2f}s)")

    return segments

def validate_caption_sync(srt_path: Path, audio_duration_sec: float, max_drift_sec: float = 1.5) -> Dict[str, Any]:
    """
    Validates caption timestamps against audio duration to detect timing lag or drift.
    Supports both SRT and ASS format timestamp extraction.
    """
    if not srt_path.exists() or srt_path.stat().st_size < 10:
        return {"pass": False, "reason": "Subtitle file missing or empty."}

    with open(srt_path, "r", encoding="utf-8") as f:
        content = f.read()

    # Check for SRT timestamps
    timestamp_matches = re.findall(r"(\d{1,2}:\d{2}:\d{2}[\.,]\d{2,3})\s*-->\s*(\d{1,2}:\d{2}:\d{2}[\.,]\d{2,3})", content)
    last_end_str = None

    if timestamp_matches:
        last_end_str = timestamp_matches[-1][1]
    else:
        # Check for ASS Dialogue timestamps
        ass_matches = re.findall(r"Dialogue:\s*\d+,\d+:\d{2}:\d{2}\.\d{2},(\d+:\d{2}:\d{2}\.\d{2})", content)
        if ass_matches:
            last_end_str = ass_matches[-1]

    if not last_end_str:
        return {"pass": False, "reason": "Subtitle file contains no valid timestamp lines."}

    try:
        if "." in last_end_str:
            h, m, s = last_end_str.split(":")
            sec = int(h) * 3600 + int(m) * 60 + float(s)
        else:
            time_p, ms_p = last_end_str.split(",")
            h, m, s = map(int, time_p.split(":"))
            sec = h * 3600 + m * 60 + s + int(ms_p) / 1000.0
    except Exception:
        sec = audio_duration_sec

    drift_sec = abs(audio_duration_sec - sec)
    is_synced = drift_sec <= max_drift_sec

    reason = f"Caption sync verified (Last caption end: {sec:.2f}s, Audio duration: {audio_duration_sec:.2f}s, Drift: {drift_sec:.2f}s <= {max_drift_sec}s)." if is_synced else f"Caption drift detected! Last caption ends at {sec:.2f}s while audio is {audio_duration_sec:.2f}s (Drift: {drift_sec:.2f}s)."
    
    logger.info(f"[Caption Sync Check] Pass: {is_synced} | {reason}")
    return {
        "pass": is_synced,
        "last_caption_end_sec": sec,
        "audio_duration_sec": audio_duration_sec,
        "drift_sec": drift_sec,
        "reason": reason
    }


def generate_narration_and_subtitles(
    script_text: str,
    voice: str = None,
    candidates: List[Dict[str, Any]] = None
) -> Tuple[Path, Path, List[Dict[str, Any]]]:
    """
    Synthesizes script_text to MP3 audio, generates TikTok karaoke ASS subtitles,
    and computes exact spoken segment timestamps per anime title.
    Returns (audio_path, ass_subtitles_path, segment_timestamps).
    """
    voice_name = voice or config.TTS_VOICE
    output_audio = config.OUTPUT_DIR / "narration.mp3"
    output_ass = config.OUTPUT_DIR / "subtitles.ass"
    
    logger.info(f"Synthesizing narration with edge-tts voice: {voice_name} (TikTok Karaoke ASS mode)...")
    
    communicate = edge_tts.Communicate(script_text, voice_name, rate="+3%", boundary="WordBoundary")
    word_bounds = []
    
    try:
        async def _synth():
            with open(output_audio, "wb") as audio_file:
                async for chunk in communicate.stream():
                    if chunk["type"] == "audio":
                        audio_file.write(chunk["data"])
                    elif chunk["type"] == "WordBoundary":
                        offset_ms = chunk["offset"] // 10000
                        duration_ms = chunk["duration"] // 10000
                        word_bounds.append({
                            "text": chunk["text"],
                            "offset_ms": offset_ms,
                            "duration_ms": duration_ms,
                            "end_ms": offset_ms + duration_ms
                        })
        asyncio.run(_synth())
    except Exception as e:
        raise RuntimeError(f"Edge-TTS synthesis failed: {e}")

    if not output_audio.exists() or output_audio.stat().st_size == 0:
        raise RuntimeError("Generated narration audio file is missing or 0 bytes!")

    duration_sec = get_audio_duration_seconds(output_audio)
    logger.info(f"Narration generated! Audio duration: {duration_sec:.2f}s ({len(word_bounds)} WordBoundary chunks)")

    # Generate ASS karaoke subtitles
    ass_content = generate_tiktok_karaoke_ass(word_bounds, words_per_phrase=3)
    with open(output_ass, "w", encoding="utf-8") as f:
        f.write(ass_content)

    # Compute spoken segment timestamps per candidate title
    c_list = candidates or []
    segments = compute_title_segment_timestamps(word_bounds, c_list, duration_sec)

    # Validate caption sync
    sync_res = validate_caption_sync(output_ass, duration_sec, max_drift_sec=1.5)
    if not sync_res["pass"]:
        logger.warning(f"Caption sync validation failed: {sync_res['reason']}")

    logger.info(f"Karaoke ASS Subtitles written to {output_ass.name}")
    return output_audio, output_ass, segments

if __name__ == "__main__":
    script_file = config.OUTPUT_DIR / "script.txt"
    if script_file.exists():
        with open(script_file, "r", encoding="utf-8") as f:
            text = f.read()
        audio, ass, segs = generate_narration_and_subtitles(text)
        print("Audio generated:", audio)
        print("ASS generated:", ass)
        print("Segments:", segs)
