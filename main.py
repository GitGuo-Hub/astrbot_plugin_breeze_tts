"""
Breeze TTS 修复版 AstrBot 插件
==============================
解决 Breeze Blue (ElevenLabs 兼容) TTS 流式返回导致的尾部截断问题。

功能：
- 非流式调用 API，彻底避免尾部截断
- 可配置语速倍率 (0.5x - 2.0x)
- 可配置风格/语气描述指令
- 自动语音概率控制 (0-100%)
- 文本超长自动分段合成
- 尾部填充防截断

使用：
1. 放入 AstrBot data/plugins/ 目录
2. 在 WebUI 插件配置中填写 API Key / Voice ID
3. 在 AstrBot 主设置里禁用原生 TTS（避免重复转语音）
"""

import os
import re
import random
import tempfile
from typing import Optional

import httpx
from pydantic.dataclasses import dataclass

from astrbot.api import logger
from astrbot.api.event import filter, AstrMessageEvent, EventResultType
from astrbot.api.star import Context, Star
from astrbot.core.agent.run_context import ContextWrapper
from astrbot.core.agent.tool import FunctionTool, ToolExecResult
from astrbot.core.message.components import Record as RecordComp

try:
    from astrbot.api.message_components import Plain, Record
except Exception:
    Plain = None
    Record = None


# ---------------------------------------------------------------------------
# 文本处理工具
# ---------------------------------------------------------------------------

_SPLIT_PATTERN = re.compile(r"(?<=[。！？!?\n])\s*")


def split_text(text: str, max_len: int) -> list[str]:
    """将长文本按句号/问号等语义边界切分，每段不超过 max_len 字符。"""
    if not text:
        return []
    text = text.strip()
    if len(text) <= max_len:
        return [text]

    sentences = [s for s in _SPLIT_PATTERN.split(text) if s.strip()]
    if not sentences:
        sentences = [text]

    chunks: list[str] = []
    current = ""
    for sent in sentences:
        if len(sent) > max_len:
            for i in range(0, len(sent), max_len):
                chunk = sent[i : i + max_len]
                if current:
                    chunks.append(current)
                    current = ""
                chunks.append(chunk)
        elif len(current) + len(sent) <= max_len:
            current += sent
        else:
            if current:
                chunks.append(current)
            current = sent
    if current:
        chunks.append(current)
    return chunks


# ---------------------------------------------------------------------------
# Breeze TTS 客户端
# ---------------------------------------------------------------------------

class BreezeTTSClient:
    """封装 Breeze Blue TTS API 的非流式调用。"""

    def __init__(
        self,
        api_key: str,
        api_base_url: str = "https://api.breeze.blue/v1",
        model_id: str = "breeze-tts-2",
        voice_id: str = "",
    ):
        self.api_key = api_key
        self.api_base_url = api_base_url.rstrip("/")
        self.model_id = model_id
        self.voice_id = voice_id

    async def synthesize(
        self,
        text: str,
        speed: float = 1.0,
        style_instruction: str = "",
        use_streaming: bool = False,
    ) -> bytes:
        """合成单段文本为音频 bytes。默认非流式以防截断。

        使用 Breeze Blue 的 ElevenLabs 兼容 endpoint：
          POST /v1/text-to-speech/{voice_id}       (非流式)
          POST /v1/text-to-speech/{voice_id}/stream (流式)

        body 只接受 text + model_id（Breeze 简化接口）。
        speed / style_instruction 暂不传递，等 Breeze 支持后再开启。
        """
        if not text.strip():
            return b""
        if not self.voice_id:
            raise RuntimeError("Voice ID 未配置")

        # ElevenLabs 风格路径：voice_id 在 URL path 里
        suffix = "/stream" if use_streaming else ""
        url = f"{self.api_base_url}/text-to-speech/{self.voice_id}{suffix}"

        # Breeze 简化接口只接受 text + model_id，其余字段会 422
        payload = {
            "text": text,
            "model_id": self.model_id,
        }

        headers = {
            "Authorization": f"Bearer {self.api_key}",
            "Content-Type": "application/json",
        }

        logger.debug(
            f"[BreezeTTS] POST text-to-speech{'/stream' if use_streaming else ''}, "
            f"text_len={len(text)}"
        )

        async with httpx.AsyncClient(timeout=60.0) as client:
            if use_streaming:
                collected = bytearray()
                async with client.stream(
                    "POST", url, json=payload, headers=headers
                ) as resp:
                    if resp.status_code != 200:
                        err = await resp.aread()
                        raise RuntimeError(
                            f"Breeze API 流式错误 {resp.status_code}: {err[:300]}"
                        )
                    async for chunk in resp.aiter_bytes(chunk_size=8192):
                        collected.extend(chunk)
                return bytes(collected)
            else:
                resp = await client.post(url, json=payload, headers=headers)
                if resp.status_code != 200:
                    raise RuntimeError(
                        f"Breeze API 错误 {resp.status_code}: {resp.text[:300]}"
                    )
                return resp.content


