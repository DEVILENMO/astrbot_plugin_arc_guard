from __future__ import annotations

import asyncio
import json
import re
import time
import unicodedata
from collections import deque
from datetime import datetime, timedelta, timezone
from typing import Any

from botpy.http import Route

from astrbot.api import AstrBotConfig, logger
from astrbot.api.event import AstrMessageEvent, filter
from astrbot.api.message_components import At, Image, Plain
from astrbot.api.provider import LLMResponse
from astrbot.api.star import Context, Star, StarTools, register
from astrbot.core.star.filter.command import CommandFilter
from astrbot.core.star.filter.command_group import CommandGroupFilter
from astrbot.core.star.filter.regex import RegexFilter

PLUGIN_VERSION = "1.12.2"
PLUGIN_NAME = "astrbot_plugin_arc_guard"
GUARD_DISPLAY = "弧光护卫"

# Default patterns (case-insensitive). Space-separated parts = AND combo
# (all parts must appear somewhere). A single token = contiguous sequence.
DEFAULT_FORBIDDEN_KEYWORDS = ("sb enmo",)

# QQ group ban upper bound (~30 days).
MAX_MUTE_SECONDS = 30 * 24 * 60 * 60

# Shared denial text for LLM tools that only super admins may use.
SUPER_ADMIN_DENIED = "权限不足：该功能仅超级管理员可用"

DEFAULT_REPLY_TEMPLATE = "竟敢辱骂至高无上的ENMO，枪毙{minutes}分钟！"
DEFAULT_NICKNAME_REPLY = "群名片写辱骂ENMO？枪毙{minutes}分钟，改干净再说话！"
DEFAULT_ACCOMPLICE_REPLY = "人家本来就傻，你还他妈逗人家，连坐惩罚！"
DEFAULT_LINKED_BOT_IDS = ("2267236706",)
DEFAULT_WHITELIST_IDS = ("593405016",)
DEFAULT_PARDON_PHRASE = "天星大人我错了，我跪下求你放过我"
DEFAULT_PARDON_REDUCE_SECONDS = 3600
DEFAULT_PARDON_COOLDOWN_SECONDS = 10
DEFAULT_PARDON_OK = (
    "念在你跪得诚恳，减刑 1 小时。还剩约 {minutes} 分钟，下次说话注意点。"
)
DEFAULT_PARDON_NONE = "你现在又没被禁言或屏蔽，跪给谁看？"
DEFAULT_PARDON_COOLDOWN = "求饶冷却中，约 {seconds} 秒后再来跪。"
DEFAULT_PARDON_BROADCAST = (
    "私聊跪下求饶了。鉴于认罪态度良好，特赦减刑 1 小时；也可申请打屁股作为替代惩罚，"
    "跪得再标准点说不定还能再减。（减刑后约剩 {minutes} 分钟）"
)
DEFAULT_PARDON_CLEARED = "念在你跪得诚恳，禁言/屏蔽已全部解除。下次说话注意点。"
DEFAULT_BOT_GROUP_CARD = "天星大人"
DEFAULT_CARD_TAMPER_PUNISH_IDS = ("1034297259",)
DEFAULT_CARD_TAMPER_MUTE_SECONDS = 3600
DEFAULT_CARD_TAMPER_REPLY = "我他妈让你改我群名片了吗？阿弥诺斯。"
CARD_WATCH_INTERVAL_SECONDS = 1.0
CARD_GROUP_LIST_REFRESH_SECONDS = 60.0
CARD_ACTION_COOLDOWN_SECONDS = 2.0
OCR_MAX_SIDE = 1920
OCR_MIN_LONG_SIDE = 640
# Timeout for official (botpy) group admin API calls (mute query / set / recall).
_OFFICIAL_API_TIMEOUT = 5
DEFAULT_RECALL_ON_HIT = True
# Newest messages kept per group so super admins can recall them by sender.
RECENT_MSGS_PER_GROUP = 200
RECALL_MAX_COUNT = 5
# qq_official refuses to recall messages sent earlier than this.
OFFICIAL_RECALL_WINDOW_SECONDS = 120


