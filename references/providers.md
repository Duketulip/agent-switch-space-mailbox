# Provider 契约与接入

## 契约（core 只依赖这些）

所有 provider 实现同一组**文件操作**，路径一律是"相对邮箱根、已通过 `policy.check_rel` 校验"的形式：

| 方法 | 语义 | 关键约束 |
|---|---|---|
| `ensure_dir(relpath)` | 幂等创建目录（含中间层级） | 已存在不报错，返回后端 id |
| `list_dir(relpath)` | 列**直接子项** | 返回 `Entry{name,is_dir,size,id,updated_at}` |
| `read_text(relpath, name)` | 读取一个文本文件 | 不存在抛 `NotFound` |
| `write_text(relpath, name, text)` | 写入**新**文件 | 不覆盖；同名冲突由 core 判定 |
| `move(relpath, name, to_relpath)` | 移动到另一目录 | 移动后必须能校验 |
| `exists(relpath, name)` | 是否存在 | — |
| `stat(relpath, name)` | 条目元数据 | 可选能力 |
| `remove(relpath, name)` | 删除 | 可选；不支持就抛 `Unsupported` |

失败一律抛 `ProviderError`（子类：`AlreadyExists` / `NotFound` / `Unsupported`）。

## 内置 provider

### `quark_drive`（真实网盘，默认）

- 实现：包装官方 `quarkclouddrive` Skill 的 CLI `scripts/quark-drive.cjs`（node，**不修改**其代码）。
- 定位 CLI：`--cli <path>` → `$QUARK_DRIVE_CLI` → `<仓库>/_vendor/quark/scripts/quark-drive.cjs`。
- 依赖：`node ≥ 16` + 该环境已完成一次 `login`（凭据在 `<skill>/<agent渠道>/config.json`）。
- 命令映射：

| 契约方法 | CLI 命令 | 备注 |
|---|---|---|
| `ensure_dir` | `create-folder --dir-path <name> --parent-fid <fid>` | 先 `browse` 查再建，fid 有缓存 |
| `list_dir` | `browse --parent-fid <fid> --all` | 读 artifact（JSONL）拿全量 |
| `read_text` | `download --fid <fid> --output-dir <tmp> --overwrite` | 落到临时目录再读 |
| `write_text` | `upload <local> --parent-fid <fid>` | 不做预检，冲突交给 core 回读发现 |
| `move` | `move <fid> --target-fid <fid>` | 只认目标目录正向证据，6×2s 重试 |
| `remove` | — | **不支持**，抛 `Unsupported` |

- 实测得到的三条硬事实（影响实现，详见 `技术可行性调研报告.md`）：
  1. 同名上传**不覆盖**，自动改名 `name(1).json`；
  2. 移动撞名同样改名；移动后 fid 可能变、目录可见性有几秒延迟 → 校验只按"目标目录出现同名/fid 命中"判定，**不**用"源目录消失"当成功依据（外部评审指出该兜底会造成假阳性，已移除）；
  3. 无删除命令 → 邮箱只增不减。

### `dir_local`（离线自测 / 本地兜底）

- 一个本地目录当邮箱根（`--ref <dir>`），语义刻意与网盘对齐：同名不覆盖、移动撞名加 `(n)`。
- 用途：无网/无授权时跑完整协议；`tests/run_tests.py` 用它做回归。
- 支持 `remove`（因此 R2 的删除闸门在这里也能被测到）。

## 新加一个 provider（换任何网盘/对象存储）

1. 在 `scripts/providers/` 新建模块，`@register("your_name")` 注册工厂。
2. 实现上表方法；把"能力差异"如实抛出来（不支持就抛 `Unsupported`，不要假装成功）。
3. **不要**自己实现安全策略：路径校验、大小闸门都在 `policy.py` 与 `mailbox_core.write_verified` 里，provider 只负责"照着做"。
4. 在 SKILL.md 的 Provider 表加一行，说明定位参数与授权要求。

## 授权与凭据

- 首次：`node scripts/quark-drive.cjs login`，会拉起本地授权页，用户确认后写入 `config.json`。
- 也支持 `--token <agent_auth_code>`（非交互）。
- **每个环境（每台机器）各自授权一次**。本次实测出于安全**没有**把登录态复制到第二台机器，因此"同一凭据跨设备复用"未验证，不要假定可用。
- `refreshToken` 有有效期（本次观察到约 2027-05 失效）；到期后需重新 `login`。
- 授权失效的返回：`-103 未登录` / `-1408 未完成授权认证` → 停下并要求用户重新授权。

## 安全边界（provider 层面同样生效）

- R1：`policy.check_rel` 只放行 `AgentSwitchSpace` 白名单子目录；`dir_local` 还会用 `realpath` 再验一次包含性，防止符号链接逃逸。
- R2：>2 GiB 写入/删除必须 `--confirm-large`。判定用**真实字节数**（写入前看本地文件大小，删除前看远端 `size`）；大小未知时**拒绝执行**而不是猜。
