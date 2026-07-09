import json
import os
import shutil
import sys

from step3_video_to_dialogue import (
    DIALOGUE_OUTPUT_DIR,
    ensure_dirs,
    extract_utterances,
    refine_dialogue_with_llm,
    save_output,
)


def main():
    basename = sys.argv[1] if len(sys.argv) > 1 else "保险理赔"
    wav = f"output/audio/{basename}.wav"
    video_name = f"{basename}.mp4"
    asr_src = sys.argv[2] if len(sys.argv) > 2 else "output/dialogues/_asr2_test.json"

    ensure_dirs()
    dst = os.path.join(DIALOGUE_OUTPUT_DIR, f"{basename}_asr_raw.json")
    shutil.copy(asr_src, dst)
    print(f"ASR raw: {dst}")

    with open(dst, encoding="utf-8") as f:
        asr_result = json.load(f)

    utterances = extract_utterances(asr_result)
    speakers = sorted({u["speaker"] for u in utterances if u["speaker"] != "unknown"})
    print(f"utterances={len(utterances)}, speakers={speakers}")

    refined = refine_dialogue_with_llm(utterances, video_name)
    out = save_output(wav, refined, video_name)
    print(f"DONE: {out}")
    print(f"role_mapping: {refined.get('role_mapping')}")
    for d in refined.get("dialogues", [])[:5]:
        print(f"  [{d['speaker']}] {d['text'][:60]}")


if __name__ == "__main__":
    main()
