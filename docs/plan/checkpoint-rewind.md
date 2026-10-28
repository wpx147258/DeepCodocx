# 代码检查点与重新生成回滚（阶段 1）

状态：已实现。目标是对齐 Claude Code 的 checkpoint/rewind 语义，同时保留更安全的冲突策略。

## 语义

- 每个用户 turn 建立一个 checkpoint，key 为用户消息 id。
- `Write` / `Edit` 首次写入某文件前保存 pre-image；工具结束后记录 post-hash。
- `Bash` 只记录 `shell_ran` 与命令预览；任意 shell 副作用不承诺回滚。
- 编辑早期消息或重新生成时，前端先请求 `/api/rewind/preview`：
  - 有文件变更：选择“恢复代码并继续”或“只重写对话”或取消；
  - 只有 shell 变更：提示副作用不可回滚，仍要求显式确认；
  - 无变更：不弹窗。
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
- `POST /api/rewind`：
  - 有变更或 shell 时必须带 `files: "restore" | "keep"`，否则 `409` 并返回 preview；
  - `files: "restore"` 时先恢复文件，再截断 transcript；
  - conflict 时 `409`，显式 `force: true` 才覆盖；
  - 返回 state 与 `rewind.preview/restore` 报告。

## 限制

- 不保证 Bash、外部进程、网络/数据库副作用可恢复；审批弹窗会列出 shell 命令作为提示。
- 手动编辑与 shell 改动无法完全区分；默认按 conflict 保护用户修改。
- 不是 Git 的替代品；大文件、硬链接、workspace 外路径不保证可恢复。
- 恢复创建的文件时不会删除因此变空的父目录。
- 本期只有编辑/重新生成入口；全局 Rewind 菜单（只恢复代码、只恢复对话、摘要）属于阶段 2。
- 最坏磁盘占用仍受“100 个 checkpoint × 单 turn 50 MiB”约束，后续可加 session 总配额。
