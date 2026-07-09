# 大模型流式语音识别 API 参考

> 官方文档：[流式语音识别 WebSocket - 火山引擎](https://www.volcengine.com/docs/6561/1354869?lang=zh)  
> 本文档基于官方文档整理，并补充 TK-copilot 网关地址，供本项目后续接入使用。

---

## 1. 接口概览

流式 ASR 通过 **WebSocket 二进制协议** 传输音频，**可直接读取本地 wav 文件分包发送**，无需先将音频上传到内网服务器再传 URL。

| 模式 | 官方地址 | TK-copilot 地址 | 特点 |
|------|----------|-----------------|------|
| 双向流式 | `wss://openspeech.bytedance.com/api/v3/sauc/bigmodel` | `ws://tkcopilot.tkitg.com/api/v3/sauc/bigmodel` | 每包输入对应一包返回，速度快 |
| 双向流式（优化版，**推荐**） | `wss://openspeech.bytedance.com/api/v3/sauc/bigmodel_async` | `ws://tkcopilot.tkitg.com/api/v3/sauc/bigmodel_async` | 仅结果变化时返回，RTF/首字时延更优 |
| 流式输入（nostream） | `wss://openspeech.bytedance.com/api/v3/sauc/bigmodel_nostream` | `ws://tkcopilot.tkitg.com/api/v3/sauc/bigmodel_nostream` | 音频 >15s 或发负包后返回，准确率更高 |

### 模式选择建议（本项目）

- **视频转对话、需说话人分离**：优先用 `bigmodel_async`（优化版），开启 `enable_speaker_info` + `ssd_version=200`（ASR 2.0）
- **实时性要求高**：`bigmodel` 或 `bigmodel_async`
- **准确率优先、可接受延迟**：`bigmodel_nostream`

### 分包建议

- 单包音频：**100~200ms**（双向流式推荐 **200ms**）
- 发包间隔：**100~200ms**
- 过大或过小都会影响性能

---

## 2. 鉴权（WebSocket 建连 Header）

TK-copilot 使用旧版控制台鉴权方式（与 `ASR_API.md` 一致）：

| Header | 说明 | 本项目示例 |
|--------|------|------------|
| `X-Api-App-Key` | App ID | `tkcopilot` |
| `X-Api-Access-Key` | Access Token（copilot 密钥） | `llm-xxx` |
| `X-Api-Resource-Id` | 资源 ID（见下表） | 见下表 |
| `X-Api-Connect-Id` | 连接追踪 ID，推荐 UUID | 随机生成 |
| `X-Api-Request-Id` | 任务 ID，推荐 UUID | 随机生成 |
| `X-Api-Sequence` | 发包序号，固定 `-1` | `-1` |

### Resource ID 对照

| 模型 | 计费方式 | Resource ID |
|------|----------|-------------|
| 流式 ASR 1.0 | 小时版 | `volc.bigasr.sauc.duration` |
| 流式 ASR 1.0 | 并发版 | `volc.bigasr.sauc.concurrent` |
| 流式 ASR 2.0 | 小时版 | `volc.seedasr.sauc.duration` |
| 流式 ASR 2.0 | 并发版 | `volc.seedasr.sauc.concurrent` |

> 注意：流式 ASR 的 Resource ID（`volc.*.sauc.*`）与录音文件识别（`volc.bigasr.auc*`）**不同**，需确认上级开通的是哪类服务。

### 建连示例

```http
GET /api/v3/sauc/bigmodel_async HTTP/1.1
Host: tkcopilot.tkitg.com
Upgrade: websocket
X-Api-App-Key: tkcopilot
X-Api-Access-Key: llm-xxx
X-Api-Resource-Id: volc.seedasr.sauc.duration
X-Api-Connect-Id: 67ee89ba-7050-4c04-a3d7-ac61a63499b3
X-Api-Request-Id: 67ee89ba-7050-4c04-a3d7-ac61a63499b4
X-Api-Sequence: -1
```

建连成功后，响应 Header 中的 `X-Tt-Logid` 务必记录，便于排错。

---

## 3. 二进制协议

WebSocket payload 由 **Header (4B) + [Sequence (4B)] + PayloadSize (4B) + Payload** 组成，整数均为**大端序**。

### Header 字段

| 字段 | 位数 | 值 |
|------|------|-----|
| Protocol version | 4 | `0b0001`（v1） |
| Header size | 4 | `0b0001`（4 字节） |
| Message type | 4 | `0b0001` full client request / `0b0010` audio only / `0b1001` server response / `0b1111` error |
| Message flags | 4 | `0b0000` 无 sequence / `0b0001` 正 sequence / `0b0010` 最后一包 / `0b0011` 负 sequence 最后一包 |
| Serialization | 4 | `0b0001` JSON（full request/response） / `0b0000` raw（audio） |
| Compression | 4 | `0b0000` 无 / `0b0001` Gzip |
| Reserved | 8 | `0x00` |

### 交互流程

```
1. WebSocket 建连（带鉴权 Header）
2. 发送 Full Client Request（JSON 配置，Gzip 压缩）
3. 循环发送 Audio Only Request（音频分包，Gzip 压缩）
4. 最后一包 Audio 设置 flags=最后一包/负包
5. 接收 Full Server Response（识别结果 JSON，Gzip 压缩）
6. 关闭连接
```

---

## 4. Full Client Request 参数

### 最小示例（中文 wav，16kHz 单声道）

```json
{
  "user": {
    "uid": "dialogue_pipeline"
  },
  "audio": {
    "format": "wav",
    "rate": 16000,
    "bits": 16,
    "channel": 1,
    "language": "zh-CN"
  },
  "request": {
    "model_name": "bigmodel",
    "enable_itn": true,
    "enable_punc": true,
    "show_utterances": true
  }
}
```

### 本项目关键参数（说话人分离 + 分句）

```json
{
  "request": {
    "model_name": "bigmodel",
    "enable_nonstream": true,
    "enable_speaker_info": true,
    "ssd_version": "200",
    "enable_itn": true,
    "enable_punc": true,
    "show_utterances": true,
    "end_window_size": 800
  }
}
```

| 参数 | 说明 |
|------|------|
| `enable_speaker_info` | 启用说话人聚类分离（默认 false） |
| `ssd_version` | 设为 `"200"` 启用大模型 SSD，**建议 ASR 2.0 时开启** |
| `enable_nonstream` | 二遍识别，在 `bigmodel_async` 上提升准确率 |
| `show_utterances` | 输出分句及时间戳 |
| `enable_itn` | 文本规范化（如「一九七零年」→「1970年」） |
| `enable_punc` | 启用标点 |
| `end_window_size` | 强制判停时间（ms），默认 800 |

> `enable_speaker_info` 需在 `language` 为空或 `zh-CN` 时可用；使用 `bigmodel_async` 时建议同时开 `enable_nonstream=true`。

### audio 字段

| 字段 | 必填 | 说明 |
|------|------|------|
| `format` | ✓ | `pcm` / `wav` / `ogg` / `mp3`（pcm/wav 内部须为 pcm_s16le） |
| `rate` | | 默认 16000，**目前只支持 16000** |
| `bits` | | 默认 16 |
| `channel` | | 1（mono）/ 2（stereo），默认 1 |
| `language` | | 如 `zh-CN`；仅 nostream 模式支持更多语种 |

---

## 5. 响应格式

```json
{
  "audio_info": { "duration": 3696 },
  "result": {
    "text": "整段识别文本",
    "utterances": [
      {
        "definite": true,
        "start_time": 0,
        "end_time": 1705,
        "text": "分句文本",
        "words": [
          { "text": "字", "start_time": 740, "end_time": 860, "blank_duration": 0 }
        ]
      }
    ]
  }
}
```

| 字段 | 说明 |
|------|------|
| `result.text` | 整段文本 |
| `utterances[].text` | 分句文本 |
| `utterances[].start_time` / `end_time` | 毫秒时间戳 |
| `utterances[].definite` | 是否为确定分句 |
| `utterances[].words` | 字级时间戳（可选） |

说话人信息在开启 `enable_speaker_info` 后，会出现在 `utterances` 的 additions 或 speaker 相关字段中（具体字段名以实际返回为准，接入时需打印原始 JSON 确认）。

---

## 6. 错误码

| 错误码 | 含义 |
|--------|------|
| `20000000` | 成功 |
| `45000001` | 请求参数无效 |
| `45000002` | 空音频 |
| `45000081` | 等包超时 |
| `45000151` | 音频格式不正确 |
| `55000031` | 服务器繁忙 |
| `550xxxxx` | 服务内部错误 |

---

## 7. 本地测试命令

官方提供 `sauc_websocket_demo.py`，TK-copilot 用法（来自 `ASR_API.md`）：

```bash
# 双向流式
python sauc_websocket_demo.py --file whoareyou.wav --url ws://tkcopilot.tkitg.com/api/v3/sauc/bigmodel

# 双向流式优化版（推荐）
python sauc_websocket_demo.py --file whoareyou.wav --url ws://tkcopilot.tkitg.com/api/v3/sauc/bigmodel_async

# 流式输入（高准确率）
python sauc_websocket_demo.py --file whoareyou.wav --url ws://tkcopilot.tkitg.com/api/v3/sauc/bigmodel_nostream
```

本项目 FFmpeg 提取的 wav 格式（16kHz / mono / pcm_s16le）与接口要求一致，可直接用于测试。

---

## 8. 与录音文件识别 API 对比

| 维度 | 流式 WebSocket（本文档） | 录音文件标准版 submit/query（当前代码） |
|------|--------------------------|----------------------------------------|
| 协议 | WebSocket 二进制 | HTTP JSON |
| 音频传入 | **本地文件分包直传** | **必须提供可下载 URL** |
| 是否需要内网服务器 | **不需要** | **需要**（ASR 服务端要能访问 URL） |
| 说话人分离 | `enable_speaker_info` + `ssd_version=200` | `with_speaker_info` + `speaker_count` |
| 适用场景 | 本地文件、实时流 | 已有 OSS/静态资源的批量离线 |
| TK-copilot 地址 | `ws://tkcopilot.tkitg.com/api/v3/sauc/*` | `http://tkcopilot.tkitg.com/api/v3/auc/bigmodel/submit` |
| Resource ID | `volc.*.sauc.*` | `volc.bigasr.auc` / `volc.bigasr.auc_turbo` |

---

## 9. 本项目接入要点

1. 当前 `step3_video_to_dialogue.py` 使用 **录音文件标准版（submit/query）**，需要 URL，**未接入流式 API**。
2. 若不想搭建内网文件服务器，建议改用 **`bigmodel_async` 流式接口**，读取 `output/audio/*.wav` 分包发送。
3. 需同步修改 `config.py` 中的 Resource ID 为流式 ASR 对应值。
4. 角色标注（代理人/客户）仍依赖 LLM 步骤，需单独申请 LLM 密钥。
5. 官方 Demo 下载：Python `sauc_python.zip`（见官方文档 Demo 章节）。

---

## 10. 向上级申请清单（速查）

### 必须申请

| 项目 | 说明 |
|------|------|
| **TK-copilot 密钥** | 格式 `llm-xxx`，填入 `X-Api-Access-Key` / `config.ASR_ACCESS_KEY` |
| **流式 ASR 服务开通** | 确认开通「大模型流式语音识别」，并告知 Resource ID |
| **LLM 密钥** | 用于对话角色区分，填入 `config.LLM_API_KEY` |

### 若继续用现有 submit/query 代码，额外需要

| 项目 | 说明 |
|------|------|
| **录音文件识别服务开通** | Resource ID：`volc.bigasr.auc` 或 `volc.bigasr.auc_turbo` |
| **内网可访问的音频 URL** | OSS 或静态服务器，ASR 服务端能下载 wav |

### 推荐问上级的原话

> 我需要做「视频转对话文本（含说话人分离）」验证，请帮忙开通：
> 1. TK-copilot 的 copilot 密钥（`llm-xxx`）
> 2. **大模型流式语音识别**（Resource ID 是 `volc.seedasr.sauc.duration` 还是 `volc.bigasr.sauc.duration`？）
> 3. LLM 对话接口密钥（qwen3.5-plus）
>
> 如果走流式 WebSocket 接口，音频可以本地直传，**不需要内网文件服务器**；如果走录音文件 submit/query 接口，则需要提供一个 ASR 能访问的音频 URL。
