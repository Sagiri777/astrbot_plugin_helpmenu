# astrbot_plugin_helpmenu

为 AstrBot 自动生成文字帮助菜单。

插件会登录 AstrBot Dashboard，拉取 `/api/commands`，筛选可用命令并生成帮助文档，用户可通过 `helpMenu` 翻页查看。

## 功能特性

- 加载刷新插件时自动拉取并生成帮助文档
- 支持手动刷新：`/updateHelpMenu`（一般用不上）
- 支持分页查看：`/helpMenu`、`/helpMenu {pageNum}`、`/helpMenu next`、`/helpMenu prev`
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
- `ASTRHost`：ASTR 后台地址（默认：`http://127.0.0.1:6185`）

> 未配置 `ASTRHost` 时，默认请求地址为 `http://127.0.0.1:6185`。

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
- 若 Dashboard 不在本机默认端口，请将 `ASTRHost` 改为你的实际地址

## 兼容与限制

- 帮助文档为纯文本消息，仅测试了aiocqhttp平台
- 文档内容来自 Dashboard 的命令接口返回结果

# 免责声明
本插件会登录 ASTRBot 的 Dashboard，本插件仅会使用您的账号和密码进行登录并获取命令信息，除插件Debug模式外不会以任何形式保存您的账号和密码或凭证，将账号和密码填写进插件配置视为您已阅读本文档并同意本插件使用您的相关信息且不会要求本插件作者承担因此造成的问题的任何责任。

请注意账号安全，勿将此插件用于非个人用途

## 安全说明
- 本插件仅在运行时临时使用您的管理员凭据进行API调用
- 所有网络通信均通过HTTPS/TLS加密传输(如适用)
- 插件不会记录、存储或传输您的认证凭据到任何第三方服务
- 请确保您的AstrBot Dashboard部署在安全的网络环境中

## 用户责任
- 用户需确保自己拥有Dashboard账户的合法访问权限
- 用户应定期更换密码以保障安全性
- 如无需再使用本插件的Debug模式，请及时关闭Debug模式并确保个人信息已被移除

## 数据处理
- 插件仅获取命令列表信息，不涉及敏感聊天记录或其他用户数据
- 所有获取的信息仅用于本地构建帮助菜单，不会上传至其他服务器
- 本插件设置了非Debug模式下自动刷新并清空配置的功能，以保障用户信息安全
