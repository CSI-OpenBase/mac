# CSI OpenBase 本地运行目录

CSI OpenBase 源码版默认把一个创作者账号的全部文件型运行状态放在本目录中。桌面版会改用用户在窗口中选择的工作目录。

```text
var/
  local/
    exports/<日期时间>/        创作者中心表格和导出清单
    works/discovery/           个人主页发现批次
    works/videos/douyin/       逐视频档案、快照、封面和手动评论批次
    openbase.sqlite3           本地索引和即时任务状态
  sessions/<工作目录哈希>/    浏览器登录态和 Cookie
```

除本说明文件外，`var` 下的内容都由 `.gitignore` 排除，不会进入正常的 Git 提交。Git 忽略不是加密或访问控制；这里仍可能包含账号数据和登录 Cookie，不要使用强制添加提交这些文件，也不要把带有 `var` 数据的项目目录制作成公开压缩包。

备份到私人存储时可以复制整个 `var`。恢复时应保持原有目录结构，并在后台和采集任务停止后操作；Chromium Cookie 可能绑定操作系统用户，换电脑后仍可能需要重新登录。

兼容用的旧多工作区管理后台仍使用 `var/active-workspace`、`var/workspaces/<slug>` 和 MySQL；它不是本地归档模式的运行依赖。
