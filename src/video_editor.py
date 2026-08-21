import logging
import os
import re
import subprocess
from pathlib import Path
from typing import List, Dict, Any

import imageio_ffmpeg
import config
from scripts.generate_bg_music import ensure_bg_music_exists

logger = logging.getLogger(__name__)

def get_audio_duration(audio_path: Path) -> float:
    """Extract exact audio duration in seconds via imageio-ffmpeg."""
    ffmpeg_exe = imageio_ffmpeg.get_ffmpeg_exe()
    cmd = [ffmpeg_exe, "-i", str(audio_path)]
    try:
        res = subprocess.run(cmd, stderr=subprocess.PIPE, stdout=subprocess.PIPE, text=True, encoding="utf-8", errors="ignore")
        match = re.search(r"Duration:\s*(\d+):(\d+):(\d+\.\d+)", res.stderr)
        if match:
            h, m, s = match.groups()
            return int(h) * 3600 + int(m) * 60 + float(s)
    except Exception as e:
        logger.warning(f"Failed to extract duration via ffmpeg: {e}")
    return 30.0

def escape_ffmpeg_path(path: Path) -> str:
    """Escape Windows file paths for FFmpeg filter arguments."""
    p_str = str(path.resolve()).replace("\\", "/")
    p_str = p_str.replace(":", "\\:")
    return p_str

