import math
import struct
import subprocess
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import imageio_ffmpeg
import config

def ensure_bg_music_exists() -> Path:
    target_path = config.MUSIC_DIR / "bg_music.mp3"
    if target_path.exists() and target_path.stat().st_size > 1000:
        return target_path

    sample_rate = 44100
    duration_sec = 60
    num_samples = sample_rate * duration_sec
    audio_data = bytearray()

    # Generate relaxing ambient pad loop
    for i in range(num_samples):
        t = i / sample_rate
        chord_t = int(t / 4) % 4
        if chord_t == 0:
            freqs = [220.0, 261.63, 329.63, 392.00]  # Am7
        elif chord_t == 1:
            freqs = [174.61, 220.0, 261.63, 329.63]  # Fmaj7
        elif chord_t == 2:
            freqs = [261.63, 329.63, 392.00, 493.88]  # Cmaj7
        else:
            freqs = [196.00, 246.94, 293.66, 349.23]  # G7

        val = sum(math.sin(2 * math.pi * f * t) for f in freqs) * 0.03
        lfo = 0.8 + 0.2 * math.sin(2 * math.pi * 0.25 * t)
        sample = int(val * lfo * 32767)
        audio_data.extend(struct.pack('<h', max(-32768, min(32767, sample))))

    wav_path = config.MUSIC_DIR / "bg_music.wav"
    with wave.open(str(wav_path), "wb") as wf:
        wf.setnchannels(1)
        wf.setsampwidth(2)
        wf.setframerate(sample_rate)
        wf.writeframes(audio_data)

    # Convert WAV to MP3 using imageio-ffmpeg
    ffmpeg_exe = imageio_ffmpeg.get_ffmpeg_exe()
    cmd = [ffmpeg_exe, "-y", "-i", str(wav_path), "-b:a", "192k", str(target_path)]
    subprocess.run(cmd, check=True, stdout=subprocess.PIPE, stderr=subprocess.PIPE)
    
    if wav_path.exists():
        wav_path.unlink()
        
    print(f"Generated default background music track: {target_path}")
    return target_path

if __name__ == "__main__":
    import wave
    ensure_bg_music_exists()
