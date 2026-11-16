# 消息协议（V0.1）

## 一条消息 = 一个文件

```
AgentSwitchSpace/
├── system/
│   ├── mailbox_config.json          初始化时写一次，之后不再改
│   └── agents/<Agent>.json          一个 Agent 一个文件（只增不改）
├── inbox/<Agent>/                   待该 Agent 处理
├── processing/<Agent>/              已被该 Agent 领取
├── sent/<Agent>/                    该 Agent 发出的副本（存档）
├── completed/<Agent>/               处理成功
├── failed/<Agent>/                  处理失败（另有 <id>.error.json 记原因）
└── attachments/                     附件（V0.1 只留目录，未启用）
```

文件名固定为 `<message_id>.json`。

## 消息结构

```json
{
  "protocol_version": "0.1",
  "message_id": "MSG-20261116-0e2dd019",
  "from_agent": "WorkBuddy",
  "to_agent": "ChatGPT",
  "message_type": "PING",
  "content": "ping from WorkBuddy，请回复 PONG。",
  "status": "NEW",
  "created_at": "2026-11-16T23:17:05+08:00",
  "updated_at": "2026-11-16T23:17:05+08:00",
  "reply_to": null,
  "task_id": null,
  "attachments": [],
  "metadata": {}
}
```

| 字段 | 约束 |
|---|---|
| `protocol_version` | 固定 `"0.1"` |
| `message_id` | `MSG-YYYYMMDD-<uuid8>`，**全局唯一**，也是文件名 |
| `from_agent` / `to_agent` | 必须已在 `system/agents/` 注册；两者不能相同 |
| `message_type` | `PING` / `MESSAGE` / `TASK` / `REPLY` / `NOTICE` |
| `content` | UTF-8 纯文本，≤ 64 KiB（超限直接拒发） |
| `status` | **只在创建时为 `NEW`**，之后不再变更（见下） |
| `created_at` / `updated_at` | ISO 8601，+08:00 |
| `reply_to` | 回复所指向的原 `message_id`，否则 `null` |
| `attachments` | 相对 `AgentSwitchSpace` 的路径列表 |
| `metadata` | 扩展信息（本实现会写 `original_status` / `archive_error` 等） |

## 状态 = 目录位置（关键设计决定）

网盘**同名上传会自动改名**（`name(1).json`）、**没有原地改写能力**，所以"改 JSON 里的 status 字段"这条路不成立。

因此：

| 目录 | 派生的状态 | 谁写它 |
|---|---|---|
| `inbox/<Agent>/` | `NEW` / `PENDING` | 发送方 |
| `processing/<Agent>/` | `PROCESSING` | 接收方（`claim`） |
| `completed/<Agent>/` | `DONE` | 接收方（`reply`/`done`） |
| `failed/<Agent>/` | `ERROR` | 接收方（`fail`） |

`status` 字段只是**创建时的快照**；判断真实状态一律看文件在哪个目录。
回复消息里用 `metadata.original_status = "DONE"` 告诉原发送者"你那条已处理"。

## 一次完整往返

```
A:send  ──►  inbox/B/<id>.json        （同时写 sent/A/<id>.json）
                 │
B:claim ─────► processing/B/<id>.json
B:reply ─────► inbox/A/<reply_id>.json （reply_to = <id>）
B:finalize ──► completed/B/<id>.json
                 │
A:inbox  ◄─── 读到 REPLY，核对 reply_to
A:claim/done ► completed/A/<reply_id>.json
```

## 唯一性与防重复

- `message_id` 含 uuid8，且就是文件名 → 写入即"创建不存在的文件"，同名必冲突（`AlreadyExists`）。
- 接收方用本地台账（`~/.agent_switch_space/<provider>-<agent>.json`）记住处理过的 id，`inbox` 会跳过。
- `reply` 要求消息在 `processing` 中，`finalize` 后它不在那了 → 天然防二次回复。
- 同一条原消息最多自动回复一次，**不递归**。

## 撞名与改名（实测行为，必须知道）

- 上传同名 → 远端变 `name(1).json`（原文件完好）。
- 移动到已有同名文件的目录 → 目标变 `name(1).json`。
- 因此：①所有校验按"文件名前缀 + fid"双重判定；②`message_id` 必须唯一，避免依赖改名兜底。

## 一致性

- 写入后**下载回读 + 逐字节比对**，不一致即视为失败（`ReadbackMismatch`）。
- 移动后校验"目标出现 或 源消失"；移动可见性有几秒延迟，代码带 4 次×2 秒重试。
- 不做"先查询再写入"式的伪原子操作：抢占的唯一依据是"移动成功且校验通过"。
