import asyncio
import json
import sys

from sauc_asr import _read_wav_pcm, _transcribe_pcm_async


def main():
    wav = sys.argv[1] if len(sys.argv) > 1 else r"output/audio/保险理赔.wav"
    seconds = int(sys.argv[2]) if len(sys.argv) > 2 else 15

    pcm = _read_wav_pcm(wav)
    if seconds > 0:
        pcm = pcm[: 16000 * 2 * seconds]
        print(f"testing ASR 2.0, {seconds}s clip from {wav}")
    else:
        print(f"testing ASR 2.0, full audio from {wav}")

    result = asyncio.run(_transcribe_pcm_async(pcm))
    utterances = (result.get("result") or {}).get("utterances", [])
    print(f"utterances: {len(utterances)}")

    speakers = set()
    for i, u in enumerate(utterances[:8]):
        additions = u.get("additions") or {}
        sp = u.get("speaker") or u.get("speaker_id") or additions.get("speaker") or additions.get("speaker_id")
        if sp is not None:
            speakers.add(str(sp))
        print(f"[{i}] speaker={sp} text={u.get('text', '')[:60]}")

    print(f"unique speakers in sample: {sorted(speakers)}")

    out = "output/dialogues/_asr2_test.json"
    with open(out, "w", encoding="utf-8") as f:
        json.dump(result, f, ensure_ascii=False, indent=2)
    print(f"saved: {out}")


if __name__ == "__main__":
    main()
