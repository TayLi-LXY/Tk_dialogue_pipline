import json
import os
import sys

DIALOGUE_OUTPUT_DIR = os.path.join(os.path.dirname(__file__), "output", "dialogues")


def dialogue_json_to_txt(json_path: str, txt_path: str | None = None) -> str:
    with open(json_path, encoding="utf-8") as f:
        data = json.load(f)

    if txt_path is None:
        base = os.path.splitext(json_path)[0]
        txt_path = base + ".txt"

    dialogues = data.get("dialogues", [])
    lines: list[str] = []

    for item in dialogues:
        speaker = item.get("speaker", "未知")
        text = (item.get("text") or "").strip()
        if not text:
            continue
        lines.append(f"{speaker}：{text}")

    with open(txt_path, "w", encoding="utf-8") as f:
        f.write("\n\n".join(lines))
        f.write("\n")

    return txt_path


if __name__ == "__main__":
    path = sys.argv[1] if len(sys.argv) > 1 else os.path.join(
        DIALOGUE_OUTPUT_DIR, "保险理赔_dialogue.json"
    )
    out = dialogue_json_to_txt(path)
    print(out)
