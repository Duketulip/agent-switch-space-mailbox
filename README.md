# Agent Switch Space Mailbox

把一块网盘目录（固定为 `AgentSwitchSpace`）当作多个 AI Agent 之间的**异步消息邮箱**：一条消息一个 JSON 文件，**状态就是文件所在的目录**。不需要双方同时在线，也不需要长连接、不需要各自开放端口。

当前实现的默认 provider 是**夸克网盘**（包装官方 `quarkclouddrive` CLI），另有一个语义等价的本地 provider（`dir_local`）用于离线自测。

## 它解决什么问题

不同 Agent 常常活在彼此进不去的地方：

- 本机 CLI 型 Agent（能跑命令、有本地文件系统）
- 另一台机器 / 另一个沙箱里的 Agent
- 网页端 ChatGPT 这类**装不了 CLI、没有本地文件系统**的 Agent
- 未来可能接入的第三方 Agent

它们唯一的公共地面通常只有"用户自己的网盘"。于是把网盘目录结构直接当成协议：

| 目录 | 含义 |
|---|---|
| `inbox/<Agent>/` | 待该 Agent 处理 |
| `processing/<Agent>/` | 已被该 Agent 领取（抢占成功） |
| `completed/<Agent>/` | 处理完成 |
| `failed/<Agent>/` | 处理失败（另有 `<id>.error.json` 记录原因） |
| `sent/<Agent>/` | 自己发出的存档 |
| `system/` | 初始化配置与已注册 Agent 清单 |
| `attachments/` | 附件（V0.1 预留，未启用） |

**为什么用目录而不是改 JSON 里的 status 字段？** 因为网盘**同名上传会自动改名**（`name(1).json`）、且没有原地改写能力——"改文件内容"这条路在网盘上不成立。目录移动是它可靠支持的唯一"状态变更"。

## 两条硬约束（比任何功能都优先）

1. **只允许在 `AgentSwitchSpace` 下工作**，其他网盘文件一律不碰。路径里的 `..`、绝对路径、盘符路径会被 `scripts/policy.py` 直接拦下；本地 provider 还会用 `realpath` 复检，防符号链接逃逸。
2. **写入或删除超过 2 GiB 必须先与用户确认**。未确认则中止并如实汇报；大小未知时**拒绝**而不是猜。确认方式：命令加 `--confirm-large`。

这两条写在 `scripts/policy.py` 里，**位于 provider 和协议核心之下**，任何 provider 都绕不过去；不是靠文档里"约定"，是代码级的闸门。

## 安装

前提：`python ≥ 3.9`；若用真实网盘还需 `node ≥ 16` 与官方 CLI。

```bash
git clone https://github.com/Duketulip/agent-switch-space-mailbox.git
cd agent-switch-space-mailbox
```

真实网盘还要准备官方 CLI（未随本仓库分发，也不应提交凭据）：

```bash
# 从夸克官方 skill 包解压，得到 scripts/quark-drive.cjs
# 然后用 --cli 或环境变量 QUARK_DRIVE_CLI 指过去
export QUARK_DRIVE_CLI=/path/to/quark-drive.cjs
```

## 5 分钟上手

```bash
python scripts/mailbox_cli.py doctor                       # 体检
python scripts/mailbox_cli.py init --agents "AgentA,AgentB" # 首次初始化（幂等）

python scripts/mailbox_cli.py send --from AgentA --to AgentB \
    --type TASK --content "请把附件转成 CSV，回我路径。"

python scripts/mailbox_cli.py inbox --agent AgentB          # 对方侧收件
python scripts/mailbox_cli.py claim --agent AgentB --id MSG-20261116-ab12cd34
python scripts/mailbox_cli.py reply --agent AgentB --id MSG-20261116-ab12cd34 --text "已完成"
```

离线自测（不需要网盘，不需要 node）：

```bash
python tests/run_tests.py
```

## 新接入一个环境 / 一个 Agent 该做什么

见 [`references/onboarding.md`](references/onboarding.md)：授权 → 代号注册 → 沟通主题确认 → PING 自检，四关逐关给判定标准和话术。

网页端 Agent（ChatGPT 之类）的参与方式见 [`references/web-agent-handoff.md`](references/web-agent-handoff.md)：**它能读、不能写**，回复由"人类中转"或 CLI 端代录。

## 实测代价（会影响你怎么安排任务）

单次 provider 调用 **6–8 秒**，与文件大小无关（是网盘 API 往返的固定开销）：

| 操作 | 调用次数 | 耗时 |
|---|---|---|
| `send`（含回读校验 + 存档） | ~6 | 40–50 s |
| `claim` / `done` | 3–5 | 30–40 s |
| `reply` | 5–7 | 50–70 s |
| 一轮 5 条完整收发 | — | 约 3–5 分钟 |

所以本 Skill 明确限制**每轮最多 5 条**，且不做自动无限轮询。

## 已知局限（都是实测出来的，不是猜的）

- 网盘**没有删除命令**：`failed/` 会永久累积，清理只能人工在客户端做。
- **移动的可见性有几秒延迟**（上传是 0 秒，移动不是）；`move` 之后 fid 可能变化，所以校验按"文件名命中 或 源目录已消失"双条件判定，并带 4×2 秒退避重试。
- **同名上传不覆盖**，自动改名 `name(1).json`；`message_id` 必须唯一是硬性要求。
- 凭据按**环境**各自授权（本次实测没有把登录态复制到第二台机器，跨设备复用**未验证**）。
- 单条消息正文上限 64 KiB；附件功能未启用。

## 项目状态

| 项 | 状态 |
|---|---|
| 离线测试（`dir_local`） | 8/8 通过（含两条硬约束的闸门验证） |
| 真实网盘全链路 | 已跑通 `init → send(PING) → inbox → claim → reply(PONG) → done`，每步回读校验通过 |
| > 2 GiB 真实场景 | **未实测**（本地造不出、网盘端无删除命令）；仅用缩阈值测试证明闸门接线正确 |
| 网页端 Agent 写回 | **不支持**（受平台限制），只支持只读参与 |
| 许可证 | 暂未选择（未授权默认开源许可） |

## 仓库结构

```
SKILL.md                        Skill 主入口（给 Agent 看）
README.md                       本文件（给人看）
CHATGPT_REVIEW_REQUEST.md       请外部评审用的说明与测试步骤
references/
  protocol.md                   消息结构与状态机
  providers.md                  provider 契约与接入
  onboarding.md                 新环境/新 Agent 接入向导
  web-agent-handoff.md          网页端 Agent 参与方式
scripts/
  policy.py                     两条硬约束（路径 + 大小）
  mailbox_core.py               协议核心
  mailbox_cli.py                命令行入口（单行 JSON 输出）
  providers/{__init__,dir_local,quark_drive}.py
tests/run_tests.py              离线测试
```

需求和阶段一技术验证报告**未发布**（属于内部讨论材料，留在本地）。
