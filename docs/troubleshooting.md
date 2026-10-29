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
无有效库时界面会展开目录输入；新转换产物可通过新建文档会话载入。

## 找到了图片，模型却没分析图像

浏览器预览与模型输入是两条路径。模型需要调用 `doc_view` 或 `Read` 得到实际图片，
所选 profile、模型和服务端也必须支持图像输入。查看工具输出确认是否返回图片块。
