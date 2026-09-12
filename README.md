# CSI OpenBase for macOS

这是自包含的 CSI OpenBase macOS 项目。原生宿主使用 SwiftUI 和 WKWebView；
仓库内的 `python-web/` 提供创作者授权、表格导出、视频归档、评论采集、持久化
和本地 Web 界面。项目不包含 CSI Core 的评分、权重、行业基准或商业报告逻辑。

正式仓库：[CSI-OpenBase/mac](https://github.com/CSI-OpenBase/mac)

SSH 克隆地址：`git@github.com:CSI-OpenBase/mac.git`

Windows 主机维护在 [CSI-OpenBase/winform](https://github.com/CSI-OpenBase/winform)。

## 使用发行版

要求 macOS 14 或更高版本。将 `CSI OpenBase.app` 移到“应用程序”目录并打开。
应用采用单实例主窗口，避免多个网页视图争用同一桌面认证 Cookie。
首次启动会在“文稿”中创建 `CSI OpenBase` 工作目录，也可以点击“选择目录”改用
其他位置。选择结果通过 macOS 安全书签持久化。

应用为每次后端启动分配随机回环端口、256 位 token 和独立 nonce。健康检查必须
同时返回正确运行模式和 nonce，随后 token 通过 WKWebView 的 HttpOnly Cookie
传递，不出现在 URL 中。后端及其 Chromium 子进程运行在原生 supervisor 创建的
独立进程组内；关闭应用时先请求后端正常结束，超时后由 supervisor 清理整组。
宿主崩溃或被强制退出时，控制管道关闭也会触发同一清理流程。运行中若 supervisor
未在安全期限内确认退出，应用会保留旧进程句柄并拒绝启动新实例或切换工作目录。

## 开发

要求：

- macOS 14+
- Xcode 15.3+ 或对应的 Swift 5.10 工具链
- Python 3.12+
- 首次打包时可访问 Python 包索引和 Playwright 浏览器下载服务

仅在 Swift `DEBUG` 构建中，可以用环境变量指定开发后端：

```bash
python3 -m venv python-web/.venv
python-web/.venv/bin/python -m pip install -e 'python-web[test]'
python-web/.venv/bin/python -m playwright install chromium
swift build
PATH="$PWD/python-web/.venv/bin:$PATH" \
  CSI_OPENBASE_BACKEND="$PWD/python-web/scripts/run_openbase.py" \
  swift run --skip-build CSIOpenBaseMac
```

仓库中的 `python-web/scripts/run_openbase.py` 已标记为可执行文件；上面的 `PATH`
确保其使用 `python-web/.venv`。`CSI_OPENBASE_BACKEND` 也可以指向 PyInstaller
onedir 内的同名主程序。`CSI_OPENBASE_LAUNCHER` 可在 DEBUG 中覆盖 supervisor，通常无需
设置。Release 构建会忽略这两个环境变量，只执行应用包内经过签名的后端和
supervisor。Swift 生成文件位于 `.build/`，Python 虚拟环境位于
`python-web/.venv/`，两者都不会提交。

## 构建应用

仓库已包含完整的 `python-web/` 源码。构建脚本默认在 `.build/backend-venv`
创建隔离环境，安装本地项目的 `.[desktop]` 依赖及 Playwright Chromium，再从
已安装包入口生成 PyInstaller onedir 后端：

```bash
chmod +x build_macos.sh
./build_macos.sh
```

正常开发和发布不需要另一个源码仓库。迁移或 CI 场景仍可显式改用另一个 Python
源码目录或已构建的 wheel；两种模式都会先安装到隔离环境再冻结，不会在发行版中
依赖该输入路径：

```bash
./build_macos.sh --python-project /absolute/path/to/python-project
./build_macos.sh --python-wheel /absolute/path/to/csi_openbase-0.0.10-py3-none-any.whl
```

高级场景也可以跳过自动冻结，显式传入同架构的 macOS 后端。路径可以是
PyInstaller onedir 目录，也可以是单个 Mach-O 可执行文件。脚本会在任何清理前
解析真实路径，拒绝输入与 `.build`/`dist` 重叠，并校验 Mach-O 与构建机架构：

```bash
./build_macos.sh \
  --backend /absolute/path/to/CSI.OpenBase.Backend \
  --backend-notices /absolute/path/to/backend-license-directory
```

输出仅写入 `dist/`：

- `dist/CSI OpenBase.app`
- `dist/CSI-OpenBase-macOS.zip`

中间 venv、浏览器和 PyInstaller 产物全部位于 `.build/`。

使用 Developer ID 证书签名：

```bash
./build_macos.sh \
  --sign "Developer ID Application: Example Company (TEAMID)"
```

脚本会把 mac 项目自身的 `LICENSE` 与 `NOTICE` 放入应用资源目录，并从隔离环境
生成 `backend-licenses/python-packages.txt`、`CPython-LICENSE.txt` 以及已安装的
`python-web` 包内 `LICENSE`、`NOTICE`、`THIRD-PARTY-NOTICES.md` 和完整
`licenses/` 子树。报告只收录文本许可内容及包内相对路径，不复制二进制或本机构建
绝对路径。使用 `--backend`
时，`--backend-notices` 目录必须提供 `CPython-LICENSE.txt`、
`python-packages.txt`，并在根目录或 `backend/` 子目录提供上述三份项目文件和
其引用的 `licenses/` 子树；缺失、包含 symlink 或含非文本文件时构建失败。

签名流程会从内到外逐层签署 Mach-O 与嵌套 bundle；Chromium/V8 使用独立的 JIT、
可执行内存和 library-validation entitlement。`codesign --verify` 通过只证明签名
结构有效，不能证明 Chromium 可运行。

### 发布前强制验证

每个正式签名产物都必须在目标架构的干净 macOS 14+ 机器上完成以下 smoke；任何
一步失败都不得发布：

1. 对最终 `.app` 执行 `codesign --verify --deep --strict --verbose=2`。
2. 从该 `.app` 启动，完成创作者授权，并实际触发一次需要 headed Chromium 的导出。
3. 正常结束任务并退出应用，确认 CSI 后端、supervisor 和该应用启动的 Chromium
   helper 均已退出；再强制结束宿主重复一次，确认进程组同样被清理。
4. 用 `xcrun notarytool` 提交并等待成功结果，完成 `xcrun stapler staple` 后执行
   `spctl --assess --type execute --verbose=4`。

## 运行时约定

Release 主机只通过应用包中的 `Contents/Resources/CSIBackendLauncher` 启动
`Contents/Resources/backend/CSI.OpenBase.Backend`，并设置：

- `CSI_OPENBASE_HOME`
- `CSI_OPENBASE_SESSION_HOME`
- `CSI_OPENBASE_HOST=127.0.0.1`
- `CSI_OPENBASE_PORT`
- `CSI_OPENBASE_DESKTOP_TOKEN`
- `CSI_OPENBASE_INSTANCE_NONCE`
- `CSI_OPENBASE_SESSION_SECRET`
- `CSI_OPENBASE_PARENT_PID`

工作数据写入用户选择的目录；浏览器登录会话按工作目录隔离，保存在
`~/Library/Application Support/CSI OpenBase/sessions/`。桌面日志位于
`~/Library/Logs/CSI OpenBase/`，日志不会记录 token 或 nonce。

## License

CSI OpenBase is licensed under the Apache License 2.0.

This license applies only to the code and documentation contained in this
repository.

CSI proprietary scoring models, weights, industry benchmarks, commercial report
logic, CSI Core components and services provided through aicsi.cn are not included
in this repository and are not licensed under Apache-2.0.