class ArcGuardAPI:
    """Cross-plugin API for Arc Guard forbidden-sequence checks.

    Other plugins should obtain this via
    ``context.get_registered_star(...).star_cls.get_api()``.
    """

    def __init__(self, plugin: ArcGuardPlugin):
        self._plugin = plugin

    def count_forbidden(self, text: str) -> int:
        """Count configured forbidden sequences in text.

        Args:
            text: Raw message text.

        Returns:
            Non-overlapping hit count (0 if none).
        """
        return count_forbidden_sequences(text, self._plugin._forbidden_sequences())

    def is_whitelisted(self, user_id: str) -> bool:
        """Whether a platform user id is on the Arc Guard whitelist (fully skipped).

        Args:
            user_id: QQ number or official member openid.

        Returns:
            True if the user must not be processed.
        """
        return self._plugin._is_whitelisted(str(user_id or ""))

    def is_blacklisted(self, user_id: str) -> bool:
        """Whether a user id is permanently shielded (the blacklist).

        Args:
            user_id: QQ user id or official member openid.

        Returns:
            True if the user's chat must be intercepted everywhere.
        """
        return self._plugin._is_permanently_shielded(str(user_id or ""))

    async def mute_user(
        self,
        event: AstrMessageEvent,
        user_id: str,
        add_seconds: int,
        *,
        respect_whitelist: bool = True,
        announce: bool = True,
    ) -> dict:
        """Accumulate mute via a message event (remaining + add_seconds).

        Args:
            event: Current group message event (provides bot/group).
            user_id: QQ number or official member openid to mute.
            add_seconds: Extra mute seconds to add on top of remaining.
            respect_whitelist: If True, skip mute for whitelist users.
                Pass False for callers such as self-destruct truck.
            announce: If True, send stack breakdown ``@user a/b/c`` in group.

        Returns:
            Dict with ``ok``, ``remaining_before``, ``added``, ``total``,
            and optional ``error``.
        """
        return await self._plugin._ban_accumulate(
            event,
            str(user_id or ""),
            int(add_seconds),
            respect_whitelist=respect_whitelist,
            announce=announce,
        )

    async def mute_user_in_group(
        self,
        bot,
        group_id: str,
        user_id: str,
        add_seconds: int,
        *,
        self_id: str = "",
        respect_whitelist: bool = True,
        announce: bool = True,
    ) -> dict:
        """Accumulate mute with an explicit bot/group (no message event).

        Args:
            bot: Platform bot client (aiocqhttp or qq_official).
            group_id: Group id or official group openid.
            user_id: QQ number or official member openid to mute.
            add_seconds: Extra mute seconds to add on top of remaining.
            self_id: Optional bot self id (skip if equal to user_id).
            respect_whitelist: If True, skip mute for whitelist users.
            announce: If True, send stack breakdown ``@user a/b/c`` in group.

        Returns:
            Dict with ``ok``, ``remaining_before``, ``added``, ``total``,
            and optional ``error``.
        """
        return await self._plugin._ban_accumulate_on_bot(
            bot,
            str(group_id or ""),
            str(user_id or ""),
            int(add_seconds),
            self_id=str(self_id or ""),
            respect_whitelist=respect_whitelist,
            announce=announce,
        )

    async def unmute_user(
        self,
        event: AstrMessageEvent,
        user_id: str,
    ) -> dict:
        """Lift the mute of one member in the current group.

        Shields are a separate feature and are left untouched.

        Args:
            event: Current group message event (provides bot/group).
            user_id: QQ number or official member openid to release.

        Returns:
            Dict with ``ok``, ``had_mute``, ``mute_cleared``, and optional
            ``error``.
        """
        return await self._plugin._unmute(
            event,
            str(user_id or ""),
        )

    async def unmute_user_in_group(
        self,
        bot,
        group_id: str,
        user_id: str,
        *,
        self_id: str = "",
    ) -> dict:
        """Lift the mute with an explicit bot/group (no message event).

        Shields are a separate feature and are left untouched.

        Args:
            bot: Platform bot client (aiocqhttp or qq_official).
            group_id: Group id or official group openid.
            user_id: QQ number or official member openid to release.
            self_id: Optional bot self id (skip if equal to user_id).

        Returns:
            Dict with ``ok``, ``had_mute``, ``mute_cleared``, and optional
            ``error``.
        """
        return await self._plugin._unmute_on_bot(
            bot,
            str(group_id or ""),
            str(user_id or ""),
            self_id=str(self_id or ""),
        )

    def mute_seconds_for_hits(self, hits: int) -> int:
        """Mute duration in seconds for a given hit count.

        Args:
            hits: Forbidden sequence hit count.

        Returns:
            Clamped mute seconds (at least one hit worth).
        """
        n = max(1, int(hits))
        return min(n * self._plugin._seconds_per_hit(), MAX_MUTE_SECONDS)

    def format_reply(self, hits: int) -> str:
        """Build the standard punishment reply for hit count.

        Args:
            hits: Forbidden sequence hit count.

        Returns:
            Warning text with minutes filled in.
        """
        seconds = self.mute_seconds_for_hits(hits)
        minutes = max(1, (seconds + 59) // 60)
        return self._plugin._reply_text(minutes)

    def check(self, text: str, *, user_id: str | None = None) -> dict:
        """Check text and optionally skip whitelist users.

        Args:
            text: Message text to scan.
            user_id: Optional QQ id; whitelist users never punish.

        Returns:
            Dict with keys ``hits``, ``should_punish``, ``mute_seconds``,
            ``reply``.
        """
        if user_id and self.is_whitelisted(user_id):
            return {
                "hits": 0,
                "should_punish": False,
                "mute_seconds": 0,
                "reply": "",
            }
        hits = self.count_forbidden(text)
        if hits <= 0:
            return {
                "hits": 0,
                "should_punish": False,
                "mute_seconds": 0,
                "reply": "",
            }
        seconds = self.mute_seconds_for_hits(hits)
        return {
            "hits": hits,
            "should_punish": True,
            "mute_seconds": seconds,
            "reply": self.format_reply(hits),
        }


# Lookalike glyphs (Greek / Cyrillic / etc.) -> Latin e,n,m,o,s,b.
# Covers attacks like Ε、Ν、Μ、Ο、S、Ь (Greek + Cyrillic soft sign as B).
_HOMOGLYPH_TO_LATIN: dict[str, str] = {
    # E
    "ε": "e",
    "Ε": "e",
    "е": "e",
    "Е": "e",
    "ё": "e",
    "Ё": "e",
    "ϵ": "e",
    "ɛ": "e",
    # N
    "ν": "n",
    "Ν": "n",
    "ƞ": "n",
    "ŋ": "n",
    "ɴ": "n",
    # M
    "μ": "m",
    "Μ": "m",
    "м": "m",
    "М": "m",
    "ɱ": "m",
    # O
    "ο": "o",
    "Ο": "o",
    "о": "o",
    "О": "o",
    "ό": "o",
    "ὀ": "o",
    "ὁ": "o",
    "օ": "o",
    # S
    "σ": "s",
    "ς": "s",
    "Σ": "s",
    "ѕ": "s",
    "с": "s",
    "С": "s",
    "ꜱ": "s",
    "ſ": "s",
    # B (incl. Cyrillic soft sign Ь used as B lookalike)
    "β": "b",
    "Β": "b",
    "в": "b",
    "В": "b",
    "ь": "b",
    "Ь": "b",
    "б": "b",
    "Б": "b",
    "Ƅ": "b",
    "ƅ": "b",
    "ɓ": "b",
}


def _extract_latin_letters(text: str) -> str:
    """Normalize text and keep Latin letters (incl. lookalikes).

    Applies NFKC (fullwidth → ASCII), then maps Greek/Cyrillic lookalikes
    to Latin, then keeps a-z via casefold. Separators / CJK are dropped.

    Args:
        text: Raw message text.

    Returns:
        Lowercase Latin letter string used for pattern counting.
    """
    normalized = unicodedata.normalize("NFKC", text)
    out: list[str] = []
    for ch in normalized:
        mapped = _HOMOGLYPH_TO_LATIN.get(ch)
        if mapped is None:
            mapped = _HOMOGLYPH_TO_LATIN.get(ch.casefold())
        if mapped is not None:
            out.append(mapped)
            continue
        folded = ch.casefold()
        if "a" <= folded <= "z":
            out.append(folded)
    return "".join(out)


def _parse_forbidden_pattern(item: str) -> tuple[str, ...]:
    """Parse one config entry into AND-parts of lowercase Latin letters.

    Whitespace / ``+`` / ``&`` separate combo parts. Example: ``sb enmo``
    → ``("sb", "enmo")`` (both must appear). A single token like ``enmosb``
    → ``("enmosb",)`` (contiguous sequence).

    Args:
        item: Raw config string.

    Returns:
        Non-empty lowercase Latin parts, or empty tuple if none.
    """
    parts: list[str] = []
    for chunk in re.split(r"[\s+&]+", str(item or "").strip()):
        letters = _extract_latin_letters(chunk)
        if letters:
            parts.append(letters)
    return tuple(parts)


def _normalize_forbidden_patterns(
    sequences: tuple[str | tuple[str, ...], ...] | None,
) -> tuple[tuple[str, ...], ...]:
    """Normalize config/API patterns into AND-part tuples.

    Args:
        sequences: Raw patterns (strings and/or already-parsed tuples).

    Returns:
        Parsed patterns; falls back to defaults when empty.
    """
    raw = sequences if sequences is not None else DEFAULT_FORBIDDEN_KEYWORDS
    patterns: list[tuple[str, ...]] = []
    for item in raw:
        if isinstance(item, tuple):
            parts = tuple(p for p in item if p)
        else:
            parts = _parse_forbidden_pattern(str(item))
        if parts and parts not in patterns:
            patterns.append(parts)
    if patterns:
        return tuple(patterns)
    fallback: list[tuple[str, ...]] = []
    for item in DEFAULT_FORBIDDEN_KEYWORDS:
        parts = _parse_forbidden_pattern(item)
        if parts:
            fallback.append(parts)
    return tuple(fallback)


def _count_non_overlapping(haystack: str, needle: str) -> int:
    """Count non-overlapping occurrences of needle in haystack.

    Args:
        haystack: Letter stream to search.
        needle: Substring pattern.

    Returns:
        Hit count (0 if needle is empty).
    """
    if not needle or not haystack:
        return 0
    count = 0
    i = 0
    n = len(needle)
    while i <= len(haystack) - n:
        if haystack.startswith(needle, i):
            count += 1
            i += n
        else:
            i += 1
    return count


def count_forbidden_sequences(
    text: str,
    sequences: tuple[str | tuple[str, ...], ...] | None = None,
) -> int:
    """Count forbidden pattern hits in text.

    Separators (commas/spaces/CJK/punctuation) are ignored in the letter
    stream so ``s b`` still matches part ``sb``. Other Latin letters are kept.

    Pattern modes:

    - Single token (e.g. ``enmosb``): contiguous substring; non-overlapping
      left-to-right across all contiguous patterns.
    - Multi-token (e.g. ``sb enmo``): AND combo — every part must appear
      somewhere (order ignored). Hit count is ``min`` of each part's
      non-overlapping counts.

    Case and Greek/Cyrillic lookalikes are folded.

    Args:
        text: Raw message or LLM reply text.
        sequences: Forbidden patterns (strings or part tuples). Defaults to
            ``DEFAULT_FORBIDDEN_KEYWORDS``.

    Returns:
        Number of forbidden hits (0 if none).
    """
    if not text:
        return 0
    letters = _extract_latin_letters(text)
    if not letters:
        return 0
    patterns = _normalize_forbidden_patterns(sequences)
    if not patterns:
        return 0

    contiguous = [parts[0] for parts in patterns if len(parts) == 1]
    combos = [parts for parts in patterns if len(parts) >= 2]

    count = 0
    if contiguous:
        i = 0
        while i < len(letters):
            matched = False
            for pattern in contiguous:
                if pattern and letters.startswith(pattern, i):
                    count += 1
                    i += len(pattern)
                    matched = True
                    break
            if not matched:
                i += 1

    for parts in combos:
        part_counts = [_count_non_overlapping(letters, part) for part in parts]
        if all(c > 0 for c in part_counts):
            count += min(part_counts)
    return count


def _is_official_target(bot: Any, *ids: str) -> bool:
    """Whether ``bot`` is a qq_official client operating on openid targets.

    The non-numeric id check is the real platform discriminator: aiocqhttp
    (OneBot) ids are always numeric QQ numbers, and its dynamic
    ``Api.__getattr__`` makes ``hasattr(bot.api, ...)`` unreliable there.

    Args:
        bot: Platform bot client.
        ids: Group / user ids to test (all must be non-numeric openids).

    Returns:
        True only for the official admin-API path.
    """
    if bot is None or not hasattr(bot, "api") or not hasattr(bot.api, "_http"):
        return False
    checked = [str(i or "").strip() for i in ids]
    return bool(checked) and all(i and not i.isdigit() for i in checked)


async def _official_set_mute(
    bot: Any, group_openid: str, member_openid: str, *, op: str, expire_epoch: int
) -> None:
    """Set one member's mute state via the official group admin API.

    The bot must be a group admin; the API only accepts regular members
    (owner / other admins / bots are rejected). Expiry is an absolute
    RFC3339 timestamp in +08:00; ``op="del"`` unmutes immediately.

    Args:
        bot: qq_official botpy client.
        group_openid: Group openid.
        member_openid: Target member openid.
        op: ``add`` (fresh mute), ``update`` (change expiry), ``del`` (unmute).
        expire_epoch: Mute expiry unix seconds (ignored for ``del``).

    Raises:
        RuntimeError: If the HTTP layer returned no response (botpy swallows
            its own timeouts / retry exhaustion and returns None).
        Exception: HTTP errors propagate (e.g. missing admin permission).
    """
    expire_at = (
        ""
        if op == "del"
        else datetime.fromtimestamp(
            max(1, int(expire_epoch)), tz=timezone(timedelta(hours=8))
        ).strftime("%Y-%m-%dT%H:%M:%S+08:00")
    )
    route = Route(
        "POST",
        "/v2/groups/{group_openid}/restrict_chat_setting",
        group_openid=group_openid,
    )
    response = await asyncio.wait_for(
        bot.api._http.request(
            route,
            json={
                "members": [
                    {
                        "op": op,
                        "member_openid": member_openid,
                        "mute_expire_at": expire_at,
                    }
                ]
            },
        ),
        timeout=_OFFICIAL_API_TIMEOUT,
    )
    if response is None:
        raise RuntimeError("official mute API returned no response")


async def _official_query_remaining(
    bot: Any, group_openid: str, member_openid: str
) -> int | None:
    """Query remaining mute seconds for one member (official admin API).

    Args:
        bot: qq_official botpy client.
        group_openid: Group openid.
        member_openid: Member openid to look up.

    Returns:
        Remaining mute seconds (0 when not muted), or None when the lookup
        failed (caller must fall back to the local tracker).
    """
    route = Route(
        "GET",
        "/v2/groups/{group_openid}/restrict_chat_setting",
        group_openid=group_openid,
    )
    try:
        response = await asyncio.wait_for(
            bot.api._http.request(route), timeout=_OFFICIAL_API_TIMEOUT
        )
    except Exception as exc:  # noqa: BLE001
        logger.warning(
            "弧光护卫：官方查询禁言状态失败 group=%s user=%s err=%s",
            group_openid,
            member_openid,
            exc,
        )
        return None
    if not isinstance(response, dict):
        return None
    members = response.get("members")
    if not isinstance(members, list):
        return 0
    for item in members:
        if not isinstance(item, dict) or item.get("member_openid") != member_openid:
            continue
        try:
            expire = datetime.fromisoformat(
                str(item.get("mute_expire_at") or "").replace("Z", "+00:00")
            )
        except ValueError:
            logger.warning(
                "弧光护卫：官方禁言到期时间格式异常 group=%s user=%s value=%s",
                group_openid,
                member_openid,
                item.get("mute_expire_at"),
            )
            return 0
        return max(0, int(expire.timestamp()) - int(time.time()))
    return 0


async def _official_recall_message(
    bot: Any, group_openid: str, message_id: str
) -> None:
    """Recall a group message via the official admin API (2-minute window).

    Args:
        bot: qq_official botpy client.
        group_openid: Group openid.
        message_id: Message id (``d.id`` of the incoming group event).

    Raises:
        RuntimeError: If the HTTP layer returned no response.
        Exception: HTTP errors propagate (e.g. window exceeded / no admin).
    """
    route = Route(
        "DELETE",
        "/v2/groups/{group_openid}/messages/{message_id}",
        group_openid=group_openid,
        message_id=message_id,
    )
    response = await asyncio.wait_for(
        bot.api._http.request(route), timeout=_OFFICIAL_API_TIMEOUT
    )
    if response is None:
        raise RuntimeError("official recall API returned no response")


@register(
    "astrbot_plugin_arc_guard",
    "DEVILENMO",
    "弧光护卫：可配置违禁词检测与累加禁言",
    PLUGIN_VERSION,
)
class ArcGuardPlugin(Star):
    """弧光护卫：检测违禁词、累加禁言、连坐与求饶。"""

    def __init__(self, context: Context, config: AstrBotConfig | None = None):
        super().__init__(context)
        self.config = config or {}
        self._api = ArcGuardAPI(self)
        # Intended mute end unix-ts by (group_id, user_id). Avoids QQ/NapCat
        # minute-rounding on short bans compounding across accumulates.
        self._mute_until: dict[tuple[str, str], int] = {}
        self._shield_until: dict[tuple[str, str], int] = {}
        self._pardon_last_ts: dict[str, float] = {}
        self._ocr_engine: Any | None = None
        self._ocr_init_failed = False
        self._card_watch_task: asyncio.Task | None = None
        self._card_group_ids: list[str] = []
        self._card_group_ids_ts: float = 0.0
        self._card_action_ts: dict[str, float] = {}
        self._card_bot_self_ids: dict[int, str] = {}
        # Newest (timestamp, sender_id, message_id) per group, for recall.
        self._recent_msgs: dict[str, deque] = {}

    async def initialize(self) -> None:
        """Start the 1s bot group-card watch loop when protection is enabled."""
        conf = self.config
        changed = False
        for key in (
            "forbidden_keywords",
            "linked_bot_ids",
            "whitelist_ids",
            "blacklist_ids",
            "card_tamper_punish_ids",
        ):
            raw = conf.get(key) if hasattr(conf, "get") else None
            if not isinstance(raw, str):
                continue
            # Spaces inside forbidden_keywords mean AND-combo (e.g. "sb enmo");
            # only commas separate distinct entries. ID lists still split on spaces.
            if key == "forbidden_keywords":
                items = [
                    p.strip() for p in re.split(r"[,，]+", raw.strip()) if p.strip()
                ]
            else:
                items = [p for p in re.split(r"[,，\s]+", raw.strip()) if p]
            conf[key] = items
            changed = True
        if changed and hasattr(conf, "save_config"):
            conf.save_config()
        self._load_shields()
        if self._card_watch_task and not self._card_watch_task.done():
            return
        self._card_watch_task = asyncio.create_task(self._card_watch_loop())
        logger.info("弧光护卫：机器人群名片守护已启动（每秒检测）")

    async def terminate(self) -> None:
        """Stop the card watch loop on unload/reload."""
        task = self._card_watch_task
        self._card_watch_task = None
        if task is None:
            return
        task.cancel()
        try:
            await task
        except asyncio.CancelledError:
            pass

    def get_api(self) -> ArcGuardAPI:
        """Return the cross-plugin Arc Guard API.

        Returns:
            Shared API instance for forbidden-sequence checks.
        """
        return self._api

    def _seconds_per_hit(self) -> int:
        """Seconds muted for each forbidden sequence hit (default 60)."""
        return max(1, int(self.config.get("mute_seconds_per_hit", 60)))

    def _forbidden_sequences(self) -> tuple[tuple[str, ...], ...]:
        """Load forbidden keyword patterns from config.

        Each entry may be a contiguous token (``enmosb``) or an AND combo
        with spaces (``sb enmo``). Commas still separate distinct entries.

        Returns:
            Parsed AND-part tuples (non-empty). Falls back to defaults.
        """
        raw = self.config.get("forbidden_keywords", list(DEFAULT_FORBIDDEN_KEYWORDS))
        chunks = raw if isinstance(raw, list) else [raw]
        items: list[str] = []
        for chunk in chunks:
            items.extend(re.split(r"[,，]+", str(chunk or "").strip()))
        return _normalize_forbidden_patterns(tuple(items))

    def _count_forbidden(self, text: str) -> int:
        """Count forbidden hits using the plugin's configured keyword list."""
        return count_forbidden_sequences(text, self._forbidden_sequences())

    def _ocr_enabled(self) -> bool:
        """Whether image OCR keyword scanning is enabled."""
        return bool(self.config.get("ocr_enabled", True))

    def _get_ocr_engine(self) -> Any | None:
        """Lazy-load RapidOCR (local ONNX small models).

        Returns:
            RapidOCR engine instance, or None if unavailable.
        """
        if self._ocr_engine is not None:
            return self._ocr_engine
        if self._ocr_init_failed:
            return None
        try:
            from rapidocr import RapidOCR

            self._ocr_engine = RapidOCR()
            logger.info("弧光护卫：RapidOCR 本地引擎已加载")
            return self._ocr_engine
        except Exception as exc:  # noqa: BLE001
            self._ocr_init_failed = True
            logger.warning(
                "弧光护卫：RapidOCR 不可用（请安装 rapidocr + onnxruntime）: %s",
                exc,
            )
            return None

    @staticmethod
    def _extract_ocr_texts(result: Any) -> list[str]:
        """Normalize RapidOCR output into plain text lines."""
        if result is None:
            return []
        txts = getattr(result, "txts", None)
        if txts:
            return [str(t).strip() for t in txts if str(t).strip()]
        if isinstance(result, tuple) and result:
            result = result[0]
        lines: list[str] = []
        if isinstance(result, list):
            for item in result:
                if isinstance(item, (list, tuple)) and len(item) >= 2:
                    text = str(item[1] or "").strip()
                    if text:
                        lines.append(text)
                elif isinstance(item, str) and item.strip():
                    lines.append(item.strip())
        return lines

    @staticmethod
    def _image_has_alpha(img: Any) -> bool:
        """Whether a PIL image carries an alpha / transparency channel."""
        mode = getattr(img, "mode", "")
        if mode in {"RGBA", "LA", "PA"}:
            return True
        if mode == "P":
            info = getattr(img, "info", None) or {}
            return "transparency" in info
        return False

    @staticmethod
    def _composite_rgba_on_rgb(rgba_img: Any, rgb: tuple[int, int, int]) -> Any:
        """Alpha-composite an RGBA image onto a solid RGB background.

        Args:
            rgba_img: PIL image in RGBA mode.
            rgb: Background color ``(r, g, b)``.

        Returns:
            RGB PIL image after compositing.
        """
        from PIL import Image as PILImage

        bg = PILImage.new("RGBA", rgba_img.size, (*rgb, 255))
        return PILImage.alpha_composite(bg, rgba_img).convert("RGB")

    @staticmethod
    def _downscale_for_ocr(img: Any, max_side: int = OCR_MAX_SIDE) -> Any:
        """Downscale images that exceed ``max_side`` x ``max_side``.

        Args:
            img: PIL image.
            max_side: Maximum width/height after scaling (default 1920).

        Returns:
            Possibly resized PIL image (aspect ratio preserved).
        """
        from PIL import Image as PILImage

        w, h = img.size
        long_side = max(w, h)
        if long_side <= max_side or long_side <= 0:
            return img
        scale = float(max_side) / float(long_side)
        new_size = (max(1, int(w * scale)), max(1, int(h * scale)))
        return img.resize(new_size, PILImage.Resampling.LANCZOS)

    @staticmethod
    def _upscale_for_ocr(img: Any, min_long_side: int = OCR_MIN_LONG_SIDE) -> Any:
        """Upscale small images so RapidOCR can read tiny mirage-tank text."""
        from PIL import Image as PILImage

        w, h = img.size
        long_side = max(w, h)
        if long_side >= min_long_side:
            return img
        scale = min(8.0, float(min_long_side) / float(long_side))
        new_size = (max(1, int(w * scale)), max(1, int(h * scale)))
        return img.resize(new_size, PILImage.Resampling.LANCZOS)

    @staticmethod
    def _pad_to_square(img: Any, fill: tuple[int, int, int] = (0, 0, 0)) -> Any:
        """Pad an RGB/RGBA image to a square canvas with a solid fill.

        Args:
            img: PIL image.
            fill: RGB fill color for the padded margins (default black).

        Returns:
            Squared PIL image (same mode as input when possible).
        """
        from PIL import Image as PILImage

        w, h = img.size
        side = max(w, h)
        if side <= 0 or (w == side and h == side):
            return img
        mode = img.mode
        if mode == "RGBA":
            bg_color: tuple[int, ...] = (*fill, 255)
        elif mode == "RGB":
            bg_color = fill
        elif mode == "L":
            bg_color = (int(sum(fill) / 3),)
        else:
            img = img.convert("RGB")
            mode = "RGB"
            bg_color = fill
        canvas = PILImage.new(mode, (side, side), bg_color)
        canvas.paste(img, ((side - w) // 2, (side - h) // 2))
        return canvas

    @staticmethod
    def _ocr_variant_images(rgb_img: Any) -> list[Any]:
        """Build OCR variants: 4 orientations + black square pad + contrast.

        Rotates 0/90/180/270 so sideways or upside-down keywords are still
        readable. Each orientation is also padded to a black square; contrast
        tweaks run on those squared canvases only.
        """
        from PIL import ImageOps

        variants: list[Any] = []
        squared_bases: list[Any] = []
        for angle in (0, 90, 180, 270):
            oriented = rgb_img if angle == 0 else rgb_img.rotate(angle, expand=True)
            squared = ArcGuardPlugin._pad_to_square(oriented, fill=(0, 0, 0))
            variants.append(oriented)
            variants.append(squared)
            squared_bases.append(squared)
        for base in squared_bases:
            try:
                ac = ImageOps.autocontrast(base)
                variants.append(ac)
                variants.append(ImageOps.invert(ac))
            except Exception:  # noqa: BLE001
                pass
        return variants

    @staticmethod
    def _merge_ocr_chunks(*chunks: str) -> str:
        """Merge OCR texts and drop duplicate lines (order preserved)."""
        seen: set[str] = set()
        lines: list[str] = []
        for chunk in chunks:
            for line in str(chunk or "").splitlines():
                text = line.strip()
                if not text:
                    continue
                key = re.sub(r"\s+", "", text).casefold()
                if key in seen:
                    continue
                seen.add(key)
                lines.append(text)
        return "\n".join(lines)

    def _run_ocr_engine(self, engine: Any, image: Any) -> str:
        """Run RapidOCR on a path / ndarray / PIL image and return text."""
        try:
            result = engine(image)
            return "\n".join(self._extract_ocr_texts(result))
        except Exception as exc:  # noqa: BLE001
            logger.warning("弧光护卫：OCR 识别失败 err=%s", exc)
            return ""

    def _ocr_image_path_sync(self, path: str) -> str:
        """Run OCR on a local image, including mirage-tank (dual-bg) passes.

        All images are first downscaled to fit within 1920x1920 when larger.
        They are also OCR'd after padding to a black square canvas and after
        90/180/270-degree rotations (four orientations). Images with alpha
        are upscaled when small, then composited onto white and black
        backgrounds (plus autocontrast / invert variants).

        Args:
            path: Local image file path.

        Returns:
            Merged OCR text (may be empty).
        """
        engine = self._get_ocr_engine()
        if engine is None:
            return ""
        try:
            import numpy as np
            from PIL import Image as PILImage

            with PILImage.open(path) as raw:
                raw.load()
                chunks: list[str] = []
                if self._image_has_alpha(raw):
                    rgba = self._upscale_for_ocr(
                        self._downscale_for_ocr(raw.convert("RGBA"))
                    )
                    for bg in ((255, 255, 255), (0, 0, 0)):
                        composed = self._composite_rgba_on_rgb(rgba, bg)
                        for variant in self._ocr_variant_images(composed):
                            text = self._run_ocr_engine(engine, np.asarray(variant))
                            if text.strip():
                                chunks.append(text)
                    merged = self._merge_ocr_chunks(*chunks)
                    logger.info(
                        "弧光护卫：幻影坦克双底 OCR path=%s variants=%s chars=%s",
                        path,
                        len(chunks),
                        len(merged),
                    )
                    return merged
                rgb = self._upscale_for_ocr(self._downscale_for_ocr(raw.convert("RGB")))
                for variant in self._ocr_variant_images(rgb):
                    text = self._run_ocr_engine(engine, np.asarray(variant))
                    if text.strip():
                        chunks.append(text)
                return self._merge_ocr_chunks(*chunks)
        except Exception as exc:  # noqa: BLE001
            logger.warning(
                "弧光护卫：OCR 预处理失败，回退原图 path=%s err=%s", path, exc
            )
            return self._run_ocr_engine(engine, path)

    async def _ocr_message_images(self, event: AstrMessageEvent) -> str:
        """Download message images and OCR their text.

        Args:
            event: Incoming message event.

        Returns:
            Joined OCR text from all images (may be empty).
        """
        if not self._ocr_enabled():
            return ""
        chunks: list[str] = []
        try:
            messages = event.get_messages() or []
        except Exception as exc:  # noqa: BLE001
            logger.warning("弧光护卫：读取消息段失败: %s", exc)
            return ""
        for comp in messages:
            if not isinstance(comp, Image):
                continue
            try:
                path = await comp.convert_to_file_path()
            except Exception as exc:  # noqa: BLE001
                logger.warning("弧光护卫：图片落盘失败: %s", exc)
                continue
            if not path:
                continue
            text = await asyncio.to_thread(self._ocr_image_path_sync, path)
            if text.strip():
                chunks.append(text.strip())
                logger.info(
                    "弧光护卫：OCR 提取文字 group=%s user=%s chars=%s",
                    event.get_group_id(),
                    event.get_sender_id(),
                    len(text),
                )
        return "\n".join(chunks)

    def _accomplice_seconds_per_hit(self) -> int:
        """Seconds muted per forbidden hit for the accomplice (default 120)."""
        return max(1, int(self.config.get("accomplice_mute_seconds", 120)))

    def _parse_id_list(self, raw, defaults: tuple[str, ...]) -> set[str]:
        """Parse platform ids from list config (also accepts legacy comma strings).

        Args:
            raw: Config value (list or legacy comma-separated string).
            defaults: Fallback ids when parsing yields nothing.

        Returns:
            Distinct non-empty id strings.
        """
        ids: set[str] = set()
        chunks = raw if isinstance(raw, list) else [raw]
        for chunk in chunks:
            for part in re.split(r"[,，\s]+", str(chunk or "").strip()):
                if part:
                    ids.add(part)
        if not ids:
            ids.update(defaults)
        return ids

    def _linked_bot_ids(self) -> set[str]:
        """QQ ids of peer bots whose @ targets get accomplice mute."""
        return self._parse_id_list(
            self.config.get("linked_bot_ids", list(DEFAULT_LINKED_BOT_IDS)),
            DEFAULT_LINKED_BOT_IDS,
        )

    def _whitelist_ids(self) -> set[str]:
        """QQ ids whose messages are ignored entirely by this plugin."""
        return self._parse_id_list(
            self.config.get("whitelist_ids", list(DEFAULT_WHITELIST_IDS)),
            DEFAULT_WHITELIST_IDS,
        )

    def _is_whitelisted(self, user_id: str) -> bool:
        """Whether user_id is on the no-process whitelist."""
        uid = str(user_id or "")
        return bool(uid) and uid in self._whitelist_ids()

    def _permanent_shield_ids(self) -> set[str]:
        """Ids shielded everywhere (config ``blacklist_ids``)."""
        return self._parse_id_list(self.config.get("blacklist_ids", []), ())

    def _is_permanently_shielded(self, user_id: str) -> bool:
        """Whether user_id is permanently shielded in every chat."""
        uid = str(user_id or "")
        return bool(uid) and uid in self._permanent_shield_ids()

    def _is_command_message(self, event: AstrMessageEvent) -> bool:
        """Whether a command handler (e.g. ``/要牌``, ``/help``) was activated.

        Shielding only blocks chat replies; command interactions must still
        work for shielded users. The wake prefix cannot be detected here:
        the pipeline strips it from ``message_str`` before handlers run
        (``waking_check`` stage). The waking stage stores every handler whose
        filters passed in the ``activated_handlers`` extra — that includes
        plain message listeners, so this only counts handlers carrying a
        command-type filter, mirroring the core's own command/listener split.

        Args:
            event: Incoming message event.

        Returns:
            True when at least one activated handler is a command.
        """
        for handler in event.get_extra("activated_handlers") or []:
            for event_filter in getattr(handler, "event_filters", None) or ():
                if isinstance(
                    event_filter, (CommandFilter, CommandGroupFilter, RegexFilter)
                ):
                    return True
        return False

    def _card_tamper_enabled(self) -> bool:
        """Whether bot group-card protection is enabled."""
        return bool(self.config.get("card_tamper_protect", True))

    def _bot_group_card(self) -> str:
        """Desired group card for the bot itself."""
        card = str(
            self.config.get("bot_group_card", DEFAULT_BOT_GROUP_CARD) or ""
        ).strip()
        return card or DEFAULT_BOT_GROUP_CARD

    def _card_tamper_punish_ids(self) -> set[str]:
        """Fallback QQ ids to mute when the card changer is unknown."""
        return self._parse_id_list(
            self.config.get(
                "card_tamper_punish_ids", list(DEFAULT_CARD_TAMPER_PUNISH_IDS)
            ),
            DEFAULT_CARD_TAMPER_PUNISH_IDS,
        )

    def _card_tamper_mute_seconds(self) -> int:
        """Extra mute seconds applied on each card-tamper punish."""
        return max(
            1,
            int(
                self.config.get(
                    "card_tamper_mute_seconds", DEFAULT_CARD_TAMPER_MUTE_SECONDS
                )
            ),
        )

    def _card_tamper_reply(self) -> str:
        """Group reply text after restoring a tampered bot card."""
        text = str(
            self.config.get("card_tamper_reply", DEFAULT_CARD_TAMPER_REPLY) or ""
        ).strip()
        return text or DEFAULT_CARD_TAMPER_REPLY

    def _reply_text(self, minutes: int) -> str:
        """Build warning text with the mute minutes filled in.

        Args:
            minutes: Mute duration in minutes to show in the reply.

        Returns:
            Warning message string.
        """
        template = str(
            self.config.get("reply_text", DEFAULT_REPLY_TEMPLATE) or ""
        ).strip()
        if not template:
            template = DEFAULT_REPLY_TEMPLATE
        try:
            return template.format(minutes=minutes, count=minutes)
        except (KeyError, ValueError, IndexError):
            return DEFAULT_REPLY_TEMPLATE.format(minutes=minutes)

    def _nickname_reply_text(self, minutes: int) -> str:
        """Build warning text for forbidden group-card / nickname hits.

        Args:
            minutes: Mute duration in minutes to show in the reply.

        Returns:
            Warning message string.
        """
        template = str(
            self.config.get("nickname_reply_text", DEFAULT_NICKNAME_REPLY) or ""
        ).strip()
        if not template:
            template = DEFAULT_NICKNAME_REPLY
        try:
            return template.format(minutes=minutes, count=minutes)
        except (KeyError, ValueError, IndexError):
            return DEFAULT_NICKNAME_REPLY.format(minutes=minutes)

    def _sender_card(self, event: AstrMessageEvent) -> str:
        """Read raw group card / nickname from the event (prefer platform card).

        Args:
            event: Incoming message event.

        Returns:
            Group card or account nickname string (may be empty).
        """
        try:
            raw = getattr(getattr(event, "message_obj", None), "raw_message", None)
            sender = None
            if isinstance(raw, dict):
                sender = raw.get("sender")
            elif raw is not None:
                sender = raw.get("sender") if hasattr(raw, "get") else None
            if isinstance(sender, dict):
                card = str(sender.get("card") or "").strip()
                nick = str(sender.get("nickname") or "").strip()
                if card:
                    return card
                if nick:
                    return nick
        except Exception:  # noqa: BLE001
            pass
        try:
            return str(event.get_sender_name() or "").strip()
        except Exception:  # noqa: BLE001
            return ""

    def _accomplice_reply(self) -> str:
        text = str(
            self.config.get("accomplice_reply", DEFAULT_ACCOMPLICE_REPLY) or ""
        ).strip()
        return text or DEFAULT_ACCOMPLICE_REPLY

    def _exempt_admins(self) -> bool:
        return bool(self.config.get("exempt_admins", True))

    def _is_astrbot_admin_id(self, user_id: str) -> bool:
        """Whether user_id is listed in AstrBot admins_id."""
        try:
            admins = self.context.get_config().get("admins_id", []) or []
        except Exception:  # noqa: BLE001
            return False
        return str(user_id) in {str(a) for a in admins}

    def _sender_is_super_admin(self, event: AstrMessageEvent) -> bool:
        """Whether the message sender is a super admin (AstrBot ``admins_id``).

        Args:
            event: Message event that triggered the LLM tool.

        Returns:
            True only when the sender id is in the global admin list.
        """
        return self._is_astrbot_admin_id(str(event.get_sender_id() or ""))

    def _collect_at_ids(self, event: AstrMessageEvent) -> list[str]:
        """Collect @ targets from the message chain.

        Args:
            event: Group message event (typically from a linked bot).

        Returns:
            Distinct user ids that were @-mentioned, excluding self / all / linked bots.
        """
        skip = (
            {str(event.get_self_id() or ""), "all", "everyone", "qq_official"}
            | self._linked_bot_ids()
            | self._whitelist_ids()
        )
        return [qq for qq, _hint in self._collect_at_targets(event) if qq not in skip]

    def _collect_at_targets(self, event: AstrMessageEvent) -> list[tuple[str, str]]:
        """Collect @ targets as ``(user_id, hint_name)`` for card lookup.

        Accepts OneBot numeric QQ ids and qq_official member openids
        (At components, ``<@openid>`` tags, and 32-char hex ids).

        Args:
            event: Group message event.

        Returns:
            Distinct ``(user_id, hint_name)`` pairs, excluding ``all`` and self.
        """
        targets: list[tuple[str, str]] = []
        seen: set[str] = set()
        self_id = str(event.get_self_id() or "")

        def add_target(raw: object, hint: str = "") -> None:
            qq = str(raw or "").strip()
            if (
                not qq
                or qq in {"all", "everyone", "qq_official", self_id}
                or qq in seen
            ):
                return
            seen.add(qq)
            targets.append((qq, str(hint or "").strip()))

        try:
            for comp in event.get_messages():
                if isinstance(comp, At) and comp.qq:
                    add_target(comp.qq, str(getattr(comp, "name", "") or ""))
                elif isinstance(comp, Plain) and comp.text:
                    for match in re.finditer(r"<@!?([A-Za-z0-9_-]+)>", str(comp.text)):
                        add_target(match.group(1))
                    for match in re.finditer(
                        r"@(\d{5,}|[A-Fa-f0-9]{32})", str(comp.text)
                    ):
                        add_target(match.group(1))
        except Exception as exc:  # noqa: BLE001
            logger.warning("弧光护卫：解析 At 目标失败: %s", exc)

        text = event.get_message_str() or event.message_str or ""
        for match in re.finditer(r"<@!?([A-Za-z0-9_-]+)>", text):
            add_target(match.group(1))
        for match in re.finditer(r"@(\d{5,}|[A-Fa-f0-9]{32})", text):
            add_target(match.group(1))

        raw = getattr(getattr(event, "message_obj", None), "raw_message", None)
        mentions = None
        if isinstance(raw, dict):
            mentions = raw.get("mentions")
        elif raw is not None:
            mentions = getattr(raw, "mentions", None)
        if isinstance(mentions, list | tuple):
            for mention in mentions:
                if isinstance(mention, dict):
                    if mention.get("is_you") is True:
                        continue
                    add_target(
                        mention.get("id")
                        or mention.get("member_openid")
                        or mention.get("user_openid"),
                        str(mention.get("username") or mention.get("name") or ""),
                    )
                    continue
                if getattr(mention, "is_you", False) is True:
                    continue
                add_target(
                    getattr(mention, "id", None)
                    or getattr(mention, "member_openid", None)
                    or getattr(mention, "user_openid", None),
                    str(
                        getattr(mention, "username", None)
                        or getattr(mention, "name", None)
                        or ""
                    ),
                )
        return targets

    @staticmethod
    def _nickname_from_at_label(label: str, qq: str) -> str:
        """Extract group nickname from ``format_platform_user_label`` output.

        Args:
            label: e.g. ``QQ用户123(sbenmo)`` or ``QQ用户123``.
            qq: Target QQ id.

        Returns:
            Nickname inside parentheses, or empty string.
        """
        text = str(label or "").strip()
        if not text:
            return ""
        # QQ用户123(群昵称) / QQ用户123（群昵称）
        match = re.search(
            rf"QQ用户{re.escape(str(qq))}\s*[（(](.+?)[）)]\s*$",
            text,
        )
        if match:
            return match.group(1).strip()
        # Fallback: label is already a bare nickname.
        if text != str(qq) and not text.startswith("QQ用户"):
            return text
        return ""

    async def _get_group_member_card(self, bot, group_id: str, user_id: str) -> str:
        """Fetch a member's group card, falling back to account nickname.

        Args:
            bot: aiocqhttp bot client.
            group_id: QQ group id.
            user_id: QQ user id.

        Returns:
            Card / nickname string (may be empty).
        """
        gid = str(group_id or "").strip()
        uid = str(user_id or "").strip()
        if not gid or not uid or bot is None:
            return ""
        if not gid.isdigit() or not uid.isdigit():
            return ""
        info = None
        try:
            if hasattr(bot, "get_group_member_info"):
                info = await bot.get_group_member_info(
                    group_id=int(gid),
                    user_id=int(uid),
                    no_cache=True,
                )
            elif hasattr(bot, "call_action"):
                info = await bot.call_action(
                    "get_group_member_info",
                    group_id=int(gid),
                    user_id=int(uid),
                    no_cache=True,
                )
        except Exception as exc:  # noqa: BLE001
            logger.debug(
                "弧光护卫：查询群名片失败 group=%s user=%s err=%s",
                gid,
                uid,
                exc,
            )
            return ""
        if not isinstance(info, dict):
            return ""
        card = str(info.get("card") or "").strip()
        nick = str(info.get("nickname") or "").strip()
        return card or nick

    async def _text_with_at_cards_expanded(
        self, event: AstrMessageEvent, text: str
    ) -> str:
        """Rebuild scan text so @QQ is also checked as @群名片.

        People set group cards to forbidden words (e.g. ``sbenmo``); chat UIs
        show ``@sbenmo`` while AstrBot / LLM often only see ``@QQ``. Resolve
        each @ target's card and substitute / append it for detection.

        Args:
            event: Incoming group message event.
            text: Original message text (and OCR text if already merged).

        Returns:
            Text augmented with @-target group cards.
        """
        targets = self._collect_at_targets(event)
        if not targets:
            return text

        bot = getattr(event, "bot", None)
        gid = str(event.get_group_id() or "")
        expanded = str(text or "")
        card_bits: list[str] = []

        for qq, hint in targets:
            card = self._nickname_from_at_label(hint, qq)
            if not card:
                card = await self._get_group_member_card(bot, gid, qq)
            card = str(card or "").strip()
            if not card:
                continue
            card_bits.append(card)
            # Replace bare @QQ / CQ-like leftovers with @群名片.
            expanded = re.sub(
                rf"@{re.escape(qq)}(?!\d)",
                f"@{card}",
                expanded,
            )
            # Common AstrBot label form: QQ用户QQ(昵称)(QQ) or ...(QQ)
            expanded = expanded.replace(f"({qq})", f"({card})")

        if not card_bits:
            return text

        # Always append cards so detection still works if replace missed.
        suffix = "\n".join(f"@{c}" for c in card_bits)
        merged = f"{expanded}\n{suffix}".strip()
        if self._count_forbidden(merged) > self._count_forbidden(text):
            logger.info(
                "弧光护卫：@群名片补全命中 group=%s sender=%s cards=%s",
                gid,
                event.get_sender_id(),
                card_bits,
            )
        return merged

    async def _get_remaining_mute_seconds(
        self, bot, group_id: str, user_id: str
    ) -> int | None:
        """Read how many mute seconds remain for a group member.

        Uses OneBot ``get_group_member_info`` when available.

        Args:
            bot: Platform bot client.
            group_id: Group id.
            user_id: QQ number.

        Returns:
            Remaining mute seconds (>= 0), or ``None`` if the lookup failed
            (caller must not treat failure as "already unmuted").
        """
        gid = str(group_id)
        uid = str(user_id)
        if _is_official_target(bot, gid, uid):
            return await _official_query_remaining(bot, gid, uid)
        if not gid.isdigit() or not uid.isdigit():
            return None
        info = None
        try:
            if hasattr(bot, "get_group_member_info"):
                info = await bot.get_group_member_info(
                    group_id=int(group_id),
                    user_id=int(user_id),
                    no_cache=True,
                )
            elif hasattr(bot, "call_action"):
                info = await bot.call_action(
                    "get_group_member_info",
                    group_id=int(group_id),
                    user_id=int(user_id),
                    no_cache=True,
                )
        except Exception as exc:  # noqa: BLE001
            logger.warning(
                "弧光护卫：查询禁言剩余失败 group=%s user=%s err=%s",
                group_id,
                user_id,
                exc,
            )
            return None
        if not isinstance(info, dict):
            return None
        raw = info.get("shut_up_timestamp")
        if raw is None:
            raw = info.get("shut_up_time")
        try:
            value = int(raw or 0)
        except (TypeError, ValueError):
            return 0
        if value <= 0:
            return 0
        now = int(time.time())
        # Millisecond end timestamp.
        if value > 10_000_000_000:
            value //= 1000
        # Unix end timestamp (seconds).
        if value > now:
            return value - now
        # Already remaining seconds (ignore stale past absolute stamps).
        if value <= MAX_MUTE_SECONDS:
            return value
        return 0

    def _tracked_remaining_seconds(self, group_id: str, user_id: str) -> int:
        """Remaining seconds from local accumulate tracker.

        Args:
            group_id: QQ group id.
            user_id: QQ user id.

        Returns:
            Remaining tracked mute seconds (>= 0).
        """
        key = (str(group_id), str(user_id))
        until = int(self._mute_until.get(key, 0) or 0)
        return max(0, until - int(time.time()))

    def _reconcile_tracked_with_qq(
        self, group_id: str, user_id: str, qq_remaining: int
    ) -> int:
        """Reconcile local mute tracker with live QQ remaining.

        QQ is the source of truth for early unmute and external reductions.
        The local tracker is only preferred when it is shorter than QQ
        remaining (NapCat/QQ often round short bans up to ~60s).

        Args:
            group_id: QQ group id.
            user_id: QQ user id.
            qq_remaining: Live remaining seconds from QQ (>= 0).

        Returns:
            Effective remaining seconds to use as accumulate/pardon base.
        """
        key = (str(group_id), str(user_id))
        tracked = self._tracked_remaining_seconds(group_id, user_id)
        qq = max(0, int(qq_remaining))

        if qq <= 0:
            if tracked > 0:
                logger.info(
                    "弧光护卫：QQ 已无禁言，清除过期本地追踪 group=%s user=%s "
                    "stale_tracked=%ss",
                    group_id,
                    user_id,
                    tracked,
                )
            self._mute_until.pop(key, None)
            return 0

        if tracked <= 0:
            return qq

        if qq < tracked:
            # Admin / other tool shortened or partially unmuted.
            logger.info(
                "弧光护卫：QQ 剩余低于本地追踪，按 QQ 对齐 group=%s user=%s "
                "tracked=%ss -> qq=%ss",
                group_id,
                user_id,
                tracked,
                qq,
            )
            self._mute_until[key] = int(time.time()) + qq
            return qq

        # tracked <= qq: keep logical intended remaining (avoid rounding inflate).
        return tracked

    async def _resolve_mute_base(
        self, bot, group_id: str, user_id: str
    ) -> tuple[int, int | None]:
        """Resolve accumulate/pardon base against QQ + local tracker.

        Args:
            bot: aiocqhttp bot client.
            group_id: QQ group id.
            user_id: QQ user id.

        Returns:
            ``(base_remaining, qq_remaining_or_none)``. When QQ lookup fails,
            ``qq_remaining_or_none`` is ``None`` and base falls back to tracker.
        """
        qq_remaining = await self._get_remaining_mute_seconds(bot, group_id, user_id)
        if qq_remaining is None:
            return self._tracked_remaining_seconds(group_id, user_id), None
        return self._reconcile_tracked_with_qq(group_id, user_id, qq_remaining), (
            qq_remaining
        )

    def _remember_mute_until(
        self, group_id: str, user_id: str, intended_seconds: int
    ) -> None:
        """Record intended mute end time after a successful accumulate ban.

        Args:
            group_id: QQ group id.
            user_id: QQ user id.
            intended_seconds: Logical mute seconds (base + added), not QQ display.
        """
        key = (str(group_id), str(user_id))
        total = max(0, int(intended_seconds))
        if total <= 0:
            self._mute_until.pop(key, None)
            return
        self._mute_until[key] = int(time.time()) + total
        # Drop expired entries opportunistically.
        now = int(time.time())
        expired = [k for k, until in self._mute_until.items() if int(until) <= now]
        for k in expired:
            self._mute_until.pop(k, None)

    def _recall_on_hit(self) -> bool:
        """Whether violating group messages should be recalled automatically."""
        return bool(self.config.get("recall_on_hit", DEFAULT_RECALL_ON_HIT))

    async def _recall_message_by_id(self, bot, group_id: str, message_id: str) -> bool:
        """Recall one group message by id (best effort).

        qq_official uses the admin recall API (2-minute window, normal
        members' messages only); aiocqhttp uses OneBot ``delete_msg``.
        Failures are logged only so callers can continue.

        Args:
            bot: Platform bot client.
            group_id: Group id or official group openid.
            message_id: Message id to recall.

        Returns:
            True when the recall API call succeeded.
        """
        gid = str(group_id or "").strip()
        mid = str(message_id or "").strip()
        if not gid or not mid or bot is None:
            return False
        try:
            if _is_official_target(bot, gid, mid):
                await _official_recall_message(bot, gid, mid)
            elif gid.isdigit() and mid.isdigit() and hasattr(bot, "call_action"):
                if hasattr(bot, "delete_msg"):
                    await bot.delete_msg(message_id=int(mid))
                else:
                    await bot.call_action("delete_msg", message_id=int(mid))
            else:
                return False
            return True
        except Exception as exc:  # noqa: BLE001
            logger.warning(
                "弧光护卫：撤回失败（超出 2 分钟窗口或无权限时属正常） group=%s mid=%s err=%s",
                gid,
                mid,
                exc,
            )
            return False

    async def _recall_message(self, event: AstrMessageEvent) -> None:
        """Best-effort recall of the offending group message.

        Args:
            event: Group message event whose message should be recalled.
        """
        if not self._recall_on_hit():
            return
        gid = str(event.get_group_id() or "").strip()
        mid = str(
            getattr(getattr(event, "message_obj", None), "message_id", "") or ""
        ).strip()
        if not await self._recall_message_by_id(getattr(event, "bot", None), gid, mid):
            return
        logger.info(
            "弧光护卫：已撤回违禁消息 group=%s user=%s mid=%s",
            gid,
            event.get_sender_id(),
            mid,
        )

    def _shield_file(self):
        """Return the persisted shield-until JSON path."""
        data_dir = StarTools.get_data_dir(PLUGIN_NAME)
        data_dir.mkdir(parents=True, exist_ok=True)
        return data_dir / "shield_until.json"

    def _load_shields(self) -> None:
        """Load timed ignore entries from plugin data dir."""
        path = self._shield_file()
        if not path.is_file():
            return
        try:
            raw = json.loads(path.read_text(encoding="utf-8"))
        except Exception as exc:  # noqa: BLE001
            logger.warning("弧光护卫：读取屏蔽列表失败: %s", exc)
            return
        now = int(time.time())
        items = raw if isinstance(raw, list) else []
        for item in items:
            if not isinstance(item, dict):
                continue
            gid = str(item.get("group_id") or "").strip()
            uid = str(item.get("user_id") or "").strip()
            until = int(item.get("until") or 0)
            if gid and uid and until > now:
                self._shield_until[(gid, uid)] = until

    def _save_shields(self) -> None:
        """Persist active timed-ignore entries."""
        now = int(time.time())
        items = []
        for (gid, uid), until in self._shield_until.items():
            if int(until or 0) > now:
                items.append({"group_id": gid, "user_id": uid, "until": int(until)})
        try:
            self._shield_file().write_text(
                json.dumps(items, ensure_ascii=False, indent=2),
                encoding="utf-8",
            )
        except Exception as exc:  # noqa: BLE001
            logger.warning("弧光护卫：保存屏蔽列表失败: %s", exc)

    def _shield_remaining(self, group_id: str, user_id: str) -> int:
        """Remaining timed-ignore seconds for one group member."""
        key = (str(group_id or ""), str(user_id or ""))
        until = int(self._shield_until.get(key, 0) or 0)
        left = until - int(time.time())
        if left <= 0:
            if key in self._shield_until:
                self._shield_until.pop(key, None)
            return 0
        return left

    def _remember_shield(self, group_id: str, user_id: str, seconds: int) -> None:
        """Set or clear timed ignore for a group member.

        Args:
            group_id: Group id or official group openid.
            user_id: QQ number or official member openid.
            seconds: Remaining ignore seconds; 0 clears the entry.
        """
        key = (str(group_id or ""), str(user_id or ""))
        total = max(0, int(seconds))
        if total <= 0:
            self._shield_until.pop(key, None)
        else:
            self._shield_until[key] = int(time.time()) + total
        now = int(time.time())
        expired = [k for k, until in self._shield_until.items() if int(until) <= now]
        for k in expired:
            self._shield_until.pop(k, None)
        self._save_shields()

    @staticmethod
    def _format_mute_duration(seconds: int) -> str:
        """Format mute seconds as a short Chinese duration string."""
        total = max(0, int(seconds))
        if total < 60:
            return f"{total}秒"
        minutes, rem = divmod(total, 60)
        if rem == 0:
            return f"{minutes}分钟"
        return f"{minutes}分{rem}秒"

    async def _announce_mute_stack(
        self,
        bot,
        group_id: str,
        user_id: str,
        before: int,
        added: int,
        total: int,
        event: AstrMessageEvent | None = None,
    ) -> None:
        """Send ``@user 之前禁言时长a，叠加禁言时长b，总禁言时长c`` in group.

        Args:
            bot: Platform bot client.
            group_id: Group id or official group openid.
            user_id: QQ number or official member openid.
            before: Remaining mute seconds before this stack.
            added: Seconds added in this stack.
            total: Intended total remaining after stack.
            event: Optional live event; preferred on qq_official: ``At`` is
                sent as markdown so ``<qqbot-at-user id="openid" />`` renders
                as a real mention (plain text would show the raw tag); the
                core falls back to plain text if markdown is rejected.
        """
        gid = str(group_id or "").strip()
        uid = str(user_id or "").strip()
        if not gid or not uid:
            return
        text = (
            f" 之前禁言时长{self._format_mute_duration(before)}，"
            f"叠加禁言时长{self._format_mute_duration(added)}，"
            f"总禁言时长{self._format_mute_duration(total)}"
        )
        if event is not None:
            # Markdown mode: the <qqbot-at-user> tag only renders as a real
            # mention there; plain text would show the raw tag. The core
            # retries with plain content if markdown is not permitted.
            result = event.chain_result([At(qq=uid), Plain(text)])
            result.use_markdown_ = True
            try:
                await event.send(result)
            except Exception as exc:  # noqa: BLE001
                logger.warning(
                    "弧光护卫：叠加禁言播报失败 group=%s user=%s err=%s",
                    gid,
                    uid,
                    exc,
                )
            return
        if not gid.isdigit() or not uid.isdigit() or bot is None:
            return
        message = [
            {"type": "at", "data": {"qq": uid}},
            {"type": "text", "data": {"text": text}},
        ]
        try:
            if hasattr(bot, "send_group_msg"):
                await bot.send_group_msg(group_id=int(gid), message=message)
            elif hasattr(bot, "call_action"):
                await bot.call_action(
                    "send_group_msg",
                    group_id=int(gid),
                    message=message,
                )
            else:
                logger.warning("弧光护卫：无法发送叠加禁言播报 group=%s", gid)
        except Exception as exc:  # noqa: BLE001
            logger.warning(
                "弧光护卫：叠加禁言播报失败 group=%s user=%s err=%s",
                gid,
                uid,
                exc,
            )

    async def _ban_accumulate_on_bot(
        self,
        bot,
        group_id: str,
        user_id: str,
        add_seconds: int,
        *,
        self_id: str = "",
        respect_whitelist: bool = True,
        announce: bool = True,
        event: AstrMessageEvent | None = None,
    ) -> dict:
        """Mute with accumulation using an explicit bot/group context.

        Mute and shield are independent: when the mute is impossible or
        fails, this only returns ``error`` and never creates a shield.

        Args:
            bot: Platform bot client (aiocqhttp or qq_official).
            group_id: Group id or official group openid.
            user_id: QQ number or official member openid to mute.
            add_seconds: Seconds to add on top of current remaining mute.
            self_id: Optional bot self id.
            respect_whitelist: If True, skip mute for whitelist users.
            announce: If True, announce before/add/total durations in group.
            event: Optional live event used for official @ announce.

        Returns:
            Dict with ``ok``, ``remaining_before``, ``added``, ``total``,
            and optional ``error``.
        """
        result = {
            "ok": False,
            "remaining_before": 0,
            "added": 0,
            "total": 0,
            "error": "",
        }
        gid = str(group_id or "").strip()
        uid = str(user_id or "").strip()
        can_onebot = bool(
            bot is not None
            and gid.isdigit()
            and uid.isdigit()
            and (hasattr(bot, "set_group_ban") or hasattr(bot, "call_action"))
        )
        can_official = _is_official_target(bot, gid, uid)
        if not gid or not uid:
            result["error"] = "当前会话无法禁言（缺少群号或禁言接口）"
            return result
        if (self_id and uid == str(self_id)) or uid in self._linked_bot_ids():
            result["error"] = "不能禁言机器人自身或联动机器人"
            return result
        if respect_whitelist and self._is_whitelisted(uid):
            result["error"] = "白名单用户不可禁言"
            logger.info("弧光护卫：跳过白名单禁言 user=%s", uid)
            return result
        if self._exempt_admins() and self._is_astrbot_admin_id(uid):
            result["error"] = "管理员豁免，未禁言"
            logger.info("弧光护卫：跳过管理员禁言 user=%s", uid)
            return result

        add = max(0, int(add_seconds))
        if add <= 0:
            result["error"] = "追加禁言秒数必须大于 0"
            return result
        if not can_onebot and not can_official:
            result["error"] = "当前会话无法禁言（缺少群号或禁言接口）"
            return result

        # Accumulate rules:
        # - Prefer live QQ remaining when user was unmuted early / externally reduced.
        # - Prefer local tracker when it is shorter than QQ (short-ban rounding).
        # - Set duration = max(intended, qq_remaining) so we never shorten an
        #   active QQ mute due to rounding.
        base, qq_remaining = await self._resolve_mute_base(bot, gid, uid)
        qq_for_set = int(qq_remaining or 0)
        tracked_remaining = self._tracked_remaining_seconds(gid, uid)
        intended = min(base + add, MAX_MUTE_SECONDS)
        duration = min(max(intended, qq_for_set), MAX_MUTE_SECONDS)
        result["remaining_before"] = base
        result["added"] = add
        result["total"] = intended

        try:
            if can_official:
                # Official admin API takes an absolute RFC3339 expiry, so the
                # live-queried base makes the accumulate precise (no minute
                # rounding). An already-muted member gets "update", a fresh
                # mute gets "add".
                await _official_set_mute(
                    bot,
                    gid,
                    uid,
                    op="update" if base > 0 else "add",
                    expire_epoch=int(time.time()) + intended,
                )
            elif hasattr(bot, "set_group_ban"):
                await bot.set_group_ban(
                    group_id=int(gid),
                    user_id=int(uid),
                    duration=int(duration),
                )
            else:
                await bot.call_action(
                    "set_group_ban",
                    group_id=int(gid),
                    user_id=int(uid),
                    duration=int(duration),
                )
            self._remember_mute_until(gid, uid, intended)
        except Exception as exc:  # noqa: BLE001
            logger.error(
                "弧光护卫禁言失败 user=%s duration=%s err=%s", uid, duration, exc
            )
            result["error"] = str(exc)
            return result

        # The mute already took effect: a failing announce must neither turn a
        # successful mute into a reported failure nor break the caller's flow.
        result["ok"] = True
        logger.info(
            "弧光护卫累加禁言：group=%s user=%s qq=%s tracked=%ss base=%ss "
            "+ add=%ss -> intended=%ss set=%ss whitelist=%s",
            gid,
            uid,
            "fail" if qq_remaining is None else f"{qq_remaining}s",
            tracked_remaining,
            base,
            add,
            intended,
            duration,
            respect_whitelist,
        )
        if announce:
            try:
                await self._announce_mute_stack(
                    bot, gid, uid, base, add, intended, event=event
                )
            except Exception as exc:  # noqa: BLE001
                logger.warning(
                    "弧光护卫：叠加禁言播报失败 group=%s user=%s err=%s",
                    gid,
                    uid,
                    exc,
                )
        return result

    async def _ban_accumulate(
        self,
        event: AstrMessageEvent,
        user_id: str,
        add_seconds: int,
        *,
        respect_whitelist: bool = True,
        announce: bool = True,
    ) -> dict:
        """Mute with accumulation: remaining + add_seconds (via event).

        Args:
            event: Current message event.
            user_id: QQ user id to mute.
            add_seconds: Seconds to add on top of current remaining mute.
            respect_whitelist: If True, skip mute for whitelist users.
            announce: If True, announce before/add/total durations in group.

        Returns:
            Dict with ``ok``, ``remaining_before``, ``added``, ``total``,
            and optional ``error``.
        """
        uid = str(user_id or "").strip()
        if (
            self._exempt_admins()
            and uid == str(event.get_sender_id() or "")
            and (event.is_admin() or self._is_astrbot_admin_id(uid))
        ):
            logger.info("弧光护卫：跳过管理员禁言 user=%s", uid)
            return {
                "ok": False,
                "remaining_before": 0,
                "added": 0,
                "total": 0,
                "error": "管理员豁免，未禁言",
            }
        return await self._ban_accumulate_on_bot(
            getattr(event, "bot", None),
            str(event.get_group_id() or ""),
            uid,
            int(add_seconds),
            self_id=str(event.get_self_id() or ""),
            respect_whitelist=respect_whitelist,
            announce=announce,
            event=event,
        )

    async def _ban(
        self,
        event: AstrMessageEvent,
        user_id: str,
        seconds: int,
        *,
        respect_whitelist: bool = True,
        announce: bool = True,
    ) -> bool:
        """Mute one user; duration is remaining mute + ``seconds``.

        Args:
            event: Current message event.
            user_id: QQ user id to mute.
            seconds: Extra mute seconds to accumulate.
            respect_whitelist: If True, skip mute for whitelist users.
            announce: If True, announce before/add/total durations in group.

        Returns:
            True if the ban API call succeeded.
        """
        result = await self._ban_accumulate(
            event,
            user_id,
            seconds,
            respect_whitelist=respect_whitelist,
            announce=announce,
        )
        return bool(result.get("ok"))

    async def _punish(
        self,
        event: AstrMessageEvent,
        hit_count: int,
        *,
        send_reply: bool = True,
        from_nickname: bool = False,
        recall: bool = True,
    ) -> str:
        """Reply with warning and mute the message source by hit count.

        Args:
            event: Current message event (sender is the punish target).
            hit_count: How many forbidden sequences were found.
            send_reply: Whether to send the warning text via event.send.
            from_nickname: Use nickname/card warning text instead of message text.
            recall: Whether to recall the offending message (skipped for
                nickname hits, where the message itself is innocent).

        Returns:
            The warning text used (also suitable as LLM reply replacement).
        """
        hits = max(1, int(hit_count))
        seconds = min(hits * self._seconds_per_hit(), MAX_MUTE_SECONDS)
        minutes = max(1, (seconds + 59) // 60)
        reply = (
            self._nickname_reply_text(minutes)
            if from_nickname
            else self._reply_text(minutes)
        )
        sender_id = str(event.get_sender_id() or "")

        if recall and not from_nickname:
            await self._recall_message(event)

        if send_reply:
            try:
                await event.send(event.plain_result(reply))
            except Exception as exc:  # noqa: BLE001
                logger.error("弧光护卫发送警告失败: %s", exc)

        ok = await self._ban(event, sender_id, seconds)
        logger.info(
            "弧光护卫：命中=%s source=%s group=%s user=%s mute=%ss (%s分钟) ok=%s",
            hits,
            "nickname" if from_nickname else "message",
            event.get_group_id(),
            sender_id,
            seconds,
            minutes,
            ok,
        )
        return reply

    async def _punish_accomplice(self, event: AstrMessageEvent, hit_count: int) -> None:
        """Mute users @-mentioned by a linked bot that output forbidden text.

        Mute duration is hit_count × accomplice_mute_seconds (default 120s,
        i.e. 2 minutes per hit → 10 hits = 20 minutes).

        Args:
            event: Group message event from the linked bot.
            hit_count: How many forbidden sequences were in the bot message.
        """
        # Recall the linked bot's violating message itself (hides the insult),
        # even when no @ target is found, then punish its @ targets.
        await self._recall_message(event)

        at_ids = self._collect_at_ids(event)
        hits = max(1, int(hit_count))
        seconds = min(hits * self._accomplice_seconds_per_hit(), MAX_MUTE_SECONDS)
        reply = self._accomplice_reply()
        if not at_ids:
            logger.info(
                "弧光护卫连坐：linked bot=%s 命中=%s 但无 @ 目标",
                event.get_sender_id(),
                hits,
            )
            return

        try:
            await event.send(event.plain_result(reply))
        except Exception as exc:  # noqa: BLE001
            logger.error("弧光护卫连坐发送失败: %s", exc)

        minutes = max(1, (seconds + 59) // 60)
        for uid in at_ids:
            ok = await self._ban(event, uid, seconds)
            logger.info(
                "弧光护卫连坐：教唆犯=%s 命中=%s mute=%ss (%s分钟) ok=%s (from bot=%s)",
                uid,
                hits,
                seconds,
                minutes,
                ok,
                event.get_sender_id(),
            )

    def _pardon_enabled(self) -> bool:
        """Whether pardon (private and/or group) is enabled."""
        return bool(self.config.get("pardon_enabled", True))

    def _pardon_in_group(self) -> bool:
        """Whether group-chat apology phrases are accepted."""
        return bool(self.config.get("pardon_in_group", True))

    def _pardon_reduce_seconds(self) -> int:
        return max(
            1,
            int(
                self.config.get("pardon_reduce_seconds", DEFAULT_PARDON_REDUCE_SECONDS)
            ),
        )

    def _pardon_cooldown_seconds(self) -> int:
        return max(
            0,
            int(
                self.config.get(
                    "pardon_cooldown_seconds", DEFAULT_PARDON_COOLDOWN_SECONDS
                )
            ),
        )

    def _pardon_phrase(self) -> str:
        phrase = str(
            self.config.get("pardon_phrase", DEFAULT_PARDON_PHRASE) or ""
        ).strip()
        return phrase or DEFAULT_PARDON_PHRASE

    @staticmethod
    def _normalize_pardon_text(text: str) -> str:
        """NFKC + strip spaces for apology phrase matching."""
        normalized = unicodedata.normalize("NFKC", text or "")
        return re.sub(r"\s+", "", normalized)

    def _is_pardon_phrase(self, text: str) -> bool:
        """Whether message text contains the configured apology phrase."""
        needle = self._normalize_pardon_text(self._pardon_phrase())
        hay = self._normalize_pardon_text(text)
        return bool(needle) and needle in hay

    def _pardon_cooldown_reply(self, left_seconds: int) -> str:
        """Build cooldown reply for an active pardon cooldown.

        Args:
            left_seconds: Remaining cooldown seconds.

        Returns:
            Formatted cooldown text.
        """
        left = max(0, int(left_seconds))
        minutes = max(1, (left + 59) // 60)
        template = (
            str(
                self.config.get("pardon_cooldown_reply", DEFAULT_PARDON_COOLDOWN) or ""
            ).strip()
            or DEFAULT_PARDON_COOLDOWN
        )
        try:
            return template.format(minutes=minutes, seconds=left)
        except (KeyError, ValueError, IndexError):
            return DEFAULT_PARDON_COOLDOWN.format(seconds=left, minutes=minutes)

    def _pardon_ok_reply(self, remaining_after: int, groups: int = 1) -> str:
        """Build success reply after mute/shield reduction.

        Args:
            remaining_after: Max remaining punishment seconds after reduce.
            groups: How many groups were updated.

        Returns:
            Formatted success text.
        """
        after = max(0, int(remaining_after))
        if after <= 0:
            return DEFAULT_PARDON_CLEARED
        minutes = max(0, (after + 59) // 60)
        template = (
            str(self.config.get("pardon_ok_reply", DEFAULT_PARDON_OK) or "").strip()
            or DEFAULT_PARDON_OK
        )
        try:
            return template.format(minutes=minutes, groups=int(groups))
        except (KeyError, ValueError, IndexError):
            return DEFAULT_PARDON_OK.format(minutes=minutes)

    def _pardon_none_reply(self) -> str:
        """Reply when the user has no active mute/shield to reduce."""
        return (
            str(self.config.get("pardon_none_reply", DEFAULT_PARDON_NONE) or "").strip()
            or DEFAULT_PARDON_NONE
        )

    async def _list_bot_group_ids(self, bot) -> list[str]:
        """List group ids the bot is in via OneBot get_group_list."""
        raw = None
        try:
            if hasattr(bot, "get_group_list"):
                raw = await bot.get_group_list()
            elif hasattr(bot, "call_action"):
                raw = await bot.call_action("get_group_list")
        except Exception as exc:  # noqa: BLE001
            logger.warning("弧光护卫：获取群列表失败: %s", exc)
            return []
        if not isinstance(raw, list):
            return []
        out: list[str] = []
        for item in raw:
            if not isinstance(item, dict):
                continue
            gid = item.get("group_id")
            if gid is None:
                continue
            s = str(gid).strip()
            if s and s not in out:
                out.append(s)
        return out

    async def _set_mute_duration(
        self, bot, group_id: str, user_id: str, duration: int
    ) -> bool:
        """Set absolute mute duration for a member (0 = unmute)."""
        try:
            gid = str(group_id)
            uid = str(user_id)
            if _is_official_target(bot, gid, uid):
                # Official admin API: absolute expiry; 0 = immediate unmute.
                op = "del" if int(duration) <= 0 else "update"
                await _official_set_mute(
                    bot,
                    gid,
                    uid,
                    op=op,
                    expire_epoch=int(time.time()) + max(0, int(duration)),
                )
            elif not (
                gid.isdigit()
                and uid.isdigit()
                and (hasattr(bot, "set_group_ban") or hasattr(bot, "call_action"))
            ):
                return False
            elif hasattr(bot, "set_group_ban"):
                await bot.set_group_ban(
                    group_id=int(gid),
                    user_id=int(uid),
                    duration=int(duration),
                )
            else:
                await bot.call_action(
                    "set_group_ban",
                    group_id=int(gid),
                    user_id=int(uid),
                    duration=int(duration),
                )
            self._remember_mute_until(gid, uid, int(duration))
            return True
        except Exception as exc:  # noqa: BLE001
            logger.warning(
                "弧光护卫：设置禁言失败 group=%s user=%s duration=%s err=%s",
                group_id,
                user_id,
                duration,
                exc,
            )
            return False

    async def _unmute_on_bot(
        self,
        bot,
        group_id: str,
        user_id: str,
        *,
        self_id: str = "",
    ) -> dict:
        """Lift the accumulated mute of one group member (shield untouched).

        Args:
            bot: Platform bot client (aiocqhttp or qq_official).
            group_id: Group id or official group openid.
            user_id: QQ number or official member openid to release.
            self_id: Optional bot self id (skip if equal to user_id).

        Returns:
            Dict with ``ok``, ``had_mute``, ``mute_cleared``, and optional
            ``error``.
        """
        result = {
            "ok": False,
            "had_mute": False,
            "mute_cleared": False,
            "error": "",
        }
        gid = str(group_id or "").strip()
        uid = str(user_id or "").strip()
        if not gid or not uid:
            result["error"] = "缺少群号或用户 ID"
            return result
        if (self_id and uid == str(self_id)) or uid in self._linked_bot_ids():
            result["error"] = "不能解除机器人自身或联动机器人的禁言"
            return result

        mute_remaining = 0
        if bot is not None:
            mute_remaining, _ = await self._resolve_mute_base(bot, gid, uid)
        else:
            mute_remaining = self._tracked_remaining_seconds(gid, uid)
        result["had_mute"] = mute_remaining > 0
        if not result["had_mute"]:
            result["error"] = "该用户当前没有被禁言"
            return result

        if bot is not None:
            result["mute_cleared"] = await self._set_mute_duration(bot, gid, uid, 0)
        else:
            self._remember_mute_until(gid, uid, 0)
            result["mute_cleared"] = True

        result["ok"] = bool(result["mute_cleared"])
        if not result["ok"]:
            result["error"] = "解除禁言失败"
        logger.info(
            "弧光护卫解除禁言：group=%s user=%s had_mute=%ss mute_cleared=%s",
            gid,
            uid,
            mute_remaining,
            result["mute_cleared"],
        )
        return result

    async def _unmute(
        self,
        event: AstrMessageEvent,
        user_id: str,
    ) -> dict:
        """Lift the mute of one member in the current group (shield untouched).

        Args:
            event: Current group message event.
            user_id: QQ number or official member openid to release.

        Returns:
            Dict with ``ok``, ``had_mute``, ``mute_cleared``, and optional
            ``error``.
        """
        return await self._unmute_on_bot(
            getattr(event, "bot", None),
            str(event.get_group_id() or ""),
            str(user_id or ""),
            self_id=str(event.get_self_id() or ""),
        )

    async def _pardon_reduce_mutes(
        self, bot, user_id: str, reduce_seconds: int
    ) -> dict:
        """Reduce remaining mute by ``reduce_seconds`` in every muted group."""
        result: dict = {
            "ok": False,
            "groups": 0,
            "group_ids": [],
            "max_remaining_after": 0,
        }
        uid = str(user_id or "").strip()
        if not uid or bot is None:
            return result
        reduce = max(1, int(reduce_seconds))
        max_after = 0
        changed_groups: list[str] = []
        now = int(time.time())
        group_ids: list[str] = []
        for (gid, tracked_uid), until in list(self._mute_until.items()):
            if (
                str(tracked_uid) == uid
                and int(until or 0) > now
                and str(gid) not in group_ids
            ):
                group_ids.append(str(gid))
        for gid in await self._list_bot_group_ids(bot):
            if gid not in group_ids:
                group_ids.append(gid)
        for gid in group_ids:
            remaining, qq_remaining = await self._resolve_mute_base(bot, gid, uid)
            if remaining <= 0:
                continue
            new_duration = max(0, remaining - reduce)
            if not await self._set_mute_duration(bot, gid, uid, new_duration):
                continue
            changed_groups.append(gid)
            max_after = max(max_after, new_duration)
            logger.info(
                "弧光护卫求饶减刑：group=%s user=%s base=%ss qq=%s -> %ss",
                gid,
                uid,
                remaining,
                "fail" if qq_remaining is None else f"{qq_remaining}s",
                new_duration,
            )
        for (gid, tracked_uid), until in list(self._shield_until.items()):
            if str(tracked_uid) != uid:
                continue
            remaining = int(until or 0) - now
            if remaining <= 0:
                continue
            new_duration = max(0, remaining - reduce)
            self._remember_shield(str(gid), uid, new_duration)
            if str(gid) not in changed_groups:
                changed_groups.append(str(gid))
            max_after = max(max_after, new_duration)
            logger.info(
                "弧光护卫求饶减屏蔽：group=%s user=%s %ss -> %ss",
                gid,
                uid,
                remaining,
                new_duration,
            )
        result["ok"] = bool(changed_groups)
        result["groups"] = len(changed_groups)
        result["group_ids"] = changed_groups
        result["max_remaining_after"] = max_after
        return result

    async def _pardon_reduce_in_group(
        self,
        bot,
        group_id: str,
        user_id: str,
        reduce_seconds: int,
    ) -> dict:
        """Reduce mute and/or shield in one group only.

        Used by group-chat pardon (platforms without private chat / mute).

        Args:
            bot: Platform bot client (optional for shield-only reduce).
            group_id: Target group id or openid.
            user_id: Punished user id.
            reduce_seconds: Seconds to subtract from remaining punishment.

        Returns:
            Dict with ``ok``, ``groups``, ``group_ids``, ``max_remaining_after``.
        """
        result: dict = {
            "ok": False,
            "groups": 0,
            "group_ids": [],
            "max_remaining_after": 0,
        }
        gid = str(group_id or "").strip()
        uid = str(user_id or "").strip()
        if not gid or not uid:
            return result
        reduce = max(1, int(reduce_seconds))
        max_after = 0
        changed = False

        if bot is not None:
            remaining, qq_remaining = await self._resolve_mute_base(bot, gid, uid)
            if remaining > 0:
                new_duration = max(0, remaining - reduce)
                if await self._set_mute_duration(bot, gid, uid, new_duration):
                    changed = True
                    max_after = max(max_after, new_duration)
                    logger.info(
                        "弧光护卫群内求饶减刑：group=%s user=%s base=%ss qq=%s -> %ss",
                        gid,
                        uid,
                        remaining,
                        "fail" if qq_remaining is None else f"{qq_remaining}s",
                        new_duration,
                    )

        shield_remaining = self._shield_remaining(gid, uid)
        if shield_remaining > 0:
            new_duration = max(0, shield_remaining - reduce)
            self._remember_shield(gid, uid, new_duration)
            changed = True
            max_after = max(max_after, new_duration)
            logger.info(
                "弧光护卫群内求饶减屏蔽：group=%s user=%s %ss -> %ss",
                gid,
                uid,
                shield_remaining,
                new_duration,
            )

        if changed:
            result["ok"] = True
            result["groups"] = 1
            result["group_ids"] = [gid]
            result["max_remaining_after"] = max_after
        return result

    async def _process_pardon(
        self,
        bot,
        user_id: str,
        *,
        group_id: str | None = None,
    ) -> tuple[str, bool, dict]:
        """Apply pardon cooldown + mute/shield reduction.

        Args:
            bot: Platform bot client.
            user_id: Sender to reduce punishment for.
            group_id: If set, only reduce in this group (group-chat pardon).
                If None, reduce across all muted/shielded groups (private).

        Returns:
            ``(reply_text, success, result)`` where ``success`` is True only
            when mute/shield time was actually reduced.
        """
        uid = str(user_id or "").strip()
        empty: dict = {
            "ok": False,
            "groups": 0,
            "group_ids": [],
            "max_remaining_after": 0,
        }
        if not uid:
            return self._pardon_none_reply(), False, empty

        cooldown = self._pardon_cooldown_seconds()
        now = time.time()
        last = float(self._pardon_last_ts.get(uid) or 0)
        if cooldown > 0 and last > 0 and now - last < cooldown:
            left = int(cooldown - (now - last))
            return self._pardon_cooldown_reply(left), False, empty

        reduce = self._pardon_reduce_seconds()
        if group_id:
            result = await self._pardon_reduce_in_group(bot, str(group_id), uid, reduce)
        else:
            if bot is None:
                return "求饶失败：当前无法操作禁言。", False, empty
            result = await self._pardon_reduce_mutes(bot, uid, reduce)

        if not result.get("ok"):
            return self._pardon_none_reply(), False, result

        self._pardon_last_ts[uid] = now
        after = int(result.get("max_remaining_after") or 0)
        reply = self._pardon_ok_reply(after, int(result.get("groups") or 0))
        return reply, True, result

    def _pardon_broadcast_text(self, user_id: str, remaining_after: int) -> str:
        """Build the group broadcast text after a successful pardon."""
        minutes = (
            max(0, (int(remaining_after) + 59) // 60) if remaining_after > 0 else 0
        )
        hours = max(1, self._pardon_reduce_seconds() // 3600)
        template = (
            str(
                self.config.get("pardon_broadcast", DEFAULT_PARDON_BROADCAST) or ""
            ).strip()
            or DEFAULT_PARDON_BROADCAST
        )
        try:
            body = template.format(qq=user_id, minutes=minutes, hours=hours)
        except (KeyError, ValueError, IndexError):
            body = DEFAULT_PARDON_BROADCAST.format(minutes=minutes)
        if remaining_after <= 0:
            return (
                "跪下求饶了。鉴于认罪态度良好，特赦——禁言/屏蔽已全部解除。"
                "打屁股名额保留，下次违禁可直接预约。"
            )
        return body

    async def _broadcast_pardon(
        self,
        bot,
        group_ids: list[str],
        user_id: str,
        remaining_after: int,
    ) -> None:
        """Announce a successful private-chat pardon in affected groups."""
        if not group_ids or bot is None:
            return
        body = self._pardon_broadcast_text(user_id, remaining_after)
        message = [
            {"type": "at", "data": {"qq": str(user_id)}},
            {"type": "text", "data": {"text": f" {body}"}},
        ]
        for gid in group_ids:
            try:
                if str(gid).isdigit() and hasattr(bot, "send_group_msg"):
                    await bot.send_group_msg(group_id=int(gid), message=message)
                elif str(gid).isdigit() and hasattr(bot, "call_action"):
                    await bot.call_action(
                        "send_group_msg",
                        group_id=int(gid),
                        message=message,
                    )
                else:
                    logger.info("弧光护卫：官方平台跳过求饶群广播 group=%s", gid)
                    continue
                logger.info("弧光护卫：求饶广播 group=%s user=%s", gid, user_id)
            except Exception as exc:  # noqa: BLE001
                logger.warning(
                    "弧光护卫：求饶广播失败 group=%s user=%s err=%s",
                    gid,
                    user_id,
                    exc,
                )

    @staticmethod
    def _parse_user_id(raw: str) -> str:
        """Extract a platform user id from LLM / command input.

        Accepts numeric QQ ids, official 32-char hex openids, ``<@openid>``,
        and labeled forms such as ``QQ用户123``.

        Args:
            raw: Raw user id text.

        Returns:
            Normalized user id, or empty string if none found.
        """
        text = str(raw or "").strip()
        if not text:
            return ""
        mention = re.fullmatch(r"<@!?([A-Za-z0-9_-]+)>", text)
        if mention:
            return mention.group(1)
        if text.isdigit() or re.fullmatch(r"[A-Fa-f0-9]{32}", text):
            return text
        mention_in_text = re.search(r"<@!?([A-Za-z0-9_-]+)>", text)
        if mention_in_text:
            return mention_in_text.group(1)
        hex_id = re.search(r"\b([A-Fa-f0-9]{32})\b", text)
        if hex_id:
            return hex_id.group(1)
        match = re.search(r"(\d{5,})", text)
        return match.group(1) if match else ""

    @filter.llm_tool(name="arc_mute_user")
    async def arc_mute_user(
        self,
        event: AstrMessageEvent,
        user_id: str,
        minutes: int,
        reason: str = "",
    ) -> str:
        """给群成员追加禁言（叠在当前剩余禁言时间上累加，不会覆盖更长的禁言）。仅超级管理员可用。

        需要惩罚群成员时调用。最终禁言 = 当前剩余禁言 + 本次追加时间。
        例如已禁言 1 小时再追加 1 分钟，结果约为 1 小时 + 1 分钟，而不是只剩 1 分钟。
        只有发起消息的人是超级管理员才会执行；其他人要求禁言时直接说明没有权限，不要调用。

        Args:
            user_id(string): 要禁言的用户 ID（QQ 号或官方 openid），不要填群昵称
            minutes(number): 本次要追加的禁言分钟数，必须大于 0
            reason(string): 可选，禁言原因说明
        """
        if not self._sender_is_super_admin(event):
            return SUPER_ADMIN_DENIED
        if not event.get_group_id():
            return "失败：只能在群聊中禁言"
        uid = self._parse_user_id(user_id)
        if not uid:
            return f"失败：无效的用户 ID（{user_id}）"
        try:
            add_minutes = int(minutes)
        except (TypeError, ValueError):
            return "失败：minutes 必须是数字"
        if add_minutes <= 0:
            return "失败：追加禁言分钟数必须大于 0"

        add_seconds = min(add_minutes * 60, MAX_MUTE_SECONDS)
        result = await self._ban_accumulate(event, uid, add_seconds)
        if not result.get("ok"):
            err = result.get("error") or "未知错误"
            return f"禁言失败：{err}"

        before_m = max(0, (int(result["remaining_before"]) + 59) // 60)
        add_m = max(1, (int(result["added"]) + 59) // 60)
        total_m = max(1, (int(result["total"]) + 59) // 60)
        reason_text = str(reason or "").strip()
        suffix = f"；原因：{reason_text}" if reason_text else ""
        return (
            f"已累加禁言 {uid}：原剩余约 {before_m} 分钟 + 追加 {add_m} 分钟"
            f" = 总计约 {total_m} 分钟{suffix}"
        )

    @filter.llm_tool(name="arc_unmute_user")
    async def arc_unmute_user(
        self,
        event: AstrMessageEvent,
        user_id: str,
        reason: str = "",
    ) -> str:
        """解除群成员的禁言（只解除禁言，不影响屏蔽）。仅超级管理员可用。

        当超级管理员要撤销禁言时调用，例如赦免、误封纠正、或用户已认错需放行。
        只解除 QQ 群禁言；要取消屏蔽请用 arc_unshield_user。
        只有发起消息的人是超级管理员才会执行；其他人要求解禁时直接说明没有权限，不要调用。

        Args:
            user_id(string): 要解除禁言的用户 ID（QQ 号或官方 openid），不要填群昵称
            reason(string): 可选，解除原因说明
        """
        if not self._sender_is_super_admin(event):
            return SUPER_ADMIN_DENIED
        if not event.get_group_id():
            return "失败：只能在群聊中解除禁言"
        uid = self._parse_user_id(user_id)
        if not uid:
            return f"失败：无效的用户 ID（{user_id}）"

        result = await self._unmute(event, uid)
        if not result.get("ok"):
            err = str(result.get("error") or "未知错误")
            return f"解除失败：{err}"

        reason_text = str(reason or "").strip()
        suffix = f"；原因：{reason_text}" if reason_text else ""
        return f"已解除 {uid} 的禁言{suffix}"

    @filter.llm_tool(name="arc_register_bot")
    async def arc_register_bot(
        self,
        event: AstrMessageEvent,
        user_id: str,
        reason: str = "",
    ) -> str:
        """把某个账号记录为联动机器人（连坐名单）。仅超级管理员可用。

        当超级管理员要求「@xx 把这个记录为机器人」时调用。记录后：该账号
        发出的含违禁词消息不再惩罚它自己，而是连坐禁言其消息里 @ 的教唆对象；
        同时不再检测它的群名片，也不会误禁它本身。
        只有发起消息的人是超级管理员才会执行；其他人提出该要求时直接说明没有权限，不要调用。

        Args:
            user_id(string): 要记录的账号：@ 标记原文、openid（32 位十六进制）或 QQ 号
            reason(string): 可选，记录原因
        """
        if not self._sender_is_super_admin(event):
            return SUPER_ADMIN_DENIED
        uid = self._parse_user_id(user_id)
        if not uid:
            return f"失败：无效的用户 ID（{user_id}）"
        if self._is_whitelisted(uid):
            return "失败：该账号在白名单，不能记录为联动机器人"
        if self._exempt_admins() and self._is_astrbot_admin_id(uid):
            return "失败：该账号是 AstrBot 管理员，不能记录为联动机器人"
        bots = self._linked_bot_ids()
        if uid in bots:
            return f"{uid} 已经是联动机器人了，当前名单：{'、'.join(sorted(bots))}"
        bots.add(uid)
        self.config["linked_bot_ids"] = sorted(bots)
        if hasattr(self.config, "save_config"):
            self.config.save_config()
        logger.info(
            "弧光护卫：LLM 记录联动机器人 user=%s reason=%s total=%s",
            uid,
            reason,
            len(bots),
        )
        return (
            f"已把 {uid} 记录为联动机器人，今后它的违禁消息将连坐其 @ 的人。"
            f"当前共 {len(bots)} 个：{'、'.join(sorted(bots))}"
        )

    @filter.llm_tool(name="arc_unregister_bot")
    async def arc_unregister_bot(
        self,
        event: AstrMessageEvent,
        user_id: str,
        reason: str = "",
    ) -> str:
        """把某个账号从联动机器人（连坐名单）移除，恢复正常检测与处罚。仅超级管理员可用。

        当超级管理员发现误记录、或确认该账号不再是机器人时调用。
        只有发起消息的人是超级管理员才会执行；其他人提出该要求时直接说明没有权限，不要调用。

        Args:
            user_id(string): 要移除的账号：openid（32 位十六进制）或 QQ 号
            reason(string): 可选，移除原因
        """
        if not self._sender_is_super_admin(event):
            return SUPER_ADMIN_DENIED
        uid = self._parse_user_id(user_id)
        if not uid:
            return f"失败：无效的用户 ID（{user_id}）"
        bots = self._linked_bot_ids()
        if uid not in bots:
            return (
                f"{uid} 不在联动机器人名单里。当前名单："
                f"{'、'.join(sorted(bots)) or '（空）'}"
            )
        bots.discard(uid)
        self.config["linked_bot_ids"] = sorted(bots)
        if hasattr(self.config, "save_config"):
            self.config.save_config()
        logger.info(
            "弧光护卫：LLM 移除联动机器人 user=%s reason=%s total=%s",
            uid,
            reason,
            len(bots),
        )
        # _parse_id_list falls back to the default list when config is empty.
        effective = bots if bots else set(DEFAULT_LINKED_BOT_IDS)
        note = "（名单已清空，回退默认名单）" if not bots else ""
        return (
            f"已把 {uid} 从联动机器人名单移除。当前共 {len(effective)} 个："
            f"{'、'.join(sorted(effective))}{note}"
        )

    @filter.llm_tool(name="arc_shield_user")
    async def arc_shield_user(
        self,
        event: AstrMessageEvent,
        user_id: str,
        minutes: int = 0,
        permanent: bool = False,
        reason: str = "",
    ) -> str:
        """屏蔽某个账号：之后不再回复他的聊天，但仍审查违禁词并照常禁言。仅超级管理员可用。

        屏蔽与禁言相互独立。当超级管理员要求「屏蔽他」「拉黑他」「以后别理他」时调用：
        permanent=true 为永久屏蔽，在所有群和私聊生效，求饶无效，只能用 arc_unshield_user 取消；
        permanent=false 为本群限时屏蔽，minutes 是本次叠加的屏蔽分钟数。
        只有发起消息的人是超级管理员才会执行；其他人要求屏蔽时直接说明没有权限，不要调用。

        Args:
            user_id(string): 要屏蔽的账号：@ 标记原文、openid（32 位十六进制）或 QQ 号，不要填群昵称
            minutes(number): 限时屏蔽的分钟数，permanent=false 时必填且必须大于 0
            permanent(boolean): 是否永久屏蔽，默认 false
            reason(string): 可选，屏蔽原因
        """
        if not self._sender_is_super_admin(event):
            return SUPER_ADMIN_DENIED
        gid = str(event.get_group_id() or "").strip()
        uid = self._parse_user_id(user_id)
        if not gid:
            return "失败：只能在群聊中屏蔽"
        if not uid:
            return f"失败：无效的用户 ID（{user_id}）"
        if uid == str(event.get_sender_id() or ""):
            return "失败：不能屏蔽自己"
        if self._is_whitelisted(uid):
            return "失败：白名单用户不受屏蔽影响，不能屏蔽"
        if self._is_astrbot_admin_id(uid):
            return "失败：不能屏蔽超级管理员"

        is_permanent = permanent is True or (
            str(permanent).strip().lower() in ("true", "1", "yes")
        )
        try:
            add_minutes = int(minutes)
        except (TypeError, ValueError):
            add_minutes = 0
        if not is_permanent and add_minutes <= 0:
            return "失败：需要填写 minutes（限时屏蔽分钟数），或 permanent=true（永久屏蔽）"

        reason_text = str(reason or "").strip()
        suffix = f"；原因：{reason_text}" if reason_text else ""
        operator = str(event.get_sender_id() or "")
        if is_permanent:
            shields = self._permanent_shield_ids()
            if uid in shields:
                return f"{uid} 已经是永久屏蔽了{suffix}"
            shields.add(uid)
            self.config["blacklist_ids"] = sorted(shields)
            if hasattr(self.config, "save_config"):
                self.config.save_config()
            logger.info(
                "弧光护卫：LLM 永久屏蔽 user=%s operator=%s reason=%s",
                uid,
                operator,
                reason_text,
            )
            return f"已永久屏蔽 {uid}：所有群和私聊都不再回复他的聊天，但仍审查违禁词{suffix}"

        if self._is_permanently_shielded(uid):
            return f"{uid} 已经是永久屏蔽，无需再限时屏蔽{suffix}"
        add = min(add_minutes * 60, MAX_MUTE_SECONDS)
        base = self._shield_remaining(gid, uid)
        total = min(base + add, MAX_MUTE_SECONDS)
        self._remember_shield(gid, uid, total)
        logger.info(
            "弧光护卫：LLM 限时屏蔽 group=%s user=%s operator=%s base=%ss "
            "+ add=%ss -> %ss reason=%s",
            gid,
            uid,
            operator,
            base,
            add,
            total,
            reason_text,
        )
        return (
            f"已在本群屏蔽 {uid}：原剩余约 {max(0, (base + 59) // 60)} 分钟 + 追加 "
            f"{add_minutes} 分钟 = 总计约 {max(1, (total + 59) // 60)} 分钟。"
            f"期间不回复他的聊天，但仍审查违禁词{suffix}"
        )

    @filter.llm_tool(name="arc_unshield_user")
    async def arc_unshield_user(
        self,
        event: AstrMessageEvent,
        user_id: str,
        reason: str = "",
    ) -> str:
        """取消某个账号的屏蔽（永久屏蔽和所有群的限时屏蔽一起解除），机器人恢复正常回复。仅超级管理员可用。

        当超级管理员要求「取消屏蔽」「解除拉黑」时调用。只解除屏蔽，不影响禁言；
        要解除禁言请用 arc_unmute_user。
        只有发起消息的人是超级管理员才会执行；其他人要求取消屏蔽时直接说明没有权限，不要调用。

        Args:
            user_id(string): 要取消屏蔽的账号：@ 标记原文、openid（32 位十六进制）或 QQ 号
            reason(string): 可选，取消原因
        """
        if not self._sender_is_super_admin(event):
            return SUPER_ADMIN_DENIED
        uid = self._parse_user_id(user_id)
        if not uid:
            return f"失败：无效的用户 ID（{user_id}）"

        removed: list[str] = []
        shields = self._permanent_shield_ids()
        if uid in shields:
            shields.discard(uid)
            self.config["blacklist_ids"] = sorted(shields)
            if hasattr(self.config, "save_config"):
                self.config.save_config()
            removed.append("永久屏蔽")
        for gid, _shield_uid in [k for k in self._shield_until if k[1] == uid]:
            self._remember_shield(gid, uid, 0)
            removed.append(f"群 {gid} 的限时屏蔽")
        reason_text = str(reason or "").strip()
        logger.info(
            "弧光护卫：LLM 取消屏蔽 user=%s operator=%s removed=%s reason=%s",
            uid,
            event.get_sender_id(),
            removed,
            reason_text,
        )
        if not removed:
            return f"{uid} 当前没有被屏蔽"
        suffix = f"；原因：{reason_text}" if reason_text else ""
        return f"已取消 {uid} 的{'、'.join(removed)}，机器人恢复正常回复{suffix}"

    @filter.llm_tool(name="arc_recall_user_messages")
    async def arc_recall_user_messages(
        self,
        event: AstrMessageEvent,
        user_id: str,
        count: int = 1,
        reason: str = "",
    ) -> str:
        """撤回某个账号在本群最近发送的几条消息。仅超级管理员可用。

        当超级管理员要求「撤回他刚才发的消息」时调用。官方平台只能撤回 2 分钟内
        发出的消息，且只能撤回普通成员的消息。
        只有发起消息的人是超级管理员才会执行；其他人要求撤回时直接说明没有权限，不要调用。

        Args:
            user_id(string): 要撤回其消息的账号：@ 标记原文、openid（32 位十六进制）或 QQ 号
            count(number): 最多撤回最近几条，默认 1，上限 5
            reason(string): 可选，撤回原因
        """
        if not self._sender_is_super_admin(event):
            return SUPER_ADMIN_DENIED
        gid = str(event.get_group_id() or "").strip()
        uid = self._parse_user_id(user_id)
        if not gid:
            return "失败：只能在群聊中撤回消息"
        if not uid:
            return f"失败：无效的用户 ID（{user_id}）"
        try:
            limit = max(1, int(count))
        except (TypeError, ValueError):
            limit = 1
        limit = min(limit, RECALL_MAX_COUNT)

        bot = getattr(event, "bot", None)
        official = _is_official_target(bot, gid, uid)
        now = time.time()
        picked: list[str] = []
        for ts, sender, mid in reversed(self._recent_msgs.get(gid) or ()):
            if sender != uid:
                continue
            if official and now - ts > OFFICIAL_RECALL_WINDOW_SECONDS:
                continue
            picked.append(mid)
            if len(picked) >= limit:
                break
        if not picked:
            window = "（官方平台只能撤回 2 分钟内的消息）" if official else ""
            return f"没有找到 {uid} 最近发送的可撤回消息{window}"

        ok = 0
        for mid in picked:
            if await self._recall_message_by_id(bot, gid, mid):
                ok += 1
        logger.info(
            "弧光护卫：LLM 撤回消息 group=%s user=%s operator=%s picked=%s ok=%s reason=%s",
            gid,
            uid,
            event.get_sender_id(),
            len(picked),
            ok,
            str(reason or "").strip(),
        )
        if ok <= 0:
            return (
                f"撤回失败：{uid} 的 {len(picked)} 条消息都没能撤回"
                "（可能无权限，或对方不是普通成员）"
            )
        failed = len(picked) - ok
        note = f"，另有 {failed} 条没能撤回" if failed > 0 else ""
        return f"已撤回 {uid} 最近的 {ok} 条消息{note}"

    @filter.event_message_type(filter.EventMessageType.PRIVATE_MESSAGE, priority=100)
    async def on_private_pardon(self, event: AstrMessageEvent):
        """Intercept permanently shielded senders; handle private-chat apology.

        Permanent shields also apply to private chat: the message is dropped
        without any reply. For everyone else the configured phrase reduces
        mute / timed-shield time, controlled by ``pardon_enabled``.

        Args:
            event: Incoming private message event.
        """
        sender_id = str(event.get_sender_id() or "")
        if not sender_id or sender_id == str(event.get_self_id() or ""):
            return
        if self._is_whitelisted(sender_id):
            return

        # Private chat has no group to mute in, so forbidden hits are log-only.
        # Command messages still pass through for shielded users.
        if self._is_permanently_shielded(sender_id):
            text = event.get_message_str() or event.message_str or ""
            if self._is_command_message(event):
                logger.info("弧光护卫：放行永久屏蔽私聊命令 user=%s", sender_id)
                return
            logger.info(
                "弧光护卫：拦截永久屏蔽私聊 user=%s forbidden_hits=%s",
                sender_id,
                self._count_forbidden(text),
            )
            event.stop_event()
            return

        if not self._pardon_enabled():
            return

        text = event.get_message_str() or event.message_str or ""
        if not self._is_pardon_phrase(text):
            return

        logger.info("弧光护卫：收到求饶私聊 user=%s text=%s", sender_id, text[:80])

        bot = getattr(event, "bot", None)
        reply, ok, result = await self._process_pardon(bot, sender_id)
        if ok:
            group_ids = list(result.get("group_ids") or [])
            after = int(result.get("max_remaining_after") or 0)
            try:
                await self._broadcast_pardon(bot, group_ids, sender_id, after)
            except Exception as exc:  # noqa: BLE001
                logger.warning("弧光护卫：求饶群广播异常: %s", exc)
            logger.info(
                "弧光护卫：求饶成功 user=%s groups=%s remaining=%ss",
                sender_id,
                group_ids,
                after,
            )
        else:
            logger.info(
                "弧光护卫：求饶未减刑 user=%s reply=%s",
                sender_id,
                reply[:80],
            )
        yield event.plain_result(reply)
        event.stop_event()

    def _iter_aiocqhttp_bots(self) -> list[Any]:
        """Return connected aiocqhttp bot clients."""
        platforms = []
        pm = getattr(self.context, "platform_manager", None)
        if pm is not None:
            platforms = list(getattr(pm, "platform_insts", []) or [])
        if not platforms:
            platforms = list(getattr(self.context, "platform_insts", []) or [])
        bots: list[Any] = []
        for platform in platforms:
            meta = None
            if callable(getattr(platform, "meta", None)):
                try:
                    meta = platform.meta()
                except Exception:  # noqa: BLE001
                    meta = None
            if meta is None:
                meta = getattr(platform, "metadata", None)
            name = str(getattr(meta, "name", "") or "")
            if name != "aiocqhttp":
                continue
            bot = None
            try:
                bot = platform.get_client()
            except Exception:  # noqa: BLE001
                bot = None
            if bot is not None:
                bots.append(bot)
        return bots

    async def _resolve_bot_self_id(self, bot) -> str:
        """Resolve bot QQ id via cached login info."""
        key = id(bot)
        cached = self._card_bot_self_ids.get(key)
        if cached:
            return cached
        info = None
        try:
            if hasattr(bot, "get_login_info"):
                info = await bot.get_login_info()
            elif hasattr(bot, "call_action"):
                info = await bot.call_action("get_login_info")
        except Exception as exc:  # noqa: BLE001
            logger.debug("弧光护卫：get_login_info 失败: %s", exc)
            return ""
        if not isinstance(info, dict):
            return ""
        uid = str(info.get("user_id") or "").strip()
        if uid.isdigit():
            self._card_bot_self_ids[key] = uid
        return uid

    async def _refresh_card_group_ids(self, bot) -> list[str]:
        """Refresh cached group id list for card watching."""
        now = time.time()
        if (
            self._card_group_ids
            and now - self._card_group_ids_ts < CARD_GROUP_LIST_REFRESH_SECONDS
        ):
            return self._card_group_ids
        groups = await self._list_bot_group_ids(bot)
        self._card_group_ids = groups
        self._card_group_ids_ts = now
        return groups

    async def _set_group_card(
        self, bot, group_id: str, user_id: str, card: str
    ) -> bool:
        """Set a member's group card via OneBot ``set_group_card``."""
        try:
            if hasattr(bot, "set_group_card"):
                await bot.set_group_card(
                    group_id=int(group_id),
                    user_id=int(user_id),
                    card=str(card),
                )
            elif hasattr(bot, "call_action"):
                await bot.call_action(
                    "set_group_card",
                    group_id=int(group_id),
                    user_id=int(user_id),
                    card=str(card),
                )
            else:
                return False
            return True
        except Exception as exc:  # noqa: BLE001
            logger.error(
                "弧光护卫：设置群名片失败 group=%s user=%s card=%s err=%s",
                group_id,
                user_id,
                card,
                exc,
            )
            return False

    async def _send_group_text(self, bot, group_id: str, text: str) -> None:
        """Send a plain text message to a group."""
        body = str(text or "").strip()
        if not body or bot is None:
            return
        message = [{"type": "text", "data": {"text": body}}]
        try:
            if hasattr(bot, "send_group_msg"):
                await bot.send_group_msg(group_id=int(group_id), message=message)
            elif hasattr(bot, "call_action"):
                await bot.call_action(
                    "send_group_msg",
                    group_id=int(group_id),
                    message=message,
                )
        except Exception as exc:  # noqa: BLE001
            logger.warning("弧光护卫：群消息发送失败 group=%s err=%s", group_id, exc)

    @staticmethod
    def _notice_operator_id(notice: dict, *, self_id: str = "") -> str:
        """Best-effort extract who changed the group card from a notice."""
        for key in (
            "operator_id",
            "operator",
            "oper_id",
            "action_user_id",
            "actionUserId",
        ):
            raw = notice.get(key)
            if raw is None:
                continue
            uid = str(raw).strip()
            if not uid or uid in {"0", "null", "None", "all"}:
                continue
            if not uid.isdigit() and not re.fullmatch(r"[A-Fa-f0-9]{32}", uid):
                match = re.search(r"(\d{5,}|[A-Fa-f0-9]{32})", uid)
                if not match:
                    continue
                uid = match.group(1)
            if self_id and uid == str(self_id):
                continue
            return uid
        return ""

    def _consume_card_action_slot(self, group_id: str) -> bool:
        """Return True once per cooldown window for punish/reply side effects."""
        gid = str(group_id or "")
        now = time.time()
        last = float(self._card_action_ts.get(gid) or 0)
        if now - last < CARD_ACTION_COOLDOWN_SECONDS:
            return False
        self._card_action_ts[gid] = now
        return True

    async def _restore_bot_card_and_punish(
        self,
        bot,
        group_id: str,
        self_id: str,
        *,
        current_card: str,
        punish_ids: set[str] | None = None,
        force_side_effects: bool = False,
    ) -> None:
        """Force bot card back to default and optionally mute offenders.

        Args:
            bot: aiocqhttp bot client.
            group_id: QQ group id.
            self_id: Bot QQ id.
            current_card: Observed wrong card text.
            punish_ids: Explicit punish targets; default config fallback list.
            force_side_effects: If True, ignore cooldown for punish/reply.
        """
        desired = self._bot_group_card()
        logger.warning(
            "弧光护卫：机器人群名片被改 group=%s %r -> 强制改回 %r",
            group_id,
            current_card,
            desired,
        )
        ok_card = await self._set_group_card(bot, group_id, self_id, desired)
        if not ok_card:
            logger.error("弧光护卫：改回群名片失败 group=%s", group_id)

        do_side = force_side_effects or self._consume_card_action_slot(group_id)
        if not do_side:
            return

        targets = set(punish_ids or self._card_tamper_punish_ids())
        mute_seconds = self._card_tamper_mute_seconds()
        for uid in sorted(targets):
            if self._is_whitelisted(uid) or uid == str(self_id):
                continue
            result = await self._ban_accumulate_on_bot(
                bot,
                group_id,
                uid,
                mute_seconds,
                self_id=self_id,
                respect_whitelist=True,
                announce=False,
            )
            logger.info(
                "弧光护卫：改名片制裁 user=%s ok=%s err=%s total=%ss",
                uid,
                result.get("ok"),
                result.get("error") or "",
                result.get("total"),
            )
        await self._send_group_text(bot, group_id, self._card_tamper_reply())

    async def _card_watch_tick(self) -> None:
        """Check bot cards in all groups once."""
        if not self._card_tamper_enabled():
            return
        desired = self._bot_group_card()
        for bot in self._iter_aiocqhttp_bots():
            self_id = await self._resolve_bot_self_id(bot)
            if not self_id:
                continue
            for gid in await self._refresh_card_group_ids(bot):
                ok, card = await self._fetch_group_member_card(bot, gid, self_id)
                if not ok:
                    continue
                if card == desired:
                    continue
                await self._restore_bot_card_and_punish(
                    bot,
                    gid,
                    self_id,
                    current_card=card or "(空)",
                )

    async def _fetch_group_member_card(
        self, bot, group_id: str, user_id: str
    ) -> tuple[bool, str]:
        """Fetch member card with explicit lookup success flag.

        Returns:
            ``(ok, card)``. ``ok`` is False when the API call failed; ``card``
            may be empty when the member uses a blank group card.
        """
        gid = str(group_id or "").strip()
        uid = str(user_id or "").strip()
        if not gid or not uid.isdigit() or bot is None:
            return False, ""
        info = None
        try:
            if hasattr(bot, "get_group_member_info"):
                info = await bot.get_group_member_info(
                    group_id=int(gid),
                    user_id=int(uid),
                    no_cache=True,
                )
            elif hasattr(bot, "call_action"):
                info = await bot.call_action(
                    "get_group_member_info",
                    group_id=int(gid),
                    user_id=int(uid),
                    no_cache=True,
                )
            else:
                return False, ""
        except Exception as exc:  # noqa: BLE001
            logger.debug(
                "弧光护卫：名片守护查询失败 group=%s user=%s err=%s",
                gid,
                uid,
                exc,
            )
            return False, ""
        if not isinstance(info, dict):
            return False, ""
        card = str(info.get("card") or "").strip()
        return True, card

    async def _card_watch_loop(self) -> None:
        """Poll bot group cards every second."""
        while True:
            try:
                await self._card_watch_tick()
            except asyncio.CancelledError:
                raise
            except Exception as exc:  # noqa: BLE001
                logger.warning("弧光护卫：群名片守护轮询异常: %s", exc)
            await asyncio.sleep(CARD_WATCH_INTERVAL_SECONDS)

    @filter.event_message_type(filter.EventMessageType.GROUP_MESSAGE, priority=200)
    async def on_bot_card_tamper(self, event: AstrMessageEvent):
        """Restore bot group card immediately on ``group_card`` notices."""
        if not self._card_tamper_enabled():
            return
        raw = self._raw_notice_payload(event)
        if raw is None:
            return
        if str(raw.get("notice_type") or "") != "group_card":
            return

        self_id = str(event.get_self_id() or raw.get("self_id") or "")
        changed_uid = str(raw.get("user_id") or "")
        if not self_id or changed_uid != self_id:
            return

        desired = self._bot_group_card()
        card_new = str(raw.get("card_new") if raw.get("card_new") is not None else "")
        if card_new == desired:
            return

        group_id = str(event.get_group_id() or raw.get("group_id") or "")
        bot = getattr(event, "bot", None)
        if not group_id or bot is None:
            return

        operator = self._notice_operator_id(raw, self_id=self_id)
        punish_ids = {operator} if operator else self._card_tamper_punish_ids()
        await self._restore_bot_card_and_punish(
            bot,
            group_id,
            self_id,
            current_card=card_new or "(空)",
            punish_ids=punish_ids,
            force_side_effects=True,
        )
        event.stop_event()

    def _raw_notice_payload(self, event: AstrMessageEvent) -> dict | None:
        """Return OneBot notice payload when this event is a notice."""
        raw = getattr(getattr(event, "message_obj", None), "raw_message", None)
        if raw is None:
            return None
        try:
            post_type = raw.get("post_type") if hasattr(raw, "get") else None
        except Exception:  # noqa: BLE001
            return None
        if post_type != "notice":
            return None
        if isinstance(raw, dict):
            return raw
        try:
            return dict(raw)
        except Exception:  # noqa: BLE001
            return None

    def _sync_mute_tracker_from_notice(self, event: AstrMessageEvent) -> bool:
        """Align local mute tracker with group_ban / lift_ban notices.

        Args:
            event: Incoming event that may be a OneBot notice.

        Returns:
            True if this was a mute-related notice (caller should stop chat scan).
        """
        raw = self._raw_notice_payload(event)
        if raw is None:
            return False
        notice_type = str(raw.get("notice_type") or "")
        if notice_type != "group_ban":
            # Other notices (poke, card change, ...): skip chat keyword scan.
            return True
        gid = str(raw.get("group_id") or event.get_group_id() or "").strip()
        uid = str(raw.get("user_id") or "").strip()
        if not gid or not uid:
            return True
        try:
            duration = int(raw.get("duration") or 0)
        except (TypeError, ValueError):
            duration = 0
        sub = str(raw.get("sub_type") or "")
        if duration <= 0 or sub == "lift_ban":
            self._remember_mute_until(gid, uid, 0)
            logger.info(
                "弧光护卫：收到解禁通知，清除本地追踪 group=%s user=%s",
                gid,
                uid,
            )
            return True

        tracked = self._tracked_remaining_seconds(gid, uid)
        operator = str(raw.get("operator_id") or "").strip()
        self_id = str(event.get_self_id() or "").strip()
        # Our own set_group_ban notice often carries QQ-rounded duration; keep
        # the logical intended remaining we already stored.
        if operator and self_id and operator == self_id and tracked > 0:
            if duration >= tracked:
                return True
        # No tracker, external shorten, or external longer ban → adopt QQ.
        self._remember_mute_until(gid, uid, min(duration, MAX_MUTE_SECONDS))
        logger.info(
            "弧光护卫：收到禁言通知，对齐本地追踪 group=%s user=%s "
            "tracked=%ss notice=%ss operator=%s",
            gid,
            uid,
            tracked,
            duration,
            operator or "-",
        )
        return True

    @filter.event_message_type(filter.EventMessageType.GROUP_MESSAGE, priority=100)
    async def on_group_message(self, event: AstrMessageEvent):
        """Scan group messages and sender cards for forbidden letter sequences.

        Shielded senders (timed in this group, or permanent everywhere) never
        get chat replies: their event is intercepted (``stop_event``), but the
        message is still scanned and punished on forbidden hits. Command
        messages (an activated command handler exists) are not intercepted so
        shielded users can keep interacting.

        Args:
            event: Incoming group message event.
        """
        if self._sync_mute_tracker_from_notice(event):
            return

        sender_id = str(event.get_sender_id() or "")
        gid = str(event.get_group_id() or "")
        mid = str(
            getattr(getattr(event, "message_obj", None), "message_id", "") or ""
        ).strip()
        # Remember recent messages so super admins can recall them by sender.
        if gid and mid:
            self._recent_msgs.setdefault(
                gid, deque(maxlen=RECENT_MSGS_PER_GROUP)
            ).append((time.time(), sender_id, mid))

        if sender_id == str(event.get_self_id() or ""):
            return
        if self._is_whitelisted(sender_id):
            return

        text = event.get_message_str() or event.message_str or ""

        # Group-chat pardon. Must run before the shield intercept so shielded
        # users can still apologize.
        if (
            self._pardon_enabled()
            and self._pardon_in_group()
            and self._is_pardon_phrase(text)
        ):
            bot = getattr(event, "bot", None)
            logger.info(
                "弧光护卫：收到群内求饶 group=%s user=%s text=%s",
                gid,
                sender_id,
                text[:80],
            )
            reply, ok, result = await self._process_pardon(bot, sender_id, group_id=gid)
            try:
                await event.send(event.plain_result(reply))
            except Exception as exc:  # noqa: BLE001
                logger.error("弧光护卫：群内求饶回复失败: %s", exc)
            logger.info(
                "弧光护卫：群内求饶%s group=%s user=%s remaining=%ss",
                "成功" if ok else "未减刑",
                gid,
                sender_id,
                int(result.get("max_remaining_after") or 0),
            )
            event.stop_event()
            return

        ocr_text = await self._ocr_message_images(event)
        scan_text = text if not ocr_text else f"{text}\n{ocr_text}".strip()
        scan_text = await self._text_with_at_cards_expanded(event, scan_text)
        text_hits = self._count_forbidden(scan_text)
        if ocr_text and text_hits > 0 and self._count_forbidden(text) <= 0:
            logger.info(
                "弧光护卫：图片 OCR 命中违禁词 group=%s user=%s hits=%s",
                event.get_group_id(),
                sender_id,
                text_hits,
            )

        # Shield (timed in this group, or permanent everywhere): never respond
        # to daily chat, but wake-prefix commands still pass so shielded users
        # can keep interacting (games, help, ...). Forbidden words are still
        # audited — linked bots → accomplice; others → mute.
        permanent = self._is_permanently_shielded(sender_id)
        if permanent or self._shield_remaining(gid, sender_id) > 0:
            kind = "永久屏蔽" if permanent else "限时屏蔽"
            if text_hits > 0 and sender_id in self._linked_bot_ids():
                await self._punish_accomplice(event, text_hits)
            elif text_hits > 0:
                await self._punish(event, text_hits, send_reply=True)
            if self._is_command_message(event):
                logger.info(
                    "弧光护卫：放行%s命令 group=%s user=%s forbidden_hits=%s",
                    kind,
                    gid,
                    sender_id,
                    text_hits,
                )
                return
            logger.info(
                "弧光护卫：拦截%s消息 group=%s user=%s linked=%s forbidden_hits=%s",
                kind,
                gid,
                sender_id,
                sender_id in self._linked_bot_ids(),
                text_hits,
            )
            event.stop_event()
            return

        # Peer bot spat out forbidden text: punish whoever it @-mentioned.
        if text_hits > 0 and sender_id in self._linked_bot_ids():
            await self._punish_accomplice(event, text_hits)
            event.stop_event()
            return

        if text_hits > 0:
            await self._punish(event, text_hits, send_reply=True)
            event.stop_event()
            return

        # Same detection on group card / nickname (e.g. SBENMO so AI @ looks like insult).
        if sender_id in self._linked_bot_ids():
            return
        nick_hits = self._count_forbidden(self._sender_card(event))
        if nick_hits <= 0:
            return

        await self._punish(event, nick_hits, send_reply=True, from_nickname=True)
        event.stop_event()

    async def _block_forbidden_ai_output(
        self, event: AstrMessageEvent, text: str, hits: int
    ) -> str:
        """Mute the prompting user when possible and return warning text.

        Group chats mute immediately. Minecraft hub AI replies have no group
        id (sender is bound QQ, otherwise XUID); the hub applies the same
        jail / mute / kill punishment as player chat hits.

        Whitelist users are still not muted, but the forbidden AI text is
        always intercepted.

        Args:
            event: Message event whose sender is the reply target.
            text: Original AI / outbound text (for logs).
            hits: Forbidden-sequence hit count.

        Returns:
            Warning text to send instead of the original output.
        """
        gid = str(event.get_group_id() or "").strip()
        seconds = min(max(1, int(hits)) * self._seconds_per_hit(), MAX_MUTE_SECONDS)
        minutes = max(1, (seconds + 59) // 60)
        if gid:
            # No recall: the AI reply (replaced before sending) was the
            # violation, not the user's prompt message.
            reply = await self._punish(event, hits, send_reply=False, recall=False)
        else:
            # MC AI hub has no QQ group on the event; the hub applies jail / mute.
            reply = self._reply_text(minutes)
        event.set_extra("arc_guard_ai_blocked", True)
        event.set_extra("arc_guard_ai_hits", max(1, int(hits)))
        event.set_extra("arc_guard_ai_mute_seconds", seconds)
        event.set_extra("arc_guard_ai_reply", reply)
        logger.info(
            "弧光护卫：拦截 AI 违禁输出 group=%s user=%s hits=%s preview=%s",
            gid or "mc_ai",
            event.get_sender_id(),
            hits,
            (text or "")[:80],
        )
        return reply

    @filter.on_llm_response(priority=1000)
    async def on_llm_response(self, event: AstrMessageEvent, resp: LLMResponse) -> None:
        """Scan AI reply; intercept it and mute the prompting user if matched.

        Args:
            event: Message event that triggered the LLM.
            resp: LLM response object (mutable).
        """
        if not event.get_group_id() and not event.get_extra("mc_ai_event"):
            return
        if getattr(resp, "is_chunk", False):
            return

        text = (resp.completion_text or "").strip()
        hits = self._count_forbidden(text)
        if hits <= 0:
            return

        reply = await self._block_forbidden_ai_output(event, text, hits)
        if resp.result_chain is not None:
            resp.result_chain.chain = [Plain(reply)]
        resp.completion_text = reply

    @filter.on_decorating_result(priority=1000)
    async def on_decorating_result(self, event: AstrMessageEvent) -> None:
        """Scan outbound replies right before send as a last gate.

        Args:
            event: Event whose result is about to be sent.
        """
        if not event.get_group_id() and not event.get_extra("mc_ai_event"):
            return
        if event.get_extra("arc_guard_ai_blocked"):
            return
        result = event.get_result()
        if result is None or not result.chain:
            return
        text = ""
        for comp in result.chain:
            if isinstance(comp, Plain):
                text += comp.text or ""
        hits = self._count_forbidden(text)
        if hits <= 0:
            return
        reply = await self._block_forbidden_ai_output(event, text, hits)
        result.chain = [Plain(reply)]
