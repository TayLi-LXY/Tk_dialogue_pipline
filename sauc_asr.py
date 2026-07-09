"""火山引擎大模型流式 ASR WebSocket 客户端（sauc 协议）。"""

import asyncio
import gzip
import json
import struct
import uuid
import wave
from typing import Any

import websockets

from config import (
    ASR_ACCESS_KEY,
    ASR_APP_KEY,
    ASR_APP_KEY_RES,
    ASR_BOOSTING_TABLE_ID,
    ASR_CHUNK_MS,
    ASR_ENABLE_DDC,
    ASR_ENABLE_NONSTREAM,
    ASR_ENABLE_SPEAKER_INFO,
    ASR_MODEL_VERSION,
    ASR_RESOURCE_ID,
    ASR_SSD_VERSION,
    ASR_WS_URL,
    FFMPEG_CHANNELS,
    FFMPEG_SAMPLE_RATE,
)

PROTOCOL_VERSION = 0b0001
HEADER_SIZE = 0b0001

FULL_CLIENT_REQUEST = 0b0001
AUDIO_ONLY_REQUEST = 0b0010
FULL_SERVER_RESPONSE = 0b1001
SERVER_ERROR_RESPONSE = 0b1111

NO_SEQUENCE = 0b0000
POS_SEQUENCE = 0b0001
LAST_PACKET = 0b0010
NEG_WITH_SEQUENCE = 0b0011

NO_SERIALIZATION = 0b0000
JSON_SERIALIZATION = 0b0001

NO_COMPRESSION = 0b0000
GZIP_COMPRESSION = 0b0001


def _build_header(
    message_type: int,
    message_type_specific_flags: int = NO_SEQUENCE,
    serialization: int = JSON_SERIALIZATION,
    compression: int = GZIP_COMPRESSION,
) -> bytes:
    return bytes([
        (PROTOCOL_VERSION << 4) | HEADER_SIZE,
        (message_type << 4) | message_type_specific_flags,
        (serialization << 4) | compression,
        0x00,
    ])


def _build_frame(
    message_type: int,
    payload: bytes,
    flags: int = NO_SEQUENCE,
    sequence: int | None = None,
    serialization: int = JSON_SERIALIZATION,
    compression: int = GZIP_COMPRESSION,
) -> bytes:
    frame = bytearray(_build_header(message_type, flags, serialization, compression))
    if flags in (POS_SEQUENCE, NEG_WITH_SEQUENCE):
        frame.extend(struct.pack(">i", sequence if sequence is not None else 1))
    frame.extend(struct.pack(">I", len(payload)))
    frame.extend(payload)
    return bytes(frame)


def _parse_response(data: bytes) -> dict[str, Any]:
    if len(data) < 4:
        raise RuntimeError(f"ASR 响应过短: {len(data)} bytes")

    header_size = data[0] & 0x0F
    message_type = data[1] >> 4
    flags = data[1] & 0x0F
    serialization = data[2] >> 4
    compression = data[2] & 0x0F

    payload = data[header_size * 4:]
    result: dict[str, Any] = {
        "message_type": message_type,
        "is_last_package": bool(flags & 0x02),
    }

    if flags & 0x01:
        if len(payload) < 4:
            return result
        result["sequence"] = struct.unpack(">i", payload[:4])[0]
        payload = payload[4:]

    if message_type == FULL_SERVER_RESPONSE:
        if len(payload) < 4:
            return result
        payload = payload[4:]
    elif message_type == SERVER_ERROR_RESPONSE:
        if len(payload) < 8:
            return result
        result["error_code"] = struct.unpack(">I", payload[:4])[0]
        payload = payload[4:]
        if len(payload) < 4:
            return result
        payload = payload[4:]
    else:
        return result

    if not payload:
        return result

    if compression == GZIP_COMPRESSION:
        payload = gzip.decompress(payload)

    if serialization == JSON_SERIALIZATION:
        result["payload"] = json.loads(payload.decode("utf-8"))
    else:
        result["payload"] = payload

    return result


def _read_wav_pcm(wav_path: str) -> bytes:
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
        return wf.readframes(wf.getnframes())


def _chunk_pcm(pcm_data: bytes, chunk_ms: int = ASR_CHUNK_MS) -> list[bytes]:
    bytes_per_ms = FFMPEG_SAMPLE_RATE * 2 // 1000
    chunk_size = max(bytes_per_ms * chunk_ms, 1)
    return [pcm_data[i:i + chunk_size] for i in range(0, len(pcm_data), chunk_size)]


def _build_client_request() -> dict[str, Any]:
    request: dict[str, Any] = {
        "model_name": "bigmodel",
        "enable_itn": True,
        "enable_punc": True,
        "show_utterances": True,
    }
    if ASR_ENABLE_DDC:
        request["enable_ddc"] = True
    if ASR_ENABLE_SPEAKER_INFO:
        request["enable_speaker_info"] = True
        if ASR_SSD_VERSION:
            request["ssd_version"] = ASR_SSD_VERSION
    if ASR_ENABLE_NONSTREAM:
        request["enable_nonstream"] = True
    if ASR_BOOSTING_TABLE_ID:
        request["corpus"] = {"boosting_table_id": ASR_BOOSTING_TABLE_ID}

    return {
        "user": {"uid": "dialogue_pipeline"},
        "audio": {
            "format": "pcm",
            "codec": "raw",
            "rate": FFMPEG_SAMPLE_RATE,
            "bits": 16,
            "channel": FFMPEG_CHANNELS,
            "language": "zh-CN",
        },
        "request": request,
    }


