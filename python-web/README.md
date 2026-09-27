# CSI OpenBase for Python

这是 macOS 仓库内置的 CSI OpenBase Python Web 后端，包含抖音创作者授权、创作者中心表格导出、主页视频本地归档、用户手动触发的匿名化评论导出，以及本地 Web 界面。

它作为普通源码直接提交，不是 Git submodule；macOS 开发和发布构建不会克隆或要求另一个项目。初始导入来源记录在 [`UPSTREAM.md`](UPSTREAM.md)。

Swift 宿主只负责窗口、WKWebView、认证会话和进程生命周期，不另行实现采集逻辑。

## Requirements

- Python 3.12+
- Chromium 支持的 Windows、macOS 或 Linux 环境

## Install and run

在本目录执行：

```powershell
python -m pip install -e ".[test]"
python -m playwright install chromium
csi-openbase
```

也可以直接运行：

```powershell
python scripts/run_openbase.py
```

程序默认监听 <http://127.0.0.1:8000/>。源码运行时数据默认保存在本项目的 `var/`；从 wheel 安装后，数据默认保存在当前用户的平台应用数据目录（Windows 为 `%LOCALAPPDATA%\CSI OpenBase`，macOS 为 `~/Library/Application Support/CSI OpenBase`，Linux 遵循 XDG data 目录）。通过 `CSI_OPENBASE_HOME` 指定归档工作目录，通过 `CSI_OPENBASE_SESSION_HOME` 指定浏览器登录态目录。

旧的多工作区管理入口仍可通过 `csi-openbase-dashboard` 或 `python scripts/run_dashboard.py` 启动，供既有源码部署继续使用。当前后台没有公网多用户认证，不应直接暴露到公网。

## Version

本目录的 `VERSION` 是 Python 包版本号的唯一来源。版本格式为 `x.x.xx`，从 `0.0.10` 开始；末段从 `10` 递增到 `99`，之后将次版本加一并把末段重置为 `10`，例如 `1.1.99` 的下一版本是 `1.2.10`。

查看下一版本但不修改文件：

```powershell
python scripts/bump_version.py
```

确认发布后写入下一版本：

```powershell
python scripts/bump_version.py --apply
```

## Data model

- 一个工作目录对应一个创作者账号；桌面端由用户选择目录，源码版默认使用 `var/local`。
- 每次创作者中心导出保存到 `exports/<日期时间>/`，并生成包含状态、大小和 SHA-256 的 manifest。
- 主页发现批次位于 `works/discovery/`，逐视频档案位于 `works/videos/douyin/`；首页“评论数”显示平台最后一次返回的总数及相对上一次的数量变化。
- “获取最新评论”只更新评论总数和变化量，不下载评论内容；评论正文仅由“导出评论”手动导出，并按视频和批次保存匿名化 `.jsonl`。
- `openbase.sqlite3` 保存本地索引和任务状态；原始下载及不可变快照仍是数据源。
- 页面可按范围清空本地数据，具体边界见 [清空本地数据](#清空本地数据)。
- 默认不下载视频 MP4，不保存观众昵称、用户 ID、头像、属地或创作者平台密码。

完整模型见 [`docs/data-model.md`](docs/data-model.md)，数据契约位于 [`admin_app/resources/`](admin_app/resources/)。

## 清空本地数据

在本地页面底部的“数据管理”区域点击“清空数据”，确认当前工作目录后选择清理范围：

| 清理范围 | 删除内容 | 保留内容 |
| --- | --- | --- |
| 平台导出的原始数据 | `exports/` 中已下载的表格、对应导出任务记录及最近导出状态 | 视频档案、评论数据、账号索引和浏览器登录授权 |
| 用户评论数据 | 各视频 `comments/` 目录、评论导出任务记录、已导出评论数及最近导出时间 | 视频档案、平台最后一次返回的评论总数和变化量、原始表格和浏览器登录授权 |
| 全部数据 | `exports/`、`works/` 及 SQLite 中的账号、视频和任务索引 | 工作目录本身、根目录中的其他文件与目录、SQLite 文件及表结构、运行日志和浏览器登录授权 |

清空操作无法撤销，需要在对话框中明确确认；存在等待中或运行中的任务时不能执行。清空“全部数据”后，本地账号索引会被移除，下一次采集前需要重新校验创作者账号，但已保存的浏览器登录授权不会被主动删除。

`exports/` 和 `works/` 是 CSI OpenBase 托管目录，清空对应范围会删除其中全部内容。需要长期保留的数据应先备份到这两个目录之外。检测到符号链接、Windows 目录联接点、文件系统挂载点，或清理范围与浏览器授权目录重叠时，程序会拒绝清空；中断的清理操作会在下次启动时根据事务状态自动恢复或继续完成。

## Repository layout

```text
./
  admin_app/                 本地后台、采集、任务、分析和页面资源
  docs/                      架构与数据模型文档
  migrations/                兼容管理后台的数据库迁移
  scripts/                   启动、工作区、导入和分析命令
  tests/                     自动化测试与样本
  var/README.md              本地数据与备份说明
  VERSION                    Python 包版本号
  pyproject.toml             包、依赖、命令入口和测试配置
```

`var/`、`workspace-data/`、浏览器会话以及导出的表格均由 Git 忽略。Git 忽略不是加密，公开源码包或问题报告不应包含这些数据。

如果从旧仓库路径迁移，工作数据会继续保留；源码版默认会话目录包含工作目录绝对路径的哈希，因此目录改名后可能需要重新进行一次创作者授权。也可以将 `CSI_OPENBASE_SESSION_HOME` 显式指向原会话目录。

## Verify

```powershell
python -m pytest
python -m compileall -q admin_app scripts
python -m pip wheel . --no-deps --wheel-dir dist
```

## License

CSI OpenBase is licensed under the Apache License 2.0.

This license applies only to the code and documentation contained
in this repository.

CSI proprietary scoring models, weights, industry benchmarks,
commercial report logic, CSI Core components and services provided
through aicsi.cn are not included in this repository and are not
licensed under Apache-2.0.
