
import json
import os
import subprocess
import sys
import wave

import requests

from audio_clips import slice_wav_by_utterances
from config import (
    AUDIO_DURATION_TOLERANCE, AUDIO_OUTPUT_DIR, DIALOGUE_OUTPUT_DIR,
    FFMPEG_CHANNELS, FFMPEG_SAMPLE_RATE, INPUT_VIDEO_DIR, LLM_API_KEY,
    LLM_CHAT_URL, LLM_MODEL,
)
from export_dialogue_txt import dialogue_json_to_txt
from sauc_asr import transcribe_wav_file


def ensure_dirs():
    for d in [AUDIO_OUTPUT_DIR, DIALOGUE_OUTPUT_DIR]:
        os.makedirs(d, exist_ok=True)


def _probe_audio_stream(video_path: str) -> dict:
    cmd = [
        "ffprobe", "-v", "quiet", "-print_format", "json",
        "-show_streams", "-show_format", video_path,
    ]
    result = subprocess.run(cmd, capture_output=True, text=True)
    if result.returncode != 0:
        print(f"[Step1] ⚠️ ffprobe 失败，跳过时长校验: {result.stderr}")
        return {}

    probe = json.loads(result.stdout)
    audio_stream = next(
        (s for s in probe.get("streams", []) if s.get("codec_type") == "audio"),
        None,
    )
    fmt = probe.get("format") or {}

    duration = None
    sample_rate = None
    if audio_stream:
        duration = audio_stream.get("duration") or fmt.get("duration")
        sample_rate = audio_stream.get("sample_rate")
    else:
        duration = fmt.get("duration")

    return {
        "duration_sec": float(duration) if duration else None,
        "sample_rate": int(sample_rate) if sample_rate else None,
    }


def _get_wav_duration_sec(wav_path: str) -> float:
    with wave.open(wav_path, "rb") as wf:
        return wf.getnframes() / wf.getframerate()


def extract_audio(video_path, output_dir):
    basename = os.path.splitext(os.path.basename(video_path))[0]
    wav_path = os.path.join(output_dir, f"{basename}.wav")

    probe_info = _probe_audio_stream(video_path)
    if probe_info.get("duration_sec"):
        print(
            f"[Step1] 视频音轨: 约 {probe_info['duration_sec']:.1f}s"
            + (f", {probe_info['sample_rate']}Hz" if probe_info.get("sample_rate") else "")
        )

    cmd = [
        "ffmpeg", "-y",
        "-fflags", "+genpts",
        "-avoid_negative_ts", "make_zero",
        "-i", video_path,
        "-map", "0:a:0",
        "-vn",
        "-acodec", "pcm_s16le",
        "-ac", str(FFMPEG_CHANNELS),
        "-af", f"aresample={FFMPEG_SAMPLE_RATE}:resampler=soxr",
        wav_path,
    ]

    print(f"[Step1] 正在提取音频: {video_path}")
    result = subprocess.run(cmd, capture_output=True, text=True)

    if result.returncode != 0:
        print(f"[Step1] ❌ FFmpeg 失败:\n{result.stderr}")
        raise RuntimeError(f"FFmpeg failed: {result.stderr}")

    wav_duration = _get_wav_duration_sec(wav_path)
    file_size = os.path.getsize(wav_path)
    print(f"[Step1] ✅ 音频已保存: {wav_path} (大小: {file_size/1024/1024:.1f}MB, {wav_duration:.1f}s)")

    video_duration = probe_info.get("duration_sec")
    if video_duration and video_duration > 0:
        ratio = abs(wav_duration - video_duration) / video_duration
        if ratio > AUDIO_DURATION_TOLERANCE:
            print(
                f"[Step1] ⚠️ 时长偏差 {ratio*100:.1f}% "
                f"(视频 {video_duration:.1f}s vs wav {wav_duration:.1f}s)，请检查音画同步"
            )
        else:
            print(f"[Step1] ✅ 时长校验通过 (偏差 {ratio*100:.1f}%)")

    return wav_path


def call_llm(system_prompt, user_prompt, max_tokens=4096):
    headers = {
        "Content-Type": "application/json",
        "Authorization": f"Bearer {LLM_API_KEY}",
    }

    data = {
        "messages": [
            {"role": "system", "content": system_prompt},
            {"role": "user", "content": user_prompt}
        ],
        "model": LLM_MODEL,
        "response_format": {"type": "json_object"},
        "temperature": 0.3,
        "max_tokens": max_tokens,
    }

    resp = requests.post(LLM_CHAT_URL, headers=headers, json=data, timeout=300)
    if resp.status_code == 200:
        return resp.json()["choices"][0]["message"]["content"]

    raise RuntimeError(f"LLM 请求失败: {resp.status_code} {resp.text}")


