# astrbot_plugin_helpmenu

为 AstrBot 自动生成「可翻页」的文字帮助菜单。

插件会登录 AstrBot Dashboard，拉取 `/api/commands`，筛选可用命令并生成帮助文档，用户可通过 `helpMenu` 像 `man` 一样翻页查看。

## 功能特性

- 首次加载插件时自动拉取并生成帮助文档
- 支持手动刷新：`/updateHelpMenu`
- 支持分页查看：`/helpMenu`、`/helpMenu 2`、`/helpMenu next`、`/helpMenu prev`
- 按插件分组展示命令，包含：
  - `effective_command`
  - `description`
  - `aliases`（若存在）
- 自动过滤不可用命令：
  - 仅保留 `type` 为 `command` 或 `sub_command`
  - 跳过 `enabled = false`
  - 跳过 `permission != everyone`

## 配置项

在插件配置中填写：

- `admin_name`：AstrBot Dashboard 登录用户名
- `admin_password`：AstrBot Dashboard 登录密码

> 默认请求地址为 `http://127.0.0.1:6185`。

## 指令说明

- `/helpMenu`
  - 显示当前会话最近查看页（首次默认第 1 页）
- `/helpMenu <页码>`
  - 跳转到指定页，如 `/helpMenu 3`
- `/helpMenu next`
  - 下一页
- `/helpMenu prev`
  - 上一页
- `/updateHelpMenu`
  - 重新登录并拉取最新命令，重建帮助文档

## 使用建议

- 新装/卸载插件、修改命令权限后，执行一次 `/updateHelpMenu`
- 若提示登录失败，请检查 `admin_name`、`admin_password` 是否正确
- 若 Dashboard 不在本机默认端口，请确认插件代码中的 `dashboard_base_url` 配置读取逻辑与你的部署一致

## 兼容与限制

- 帮助文档为纯文本消息，适配多数平台
- 文档内容来自 Dashboard 的命令接口返回结果
- 当前只展示 `everyone` 权限命令（面向普通用户）
