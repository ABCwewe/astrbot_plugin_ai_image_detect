"""AstrBot plugin: AI-generated image detection via Sightengine.

Ponytail notes:
- 冷却: 单 dict {unified_msg_origin: last_ts}, 单线程 bot 不加锁
- 不缓存: 同一图几乎不会重复,缓存省 ops 但占内存,YAGNI
- per-generator 分数先不做: Sightengine 基础 API 不返回, 高级要联系销售
"""
import asyncio
import io
import os
import time
from typing import Optional, AsyncGenerator

import aiofiles
import httpx
from PIL import Image as PILImage
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
    "v0.1.1",
    "https://github.com/ABCwewe/astrbot_plugin_ai_image_detect",
)
class AIImageDetectPlugin(Star):
    def __init__(self, context: Context, config: Optional[dict] = None):
        super().__init__(context)
        self.config = config or {}
        self.api_user = self.config.get("api_user", "")
        self.api_secret = self.config.get("api_secret", "")
        self.threshold = float(self.config.get("ai_threshold", 0.5))
        self.cooldown = int(self.config.get("cooldown_seconds", 30))
        self._skip_animated_override: Optional[bool] = None
        self._detect_animated_frames_override: Optional[bool] = None
        # ponytail: 单 dict, 单线程 bot 不加锁
        self._last_call: dict[str, float] = {}
        self._client: httpx.AsyncClient | None = None

    @property
    def skip_animated(self) -> bool:
        if self._skip_animated_override is not None:
            return self._skip_animated_override
        return bool(
            self.config.get(
                "skip_animated",
                self.config.get("ignore_animated", self.config.get("skip_gif", False)),
            )
        )

    @skip_animated.setter
    def skip_animated(self, value: bool):
        self._skip_animated_override = bool(value)

    @property
    def detect_animated_frames(self) -> bool:
        if self._detect_animated_frames_override is not None:
            return self._detect_animated_frames_override
        return bool(
            self.config.get(
                "detect_animated_frames",
                self.config.get(
                    "sample_animated_frames",
                    self.config.get("extract_animated_frames", False),
                ),
            )
        )

    @detect_animated_frames.setter
    def detect_animated_frames(self, value: bool):
        self._detect_animated_frames_override = bool(value)

    async def initialize(self):
        self._client = httpx.AsyncClient(timeout=30.0)

    async def terminate(self):
        if self._client:
            await self._client.aclose()

    def _extract_image_path(self, event: AstrMessageEvent) -> str | None:
        # ponytail: AstrBot 把图下载到 data/temp/, Image.file/url/path 全是本地路径
        # 返回 path 字段(本地绝对路径)或 file 字段,供 _call_sightengine 上传
        def _scan(comps):
            for c in comps or []:
                if isinstance(c, Image):
                    return c.path or c.file or ""
            return None
        src = _scan(event.get_messages())
        if src and os.path.isfile(src):
            return src
        for comp in event.get_messages():
            if isinstance(comp, Reply) and getattr(comp, "chain", None):
                src = _scan(comp.chain)
                if src and os.path.isfile(src):
                    return src
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

    async def _is_animated_image(self, image_src: str) -> bool:
        """判断目标图片是否为动图（GIF、动态 WebP/APNG 等）。"""
        if not image_src:
            return False

        try:
            # 常见后缀快路径
            clean_url = image_src.split("?")[0].lower()
            if clean_url.endswith(".gif"):
                return True

            if image_src.startswith(("http://", "https://")):
                if not self._client:
                    return False
                resp = await self._client.get(image_src)
                if resp.status_code != 200:
                    return False
                content_type = resp.headers.get("content-type", "").lower()
                if "image/gif" in content_type:
                    return True
                content = resp.content

                def _check_bytes() -> bool:
                    with PILImage.open(io.BytesIO(content)) as img:
                        return bool(
                            getattr(img, "is_animated", False)
                            or getattr(img, "n_frames", 1) > 1
                            or (getattr(img, "format", "") or "").upper() == "GIF"
                        )

                return await asyncio.to_thread(_check_bytes)
            else:
                if not os.path.isfile(image_src):
                    return False

                def _check_file() -> bool:
                    with PILImage.open(image_src) as img:
                        return bool(
                            getattr(img, "is_animated", False)
                            or getattr(img, "n_frames", 1) > 1
                            or (getattr(img, "format", "") or "").upper() == "GIF"
                        )

                return await asyncio.to_thread(_check_file)
        except Exception as e:
            logger.warning(f"动图判断失败: {e}")
            return False

    async def _extract_first_and_mid_frames(self, image_src: str) -> list[tuple[str, bytes]]:
        """提取动图的首帧和中间帧静帧数据。返回 [(帧描述, bytes), ...]"""
        if not image_src:
            return []

        try:
            raw_bytes: bytes | None = None
            if image_src.startswith(("http://", "https://")):
                if not self._client:
                    return []
                resp = await self._client.get(image_src)
                if resp.status_code != 200:
                    return []
                raw_bytes = resp.content
            else:
                if not os.path.isfile(image_src):
                    return []
                async with aiofiles.open(image_src, "rb") as f:
                    raw_bytes = await f.read()

            if not raw_bytes:
                return []

            def _frame_to_jpeg_bytes(frame: PILImage.Image) -> bytes:
                buf = io.BytesIO()
                if frame.mode in ("RGBA", "LA") or (frame.mode == "P" and "transparency" in frame.info):
                    frame_rgba = frame.convert("RGBA")
                    bg = PILImage.new("RGBA", frame_rgba.size, (255, 255, 255))
                    composite = PILImage.alpha_composite(bg, frame_rgba)
                    composite.convert("RGB").save(buf, format="JPEG", quality=92)
                else:
                    frame.convert("RGB").save(buf, format="JPEG", quality=92)
                return buf.getvalue()

            def _extract() -> list[tuple[str, bytes]]:
                frames_data: list[tuple[str, bytes]] = []
                with PILImage.open(io.BytesIO(raw_bytes)) as img:
                    n_frames = getattr(img, "n_frames", 1)
                    if n_frames <= 1:
                        # 单帧动图
                        return [("首帧", _frame_to_jpeg_bytes(img))]

                    first_idx = 0
                    mid_idx = n_frames // 2

                    # 第一帧
                    img.seek(first_idx)
                    frames_data.append(("第1帧", _frame_to_jpeg_bytes(img)))

                    # 中间帧
                    img.seek(mid_idx)
                    frames_data.append((f"中间帧(第{mid_idx + 1}帧)", _frame_to_jpeg_bytes(img)))

                return frames_data

            return await asyncio.to_thread(_extract)
        except Exception as e:
            logger.warning(f"动图静帧提取失败: {e}")
            return []

    async def _detect_animated_frames_verdict(self, frames: list[tuple[str, bytes]]) -> str:
        """对抽取的静帧分别请求 API 并聚合置信度。"""
        tasks = [
            self._call_sightengine(frame_bytes, filename=f"frame_{i}.jpg")
            for i, (_, frame_bytes) in enumerate(frames)
        ]
        results = await asyncio.gather(*tasks)

        succeeded = [
            (desc, res) for (desc, _), res in zip(frames, results) if res.get("ok")
        ]
        if not succeeded:
            errors = "; ".join(
                f"{desc}:{res.get('error')}"
                for (desc, _), res in zip(frames, results)
            )
            return f"动图静帧检测失败: {errors}"

        total_score = sum(res["score"] for _, res in succeeded)
        avg_score = total_score / len(succeeded)

        total_ops = 0
        for _, res in succeeded:
            try:
                total_ops += int(res.get("operations", 5))
            except (ValueError, TypeError):
                total_ops += 5

        verdict = "AI 生成" if avg_score >= self.threshold else "人类制作"
        avg_pct = f"{avg_score * 100:.1f}%"
        frame_details = ", ".join(f"{desc} {res['score'] * 100:.1f}%" for desc, res in succeeded)

        if len(succeeded) < len(frames):
            return (
                f"{verdict}(动图抽帧聚合 AI 概率 {avg_pct}, {frame_details}, "
                f"阈值 {self.threshold}, 消耗 {total_ops} ops, 部分静帧检测失败)"
            )

        return (
            f"{verdict}(动图抽帧聚合 AI 概率 {avg_pct}, {frame_details}, "
            f"阈值 {self.threshold}, 消耗 {total_ops} ops)"
        )

    async def _call_sightengine(self, image_src: str | bytes, filename: str = "image.jpg") -> dict:
        # ponytail: 本地文件/内存字节走 multipart 上传, 远程 URL 走 GET
        if not self.api_user or not self.api_secret:
            return {"ok": False, "error": "未配置 API user/secret,在插件配置里填"}
        data = {"models": "genai", "api_user": self.api_user, "api_secret": self.api_secret}
        try:
            if isinstance(image_src, bytes):
                r = await self._client.post(SE_API, data=data, files={"media": (filename, image_src)})
            elif image_src.startswith(("http://", "https://")):
                r = await self._client.get(SE_API, params={**data, "url": image_src})
            else:
                async with aiofiles.open(image_src, "rb") as f:
                    content = await f.read()
                r = await self._client.post(SE_API, data=data, files={"media": (os.path.basename(image_src), content)})
            if r.status_code != 200:
                return {"ok": False, "error": f"HTTP {r.status_code}: {r.text[:200]}"}
            resp = r.json()
            if resp.get("status") != "success":
                return {"ok": False, "error": f"API failure: {resp}"}
            score = resp.get("type", {}).get("ai_generated")
            if score is None:
                return {"ok": False, "error": f"返回里没有 ai_generated: {resp}"}
            return {"ok": True, "score": float(score), "operations": resp.get("request", {}).get("operations", "?")}
        except (httpx.HTTPError, OSError) as e:
            return {"ok": False, "error": f"{type(e).__name__}: {e}"}

    def _format_verdict(self, result: dict) -> str:
        if not result["ok"]:
            return f"检测失败:{result['error']}"
        score = result["score"]
        verdict = "AI 生成" if score >= self.threshold else "人类制作"
        pct = f"{score * 100:.1f}%"
        ops = result["operations"]
        return f"{verdict}(AI 概率 {pct},阈值 {self.threshold},消耗 {ops} ops)"

    @command("鉴图")
    async def cmd_detect(self, event: AstrMessageEvent) -> AsyncGenerator[MessageEventResult, None]:
        """鉴图 - 检测引用/最新消息中的图片是否为 AI 生成。"""
        # 支持通过指令快捷切换动图屏蔽与抽帧开关
        msg_str = event.get_message_str().strip()
        parts = msg_str.split()
        if len(parts) >= 2 and parts[1] in ("动图", "动图检测", "动图屏蔽", "抽帧", "动图抽帧"):
            sub_cmd = parts[1]
            if sub_cmd in ("抽帧", "动图抽帧"):
                if len(parts) >= 3 and parts[2].lower() in ("开", "开启", "on", "enable", "true", "1"):
                    self.detect_animated_frames = True
                    yield event.plain_result("已开启动图抽帧检测：遇到动图将截取首帧与中间帧分别检测并发送聚合置信度")
                    return
                elif len(parts) >= 3 and parts[2].lower() in ("关", "关闭", "off", "disable", "false", "0"):
                    self.detect_animated_frames = False
                    yield event.plain_result("已关闭动图抽帧检测")
                    return
                else:
                    status_str = "已开启（首帧+中间帧聚合）" if self.detect_animated_frames else "已关闭"
                    yield event.plain_result(f"当前动图抽帧检测状态：{status_str}\n可使用指令切换：/鉴图 抽帧 开 或 /鉴图 抽帧 关")
                    return
            else:
                if len(parts) >= 3 and parts[2].lower() in ("开", "开启", "on", "enable", "true", "1"):
                    self.skip_animated = True
                    yield event.plain_result("已开启动图屏蔽：后续将跳过动图检测")
                    return
                elif len(parts) >= 3 and parts[2].lower() in ("关", "关闭", "off", "disable", "false", "0"):
                    self.skip_animated = False
                    yield event.plain_result("已关闭动图屏蔽：后续将正常检测动图")
                    return
                else:
                    skip_status = "已开启（跳过动图检测）" if self.skip_animated else "已关闭"
                    frame_status = "已开启（首帧+中间帧聚合）" if self.detect_animated_frames else "已关闭"
                    yield event.plain_result(
                        f"当前动图设置状态：\n"
                        f"- 动图屏蔽 (skip_animated): {skip_status}\n"
                        f"- 抽帧检测 (detect_animated_frames): {frame_status}\n"
                        f"指令切换：\n"
                        f"- /鉴图 动图 开/关 (屏蔽动图)\n"
                        f"- /鉴图 抽帧 开/关 (抽帧聚合检测)"
                    )
                    return

        origin = event.unified_msg_origin
        allowed, remain = self._check_cooldown(origin)
        if not allowed:
            yield event.plain_result(f"冷却中,还剩 {remain} 秒")
            return

        src = self._extract_image_path(event)
        if not src:
            yield event.plain_result("没找到图片,发图片或引用含图片的消息")
            return

        is_animated = await self._is_animated_image(src)
        if is_animated:
            if self.detect_animated_frames:
                self._update_cooldown(origin)
                frames = await self._extract_first_and_mid_frames(src)
                if not frames:
                    yield event.plain_result("动图抽帧失败，无法解析静帧")
                    return
                verdict_str = await self._detect_animated_frames_verdict(frames)
                yield event.plain_result(verdict_str)
                return
            elif self.skip_animated:
                yield event.plain_result("检测到动图，已跳过检测")
                return

        self._update_cooldown(origin)
        result = await self._call_sightengine(src)
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
            image_url = self._extract_image_path(event) or ""
        if not image_url:
            return "没找到图片 URL,需要发图片或提供 image_url"

        is_animated = await self._is_animated_image(image_url)
        if is_animated:
            if self.detect_animated_frames:
                self._update_cooldown(origin)
                frames = await self._extract_first_and_mid_frames(image_url)
                if not frames:
                    return "动图抽帧失败，无法解析静帧"
                return await self._detect_animated_frames_verdict(frames)
            elif self.skip_animated:
                return "检测到动图，已跳过检测"

        self._update_cooldown(origin)
        result = await self._call_sightengine(image_url)
        if not result["ok"]:
            return f"检测失败:{result['error']}"
        score = result["score"]
        verdict = "AI 生成" if score >= self.threshold else "人类制作"
        return f"{verdict}(AI 概率 {score * 100:.1f}%,阈值 {self.threshold})"
