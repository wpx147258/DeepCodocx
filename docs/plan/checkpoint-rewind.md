# 代码检查点与回滚（阶段 1 + 2.1 + 2.2）

状态：已实现。目标是对齐 Claude Code 的 checkpoint/rewind 语义，同时保留更安全的冲突策略。

## 语义

- 每个用户 turn 建立一个 checkpoint，key 为用户消息 id。
- `Write` / `Edit` 首次写入某文件前保存 pre-image；工具结束后记录 post-hash。
- `Bash` 只记录 `shell_ran` 与命令预览；任意 shell 副作用不承诺回滚。
- 编辑早期消息或重新生成时，前端先请求 `/api/rewind/preview`：
  - 有文件变更：选择“恢复代码并继续”或“只重写对话”或取消；
  - 没有文件变更：直接回滚，不弹阻塞确认；shell 命令只作为信息展示。
- 每条用户消息都有 `Rewind` 入口，可独立决定：
  - **恢复代码和对话**：文件回到该消息之前，同时删除该消息及其后的对话；
  - **只恢复代码**：保留对话，只把文件恢复到该消息之前；
  - **只恢复对话**：保留文件，只删除该消息及其后的对话。
- 存在冲突时默认选中“只恢复对话”；选择任何代码恢复都必须显式点
  “Restore anyway” 才会覆盖用户手动修改。
- 冲突文件行有 `Diff` 按钮，按需展开“当前内容 → 恢复目标”的 diff，
  覆盖前可以先看清会丢失什么；diff 只读，不影响文件或 checkpoint。
- 文件在 turn 之后被手动修改时标记 conflict；默认拒绝覆盖，只有 `force` 才写回。
- 同一个用户 turn 的 continue/regenerate 会复用已有 manifest，不会丢掉第一次尝试
  的 pre-image。
- 符号链接、硬链接、非常规文件、workspace 外路径不 checkpoint；超大文件/超出
  turn 配额标记 partial，恢复中止。
- 恢复前先校验全部 blob 和目标父目录；任何一个目标不安全或备份缺失/损坏都
  **不写入并中止**，避免半恢复后截断对话。
- 只有 `persist=True` 的会话创建 checkpoint；`run` 的临时会话不做快照。

## 存储

```text
<session>/checkpoints/
  turns/<user_message_id>.json
  blobs/<sha256[:2]>/<sha256>
```

- pre-image 按内容去重，manifest 保存 before/after state、hash、mode。
- 保留最近 100 个 checkpoint，30 天后清理；未被 manifest 引用的 blob 一并 GC。
- 位于 session 根目录，`--fork` 的整目录复制会带上；删除 session 时一起删除。
- fork 后若 workspace 路径变化，manifest 会被忽略并提示。

## API

- `POST /api/rewind/preview` `{message_id}` 或 `{last_user: true}`：
  返回 files/status/conflict、shell_ran、commands、warnings、partial、restorable。
- `POST /api/rewind/diff` `{message_id | last_user, path}`：
  返回**当前文件 → 恢复目标**的 unified diff（`--- current` /
  `+++ restored`），以及 status、conflict、字节数和 warning。
  只能读取该 checkpoint 中真实存在的路径；不修改任何文件。
  - 输出上限 64 KiB / 800 行，超出标记 `truncated`；
  - 输入超过 1 MiB 或包含 NUL 时返回 `binary`/warning，不生成 diff；
  - 父目录被符号链接替换、文件不可读时只返回 warning。
- `POST /api/rewind`：
  - `files: "restore" | "keep"` 决定文件；`conversation: "rewind" | "keep"`
    决定对话，缺省为 `"rewind"`（兼容编辑/重新生成流程）；
  - 只有存在文件变更时才要求 `files` 决策，否则 `409` 并返回 preview；
    纯 shell turn 不再弹确认；
  - `files: "restore"` 时先恢复文件；`conversation: "rewind"` 再截断 transcript，
    `"keep"` 则只改代码、保留对话（恢复后 manifest 的 after 重置为 before，
    再次 preview 不再出现可恢复文件）；
  - 没有可恢复文件却请求 restore，或两者都选择 keep，返回 `400`；
  - conflict 时 `409`，显式 `force: true` 才覆盖；
  - 返回 state 与 `rewind.preview/restore/files/conversation` 报告。

## 限制

- 不保证 Bash、外部进程、网络/数据库副作用可恢复；选择代码恢复时会列出 shell
  命令作为提示，但纯 shell turn 不再弹阻塞确认。
- 手动编辑与 shell 改动无法完全区分；默认按 conflict 保护用户修改。
- 不是 Git 的替代品；大文件、硬链接、workspace 外路径不保证可恢复。
- 恢复创建的文件时不会删除因此变空的父目录。
- 只恢复代码后，对话仍描述被撤销的改动；这是显式选择，后续 turn 以新的文件状态为准。
- CLI 尚无 `/rewind`；全局 Rewind 目前只在 Web 端。
- 最坏磁盘占用仍受“100 个 checkpoint × 单 turn 50 MiB”约束，后续可加 session 总配额。
