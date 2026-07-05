"""AstrBot plugin: AI-generated image detection via Sightengine.

Ponytail notes:
- 冷却: 单 dict {unified_msg_origin: last_ts}, 单线程 bot 不加锁
- 不缓存: 同一图几乎不会重复,缓存省 ops 但占内存,YAGNI
- per-generator 分数先不做: Sightengine 基础 API 不返回, 高级要联系销售
"""
import time
from typing import Optional, AsyncGenerator

import httpx
from astrbot.api.event import filter, AstrMessageEvent, MessageEventResult
from astrbot.api.star import Context, Star, register
from astrbot.api.event.filter import command, llm_tool
from astrbot.api import logger
from astrbot.core.message.components import Image, Reply


SE_API = "https://api.sightengine.com/1.0/check.json"


@register(
    "astrbot_plugin_ai_image_detect",
    "publieople",
    "AI 图像检测(Sightengine genai),支持命令与 LLM 工具调用",
    "v0.1.0",
    "https://github.com/publieople/astrbot_plugin_ai_image_detect",
)
class AIImageDetectPlugin(Star):
    def __init__(self, context: Context, config: Optional[dict] = None):
        super().__init__(context)
        self.config = config or {}
        self.api_user = self.config.get("api_user", "")
        self.api_secret = self.config.get("api_secret", "")
        self.threshold = float(self.config.get("ai_threshold", 0.5))
        self.cooldown = int(self.config.get("cooldown_seconds", 30))
        # ponytail: 单 dict, 单线程 bot 不加锁
        self._last_call: dict[str, float] = {}
        self._client: httpx.AsyncClient | None = None

    async def initialize(self):
        self._client = httpx.AsyncClient(timeout=30.0)

    async def terminate(self):
        if self._client:
            await self._client.aclose()

    def _extract_image_url(self, event: AstrMessageEvent) -> str | None:
        # ponytail: aiocqhttp 上 Image.file 是 file://, Image.url 才是 https CDN
        # Reply 组件包着原消息的 chain, 引用模式要从那里找
        def _first_url(comps):
            for c in comps or []:
                if isinstance(c, Image):
                    url = c.url or c.file or ""
                    if url.startswith(("http://", "https://")):
                        return url
            return None
        url = _first_url(event.get_messages())
        if url:
            return url
        for comp in event.get_messages():
            if isinstance(comp, Reply) and comp.chain:
                url = _first_url(comp.chain)
                if url:
                    return url
        return None

    def _check_cooldown(self, origin: str) -> tuple[bool, int]:
        """返回 (是否允许, 剩余冷却秒数)。"""
        now = time.time()
        last = self._last_call.get(origin, 0.0)
        if now - last < self.cooldown:
            return False, int(self.cooldown - (now - last)) + 1
        return True, 0

    def _update_cooldown(self, origin: str):
        self._last_call[origin] = time.time()

    async def _call_sightengine(self, image_url: str) -> dict:
        if not self.api_user or not self.api_secret:
            return {"ok": False, "error": "未配置 API user/secret,在插件配置里填"}
        params = {
            "models": "genai",
            "api_user": self.api_user,
            "api_secret": self.api_secret,
            "url": image_url,
        }
        try:
            r = await self._client.get(SE_API, params=params)
            if r.status_code != 200:
                return {"ok": False, "error": f"HTTP {r.status_code}: {r.text[:200]}"}
            data = r.json()
            if data.get("status") != "success":
                return {"ok": False, "error": f"API failure: {data}"}
            score = data.get("type", {}).get("ai_generated")
            if score is None:
                return {"ok": False, "error": f"返回里没有 ai_generated: {data}"}
            ops = data.get("request", {}).get("operations", "?")
            return {
                "ok": True,
                "score": float(score),
                "operations": ops,
                "raw": data,
            }
        except httpx.HTTPError as e:
            return {"ok": False, "error": f"网络错误: {e}"}

    def _format_verdict(self, result: dict) -> str:
        if not result["ok"]:
            return f"检测失败:{result['error']}"
        score = result["score"]
        verdict = "AI 生成" if score >= self.threshold else "疑似真人"
        pct = f"{score * 100:.1f}%"
        ops = result["operations"]
        return f"{verdict}(AI 概率 {pct},阈值 {self.threshold},消耗 {ops} ops)"

    @command("鉴图")
    async def cmd_detect(self, event: AstrMessageEvent) -> AsyncGenerator[MessageEventResult, None]:
        """鉴图 - 检测引用/最新消息中的图片是否为 AI 生成。"""
        origin = event.unified_msg_origin
        allowed, remain = self._check_cooldown(origin)
        if not allowed:
            yield event.plain_result(f"冷却中,还剩 {remain} 秒")
            return

        url = self._extract_image_url(event)
        if not url:
            yield event.plain_result("没找到图片 URL,发图片或引用含图片的消息")
            return

        self._update_cooldown(origin)
        result = await self._call_sightengine(url)
        yield event.plain_result(self._format_verdict(result))

    @llm_tool(name="ai_image_check")
    async def llm_check(self, event: AstrMessageEvent, image_url: str = ""):
        """检测一张图片是否为 AI 生成。

        Args:
            image_url(string): 要检测的图片 URL,留空则检测当前消息中的图片
        """
        origin = event.unified_msg_origin
        allowed, remain = self._check_cooldown(origin)
        if not allowed:
            return f"冷却中,还剩 {remain} 秒"

        if not image_url:
            image_url = self._extract_image_url(event) or ""
        if not image_url:
            return "没找到图片 URL,需要发图片或提供 image_url"

        self._update_cooldown(origin)
        result = await self._call_sightengine(image_url)
        if not result["ok"]:
            return f"检测失败:{result['error']}"
        score = result["score"]
        verdict = "AI 生成" if score >= self.threshold else "疑似真人"
        return f"{verdict}(AI 概率 {score * 100:.1f}%,阈值 {self.threshold})"
