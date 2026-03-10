# astrbot_plugin_helpmenu

为 AstrBot 自动生成可翻页的帮助菜单，支持文本输出与图片输出。

## 功能概览

- 支持两种文档获取模式：
  - `metadata`（默认）：从本地插件元数据和处理器注册表生成命令文档，不依赖 Dashboard 登录。
  - `api`：通过 Dashboard 的 `/api/commands` 接口拉取命令文档。
- 支持两种输出模式：
  - `text`：文本分页输出。
  - `image`：通过 HTML 模板渲染帮助菜单图片。
- 支持图片模板：`classic`、`frost`、`compact`、`ember_industrial`（暖色现代工业风）。
- 支持会话分页浏览：`/helpMenu`、`/helpMenu <页码>`、`/helpMenu next`、`/helpMenu prev`。
- 支持管理员刷新：`/updateHelpMenu`。
- 插件加载/卸载事件发生时会自动刷新帮助菜单缓存。
- 普通会话仅展示公开命令；管理员私聊会额外展示管理员命令。

## 配置项

- `fetch_mode`：命令文档获取模式，`metadata` 或 `api`，默认 `metadata`。
- `output_mode`：帮助菜单输出模式，`text` 或 `image`，默认 `text`。
- `image_template`：图片模板风格，`classic` / `frost` / `compact` / `ember_industrial`，默认 `classic`。
- 图片渲染参数已内置为经过测试的默认值（PNG、整页截图、禁用动画、隐藏光标、CSS 缩放），无需额外配置。
- `admin_name`：Dashboard 登录用户名（仅 `api` 模式需要）。
- `admin_password`：Dashboard 登录密码（仅 `api` 模式需要）。
- `ASTRHost`：Dashboard 地址，默认 `http://127.0.0.1:6185`（仅 `api` 模式需要）。
- `auto_clear_config_after_run`：刷新成功后自动清空配置中的账号密码，默认 `false`。

## 指令说明

- `/helpMenu`
  - 显示当前会话最近查看页（首次默认第 1 页）。
- `/helpMenu <页码>`
  - 跳转到指定页，例如 `/helpMenu 3`。
- `/helpMenu next`
  - 查看下一页。
- `/helpMenu prev`
  - 查看上一页。
- `/updateHelpMenu`
  - 立即刷新帮助菜单缓存（仅管理员）。

## 行为说明

- 文档头部会显示：
  - 当前页与总页数
  - 命令总数
  - 数据来源（元数据模式/API 模式）
  - 文档更新时间
- `api` 模式下若未配置可用账号密码，刷新会被跳过并给出提示。
- 图片渲染失败时会自动回退到文本输出。

## 兼容与限制

- 命令展示依赖 AstrBot 的插件元数据、事件过滤器与命令注册信息。
- `api` 模式依赖 Dashboard 可访问且鉴权成功。
- 图片输出依赖运行环境支持 `html_render`。

## 免责声明

当使用 `api` 模式时，插件会使用您配置的 Dashboard 账号密码登录并拉取命令信息。Token 与凭据仅保存在运行内存中用于会话内续登；若启用 `auto_clear_config_after_run`，刷新成功后会清空配置中的账号密码。

请妥善保管账号信息，确保 Dashboard 部署在可信网络环境中。

## 参考

本项目借鉴、引用了以下项目中的部分代码：

- [AstrBot](https://github.com/AstrBot/AstrBot)
- [astrbot 帮助插件](https://github.com/tinkerbellqwq/astrbot_plugin_help)
- [插件变更提醒](https://github.com/PyuraMazo/astrbot_plugin_alteration_notifier)
