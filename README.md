# ForceJoin — 强制入群

新成员入群后自动提醒，要求在限定时间内加入指定目标群；超时未加入则自动移出。

## 功能

- **入群引导**：新成员入群后 @ 对方并发送引导消息
- **加群确认**：拦截目标群的入群事件，检测到成员加入后自动解除禁言并在原群发送确认通知
- **超时移出**：未在时限内加入目标群的成员将被自动移出
- **入群禁言**（可选）：新成员入群立即禁言，完成加群后自动解除
- **群级过滤**：群白名单/黑名单控制生效范围
- **用户豁免/拒绝**：白名单免规则，黑名单立即移出
- **持久化**：基于 AstrBot KV 存储，重启不丢失

## 工作流程

```
新成员入群事件
  ├─ 目标群号未配置 → 跳过
  ├─ 来自目标群 → 待处理列表中？→ 解除禁言 + 原群通知 + 移除记录
  └─ 来自其他群 → 群过滤 → 用户过滤 → 禁言(可选) → 引导消息 → 记录待处理

后台任务（每 30s）
  └─ 超时用户 → 解除禁言 → 移出群聊
```

## 配置项

| 键 | 类型 | 默认值 | 说明 |
|---|---|---|---|
| `target_group_id` | string | — | 目标群号，**必填** |
| `timeout_minutes` | int | `10` | 加群时限（分钟） |
| `welcome_text` | text | 见默认值 | 入群引导消息模板 |
| `completed_welcome_text` | text | 见默认值 | 完成加群后原群通知模板 |
| `kick_reason` | string | 未在规定时间内加入目标群 | 移出理由 |
| `mute_on_join` | bool | `false` | 入群时是否自动禁言 |
| `group_whitelist` | list | `[]` | 生效群白名单，留空全部生效 |
| `group_blacklist` | list | `[]` | 生效群黑名单 |
| `user_whitelist` | list | `[]` | 豁免用户（QQ号） |
| `user_blacklist` | list | `[]` | 拒绝用户（QQ号） |

### 消息占位符

| 占位符 | 说明 |
|---|---|
| `{timeout}` | 超时分钟数 |
| `{target_group}` | 目标群号 |
| `{user_id}` | 用户 QQ 号 |
| `{group_id}` | 当前群号 |

## 管理指令

| 指令 | 权限 | 说明 |
|---|---|---|
| `/forcejoin_status` | 群主/管理员 | 查看本群待处理成员及剩余时间 |
| `/forcejoin_remove <QQ号>` | 群主/管理员 | 手动移除待处理记录 |

## 支持平台

- aiocqhttp（OneBot v11）

## 依赖

- AstrBot >= 4.9.2
- 机器人需拥有群管理权限（移出成员、禁言）

## 安装

将插件目录放入 AstrBot 的 `plugins/` 目录，重启即可。

```bash
cd AstrBot/plugins
git clone https://github.com/youshen2/astrbot_plugin_forcejoin.git
```