def assemble_short_video(
    image_paths: List[Path],
    audio_path: Path,
    subtitles_path: Path,
    output_path: Path,
    bg_music_path: Path = None,
    concept_key: str = "top_recommendations",
    candidates: List[Dict[str, Any]] = None,
    segment_timestamps: List[Dict[str, Any]] = None
) -> Path:
    """
    Assembles vertical (9:16, 1080x1920) YouTube Short with exact narration segment alignment.
    
    Features:
    - Poster images switch at the EXACT timestamp narration introduces each anime title.
    - TikTok/Shorts style ASS Karaoke subtitles burned into lower third.
    - Alternating Ken Burns pan/zoom directions.
    - Synchronized narration audio + background music loop.
    """
    ffmpeg_exe = imageio_ffmpeg.get_ffmpeg_exe()
    
    if not image_paths:
        raise ValueError("No images provided for video assembly!")
    if not audio_path.exists():
        raise FileNotFoundError(f"Narration audio file not found: {audio_path}")
        
    bg_music = bg_music_path or config.DEFAULT_BG_MUSIC
    if not bg_music.exists():
        bg_music = ensure_bg_music_exists()

    total_duration = get_audio_duration(audio_path)
    num_images = len(image_paths)
    fps = config.VIDEO_FPS

    # Determine ordered images and durations per segment
    ordered_images = []
    segment_durations = []

    if segment_timestamps and len(segment_timestamps) == num_images:
        for seg in segment_timestamps:
            cand_idx = seg.get("candidate_idx", 0)
            if 0 <= cand_idx < len(image_paths):
                ordered_images.append(image_paths[cand_idx])
            else:
                ordered_images.append(image_paths[len(ordered_images) % len(image_paths)])
            segment_durations.append(max(2.0, seg.get("duration_sec", total_duration / num_images)))
    else:
        ordered_images = list(image_paths)
        dur_per_img = max(3.0, total_duration / num_images)
        segment_durations = [dur_per_img] * num_images

    logger.info(f"Assembling Short ({num_images} images with exact spoken segment alignment): {total_duration:.2f}s total duration")
    for idx, (img_p, d) in enumerate(zip(ordered_images, segment_durations)):
        logger.info(f"  Segment #{idx+1} ({img_p.name}): {d:.2f}s duration")

    inputs = []
    filter_chains = []
    
    # Single image inputs (WITHOUT -loop 1)
    for idx, img_path in enumerate(ordered_images):
        inputs.extend(["-i", str(img_path)])

        
        dur_sec = segment_durations[idx] if idx < len(segment_durations) else (total_duration / num_images)
        frames_per_img = int(dur_sec * fps)
        
        pattern_mode = idx % 4
        if pattern_mode == 0:
            zoom_expr = "zoompan=z='min(zoom+0.0015,1.15)':x='iw/2-(iw/zoom/2)':y='ih/2-(ih/zoom/2)'"
        elif pattern_mode == 1:
            zoom_expr = "zoompan=z='max(1.15-0.0015*on,1.0)':x='iw/2-(iw/zoom/2)':y='ih/2-(ih/zoom/2)'"
        elif pattern_mode == 2:
            zoom_expr = "zoompan=z='1.12':x='max(0,iw/2-(iw/zoom/2)-(on*2))':y='ih/2-(ih/zoom/2)'"
        else:
            zoom_expr = "zoompan=z='1.12':x='min(iw-iw/zoom,iw/2-(iw/zoom/2)+(on*2))':y='ih/2-(ih/zoom/2)'"

        filter_chains.append(
            f"[{idx}:v]scale=1080:1920:force_original_aspect_ratio=increase,"
            f"crop=1080:1920,"
            f"{zoom_expr}:d={frames_per_img}:s=1080x1920:fps={fps},"
            f"setsar=1[v{idx}];"
        )

    # Concat video clips
    v_concat_inputs = "".join(f"[v{i}]" for i in range(num_images))
    filter_chains.append(f"{v_concat_inputs}concat=n={num_images}:v=1:a=0[v_concat];")

    # Audio inputs
    audio_idx = num_images
    inputs.extend(["-i", str(audio_path)])
    
    bg_music_idx = num_images + 1
    inputs.extend(["-stream_loop", "-1", "-i", str(bg_music)])

    # Mix audio: Narration volume=1.0, BG music volume=0.12 with fade-out
    fade_start = max(0.0, total_duration - 1.5)
    filter_chains.append(
        f"[{bg_music_idx}:a]volume=0.12,afade=t=out:st={fade_start:.2f}:d=1.5[bg_audio];"
        f"[{audio_idx}:a][bg_audio]amix=inputs=2:duration=first:dropout_transition=2[a_mixed];"
    )

    # Burn subtitles filter if ASS/SRT file exists
    if subtitles_path.exists() and subtitles_path.stat().st_size > 10:
        escaped_sub = escape_ffmpeg_path(subtitles_path)
        if subtitles_path.suffix.lower() == ".ass":
            filter_chains.append(f"[v_concat]subtitles='{escaped_sub}'[v_final]")
        else:
            sub_style = "Alignment=2,FontSize=20,PrimaryColour=&H00FFFFFF,OutlineColour=&H00000000,BorderStyle=1,Outline=2,Shadow=1,MarginV=220,Bold=1"
            filter_chains.append(f"[v_concat]subtitles='{escaped_sub}':force_style='{sub_style}'[v_final]")
        final_v_tag = "[v_final]"
    else:
        final_v_tag = "[v_concat]"

    complex_filter = "".join(filter_chains)

    cmd = [
        ffmpeg_exe, "-y"
    ] + inputs + [
        "-filter_complex", complex_filter,
        "-map", final_v_tag,
        "-map", "[a_mixed]",
        "-t", f"{total_duration:.3f}",
        "-c:v", "libx264",
        "-preset", "fast",
        "-crf", "22",
        "-pix_fmt", "yuv420p",
        "-c:a", "aac",
        "-b:a", "192k",
        str(output_path)
    ]

    logger.info("Executing FFmpeg rendering pipeline with exact spoken audio segment alignment & Karaoke ASS captions...")
    result = subprocess.run(cmd, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True, encoding="utf-8", errors="ignore")

    if result.returncode != 0:
        logger.error(f"FFmpeg rendering failed with exit code {result.returncode}!")
        logger.error(f"FFmpeg error log:\n{result.stderr[-2000:]}")
        raise RuntimeError(f"FFmpeg rendering error: {result.stderr[-500:]}")

    if not output_path.exists() or output_path.stat().st_size == 0:
        raise RuntimeError("FFmpeg completed but output video file is missing or 0 bytes!")

    size_mb = output_path.stat().st_size / (1024 * 1024)
    logger.info(f"Video assembly successful! Saved to {output_path.name} ({size_mb:.2f} MB)")
    return output_path

if __name__ == "__main__":
    from src.visuals import get_cached_image_paths
    imgs = get_cached_image_paths()
    aud = config.OUTPUT_DIR / "narration.mp3"
    sub = config.OUTPUT_DIR / "subtitles.ass"
    out = config.OUTPUT_DIR / "final_short.mp4"
    assemble_short_video(imgs, aud, sub, out)