async def _recv_with_timeout(ws, timeout: float = 30.0) -> dict[str, Any]:
    try:
        data = await asyncio.wait_for(ws.recv(), timeout=timeout)
    except asyncio.TimeoutError as exc:
        raise TimeoutError(f"ASR 响应超时 ({timeout}s)") from exc
    return _parse_response(data)


async def _drain_responses(ws, state: dict[str, Any]) -> None:
    """后台持续接收 ASR 响应，避免发送循环被逐包 recv 阻塞。"""
    while not state.get("done"):
        try:
            resp = await _recv_with_timeout(ws, timeout=180)
        except TimeoutError as exc:
            if state.get("audio_sent"):
                state["error"] = TimeoutError("ASR 在音频发送完成后长时间无响应")
                break
            continue

        if resp.get("message_type") == SERVER_ERROR_RESPONSE:
            state["error"] = RuntimeError(f"ASR 识别错误: {resp}")
            break

        payload = resp.get("payload") or {}
        if payload:
            state["final_result"] = payload
            utterances = (
                payload.get("result", {}).get("utterances")
                or payload.get("utterances")
                or []
            )
            if utterances:
                state["utterances"] = utterances

        if resp.get("is_last_package"):
            state["done"] = True
            break


async def _transcribe_pcm_async(pcm_data: bytes) -> dict[str, Any]:
    request_id = str(uuid.uuid4())
    connect_id = str(uuid.uuid4())

    headers = {
        "X-Api-App-Key": ASR_APP_KEY,
        "X-Api-Access-Key": ASR_ACCESS_KEY,
        "X-Api-Resource-Id": ASR_RESOURCE_ID,
        "X-Api-Connect-Id": connect_id,
        "X-Api-Request-Id": request_id,
        "X-Api-Sequence": "-1",
        "X-Api-App-Key-Res": ASR_APP_KEY_RES,
    }

    chunks = _chunk_pcm(pcm_data)
    if not chunks:
        raise RuntimeError("音频为空")

    print(f"[Step2] 连接流式 ASR: {ASR_WS_URL}")
    print(f"  - model: {ASR_MODEL_VERSION}, resource-id: {ASR_RESOURCE_ID}")
    print(f"  - speaker_info={ASR_ENABLE_SPEAKER_INFO}, ssd_version={ASR_SSD_VERSION}")
    print(f"  - 音频分包: {len(chunks)} 包 x ~{ASR_CHUNK_MS}ms")

    state: dict[str, Any] = {
        "done": False,
        "audio_sent": False,
        "final_result": {},
        "utterances": [],
        "error": None,
    }

    async with websockets.connect(
        ASR_WS_URL,
        additional_headers=headers,
        max_size=100 * 1024 * 1024,
        open_timeout=30,
    ) as ws:
        logid = ws.response.headers.get("X-Tt-Logid", "")
        if logid:
            print(f"  - X-Tt-Logid: {logid}")

        req_json = json.dumps(_build_client_request(), ensure_ascii=False).encode("utf-8")
        init_frame = _build_frame(
            FULL_CLIENT_REQUEST,
            gzip.compress(req_json),
            flags=POS_SEQUENCE,
            sequence=1,
        )
        await ws.send(init_frame)
        init_resp = await _recv_with_timeout(ws, timeout=30)
        if init_resp.get("message_type") == SERVER_ERROR_RESPONSE:
            raise RuntimeError(f"ASR 初始化失败: {init_resp}")

        receiver = asyncio.create_task(_drain_responses(ws, state))

        for idx, chunk in enumerate(chunks):
            is_last = idx == len(chunks) - 1
            flags = LAST_PACKET if is_last else POS_SEQUENCE
            sequence = idx + 2 if not is_last else None
            audio_frame = _build_frame(
                AUDIO_ONLY_REQUEST,
                gzip.compress(chunk),
                flags=flags,
                sequence=sequence,
                serialization=NO_SERIALIZATION,
            )
            await ws.send(audio_frame)
            await asyncio.sleep(ASR_CHUNK_MS / 1000.0)

            if (idx + 1) % 50 == 0 or is_last:
                print(f"  - 已发送 {idx + 1}/{len(chunks)} 包", flush=True)

        state["audio_sent"] = True
        await receiver

        if state.get("error"):
            raise state["error"]

    final_result = state.get("final_result") or {}
    all_utterances = state.get("utterances") or []

    if all_utterances:
        final_result.setdefault("result", {})["utterances"] = all_utterances

    text = (final_result.get("result") or {}).get("text", "")
    speakers = set()
    for u in all_utterances:
        additions = u.get("additions") or {}
        sp = u.get("speaker") or u.get("speaker_id") or additions.get("speaker") or additions.get("speaker_id")
        if sp is not None:
            speakers.add(str(sp))
    speaker_info = f"，说话人 {len(speakers)} 个" if speakers else "，未返回说话人字段"
    print(f"[Step2] ASR 完成，文本长度 {len(text)}，分句 {len(all_utterances)} 条{speaker_info}")
    return final_result


def transcribe_wav_file(wav_path: str) -> dict[str, Any]:
    pcm_data = _read_wav_pcm(wav_path)
    duration = len(pcm_data) / (FFMPEG_SAMPLE_RATE * 2)
    print(f"[Step2] 读取音频: {wav_path} (约 {duration:.1f}s)")
    return asyncio.run(_transcribe_pcm_async(pcm_data))
