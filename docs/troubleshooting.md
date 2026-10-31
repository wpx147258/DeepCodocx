# 常见问题

**简体中文** | [English](troubleshooting.en.md) · [首页](../README.md)

## WSL 提示没有图形浏览器

使用 `--no-open`，在 Windows 浏览器手动打开终端显示的地址：

```bash
uv run dcx web --provider deepseek --no-open
```

## address already in use

另一个进程已经监听该端口。可以保留它，为 DeepCodocx 选择另一个端口：

```bash
uv run dcx web --provider deepseek --port 8766 --no-open
```

在 Linux / WSL 上，`ss -ltnp 'sport = :8765'` 可查看当前监听者。

## 移动项目后虚拟环境失效

Python 虚拟环境的激活脚本和命令入口包含绝对路径，移动目录后应重建。
先退出旧环境，再在新的项目目录执行：

```bash
deactivate
uv venv --clear --prompt dcx .venv
uv sync --extra dev
source .venv/bin/activate
```

`deactivate` 只在当前已激活环境时使用。无需激活也可以直接 `uv run dcx ...`。
包名为 `dcx`，`pyproject.toml` 的入口应指向 `dcx.entry:main`。

## 从 cli-agent 升级到 dcx

更新代码后执行 `uv sync`，再使用 `uv run dcx ...`。发行包、可执行命令与 Python 包名
均为 `dcx`；旧 `cli-agent` 命令不再安装。仅更改命令名不会改变项目 ID。

配置目录改为 `~/.config/dcx`，数据目录改为 `~/.local/share/dcx`，并遵循对应的 XDG
环境变量。若新目录不存在、旧 `cli-agent` 目录存在，首次启动会创建 `dcx → cli-agent`
符号链接。原文件保持在原处，历史中的绝对路径、会话锁和文档库选择继续有效；
**保留旧目录，不要删除链接所指向的数据**。两套目录都存在时优先使用 `dcx`，不会
自动合并或覆盖。无法创建链接时会报错，不会悄悄切换到空白历史。

在同一浏览器地址下，旧版的草稿、主题、侧栏设置及当前标签页的 token 会自动转为
`dcx-*` 存储键。已有的新设置优先；更换主机或端口会使用浏览器的另一套存储。

## 移动项目目录后历史会话不见了

项目 ID 按工作目录绝对路径的 SHA-256 前 12 位生成。移动或重命名项目目录会切换历史命名空间，旧文件
仍位于 `$XDG_DATA_HOME/dcx/projects/<旧项目 ID>/sessions/`，默认数据目录是
`~/.local/share/dcx`。

迁移时先停止相关会话写入、备份数据，再将旧 sessions 中不重名的目录复制到新项目的
sessions 目录。保留 session id、记录内容、资源和时间戳；遇到同名目录应比较内容，
不覆盖已有会话。记忆和检查点也需要单独核对；检查点含工作目录和内容 hash 校验。

普通与 DeepDocx 历史分开显示，空白会话不会出现在列表中。恢复后刷新页面即可重新
读取列表。通过旧目录兼容链接访问的数据仍保存在原来的 `cli-agent` 目录中。

## 文档库列表为空

连接的应是服务端能访问的、已转换为 bundle 的目录。支持 `paper-bundles` 根目录，
以及包含它的 `data` 目录。可以用配置直接指定：

```toml
[deepdocx]
default_library = "/path/to/paper-bundles"
```

成功使用的选择会被记住。没有保存或配置的目录时，会发现附近的 `deep-docs-unified/data`。
无有效库时界面会展开目录输入。Web／CLI 新入库会通知读侧在下次读取时刷新；若会话
已限定文档范围，范围外的新文档需切换范围。外部复制的 bundle 可通过新会话重新发现，
语义索引另需手动同步。

## 入库完成，但语义检索没有结果

入库完成的是源 bundle 和全文目录；向量需要独立同步。先在 **索引管理** 查看当前库
的覆盖、服务状态及任务错误，或运行：

```bash
dcx docs index status --library /path/to/paper-bundles
dcx docs index sync --library /path/to/paper-bundles
```

默认首次手动同步后，该库才会对后续入库自动排队。仅配置密钥、恢复旧会话或外部复制
bundle 不会自动建库。确认查询所属库与已选文档范围一致；没有当前有效索引时继续
使用关键词 `doc_search`，不要把空语义结果当作论文中不存在相关内容的证据。

## 密钥已配置，仍提示未设置或请求失败

默认全部编码读取 `DASHSCOPE_API_KEY`，生成描述读取 `GITEE_API_KEY`；它们与
`DEEPSEEK_API_KEY` 等对话密钥独立。导出变量后需重启 Web／MCP 进程；只改
`.bashrc` 或在另一个终端设置不会改变已有进程的环境。若 TOML 改过 `api_key_env`
或 `joint_key_env`，应导出对应名称。

“已配置”只代表存在配置和环境变量，不保证服务授权、余额、模型可用。查看任务错误
和 `worker.log`，核对端点与地域。百炼默认模型 ID 是 `qwen3-vl-embedding`，不是
Gitee 使用的 `Qwen3-VL-Embedding-8B`；两种服务的模型和维度不能互换。

## 缺少图注、图文联合或生成描述计数

若点击同步要处理整库，改用 **选择论文**，按标题搜索并勾选一篇；CLI 可重复传入
`--doc DOC_ID`。只同步一篇不会删除其它文档的索引。百炼独立与联合编码的新默认值
都是 2560 维；若配置文件仍显式指定旧 Gitee 编码，需要移除旧覆盖或改为新默认配置，
再重启宿主并同步。未改的旧配置不会自动当作百炼使用。

Basic 通常提供页面图，Marker 才能提供更多图表裁图和原图注。缺少原图注时，
`caption_original` 及对应联合表征可以为零；关闭描述生成后，生成描述相关计数也会为零。
原生联合表征还需要百炼服务可用。先查看文档级状态和具体错误，再普通同步补齐，
不要仅凭“四类计数不相同”判断构建失败。

## 文档显示部分完成、待同步或 worker 已停止

源内容或配置变化会使旧代索引过期，查询会过滤它。部分完成说明当前仍有表征可用，
再次普通同步会复用缓存并重试；无需默认使用 `--force`。修复损坏的源文件后也可
重新同步。worker 意外退出不会自动重新启动任务，记录会标为失败；手动同步继续处理。

关闭面板或停止对话不会停止索引，请在索引面板停止，或使用：

```bash
dcx docs index cancel --library /path/to/paper-bundles JOB_ID
```

日志位于 `$XDG_DATA_HOME/dcx/semantic/<library-id>/jobs/<job-id>/worker.log`。
当前没有文件系统监听；外部新增产物需同步。库根路径变化产生新的 library ID，也需
重新同步。流程与状态字段见[多模态索引](multimodal-index.md)。

## 找到了图片，模型却没分析图像

浏览器预览与模型输入是两条路径。模型需要调用 `doc_view` 或 `Read` 得到实际图片，
所选 profile、模型和服务端也必须支持图像输入。查看工具输出确认是否返回图片块。
以图搜图只是编码并定位相似来源，也不会自动把命中的图片送给回答模型。
