      
import json
import os
import subprocess
import sys

import requests

from config import (
    AUDIO_OUTPUT_DIR, DIALOGUE_OUTPUT_DIR, FFMPEG_CHANNELS, FFMPEG_SAMPLE_RATE,
    INPUT_VIDEO_DIR, LLM_API_KEY, LLM_CHAT_URL, LLM_MODEL,
)
from sauc_asr import transcribe_wav_file


def ensure_dirs():
    for d in [AUDIO_OUTPUT_DIR, DIALOGUE_OUTPUT_DIR]:
        os.makedirs(d, exist_ok=True)


def extract_audio(video_path, output_dir):
    basename = os.path.splitext(os.path.basename(video_path))[0]
    wav_path = os.path.join(output_dir, f"{basename}.wav")

    cmd = [
        "ffmpeg", "-y", "-i", video_path,
        "-vn",
        "-acodec", "pcm_s16le",
        "-ar", str(FFMPEG_SAMPLE_RATE),
        "-ac", str(FFMPEG_CHANNELS),
        wav_path
    ]

    print(f"[Step1] 正在提取音频: {video_path}")
    result = subprocess.run(cmd, capture_output=True, text=True)

    if result.returncode != 0:
        print(f"[Step1] ❌ FFmpeg 失败:\n{result.stderr}")
        raise RuntimeError(f"FFmpeg failed: {result.stderr}")

    file_size = os.path.getsize(wav_path)
    duration_approx = file_size / (FFMPEG_SAMPLE_RATE * 2)
    print(f"[Step1] ✅ 音频已保存: {wav_path} (大小: {file_size/1024/1024:.1f}MB, 约{duration_approx:.0f}秒)")
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
        return [{"speaker": "unknown", "text": data, "start_time": "00:00:00", "end_time": "00:00:00"}]

    normalized = []
    for i, u in enumerate(utterances):
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
        start = u.get("start_time") or u.get("start_ms") or u.get("begin_time") or 0
        end = u.get("end_time") or u.get("end_ms") or u.get("finish_time") or 0

        # 流式 ASR 返回毫秒时间戳
        if isinstance(start, (int, float)):
            start = start / 1000
        if isinstance(end, (int, float)):
            end = end / 1000

        def fmt(s):
            if isinstance(s, (int, float)):
                h = int(s // 3600)
                m = int((s % 3600) // 60)
                sec = int(s % 60)
                return f"{h:02d}:{m:02d}:{sec:02d}"
            return str(s)

        normalized.append({
            "speaker": speaker_label,
            "text": text,
            "start_time": fmt(start),
            "end_time": fmt(end),
        })

    return normalized


def refine_dialogue_with_llm(utterances, video_name=""):
    raw_text = json.dumps(utterances, ensure_ascii=False, indent=2)

    if len(raw_text) > 30000:
        print(f"[Step3] ⚠️ 文本过长 ({len(raw_text)} 字符)，将分段处理")
        return process_large_dialogue(utterances, video_name)

    sys_prompt = """你是一个专业的对话分析专家，擅长从语音转写文本中识别对话角色和场景。

输入数据可能包含 ASR 说话人分离结果（如 speaker_0、speaker_1）。规则：
1. 同一个 speaker 编号始终对应同一人，请先归纳各 speaker 的身份，再标注每轮对话
2. 全程通常只有 2 个角色：「保险代理人」和「客户」；若 speaker 编号超过 2 个，将语义上同一人合并到同一角色
3. 判断依据：主动推销、讲解产品、办理理赔的是代理人；表达态度、提问、回应的是客户
4. 修正 ASR 错别字，保留原始时间戳，不要合并不同说话人的句子

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
      "confidence": 0.9
    }
  ],
  "summary": "一句话概括这段对话的主题"
}"""

    user_prompt = f"""以下是从视频「{video_name}」转录的原始对话（包含时间戳和说话人标签）：

{raw_text}

请分析对话内容，判断角色并输出结构化对话。"""

    print(f"[Step3] 调用 LLM 精修对话...")
    response = call_llm(sys_prompt, user_prompt)
    parsed = json.loads(response)

    print(f"[Step3] ✅ 场景: {parsed.get('scenario_type', '未知')}")
    print(f"[Step3] ✅ 角色映射: {parsed.get('role_mapping', {})}")
    print(f"[Step3] ✅ 对话轮数: {len(parsed.get('dialogues', []))}")

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

        sys_prompt = """你是对话分析专家。分析以下对话片段，判断角色并输出JSON：
{
  "scenario_type": "保险销售/保险咨询/其他",
  "role_mapping": {"speaker_X": "代理人", "speaker_Y": "客户"},
  "dialogues": [
    {"turn_id": 1, "speaker": "代理人或客户", "text": "文本", "start_time": "00:00:00", "end_time": "00:00:00", "confidence": 0.9}
  ],
  "summary": "片段摘要"
}"""
        user_prompt = f"视频「{video_name}」片段 {idx + 1}:\n{json.dumps(chunk, ensure_ascii=False)}"

        try:
            resp_text = call_llm(sys_prompt, user_prompt)
            parsed = json.loads(resp_text)
            all_dialogues.extend(parsed.get("dialogues", []))
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
                    "confidence": 0.5,
                })

    for i, d in enumerate(all_dialogues):
        d["turn_id"] = i + 1

    final_summary = " ".join(summaries) if summaries else ""
    print(f"[Step3] ✅ 全部 {len(chunks)} 段合并完成, 共 {len(all_dialogues)} 轮对话")

    return {
        "scenario_type": scenario_type,
        "role_mapping": role_mapping,
        "dialogues": all_dialogues,
        "summary": final_summary,
    }


def save_output(wav_path, refined_result, video_name):
    basename = os.path.splitext(video_name)[0]
    dialogue_path = os.path.join(DIALOGUE_OUTPUT_DIR, f"{basename}_dialogue.json")

    output = {
        "source_video": video_name,
        "audio_file": os.path.basename(wav_path),
        "audio_path": wav_path,
        **refined_result,
    }

    with open(dialogue_path, "w", encoding="utf-8") as f:
        json.dump(output, f, ensure_ascii=False, indent=2)

    print(f"[输出] ✅ 对话已保存: {dialogue_path}")
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
    if not utterances:
        print("[Step2] ⚠️ 未提取到任何对话内容，跳过 LLM 精修")
        refined = {"dialogues": [], "scenario_type": "empty", "role_mapping": {}, "summary": "ASR 未返回内容"}
    else:
        print(f"[Step2] ✅ 提取到 {len(utterances)} 条原始对话")
        refined = refine_dialogue_with_llm(utterances, video_name)

    out = save_output(wav_path, refined, video_name)
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

    