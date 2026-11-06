# AstrBot Plugin: Breeze TTS 修复版

> 解决 [Breeze Blue](https://breezeblue.ai/) TTS（ElevenLabs 兼容）在 AstrBot 原生 Provider 中**尾部截断**的问题。支持非流式合成、自动语音概率控制、LLM Function Tool 调用、以及语音+文字双输出。

---

## 🪧 为什么要做这个插件？

AstrBot 原生的 Breeze TTS Provider 使用**流式接口**（边生成边接收），当 Breeze API 尾部音频数据还没吐完时，AstrBot 就开始往 QQ / Telegram / 微信等平台发送，导致**最后 1-2 个字被截断**。

本插件改用 **ElevenLabs 风格非流式 endpoint**（`POST /v1/text-to-speech/{voice_id}`），等 API 完整返回 MP3 再处理，从根本上消除截断。同时保留流式接口作为可选方案。

---

## ✨ 功能特性

| 功能 | 说明 |
|------|------|
| **彻底解决尾部截断** | 默认非流式调用，完整接收 MP3 后再发送 |
| **LLM Function Tool** | AI 在对话中主动调用 `speak_with_breeze` 工具发语音 |
| **自动语音概率** | 0-100% 概率自动把 AI 回复转语音（可与原生 TTS 共存） |
| **语音 + 文字双输出** | 可配置：同时发送语音和原文 / 仅发送语音替换文字 |
| **一键连通性测试** | `/breezettsset test` 直接测 API 配置是否正确 |
| **文本超长自动分段** | 按句号/问号切分，逐段合成后拼接 |
| **尾部填充** | 自动在文本末尾追加 `。` 防止边缘截断 |
| **跨平台支持** | aiocqhttp / telegram / wecom / lark / dingtalk / slack |

---

## 📦 安装

### 方式一：AstrBot 插件市场（推荐）

1. 下载最新 Release 的 `.zip` 包（文件名为 `astrbot_plugin_breeze_tts_fix.zip`）
2. 打开 AstrBot 管理面板 → **插件** → **从本地安装**
3. 上传 zip 文件，等待自动安装完成
4. 点「重载插件」

### 方式二：手动安装

1. 将 `astrbot_plugin_breeze_tts_fix/` 文件夹放入 AstrBot 的 `data/plugins/` 目录
2. 重启 AstrBot

### 前置依赖

插件仅依赖 `httpx>=0.27.0`，AstrBot 通常已内置。如需手动安装：

```bash
pip install httpx>=0.27.0
```

---

## ⚙️ 配置

安装完成后，在 AstrBot 管理面板 → **插件** → **Breeze TTS 修复版** → **配置**：

| 配置项 | 说明 | 默认值 |
|--------|------|--------|
| `enable` | 启用插件 | `true` |
| `api_key` | Breeze Blue API Key（**必填**） | - |
| `api_base_url` | API 基础地址 | `https://api.breeze.blue/v1` |
| `model_id` | 模型 ID | `breeze-tts-2` |
| `voice_id` | 音色 ID，如 `voc_vr49te5u3tp9`（**必填**） | - |
| `speed` | 语速倍率 0.5-2.0 | `1.0` |
| `style_instruction` | 风格/语气描述指令 | 空 |
| `auto_tts_probability` | 自动语音概率 0-100 | `50` |
| `dual_output` | 语音+文字同时发送 | `true` |
| `tail_padding` | 尾部填充字符 | `。` |
| `max_text_length` | 单次合成最大字符数 | `500` |
| `use_streaming` | 使用流式接口（不推荐） | `false` |

### ⚠️ 重要：关闭 AstrBot 原生 TTS

安装本插件后，请在 AstrBot 主设置 → **AI** → **启用文字转语音模型** → **关闭**。否则原生 TTS 和本插件会对同一条回复重复转语音。

### 📝 为什么 `speed` 和 `style_instruction` 暂时没生效？

Breeze Blue 的简化 TTS 接口只接受 `text` + `model_id` 两个字段，多传一个参数就返回 422。本插件保留了这两个配置项，等 Breeze Blue 开放 ElevenLabs 完整兼容接口后会自动生效。

---

## 📱 指令

| 指令 | 说明 |
|------|------|
| `/tts <文本>` | 手动把文本转语音发送 |
| `/breezettsset status` | 查看当前配置状态 |
| `/breezettsset test [文本]` | 一键测试 API 连通性，成功会直接发语音 |

### 示例

```
/tts 你好呀，今天天气真不错

/breezettsset status

/breezettsset test 测试一下，如果你听到了这段话说明插件正常
```

---

## 🤖 LLM Function Tool

插件会向 AstrBot 注册一个名为 `speak_with_breeze` 的函数工具，AI 可以在对话中主动调用。

### 工具签名

```json
{
  "name": "speak_with_breeze",
  "description": "将指定文本转换为语音消息并发送给用户。当用户明确要求说话（如「说一下」「用语音说」「念出来」），或你判断简短回复适合用语音传达时调用。不要对超过 200 字的文本调用。",
  "parameters": {
    "type": "object",
    "properties": {
      "text": { "type": "string", "description": "要转换为语音的文本" },
      "speed": { "type": "number", "minimum": 0.5, "maximum": 2.0 }
    },
    "required": ["text"]
  }
}
```

### 使用前提

- LLM Provider 必须支持 Function Calling（DeepSeek / GPT-4 / Claude 3 / 豆包 等都支持）
- `show_tool_use_status: true`（可选，在 AstrBot 配置中打开可看到工具调用过程）

### 触发示例

用户：`用语音说一下你好`

AI 内部：
1. 判断需要调用 `speak_with_breeze`
2. 提交 `{"text": "你好"}`
3. 插件合成语音并发送

---

## 🐛 FAQ

### Q: 语音最后还是有一点点截断？

A: 请先听一下 Breeze API 直接返回的 MP3 文件（用 `/breezettsset test` 发送的语音如果在 QQ 里截断了，可能是 QQ 平台本身对语音消息有上传截断）。

如果确认是 API 返回的音频本身有轻微截断，可以尝试：
1. 把 `tail_padding` 从 `。` 改成 `。。` 或 `~.~`
2. 在 AI 回复 System Prompt 里让 AI 结尾多说一个语气词

### Q: `/breezettsset test` 返回 404？

A: 检查 `api_base_url` 是否为 `https://api.breeze.blue/v1`，尾部不要多加 `/`。

### Q: AI 一直不调用 `speak_with_breeze` 工具？

A: 检查：
1. AstrBot 控制台是否有 `✅ LLM function tool 'speak_with_breeze' 已注册`
2. 你的 LLM 模型是否支持 Function Calling
3. 打开 `show_tool_use_status: true` 看工具调用状态
4. 在 System Prompt 里提醒 AI：「当用户要求语音回复时，调用 speak_with_breeze 工具」

### Q: 同时开了本插件和 MiMo TTS / GPT-SoVITS？

A: 没问题，它们是独立的 TTS Provider。建议只开一个，避免重复转语音浪费 API 调用。

---

## 📂 文件结构

```
astrbot_plugin_breeze_tts_fix/
├── main.py              # 插件主逻辑（事件钩子 + Function Tool + 指令）
├── metadata.yaml        # 插件元数据
├── _conf_schema.json    # WebUI 配置项定义
└── requirements.txt     # Python 依赖
```

---

## 🔧 技术细节

### API Endpoint

```
POST {api_base_url}/text-to-speech/{voice_id}
POST {api_base_url}/text-to-speech/{voice_id}/stream  (流式)

Body: {
  "text": "...",
  "model_id": "breeze-tts-2"
}
```

Breeze Blue 使用 ElevenLabs 风格路径，`voice_id` 必须在 URL path 中，不能在 body 里。

### 截断根因

| 调用方式 | AstrBot 原生 | 本插件 |
|----------|--------------|--------|
| Endpoint | `/audio/speech` (OpenAI 风格) ❌ 404 | `/text-to-speech/{voice_id}` (ElevenLabs 风格) ✅ |
| Body 字段 | `model`, `input`, `voice_id` | `text`, `model_id` |
| 接收方式 | 流式（边收边发，尾部截断） | 非流式（等完整 MP3） |

---

## 📄 License

MIT
