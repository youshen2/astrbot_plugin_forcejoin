import asyncio
import time

from astrbot.api import AstrBotConfig, logger
from astrbot.api.event import AstrMessageEvent, MessageChain, filter
from astrbot.api.star import Context, Star, register

import astrbot.api.message_components as Comp


@register("forcejoin", "爅峫", "支持多目标群验证及目标群成员自动清理", "1.1.0")
class ForceJoinPlugin(Star):
    def __init__(self, context: Context, config: AstrBotConfig = None):
        super().__init__(context)
        self.config = config or {}
        self.pending_users: dict[str, dict] = {}
        self._task: asyncio.Task | None = None
        self._cleanup_task: asyncio.Task | None = None
        self._cleanup_lock = asyncio.Lock()
        self._api = None

    def _get_target_groups(self) -> list[str]:
        targets = [str(g).strip() for g in self.config.get("target_group_ids", [])]
        targets = list(dict.fromkeys(g for g in targets if g))
        if targets:
            return targets
        target = str(self.config.get("target_group_id", "")).strip()
        return [target] if target else []

    def _is_source_group(self, group_id: str, targets: list[str]) -> bool:
        whitelist = [str(g) for g in self.config.get("group_whitelist", [])]
        blacklist = [str(g) for g in self.config.get("group_blacklist", [])]
        return (group_id not in targets
                and (not whitelist or group_id in whitelist)
                and group_id not in blacklist)

    async def initialize(self):
        self._init_api_client()
        self.pending_users = await self.get_kv_data("pending_users", {})
        self._task = asyncio.create_task(self._kick_loop())
        self._cleanup_task = asyncio.create_task(self._target_cleanup_loop())
        logger.info(f"ForceJoin 已初始化，{len(self.pending_users)} 条待处理")

    async def terminate(self):
        tasks = [task for task in (self._task, self._cleanup_task) if task]
        for task in tasks:
            task.cancel()
        await asyncio.gather(*tasks, return_exceptions=True)
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

    async def _find_joined_target(self, user_id: str, targets: list[str]) -> str | None:
        for group_id in targets:
            info = await self._call("get_group_member_info",
                                    group_id=int(group_id), user_id=int(user_id))
            if info is not None:
                return group_id
        return None

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

    # ── 目标群清理 ───────────────────────────────────────────────

    async def _get_source_groups(self, targets: list[str]) -> list[str]:
        whitelist = self.config.get("group_whitelist", [])
        if whitelist:
            groups = [str(g) for g in whitelist]
        else:
            groups_info = await self._call("get_group_list")
            if not isinstance(groups_info, list):
                return []
            groups = [str(g["group_id"]) for g in groups_info]
        return list(dict.fromkeys(g for g in groups if self._is_source_group(g, targets)))

    async def _target_cleanup_loop(self):
        while True:
            try:
                interval = max(1, int(self.config.get("target_cleanup_interval_minutes", 10)))
                await asyncio.sleep(interval * 60)
                await self._cleanup_target_groups()
            except asyncio.CancelledError:
                break
            except Exception as e:
                logger.error(f"ForceJoin 目标群清理循环出错: {e}")

    async def _cleanup_target_groups(self, user_id: str | None = None,
                                      left_group_id: str | None = None):
        if not self.config.get("target_cleanup_enabled", False) or self._api is None:
            return
        targets = self._get_target_groups()
        if not targets:
            return

        async with self._cleanup_lock:
            sources = await self._get_source_groups(targets)
            if not sources or (left_group_id is not None and left_group_id not in sources):
                return

            source_members = set()
            for group_id in sources:
                # 退群事件已确认此人在该群离开，避免成员列表缓存影响判断。
                if group_id == left_group_id:
                    continue
                members = await self._call("get_group_member_list", group_id=int(group_id))
                if not isinstance(members, list):
                    logger.warning(f"ForceJoin: 无法获取生效群 {group_id} 的成员，跳过本次目标群清理")
                    return
                source_members.update(str(member["user_id"]) for member in members)
                if user_id is not None and user_id in source_members:
                    return

            login = await self._call("get_login_info")
            if not login:
                return
            exempt_users = {str(u) for u in self.config.get("user_whitelist", [])}
            exempt_users.add(str(login["user_id"]))

            for target in targets:
                members = await self._call("get_group_member_list", group_id=int(target))
                if not isinstance(members, list):
                    continue
                for member in members:
                    member_id = str(member["user_id"])
                    if user_id is not None and member_id != user_id:
                        continue
                    if member_id in source_members or member_id in exempt_users:
                        continue
                    await self._kick(target, member_id)
                    logger.info(f"ForceJoin: 请求从目标群 {target} 移出 {member_id}，已不在任何生效群")

    # ── 事件 ─────────────────────────────────────────────────────

    @filter.event_message_type(filter.EventMessageType.ALL)
    async def on_group_notice(self, event: AstrMessageEvent):
        try:
            raw = event.message_obj.raw_message
            if not isinstance(raw, dict):
                return
            notice_type = raw.get("notice_type")
            if notice_type not in ("group_increase", "group_decrease"):
                return

            group_id = str(raw.get("group_id", ""))
            user_id = str(raw.get("user_id", ""))
            if not group_id or not user_id:
                return

            targets = self._get_target_groups()
            if notice_type == "group_decrease":
                if raw.get("sub_type") == "kick_me" or user_id == str(raw.get("self_id", "")):
                    return
                if self.pending_users.pop(f"{group_id}:{user_id}", None) is not None:
                    await self.put_kv_data("pending_users", self.pending_users)
                if self._is_source_group(group_id, targets):
                    await self._cleanup_target_groups(user_id, group_id)
                return
            if not targets:
                return

            if group_id in targets:
                await self._handle_target_join(user_id, group_id)
            else:
                await self._handle_source_join(event, group_id, user_id, targets)
        except Exception as e:
            logger.error(f"ForceJoin 事件处理出错: {e}")

    async def _handle_target_join(self, user_id: str, target: str):
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
                msg = text.format(user_id=user_id, target_group=target,
                                  group_id=info["group_id"],
                                  timeout=self.config.get("timeout_minutes", 10))
                chain = MessageChain([Comp.At(qq=user_id), Comp.Plain(" " + msg)])
                await self.context.send_message(umo, chain)

            logger.info(f"ForceJoin: {user_id} 已加入目标群 {target}")
            del self.pending_users[key]

        await self.put_kv_data("pending_users", self.pending_users)

    async def _handle_source_join(self, event, group_id: str, user_id: str,
                                  targets: list[str]):
        # 群级过滤
        if not self._is_source_group(group_id, targets):
            return

        # 用户过滤
        if user_id in [str(u) for u in self.config.get("user_blacklist", [])]:
            logger.info(f"ForceJoin: 黑名单 {user_id}")
            await self._kick(group_id, user_id)
            return
        if user_id in [str(u) for u in self.config.get("user_whitelist", [])]:
            return

        joined_target = await self._find_joined_target(user_id, targets)
        if joined_target:
            logger.info(f"ForceJoin: {user_id} 已在目标群 {joined_target}")
            text = self.config.get("completed_welcome_text",
                                   "新成员 {user_id} 已加入目标群，欢迎！")
            msg = text.format(user_id=user_id, target_group=joined_target,
                              group_id=group_id,
                              timeout=self.config.get("timeout_minutes", 10))
            await event.send(event.chain_result(
                [Comp.At(qq=user_id), Comp.Plain(" " + msg)]))
            return

        logger.info(f"ForceJoin: group={group_id}, user={user_id}")

        muted = False
        if self.config.get("mute_on_join", False):
            await self._mute(group_id, user_id)
            muted = True

        text = self.config.get("welcome_text",
                               "欢迎新成员！请在 {timeout} 分钟内加入以下任意一个目标群："
                               "{target_group}，否则将被移出本群。")
        timeout = self.config.get("timeout_minutes", 10)
        msg = text.format(timeout=timeout, target_group="、".join(targets),
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
        target = "、".join(self._get_target_groups())
        now = time.time()
        items = {k: v for k, v in self.pending_users.items() if v["group_id"] == gid}
        if not items:
            yield event.plain_result("📋 当前群没有待处理的新成员。")
            return

        lines = [f"📋 本群待处理（{len(items)} 人）",
                 f"目标群（加入任意一个）: {target}  |  超时: {timeout} 分钟", "─" * 30]
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
