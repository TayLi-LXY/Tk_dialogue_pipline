import os

BASE_DIR = os.path.dirname(os.path.abspath(__file__))

TK_COPILOT_URL = "http://tkcopilot.tkitg.com"
TK_COPILOT_URL_HTTPS = "https://tkcopilot.taikang.com"

# 流式 ASR 1.0 WebSocket
ASR_WS_URL = "ws://tkcopilot.tkitg.com/api/v3/sauc/bigmodel"
ASR_APP_KEY = "tkcopilot"
ASR_ACCESS_KEY = "你的ASR密钥填这里"
ASR_RESOURCE_ID = "volc.bigasr.sauc.duration"
ASR_APP_KEY_RES = "PlgvMymc7f3tQnJ6"

# ASR 请求参数
ASR_BOOSTING_TABLE_ID = "7629728d-cc2c-498a-aa83-ecb3f94df82c"
ASR_ENABLE_DDC = True
ASR_ENABLE_SPEAKER_INFO = True
ASR_CHUNK_MS = 200

# 录音文件标准版（备用，当前未使用）
ASR_SUBMIT_URL = f"{TK_COPILOT_URL}/api/v3/auc/bigmodel/submit"
ASR_QUERY_URL = f"{TK_COPILOT_URL}/api/v3/auc/bigmodel/query"

# LLM 角色标注
LLM_API_KEY = "你的LLM密钥填这里"
LLM_MODEL = "qwen3.5-plus"
LLM_CHAT_URL = f"{TK_COPILOT_URL_HTTPS}/v1/tongyi/chat/completions"

INPUT_VIDEO_DIR = os.path.join(BASE_DIR, "input_videos")
OUTPUT_DIR = os.path.join(BASE_DIR, "output")
AUDIO_OUTPUT_DIR = os.path.join(OUTPUT_DIR, "audio")
DIALOGUE_OUTPUT_DIR = os.path.join(OUTPUT_DIR, "dialogues")

FFMPEG_SAMPLE_RATE = 16000
FFMPEG_CHANNELS = 1

ASR_POLL_INTERVAL = 5
ASR_MAX_WAIT_SECONDS = 1800

SPEAKER_COUNT = 2
