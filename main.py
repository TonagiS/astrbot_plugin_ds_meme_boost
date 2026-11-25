"""ds 表情包加权收录：看图的**内容**，不靠消息文本。

流程（每条带图群消息最多一张）：
1. 过滤尺寸：文件 ≤ max_kb（默认 2048KB）
2. 视觉判定：问 flash「是不是大肥鱼/鲸鱼娘设定（蓝发女仆、头鳍、鲸尾）的表情包/梗图」→ YES/NO
3. YES 再按 probability（默认 0.9）掷骰 → 命中就调 stealer 的 `_steal_sticker_impl` 入库

限流：每群每小时最多判 max_judge_per_hour 张（视觉调用有成本），两次判定至少隔 cooldown_seconds。
"""

from __future__ import annotations

import random
import time
from pathlib import Path

import aiohttp

from astrbot.api import AstrBotConfig, logger
from astrbot.api.event import AstrMessageEvent, filter
from astrbot.api.message_components import Image
from astrbot.api.platform import MessageType
from astrbot.api.star import Context, Star

PLUGIN_DIR = Path(__file__).resolve().parent
LOG_PATH = PLUGIN_DIR / "data" / "ds_meme_boost.log"

JUDGE_PROMPT = (
    "看图回答，只输出一行：\n"
    "1) 这是不是“大肥鱼 / 鲸鱼娘”这类设定的角色（蓝色头发、女仆装、头顶鳍、鲸鱼尾巴之一）？\n"
    "2) 它像不像表情包/梗图（Q 版、抠图、带字、表情包常见裁切）？\n"
    "两项都满足回答 YES，否则 NO。然后空一格写不超过 12 字的画面描述。\n"
    "格式：YES/NO 描述"
)


def _log(msg: str) -> None:
    try:
        LOG_PATH.parent.mkdir(parents=True, exist_ok=True)
        if LOG_PATH.exists() and LOG_PATH.stat().st_size > 1024 * 1024:
            LOG_PATH.write_text("", encoding="utf-8")
        with LOG_PATH.open("a", encoding="utf-8") as fh:
            fh.write(f"[{time.strftime('%m-%d %H:%M:%S')}] {msg}\n")
    except Exception:  # noqa: BLE001
        pass


class DsMemeBoost(Star):
    def __init__(self, context: Context, config: AstrBotConfig | None = None) -> None:
        super().__init__(context)
        self.config = config or {}
        self._quota: dict[str, tuple[int, int]] = {}
        self._last_judge: dict[str, float] = {}

    def _cfg(self, key: str, default=None):
        try:
            value = self.config.get(key, default)
        except Exception:  # noqa: BLE001
            value = default
        return default if value is None else value

    def _stealer(self):
        name = str(self._cfg("stealer_name", "astrbot_plugin_stealer") or "")
        try:
            meta = self.context.get_registered_star(name)
        except Exception:  # noqa: BLE001
            return None
        return getattr(meta, "star_cls", None) if meta else None

    def _budget(self, umo: str) -> bool:
        now = time.time()
        if now - self._last_judge.get(umo, 0.0) < float(self._cfg("cooldown_seconds", 15)):
            return False
        hour = int(now // 3600)
        mark, count = self._quota.get(umo, (hour, 0))
        if mark != hour:
            mark, count = hour, 0
        if count >= int(self._cfg("max_judge_per_hour", 12)):
            return False
        self._last_judge[umo] = now
        self._quota[umo] = (mark, count + 1)
        return True

    async def _size_ok(self, url: str) -> tuple[bool, int]:
        limit = int(self._cfg("max_kb", 2048)) * 1024
        try:
            async with aiohttp.ClientSession() as session:
                async with session.head(url, timeout=aiohttp.ClientTimeout(total=8)) as resp:
                    length = int(resp.headers.get("Content-Length") or 0)
                    if length and length > limit:
                        return False, length
                    return True, length
        except Exception:  # noqa: BLE001
            return True, 0

    async def _judge(self, url: str) -> bool:
        provider_id = str(self._cfg("judge_provider_id", "deepseek/deepseek-flash") or "")
        try:
            provider = (
                self.context.get_provider_by_id(provider_id)
                if provider_id
                else await self.context.get_using_provider_async()
            )
        except Exception as exc:  # noqa: BLE001
            _log(f"取视觉模型失败: {exc}")
            return False
        if not provider:
            return False
        try:
            resp = await provider.text_chat(
                prompt=JUDGE_PROMPT,
                session_id=f"ds-meme-{int(time.time())}",
                image_urls=[url],
                persist=False,
            )
            answer = str(getattr(resp, "completion_text", "") or "").strip()
        except Exception as exc:  # noqa: BLE001
            _log(f"视觉判定失败: {exc}")
            return False
        _log(f"判定: {answer[:60]}")
        return answer.upper().startswith("YES")

    async def initialize(self) -> None:
        inst = self._stealer()
        ok = bool(inst and hasattr(inst, "_steal_sticker_impl"))
        _log(f"init: stealer 实例={'有' if inst else '无'}，入库方法={ok}")
        logger.info(f"ds_meme_boost: stealer 可用={ok}")

    @filter.event_message_type(filter.EventMessageType.GROUP_MESSAGE)
    async def on_group_image(self, event: AstrMessageEvent):
        if not self._cfg("enable", True):
            return
        if event.get_message_type() != MessageType.GROUP_MESSAGE:
            return
        groups = self._cfg("groups", [])
        if groups:
            gid = str(event.get_group_id() or "")
            if gid not in [str(g) for g in groups] and event.unified_msg_origin not in groups:
                return

        images = [c for c in event.get_messages() if isinstance(c, Image)]
        if not images:
            return
        umo = event.unified_msg_origin
        if not self._budget(umo):
            return

        comp = images[0]
        ref = comp.url or comp.file or ""
        if not ref:
            return

        ok, length = await self._size_ok(ref)
        if not ok:
            _log(f"跳过：文件太大 {length} bytes ({umo})")
            return

        if not await self._judge(ref):
            return
        if random.random() > float(self._cfg("probability", 0.9)):
            _log(f"跳过：判定 YES 但概率没过 ({umo})")
            return

        stealer = self._stealer()
        impl = getattr(stealer, "_steal_sticker_impl", None) if stealer else None
        if impl is None:
            _log("跳过：找不到 stealer 入库方法")
            return
        try:
            async for _ in impl(event, ref):
                pass
            _log(f"已入库 ({umo}) {ref[:90]}")
        except Exception as exc:  # noqa: BLE001
            _log(f"入库失败: {exc}")
            logger.warning(f"ds_meme_boost: 入库失败 {exc}")