# ---------------------------------------------------------------------------
# LLM Function Tool（让 AI 自己决定什么时候发语音）
# ---------------------------------------------------------------------------

@dataclass
class SpeakWithBreezeTool(FunctionTool):
    """AI 可用的语音合成函数工具。

    AI 在对话中主动调用此工具，把文本转成语音发给用户。
    """

    __pydantic_config__ = {"arbitrary_types_allowed": True}

    name: str = "speak_with_breeze"
    description: str = (
        "将指定文本转换为语音消息并发送给用户。"
        "当用户明确要求你说话（如「说一下」「用语音说」「发音」「念出来」），"
        "或者你判断一段简短的回复适合用语音传达时（如打招呼、告别、简短确认），"
        "请调用此工具。不要对太长的文本（超过 200 字）调用。"
    )
    parameters: dict = None  # 运行时由 __post_init__ 填充
    plugin: object = None    # 引用 BreezeTTSFixPlugin 实例

    def __post_init__(self):
        self.parameters = {
            "type": "object",
            "properties": {
                "text": {
                    "type": "string",
                    "description": "要转换为语音的文本。建议控制在 200 字以内以获得最佳效果。",
                },
                "speed": {
                    "type": "number",
                    "description": "语速倍率，0.5 到 2.0 之间。1.0 是正常速度。不填则使用插件默认值。",
                    "minimum": 0.5,
                    "maximum": 2.0,
                },
            },
            "required": ["text"],
        }

    async def call(
        self,
        context: ContextWrapper,
        text: str,
        speed: float | None = None,
    ) -> ToolExecResult:
        """执行语音合成并发送。"""
        text = (text or "").strip()
        if not text:
            return "语音合成失败：文本为空"

        if self.plugin is None or not getattr(self.plugin, "_client", None):
            return "语音合成失败：TTS 服务未配置。请先在插件设置中填写 API Key 和 Voice ID。"

        try:
            voice_bytes = await self.plugin._synthesize_full(text)
        except Exception as e:
            logger.error(f"[BreezeTTS] FunctionTool 合成失败: {e}")
            return f"语音合成失败：{e}"

        if not voice_bytes:
            return "语音合成失败：结果为空"

        temp_path = self.plugin._save_temp_mp3(voice_bytes)
        if not temp_path:
            return "语音合成失败：临时文件写入失败"

        # 通过 StarTools 发语音，注意必须传 MessageChain 对象
        try:
            event = context.context.event
            session = event.unified_msg_origin
            from astrbot.api.star import StarTools
            from astrbot.core.message.message_event_result import MessageChain
            chain = MessageChain(chain=[RecordComp(file=temp_path)])
            ok = await StarTools.send_message(session, chain)
            if ok:
                logger.info(f"[BreezeTTS] FunctionTool 已发语音: {len(voice_bytes)} bytes")
                return f"语音已发送（{len(text)} 字，{len(voice_bytes)} bytes）"
            else:
                return f"语音合成成功但消息发送失败（平台不支持）"
        except Exception as e:
            logger.error(f"[BreezeTTS] FunctionTool 发送失败: {e}", exc_info=True)
            return f"语音合成成功但发送失败：{e}"


