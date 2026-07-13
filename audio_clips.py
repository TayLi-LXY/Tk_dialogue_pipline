"""按 ASR 分句时间戳从完整 wav 切出 1.wav, 2.wav, ..."""

import os
import wave

from config import AUDIO_CLIP_PADDING_MS, FFMPEG_CHANNELS, FFMPEG_SAMPLE_RATE


def _get_wav_duration_ms(wav_path: str) -> int:
    with wave.open(wav_path, "rb") as wf:
        return int(wf.getnframes() * 1000 / wf.getframerate())


def slice_wav_by_utterances(
    wav_path: str,
    utterances: list[dict],
    output_dir: str,
    padding_ms: int = AUDIO_CLIP_PADDING_MS,
) -> list[str]:
    """按序号输出 1.wav, 2.wav, ... 返回 clip 路径列表。"""
    os.makedirs(output_dir, exist_ok=True)

    with wave.open(wav_path, "rb") as wf:
        channels = wf.getnchannels()
        sample_width = wf.getsampwidth()
        framerate = wf.getframerate()
        if channels != FFMPEG_CHANNELS:
            raise ValueError(f"期望单声道，实际 {channels} 声道")
        if sample_width != 2:
            raise ValueError(f"期望 16-bit PCM，实际 sample_width={sample_width}")
        if framerate != FFMPEG_SAMPLE_RATE:
            raise ValueError(f"期望 {FFMPEG_SAMPLE_RATE}Hz，实际 {framerate}Hz")

        pcm_data = wf.readframes(wf.getnframes())

    total_ms = len(pcm_data) * 1000 // (framerate * sample_width * channels)
    bytes_per_ms = framerate * sample_width * channels // 1000
    clip_paths: list[str] = []

    for i, u in enumerate(utterances):
        start_ms = int(u.get("start_ms", 0))
        end_ms = int(u.get("end_ms", start_ms))
        if end_ms <= start_ms:
            end_ms = start_ms + 100

        clip_start = max(0, start_ms - padding_ms)
        clip_end = min(total_ms, end_ms + padding_ms)

        start_byte = clip_start * bytes_per_ms
        end_byte = min(len(pcm_data), clip_end * bytes_per_ms)
        clip_pcm = pcm_data[start_byte:end_byte]

        clip_name = f"{i + 1}.wav"
        clip_path = os.path.join(output_dir, clip_name)
        with wave.open(clip_path, "wb") as out_wf:
            out_wf.setnchannels(channels)
            out_wf.setsampwidth(sample_width)
            out_wf.setframerate(framerate)
            out_wf.writeframes(clip_pcm)

        clip_paths.append(clip_path)

    print(f"[Step2.5] ✅ 已切分 {len(clip_paths)} 条音频到: {output_dir}")
    return clip_paths
