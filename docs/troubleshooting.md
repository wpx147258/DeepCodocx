<!-- Generated language panels. Edit this file's default-open language, then run python scripts/sync_doc_languages.py. -->

<details name="deepcodocx-doc-language" lang="en">
<summary>English</summary>

<!-- doc-language:en:start -->
# Troubleshooting

[Home](../README.en.md)

## No graphical browser in WSL

Use `--no-open`, then open the printed URL manually in your Windows browser:

```bash
uv run cli-agent web --provider deepseek --no-open
```

## Address already in use

Another process is listening on the port. Keep it running and choose a different
port for DeepCodocx:

```bash
uv run cli-agent web --provider deepseek --port 8766 --no-open
```

On Linux / WSL, `ss -ltnp 'sport = :8765'` shows the current listener.

## Virtual environment breaks after moving the project

Activation scripts and installed commands contain absolute paths. Rebuild the
environment after moving the directory. Leave the old environment first, then
run these commands in the new project directory:

```bash
deactivate
uv venv --clear .venv
uv sync --extra dev
source .venv/bin/activate
```

Use `deactivate` only when an environment is active. Activation is optional;
`uv run cli-agent ...` can use the project environment directly. The package is
`dcx`, so the entry point in `pyproject.toml` must target `dcx.entry:main`.

## Session history disappears after a rename

The project ID is the first 12 characters of the SHA-256 hash of the absolute
working-directory path. Renaming the directory changes its history namespace.
The original files remain under
`$XDG_DATA_HOME/cli-agent/projects/<old-project-id>/sessions/`; the default data
directory is `~/.local/share/cli-agent`.

Before migrating, stop related session writes and back up the data. Copy session
directories with non-conflicting IDs into the new project's sessions directory.
Preserve IDs, records, resources, and timestamps. Compare conflicting
directories instead of overwriting existing sessions. Review memory and
checkpoints separately; checkpoints validate their workspace and content
hashes.

General and DeepDocx sessions have separate lists, and empty sessions are
hidden. Refresh the page after recovery to reload the list. Configuration and
saved library selections continue to use the `cli-agent` XDG paths.

## Empty document library list

Connect a preprocessed bundle directory that the server can access. The
`paper-bundles` root and a `data` directory containing it are both accepted. You
can set a directory in configuration:

```toml
[deepdocx]
default_library = "/path/to/paper-bundles"
```

A successful selection is remembered. When no directory is saved or configured,
a nearby `deep-docs-unified/data` can be discovered. If no valid library is
available, the dialog expands its directory input. Create a new document session
to discover newly converted bundles.

## The figure is visible, but the model does not analyze it

Browser previews and model input are separate paths. The model must receive an
actual image through `doc_view` or Read, and the selected profile, model, and
endpoint must support images. Inspect the tool output to confirm that image
blocks were returned.
<!-- doc-language:en:end -->

</details>

<details name="deepcodocx-doc-language" lang="zh-CN" open>
<summary>简体中文</summary>

<!-- doc-language:zh-CN:start -->
# 常见问题

[首页](../README.md)

## WSL 提示没有图形浏览器

使用 `--no-open`，在 Windows 浏览器手动打开终端显示的地址：

```bash
uv run cli-agent web --provider deepseek --no-open
```

## address already in use

另一个进程已经监听该端口。可以保留它，为 DeepCodocx 选择另一个端口：

```bash
uv run cli-agent web --provider deepseek --port 8766 --no-open
```

在 Linux / WSL 上，`ss -ltnp 'sport = :8765'` 可查看当前监听者。

## 移动项目后虚拟环境失效

Python 虚拟环境的激活脚本和命令入口包含绝对路径，移动目录后应重建。
先退出旧环境，再在新的项目目录执行：

```bash
deactivate
uv venv --clear .venv
uv sync --extra dev
source .venv/bin/activate
```

`deactivate` 只在当前已激活环境时使用。无需激活也可以直接 `uv run cli-agent ...`。
包名为 `dcx`，`pyproject.toml` 的入口应指向 `dcx.entry:main`。

## 更名后历史会话不见了

项目 ID 按工作目录绝对路径的 SHA-256 前 12 位生成。更名会切换历史命名空间，旧文件
仍位于 `$XDG_DATA_HOME/cli-agent/projects/<旧项目 ID>/sessions/`，默认数据目录是
`~/.local/share/cli-agent`。

迁移时先停止相关会话写入、备份数据，再将旧 sessions 中不重名的目录复制到新项目的
sessions 目录。保留 session id、记录内容、资源和时间戳；遇到同名目录应比较内容，
不覆盖已有会话。记忆和检查点也需要单独核对；检查点含工作目录和内容 hash 校验。

普通与 DeepDocx 历史分开显示，空白会话不会出现在列表中。恢复后刷新页面即可重新
读取列表。配置目录和已选文档库仍使用 `cli-agent` 的 XDG 路径。

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
<!-- doc-language:zh-CN:end -->

</details>