# ---------------------------------------------------------------------------
# 插件主体
# ---------------------------------------------------------------------------

class BreezeTTSFixPlugin(Star):
    """Breeze TTS 修复版插件。"""

    def __init__(self, context: Context, config: dict):
        super().__init__(context)
        self._cfg = config or {}
        self._client: Optional[BreezeTTSClient] = None
        self._llm_tool_registered = False
        self._rebuild_client()
        self._register_llm_tool()
        logger.info("[BreezeTTS] 插件已加载，自动语音概率 = "
                     f"{self._cfg.get('auto_tts_probability', 0)}%")

    def _register_llm_tool(self):
        """向 AstrBot 注册 LLM function tool。"""
        if self._llm_tool_registered:
            return
        try:
            tool = SpeakWithBreezeTool(plugin=self)
            self.context.add_llm_tools(tool)
            self._llm_tool_registered = True
            logger.info("[BreezeTTS] ✅ LLM function tool 'speak_with_breeze' 已注册")
        except Exception as e:
            logger.error(f"[BreezeTTS] ❌ 注册 LLM tool 失败: {e}", exc_info=True)

    def _rebuild_client(self):
        """根据最新配置重建 TTS 客户端。"""
        api_key = self._cfg.get("api_key", "").strip()
        if not api_key:
            self._client = None
            return
        self._client = BreezeTTSClient(
            api_key=api_key,
            api_base_url=self._cfg.get("api_base_url", "https://api.breeze.blue/v1"),
            model_id=self._cfg.get("model_id", "breeze-tts-2"),
            voice_id=self._cfg.get("voice_id", ""),
        )

    def _read_cfg(self) -> dict:
        return self._cfg

    # ------------------------------------------------------------------
    # 事件钩子：拦截 AI 回复
    # ------------------------------------------------------------------

    @filter.on_decorating_result()
    async def on_ai_reply(self, event: AstrMessageEvent):
        """在 AI 回复被发送前拦截，根据概率决定是否转语音。"""
        cfg = self._read_cfg()
        if not cfg.get("enable", True):
            return
        if not self._client:
            return

        result = event.get_result()
        if result is None:
            return

        # 只处理 AI 生成的 LLM 回复
        is_llm = getattr(result, "is_llm_result", None)
        if callable(is_llm) and not is_llm():
            return

        chain = getattr(result, "chain", None)
        if not chain:
            return

        text = self._extract_plain_text_chain(chain)
        if not text:
            return

        # 概率判断
        prob = cfg.get("auto_tts_probability", 0)
        if prob <= 0:
            return
        if random.uniform(0, 100) >= prob:
            return

        logger.info(f"[BreezeTTS] 命中自动语音概率 ({prob}%)，正在合成...")
        try:
            voice_bytes = await self._synthesize_full(text)
        except Exception as e:
            logger.error(f"[BreezeTTS] 合成失败: {e}")
            return

        if voice_bytes:
            temp_path = self._save_temp_mp3(voice_bytes)
            if temp_path and Record is not None:
                dual_output = cfg.get("dual_output", True)
                if dual_output:
                    # 双输出：保留原文字 + 追加语音
                    from astrbot.api.message_components import Plain as PlainComp
                    # 把原来的纯文本组件保留，过滤掉已有 Record
                    new_chain = []
                    for comp in chain:
                        if getattr(comp, "type", "") != "record":
                            new_chain.append(comp)
                    new_chain.append(RecordComp(file=temp_path))
                    result.chain = new_chain
                    logger.info(f"[BreezeTTS] 双输出（文字+语音）已生成，大小 {len(voice_bytes)} bytes")
                else:
                    # 单输出：替换文字为语音
                    result.chain = [Record(file=temp_path)]
                    logger.info(f"[BreezeTTS] 单输出（仅语音）已生成，大小 {len(voice_bytes)} bytes")

    # ------------------------------------------------------------------
    # 指令：手动触发 /tts
    # ------------------------------------------------------------------

    @filter.command("tts")
    async def tts_command(self, event: AstrMessageEvent, message: str = ""):
        """手动触发 TTS：/tts 你好，今天天气不错"""
        cfg = self._read_cfg()
        if not cfg.get("enable", True):
            yield event.plain_result("插件未启用，请先在 WebUI 中开启 Breeze TTS 修复版。")
            return
        if not self._client:
            yield event.plain_result("请先在 WebUI 中配置 Breeze API Key 和 Voice ID。")
            return

        text = message.strip() or event.message_str.strip()
        if text.startswith("/tts"):
            text = text[4:].strip()
        if not text:
            yield event.plain_result("请提供要转语音的文本，例如：/tts 你好")
            return

        try:
            yield event.plain_result(f"🎙️ 正在合成语音 ({len(text)} 字)...")
            voice_bytes = await self._synthesize_full(text)
        except Exception as e:
            logger.error(f"[BreezeTTS] /tts 合成失败: {e}")
            yield event.plain_result(f"❌ 合成失败: {e}")
            return

        if voice_bytes and Record is not None:
            temp_path = self._save_temp_mp3(voice_bytes)
            if temp_path:
                yield event.make_result().message([Record(file=temp_path)])
        else:
            yield event.plain_result("❌ 合成结果为空或 Record 组件不可用")

    @filter.command_group("breezettsset")
    def breezettsset(self):
        """Breeze TTS 插件设置指令组"""
        pass

    @breezettsset.command("status")
    async def cmd_status(self, event: AstrMessageEvent):
        """查看插件当前配置状态"""
        cfg = self._read_cfg()
        enabled = cfg.get("enable", True)
        prob = cfg.get("auto_tts_probability", 0)
        speed = cfg.get("speed", 1.0)
        vi = cfg.get("voice_id", "(未设置)" if not cfg.get("voice_id") else "已配置")
        key_ok = "已配置" if cfg.get("api_key") else "未配置"
        model = cfg.get("model_id", "breeze-tts-2")
        streaming = cfg.get("use_streaming", False)
        instruction = cfg.get("style_instruction", "") or "(未设置)"

        msg = (
            "🎙️ Breeze TTS 修复版 状态\n"
            "────────────────────\n"
            f"启用开关: {'✅' if enabled else '❌'}\n"
            f"API Key: {key_ok}\n"
            f"Voice ID: {vi}\n"
            f"模型: {model}\n"
            f"语速: {speed}x (⚠️ Breeze 暂不支持此参数)\n"
            f"风格指令: {instruction[:40]}{'…' if len(instruction) > 40 else ''} (⚠️ Breeze 暂不支持)\n"
            f"自动语音概率: {prob}%\n"
            f"流式接口: {'⚠️ 开启（可能截断）' if streaming else '✅ 关闭（推荐）'}\n"
            f"尾部填充: '{cfg.get('tail_padding', '。')}'\n"
            f"最大分段长度: {cfg.get('max_text_length', 500)}\n"
            f"LLM Tool: {'✅ speak_with_breeze 已注册' if self._llm_tool_registered else '❌ 未注册'}\n"
            "\n💡 输入 /breezettsset test 快速测试 API 连通性"
        )
        yield event.plain_result(msg)

    @breezettsset.command("test")
    async def cmd_test(self, event: AstrMessageEvent, message: str = ""):
        """一键测试 Breeze API 连通性。

        用法：/breezettsset test           ← 用默认文字测试
              /breezettsset test 测试一下  ← 用自定义文字测试
        """
        cfg = self._read_cfg()

        # 1. 配置检查
        yield event.plain_result("🔍 正在检查配置...")
        missing = []
        if not cfg.get("api_key", "").strip():
            missing.append("API Key")
        if not cfg.get("voice_id", "").strip():
            missing.append("Voice ID")
        if missing:
            yield event.plain_result(
                f"❌ 配置不完整！缺少: {', '.join(missing)}\n"
                f"请先在 WebUI 插件设置中填写。"
            )
            return

        yield event.plain_result(
            f"✅ 配置完整\n"
            f"   API: {cfg.get('api_base_url')}\n"
            f"   Voice: {cfg.get('voice_id')}\n"
            f"   Model: {cfg.get('model_id')}\n"
            f"   正在请求 API..."
        )

        # 2. 准备测试文本
        test_text = (message or "你好，这是一段来自 Breeze TTS 的测试语音，如果你听到了这段话，说明插件一切正常！").strip()
        if test_text.startswith("test"):
            test_text = test_text[4:].strip()
        if not test_text:
            test_text = "你好，这是一段来自 Breeze TTS 的测试语音。"

        # 3. 调 API
        try:
            yield event.plain_result(f"🎙️ 请求合成 ({len(test_text)} 字)...")
            voice_bytes = await self._synthesize_full(test_text)
        except Exception as e:
            logger.error(f"[BreezeTTS] 测试失败: {e}")
            # 识别常见错误类型
            err_str = str(e)
            hint = ""
            if "401" in err_str or "403" in err_str:
                hint = "\n💡 可能原因: API Key 无效或已过期"
            elif "404" in err_str:
                hint = "\n💡 可能原因: API 地址不对（应为 https://api.breeze.blue/v1）或 Voice ID 不存在"
            elif "422" in err_str:
                hint = "\n💡 可能原因: 请求参数格式不对"
            elif "500" in err_str:
                hint = "\n💡 可能原因: Breeze 服务端临时故障"
            yield event.plain_result(f"❌ API 调用失败: {err_str}{hint}")
            return

        if not voice_bytes:
            yield event.plain_result("❌ API 返回空音频")
            return

        # 4. 发送结果
        size_kb = len(voice_bytes) / 1024
        yield event.plain_result(f"✅ 合成成功！音频大小 {size_kb:.1f} KB，正在发送...")

        temp_path = self._save_temp_mp3(voice_bytes)
        if temp_path and Record is not None:
            yield event.make_result().message([Record(file=temp_path)])
        else:
            yield event.plain_result("⚠️ 音频写入临时文件失败，但 API 连通性确认正常")

    # ------------------------------------------------------------------
    # 核心合成逻辑
    # ------------------------------------------------------------------

    async def _synthesize_full(self, text: str) -> bytes:
        """合成整段文本，自动分段 + 尾部填充。"""
        cfg = self._read_cfg()
        if not self._client:
            return b""

        tail = cfg.get("tail_padding", "。") or ""
        text = text + tail

        max_len = cfg.get("max_text_length", 500)
        chunks = split_text(text, max_len)
        if not chunks:
            return b""

        speed = cfg.get("speed", 1.0)
        instruction = cfg.get("style_instruction", "")
        use_streaming = cfg.get("use_streaming", False)

        parts: list[bytes] = []
        for i, chunk in enumerate(chunks):
            logger.debug(f"[BreezeTTS] 分段 {i+1}/{len(chunks)}: {chunk[:30]}...")
            audio = await self._client.synthesize(
                text=chunk,
                speed=speed,
                style_instruction=instruction,
                use_streaming=use_streaming,
            )
            if audio:
                parts.append(audio)

        return b"".join(parts)

    # ------------------------------------------------------------------
    # 工具方法
    # ------------------------------------------------------------------

    @staticmethod
    def _extract_plain_text_chain(chain) -> str:
        """从 result.chain 中提取纯文本。"""
        try:
            texts = []
            for comp in chain:
                t = getattr(comp, "text", None) or getattr(comp, "content", None)
                if t:
                    texts.append(str(t))
            return "\n".join(texts).strip()
        except Exception:
            return ""

    @staticmethod
    def _save_temp_mp3(data: bytes) -> Optional[str]:
        """将音频 bytes 写入临时文件。"""
        if not data:
            return None
        try:
            tmp_dir = tempfile.gettempdir()
            path = os.path.join(tmp_dir, f"breeze_tts_{os.getpid()}_{id(data)}.mp3")
            with open(path, "wb") as f:
                f.write(data)
            return path
        except Exception as e:
            logger.error(f"[BreezeTTS] 写入临时文件失败: {e}")
            return None

    async def terminate(self):
        logger.info("[BreezeTTS] 插件已卸载")