def _fmt_time(seconds: float) -> str:
    h = int(seconds // 3600)
    m = int((seconds % 3600) // 60)
    sec = int(seconds % 60)
    return f"{h:02d}:{m:02d}:{sec:02d}"


def _parse_time_to_ms(value) -> int:
    if isinstance(value, (int, float)):
        # ASR 流式返回毫秒时间戳
        return int(value)
    if isinstance(value, str) and ":" in value:
        parts = value.split(":")
        if len(parts) == 3:
            h, m, s = int(parts[0]), int(parts[1]), int(parts[2])
            return (h * 3600 + m * 60 + s) * 1000
    return 0


def extract_utterances(asr_result):
    data = asr_result.get("data") or asr_result.get("result") or asr_result
    if isinstance(data, dict) and isinstance(data.get("result"), dict):
        data = data["result"]

    utterances = []
    if isinstance(data, dict):
        utterances = (
            data.get("utterances")
            or data.get("sentences")
            or data.get("words_info")
            or []
        )
        if not utterances and data.get("text"):
            utterances = [{"text": data["text"]}]
    elif isinstance(data, str):
        return [{
            "speaker": "unknown",
            "text": data,
            "start_ms": 0,
            "end_ms": 0,
            "start_time": "00:00:00",
            "end_time": "00:00:00",
        }]

    normalized = []
    for u in utterances:
        additions = u.get("additions") or {}
        speaker = (
            u.get("speaker")
            or u.get("speaker_id")
            or additions.get("speaker")
            or additions.get("speaker_id")
            or u.get("channel")
        )
        if speaker is None:
            speaker_label = "unknown"
        elif str(speaker).startswith("speaker"):
            speaker_label = str(speaker)
        else:
            speaker_label = f"speaker_{speaker}"
        text = u.get("text") or u.get("words") or u.get("sentence") or u.get("content") or ""

        raw_start = u.get("start_time") or u.get("start_ms") or u.get("begin_time") or 0
        raw_end = u.get("end_time") or u.get("end_ms") or u.get("finish_time") or 0
        start_ms = _parse_time_to_ms(raw_start)
        end_ms = _parse_time_to_ms(raw_end)

        normalized.append({
            "speaker": speaker_label,
            "text": text,
            "start_ms": start_ms,
            "end_ms": end_ms,
            "start_time": _fmt_time(start_ms / 1000),
            "end_time": _fmt_time(end_ms / 1000),
        })

    return normalized


LLM_SYSTEM_PROMPT = """你是一个专业的对话分析专家，擅长从语音转写文本中识别对话角色和场景。

输入数据可能包含 ASR 说话人分离结果（如 speaker_0、speaker_1）。规则：
1. 同一个 speaker 编号始终对应同一人，请先归纳各 speaker 的身份，再标注每轮对话
2. 全程通常只有 2 个角色：「保险代理人」和「客户」；若 speaker 编号超过 2 个，将语义上同一人合并到同一角色
3. 判断依据：主动推销、讲解产品、办理理赔的是代理人；表达态度、提问、回应的是客户
4. 修正 ASR 错别字，保留原始时间戳
5. **硬性要求：输入 N 条 utterance，必须输出 N 条 dialogue，禁止合并、禁止拆分、禁止跳过**
6. turn_id 从 1 递增，与输入顺序严格一一对应

输出格式（严格 JSON）：
{
  "scenario_type": "保险销售/保险咨询/其他",
  "role_mapping": {"speaker_0": "代理人", "speaker_1": "客户"},
  "dialogues": [
    {
      "turn_id": 1,
      "speaker": "代理人或客户",
      "text": "清洗后的对话文本",
      "start_time": "00:00:00",
      "end_time": "00:00:00",
      "start_ms": 542,
      "end_ms": 2001,
      "audio_file": "1.wav",
      "confidence": 0.9
    }
  ],
  "summary": "一句话概括这段对话的主题"
}"""


def _parse_llm_json(response: str) -> dict:
    text = response.strip()
    if text.startswith("```"):
        lines = text.split("\n")
        lines = lines[1:]
        if lines and lines[-1].strip() == "```":
            lines = lines[:-1]
        text = "\n".join(lines)
    return json.loads(text)


def _fallback_dialogues_from_utterances(utterances: list[dict]) -> dict:
    dialogues = []
    for i, u in enumerate(utterances):
        dialogues.append({
            "turn_id": i + 1,
            "speaker": u.get("speaker", "unknown"),
            "text": u.get("text", ""),
            "start_time": u.get("start_time", "00:00:00"),
            "end_time": u.get("end_time", "00:00:00"),
            "start_ms": u.get("start_ms", 0),
            "end_ms": u.get("end_ms", 0),
            "audio_file": f"{i + 1}.wav",
            "confidence": 0.5,
        })
    return {
        "scenario_type": "其他",
        "role_mapping": {},
        "dialogues": dialogues,
        "summary": "LLM 精修失败，使用 ASR 原始分句",
    }


def refine_dialogue_with_llm(utterances, video_name=""):
    raw_text = json.dumps(utterances, ensure_ascii=False, indent=2)

    if len(raw_text) > 30000:
        print(f"[Step3] ⚠️ 文本过长 ({len(raw_text)} 字符)，将分段处理")
        return process_large_dialogue(utterances, video_name)

    user_prompt = f"""以下是从视频「{video_name}」转录的原始对话（共 {len(utterances)} 条，包含时间戳和说话人标签）：

{raw_text}

请分析对话内容，判断角色并输出结构化对话。dialogues 必须恰好 {len(utterances)} 条。"""

    print(f"[Step3] 调用 LLM 精修对话...")
    try:
        response = call_llm(LLM_SYSTEM_PROMPT, user_prompt, max_tokens=8192)
        parsed = _parse_llm_json(response)
    except (json.JSONDecodeError, KeyError, RuntimeError) as e:
        print(f"[Step3] ⚠️ LLM 精修失败: {e}，使用 ASR 原始分句")
        return _fallback_dialogues_from_utterances(utterances)

    dialogues = parsed.get("dialogues", [])
    if len(dialogues) != len(utterances):
        print(
            f"[Step3] ⚠️ LLM 返回 {len(dialogues)} 条，期望 {len(utterances)} 条，将按序号对齐"
        )

    print(f"[Step3] ✅ 场景: {parsed.get('scenario_type', '未知')}")
    print(f"[Step3] ✅ 角色映射: {parsed.get('role_mapping', {})}")
    print(f"[Step3] ✅ 对话轮数: {len(dialogues)}")

    return parsed


def process_large_dialogue(utterances, video_name):
    CHUNK_UTTERANCES = 100
    chunks = [utterances[i:i + CHUNK_UTTERANCES] for i in range(0, len(utterances), CHUNK_UTTERANCES)]

    all_dialogues = []
    role_mapping = {}
    scenario_type = ""
    summaries = []

    for idx, chunk in enumerate(chunks):
        print(f"[Step3] 处理分段 {idx + 1}/{len(chunks)}...")

        chunk_prompt = LLM_SYSTEM_PROMPT + f"\n\n当前片段共 {len(chunk)} 条 utterance，dialogues 必须恰好 {len(chunk)} 条。"
        user_prompt = f"视频「{video_name}」片段 {idx + 1}:\n{json.dumps(chunk, ensure_ascii=False)}"

        try:
            resp_text = call_llm(chunk_prompt, user_prompt, max_tokens=8192)
            parsed = _parse_llm_json(resp_text)
            chunk_dialogues = parsed.get("dialogues", [])
            if len(chunk_dialogues) != len(chunk):
                print(
                    f"[Step3] ⚠️ 分段 {idx + 1} 返回 {len(chunk_dialogues)} 条，期望 {len(chunk)} 条"
                )
            all_dialogues.extend(chunk_dialogues)
            role_mapping.update(parsed.get("role_mapping", {}))
            if parsed.get("scenario_type"):
                scenario_type = parsed["scenario_type"]
            if parsed.get("summary"):
                summaries.append(parsed["summary"])
        except Exception as e:
            print(f"[Step3] ⚠️ 分段 {idx + 1} 处理失败: {e}")
            for u in chunk:
                all_dialogues.append({
                    "turn_id": len(all_dialogues) + 1,
                    "speaker": u.get("speaker", "unknown"),
                    "text": u.get("text", ""),
                    "start_time": u.get("start_time", "00:00:00"),
                    "end_time": u.get("end_time", "00:00:00"),
                    "start_ms": u.get("start_ms", 0),
                    "end_ms": u.get("end_ms", 0),
                    "confidence": 0.5,
                })

    for i, d in enumerate(all_dialogues):
        d["turn_id"] = i + 1
        d["audio_file"] = f"{i + 1}.wav"

    final_summary = " ".join(summaries) if summaries else ""
    print(f"[Step3] ✅ 全部 {len(chunks)} 段合并完成, 共 {len(all_dialogues)} 轮对话")

    return {
        "scenario_type": scenario_type,
        "role_mapping": role_mapping,
        "dialogues": all_dialogues,
        "summary": final_summary,
    }


def attach_audio_to_dialogues(refined: dict, clips_dir: str, utterance_count: int):
    dialogues = refined.get("dialogues", [])
    if len(dialogues) != utterance_count:
        print(
            f"[Step4] ⚠️ dialogue 数 ({len(dialogues)}) 与 utterance 数 ({utterance_count}) 不一致"
        )

    for i in range(min(len(dialogues), utterance_count)):
        d = dialogues[i]
        turn_id = i + 1
        d["turn_id"] = turn_id
        d["audio_file"] = f"{turn_id}.wav"
        d["audio_path"] = os.path.join(clips_dir, f"{turn_id}.wav")

    refined["clip_count"] = utterance_count
    refined["audio_clips_dir"] = clips_dir


def save_output(wav_path, refined_result, video_name, clips_dir=None):
    basename = os.path.splitext(video_name)[0]
    dialogue_path = os.path.join(DIALOGUE_OUTPUT_DIR, f"{basename}_dialogue.json")

    output = {
        "source_video": video_name,
        "audio_file": os.path.basename(wav_path),
        "audio_path": wav_path,
        **refined_result,
    }
    if clips_dir:
        output["audio_clips_dir"] = clips_dir

    with open(dialogue_path, "w", encoding="utf-8") as f:
        json.dump(output, f, ensure_ascii=False, indent=2)

    txt_path = dialogue_json_to_txt(dialogue_path)
    print(f"[输出] ✅ 对话已保存: {dialogue_path}")
    print(f"[输出] ✅ 文本已保存: {txt_path}")
    return dialogue_path


def process_single_video(video_path):
    video_name = os.path.basename(video_path)
    print(f"\n{'='*60}")
    print(f"🚀 开始处理: {video_name}")
    print(f"{'='*60}")

    wav_path = extract_audio(video_path, AUDIO_OUTPUT_DIR)
    asr_result = transcribe_wav_file(wav_path)

    basename = os.path.splitext(video_name)[0]
    asr_raw_path = os.path.join(DIALOGUE_OUTPUT_DIR, f"{basename}_asr_raw.json")
    with open(asr_raw_path, "w", encoding="utf-8") as f:
        json.dump(asr_result, f, ensure_ascii=False, indent=2)
    print(f"[Step2] ASR 原始结果已保存: {asr_raw_path}")

    utterances = extract_utterances(asr_result)
    clips_dir = os.path.join(AUDIO_OUTPUT_DIR, basename)

    if not utterances:
        print("[Step2] ⚠️ 未提取到任何对话内容，跳过 LLM 精修")
        refined = {"dialogues": [], "scenario_type": "empty", "role_mapping": {}, "summary": "ASR 未返回内容"}
    else:
        print(f"[Step2] ✅ 提取到 {len(utterances)} 条原始对话")
        slice_wav_by_utterances(wav_path, utterances, clips_dir)
        refined = refine_dialogue_with_llm(utterances, video_name)
        attach_audio_to_dialogues(refined, clips_dir, len(utterances))

    out = save_output(wav_path, refined, video_name, clips_dir if utterances else None)
    print(f"🎉 完成: {video_name}\n")
    return out


def main():
    ensure_dirs()
    os.makedirs(INPUT_VIDEO_DIR, exist_ok=True)

    if len(sys.argv) > 1:
        video_paths = sys.argv[1:]
    else:
        video_paths = []
        supported_ext = (".mp4", ".mkv", ".avi", ".mov", ".flv", ".webm", ".wmv")
        for f in sorted(os.listdir(INPUT_VIDEO_DIR)):
            if f.lower().endswith(supported_ext):
                video_paths.append(os.path.join(INPUT_VIDEO_DIR, f))

        if not video_paths:
            print(f"请将视频文件放入: {INPUT_VIDEO_DIR}")
            print("或者通过命令行传入: python step3_video_to_dialogue.py <video_path1> [video_path2] ...")
            sys.exit(1)

    print(f"📦 共发现 {len(video_paths)} 个视频待处理\n")

    results = []
    for i, vp in enumerate(video_paths):
        if not os.path.exists(vp):
            print(f"⚠️ 文件不存在，跳过: {vp}")
            continue
        try:
            out = process_single_video(vp)
            results.append({"video": vp, "status": "success", "output": out})
        except Exception as e:
            print(f"❌ 处理失败: {vp}\n   错误: {e}")
            results.append({"video": vp, "status": "failed", "error": str(e)})

    print(f"\n{'='*60}")
    print(f"📊 处理总结:")
    for r in results:
        icon = "✅" if r["status"] == "success" else "❌"
        print(f"  {icon} {os.path.basename(r['video'])}")
    print(f"{'='*60}")


if __name__ == "__main__":
    main()
