import asyncio
import time

from astrbot.api import AstrBotConfig, logger
from astrbot.api.event import AstrMessageEvent, MessageChain, filter
from astrbot.api.star import Context, Star, register

import astrbot.api.message_components as Comp


@register("forcejoin", "爅峫", "强制新入群成员在限定时间内加入另外一个群，超时未加入则自动踢出", "1.0.0")
class ForceJoinPlugin(Star):
    def __init__(self, context: Context, config: AstrBotConfig = None):
        super().__init__(context)
        self.config = config or {}
        self.pending_users: dict[str, dict] = {}
        self._task: asyncio.Task | None = None
        self._api = None

    async def initialize(self):
        self._init_api_client()
        self.pending_users = await self.get_kv_data("pending_users", {})
        self._task = asyncio.create_task(self._kick_loop())
        logger.info(f"ForceJoin 已初始化，{len(self.pending_users)} 条待处理")

    async def terminate(self):
        if self._task:
            self._task.cancel()
            try:
                await self._task
            except asyncio.CancelledError:
                pass
        await self.put_kv_data("pending_users", self.pending_users)
        logger.info("ForceJoin 已终止")

    # ── API ──────────────────────────────────────────────────────

    def _init_api_client(self):
        try:
            for p in self.context.platform_manager.get_insts():
                client = getattr(p, "get_client", None)
                if client is not None:
                    c = client()
                    if hasattr(c, "api") and hasattr(c.api, "call_action"):
                        self._api = c.api
                        return
        except Exception as e:
            logger.error(f"ForceJoin 初始化 API 失败: {e}")

    async def _call(self, action: str, **params):
        try:
            return await self._api.call_action(action, **params)
        except Exception as e:
            logger.error(f"ForceJoin API 调用失败 [{action}]: {e}")
            return None

    async def _kick(self, group_id: str, user_id: str):
        await self._call("set_group_kick", group_id=int(group_id),
                         user_id=int(user_id), reject_add_request=False)

    async def _mute(self, group_id: str, user_id: str):
        await self._call("set_group_ban", group_id=int(group_id),
                         user_id=int(user_id), duration=2592000)

    async def _unmute(self, group_id: str, user_id: str):
        await self._call("set_group_ban", group_id=int(group_id),
                         user_id=int(user_id), duration=0)

    async def _check_privilege(self, group_id: str, user_id: str) -> bool:
        info = await self._call("get_group_member_info",
                                group_id=int(group_id), user_id=int(user_id))
        if info:
            return str(info.get("role", "")).lower() in ("owner", "admin")
        return False

    # ── 后台踢出 ─────────────────────────────────────────────────

    async def _kick_loop(self):
        while True:
            try:
                await asyncio.sleep(10)
                await self._kick_expired()
            except asyncio.CancelledError:
                break
            except Exception as e:
                logger.error(f"ForceJoin 踢出循环出错: {e}")

    async def _kick_expired(self):
        if self._api is None:
            return
        timeout_seconds = int(self.config.get("timeout_minutes", 10)) * 60
        now = time.time()
        reason = self.config.get("kick_reason", "未在规定时间内加入目标群")

        expired = [(k, v) for k, v in self.pending_users.items()
                   if now - v["join_time"] >= timeout_seconds]
        if not expired:
            return

        for key, info in expired:
            if info.get("muted"):
                await self._unmute(info["group_id"], info["user_id"])
            await self._kick(info["group_id"], info["user_id"])
            logger.info(f"ForceJoin: 超时踢出 {info['user_id']}，原因: {reason}")
            del self.pending_users[key]

        await self.put_kv_data("pending_users", self.pending_users)

    # ── 事件 ─────────────────────────────────────────────────────

    @filter.event_message_type(filter.EventMessageType.ALL)
    async def on_group_increase(self, event: AstrMessageEvent):
        try:
            raw = event.message_obj.raw_message
            if not isinstance(raw, dict) or raw.get("notice_type") != "group_increase":
                return

            group_id = str(raw.get("group_id", ""))
            user_id = str(raw.get("user_id", ""))
            if not group_id or not user_id:
                return

            target = str(self.config.get("target_group_id", "")).strip()
            if not target:
                return

            if group_id == target:
                await self._handle_target_join(user_id)
            else:
                await self._handle_source_join(event, group_id, user_id, target)
        except Exception as e:
            logger.error(f"ForceJoin 事件处理出错: {e}")

    async def _handle_target_join(self, user_id: str):
        matched = [k for k, v in self.pending_users.items()
                   if v["user_id"] == user_id]
        if not matched:
            return

        for key in matched:
            info = self.pending_users[key]
            if info.get("muted"):
                await self._unmute(info["group_id"], info["user_id"])

            umo = info.get("umo")
            if umo:
                text = self.config.get("completed_welcome_text",
                                       "新成员 {user_id} 已加入目标群，欢迎！")
                target = str(self.config.get("target_group_id", ""))
                msg = text.format(user_id=user_id, target_group=target,
                                  group_id=info["group_id"],
                                  timeout=self.config.get("timeout_minutes", 10))
                await self.context.send_message(umo, MessageChain().message(msg))

            logger.info(f"ForceJoin: {user_id} 已加入目标群")
            del self.pending_users[key]

        await self.put_kv_data("pending_users", self.pending_users)

    async def _handle_source_join(self, event, group_id: str, user_id: str, target: str):
        # 群级过滤
        whitelist = [str(g) for g in self.config.get("group_whitelist", [])]
        blacklist = [str(g) for g in self.config.get("group_blacklist", [])]
        if whitelist and group_id not in whitelist:
            return
        if group_id in blacklist:
            return

        # 用户过滤
        if user_id in [str(u) for u in self.config.get("user_blacklist", [])]:
            logger.info(f"ForceJoin: 黑名单 {user_id}")
            await self._kick(group_id, user_id)
            return
        if user_id in [str(u) for u in self.config.get("user_whitelist", [])]:
            return

        logger.info(f"ForceJoin: group={group_id}, user={user_id}")

        muted = False
        if self.config.get("mute_on_join", False):
            await self._mute(group_id, user_id)
            muted = True

        text = self.config.get("welcome_text",
                               "欢迎新成员！请在 {timeout} 分钟内加入目标群 "
                               "{target_group}，否则将被移出本群。")
        timeout = self.config.get("timeout_minutes", 10)
        msg = text.format(timeout=timeout, target_group=target,
                          user_id=user_id, group_id=group_id)
        await event.send(event.chain_result(
            [Comp.At(qq=user_id), Comp.Plain(" " + msg)]))

        key = f"{group_id}:{user_id}"
        self.pending_users[key] = {
            "group_id": group_id, "user_id": user_id,
            "join_time": time.time(), "muted": muted,
            "umo": event.unified_msg_origin,
        }
        await self.put_kv_data("pending_users", self.pending_users)

    # ── 指令 ─────────────────────────────────────────────────────

    @filter.command("forcejoin_status")
    async def cmd_status(self, event: AstrMessageEvent):
        sender = event.get_sender_id()
        gid = event.get_group_id()
        if not gid:
            yield event.plain_result("❌ 仅限群聊中使用。")
            return
        if not await self._check_privilege(gid, sender):
            yield event.plain_result("❌ 仅群主/管理员可查看。")
            return
        if not self.pending_users:
            yield event.plain_result("📋 没有待处理的新成员。")
            return

        timeout = int(self.config.get("timeout_minutes", 10))
        target = str(self.config.get("target_group_id", ""))
        now = time.time()
        items = {k: v for k, v in self.pending_users.items() if v["group_id"] == gid}
        if not items:
            yield event.plain_result("📋 当前群没有待处理的新成员。")
            return

        lines = [f"📋 本群待处理（{len(items)} 人）",
                 f"目标群: {target}  |  超时: {timeout} 分钟", "─" * 30]
        for info in items.values():
            remaining = max(0, timeout * 60 - (now - info["join_time"]))
            lines.append(f"• {info['user_id']} — "
                         f"剩余 {int(remaining // 60)} 分 {int(remaining % 60)} 秒")
        yield event.plain_result("\n".join(lines))

    @filter.command("forcejoin_remove")
    async def cmd_remove(self, event: AstrMessageEvent, user_id: str):
        sender = event.get_sender_id()
        gid = event.get_group_id()
        if not gid:
            yield event.plain_result("❌ 仅限群聊中使用。")
            return
        if not await self._check_privilege(gid, sender):
            yield event.plain_result("❌ 仅群主/管理员可操作。")
            return

        removed = [k for k, v in list(self.pending_users.items())
                   if v["user_id"] == str(user_id) and v["group_id"] == gid]
        for k in removed:
            del self.pending_users[k]
        if removed:
            await self.put_kv_data("pending_users", self.pending_users)
            yield event.plain_result(f"✅ 已移除 {user_id}。")
        else:
            yield event.plain_result(f"❌ 未找到 {user_id}。")
