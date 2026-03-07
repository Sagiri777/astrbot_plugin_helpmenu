import asyncio
import base64
import hashlib
import json
import re
from collections import OrderedDict, defaultdict
from dataclasses import dataclass
from datetime import datetime

import aiohttp

from astrbot.api import AstrBotConfig, logger
from astrbot.api.event import AstrMessageEvent, filter
from astrbot.api.star import Context, Star, register


@dataclass(slots=True)
class CommandDocItem:
    plugin_name: str
    command: str
    description: str
    aliases: list[str]


@dataclass(slots=True, frozen=True)
class HelpCacheSnapshot:
    pages: tuple[str, ...]
    total_items: int
    last_update: str


@register("helpmenu", "Sagiri777", "自动生成可翻页的指令帮助菜单", "0.1.0")
class MyPlugin(Star):
    _SESSION_PAGE_CACHE_MAX_SIZE = 1024
    _MAX_SESSION_KEY_LEN = 128
    _EXCLUDED_PLUGINS = {"builtin_commands"}

    def __init__(self, context: Context, config: AstrBotConfig):
        super().__init__(context)
        self.config = config
        self._refresh_lock = asyncio.Lock()
        self._session_page_lock = asyncio.Lock()
        self._http_session_lock = asyncio.Lock()
        self._help_cache = HelpCacheSnapshot(pages=tuple(), total_items=0, last_update="从未")
        self._session_page: OrderedDict[str, int] = OrderedDict()
        self._auth_token: str = ""
        self._token_expire_at: int = 0
        self._cached_admin_name: str = ""
        self._cached_admin_password: str = ""
        self._http_session: aiohttp.ClientSession | None = None

    def _is_debug_enabled(self) -> bool:
        return bool(self.config.get("debug", False))

    def _log(self, message: str) -> None:
        logger.info(f"[helpmenu] {message}")

    def _log_debug(self, message: str) -> None:
        if self._is_debug_enabled():
            logger.info(f"[helpmenu][debug] {message}")

    def _is_auto_clear_enabled(self) -> bool:
        return bool(self.config.get("auto_clear_config_after_run", False))

    def _capture_credentials_from_config(self) -> tuple[str, str]:
        admin_name = (self.config.get("admin_name") or "").strip()
        admin_password = (self.config.get("admin_password") or "").strip()
        if admin_name and admin_password:
            self._cached_admin_name = admin_name
            self._cached_admin_password = admin_password
        return self._cached_admin_name, self._cached_admin_password

    def _decode_token_expire_at(self, token: str) -> int:
        try:
            parts = token.split(".")
            if len(parts) != 3:
                return 0
            payload = parts[1]
            payload += "=" * (-len(payload) % 4)
            decoded = base64.urlsafe_b64decode(payload.encode("utf-8")).decode("utf-8")
            data = json.loads(decoded)
            exp = data.get("exp")
            if isinstance(exp, bool):
                return 0
            if isinstance(exp, (int, float)):
                return int(exp)
            if isinstance(exp, str):
                exp_text = exp.strip()
                if not exp_text:
                    return 0
                return int(exp_text)
            return 0
        except Exception:  # noqa: BLE001
            self._log_debug("Token 非 JWT 或缺少 exp 字段，将依赖 401 触发重登。")
            return 0

    def _is_token_expired(self) -> bool:
        if not self._auth_token:
            return True
        if self._token_expire_at <= 0:
            return False
        return int(datetime.now().timestamp()) >= self._token_expire_at - 30

    async def initialize(self):
        await self._get_http_session()
        ok, message = await self._refresh_help_cache()
        if ok:
            self._log(message)
        else:
            logger.warning(f"[helpmenu] {message}")

    async def _get_http_session(self) -> aiohttp.ClientSession:
        if self._http_session and not self._http_session.closed:
            return self._http_session

        async with self._http_session_lock:
            if self._http_session and not self._http_session.closed:
                return self._http_session
            self._http_session = aiohttp.ClientSession(trust_env=True)
            return self._http_session

    def _clear_sensitive_config_if_needed(self) -> None:
        if not self._is_auto_clear_enabled():
            self._log_debug("未启用运行后自动清空配置，跳过账号密码清空。")
            return

        try:
            self.config["admin_name"] = ""
            self.config["admin_password"] = ""
            self.config.save_config()
            self._log("刷新成功，已清空配置中的 admin_name/admin_password。")
        except Exception as exc:  # noqa: BLE001
            logger.warning(f"[helpmenu] 清空敏感配置失败：{exc}")

    def _build_login_password(self, raw_password: str) -> str:
        if re.fullmatch(r"[0-9a-fA-F]{32}", raw_password):
            self._log_debug("检测到配置密码已是 32 位 MD5，直接用于登录。")
            return raw_password.lower()
        md5_password = hashlib.md5(raw_password.encode("utf-8")).hexdigest()  # noqa: S324
        self._log_debug("已将配置密码转换为 MD5 后提交登录。")
        return md5_password

    def _get_base_url(self) -> str:
        return (
            self.config.get("ASTRHost")
            or self.config.get("dashboard_base_url")
            or "http://127.0.0.1:6185"
        ).rstrip("/")

    async def _login_and_get_token(self) -> str:
        admin_name, admin_password = self._capture_credentials_from_config()
        if not admin_name or not admin_password:
            raise ValueError(
                "插件配置缺少 admin_name 或 admin_password，请先填写。",
            )

        base_url = self._get_base_url()
        login_url = f"{base_url}/api/auth/login"
        payload = {
            "username": admin_name,
            "password": self._build_login_password(admin_password),
        }
        self._log_debug(f"登录地址: {login_url}")
        self._log_debug(f"登录用户名: {admin_name}")

        timeout = aiohttp.ClientTimeout(total=12)
        session = await self._get_http_session()
        async with session.post(login_url, json=payload, timeout=timeout) as response:
            data = await response.json(content_type=None)
            self._log_debug(f"登录状态码: {response.status}")
            self._log_debug(f"登录响应: {data}")

            if not isinstance(data, dict):
                raise ValueError(
                    f"登录接口返回格式异常: {type(data).__name__}",
                )

            status = str(data.get("status") or "").lower()
            message = str(data.get("message") or "").strip()
            if status != "ok":
                if "用户名或密码错误" in message:
                    raise ValueError("后台用户名或密码错误，请检查插件配置。")
                raise ValueError(f"登录失败: {message or '未知错误'}")

            data_node = data.get("data")
            if not isinstance(data_node, dict):
                raise ValueError(
                    f"登录响应中的 data 字段异常: {data_node}",
                )

            token = str(data_node.get("token") or "").strip()
            if not token:
                raise ValueError(
                    "登录成功但未获取到 token。",
                )
            self._auth_token = token
            self._token_expire_at = self._decode_token_expire_at(token)
            if self._token_expire_at > 0:
                self._log_debug(f"登录成功，Token 过期时间戳: {self._token_expire_at}")
            else:
                self._log_debug("登录成功，未解析到 Token 过期时间。")
            return token

    async def _fetch_command_items(self, token: str) -> list[dict]:
        base_url = self._get_base_url()
        commands_url = f"{base_url}/api/commands"
        headers = {"Authorization": f"Bearer {token}"}
        timeout = aiohttp.ClientTimeout(total=18)
        self._log_debug(f"命令列表地址: {commands_url}")
        session = await self._get_http_session()
        async with session.get(
            commands_url, headers=headers, timeout=timeout
        ) as response:
            if response.status == 401:
                raise PermissionError("token_unauthorized")
            data = await response.json(content_type=None)
            self._log_debug(f"命令列表状态码: {response.status}")
            if not isinstance(data, dict):
                raise ValueError(
                    f"命令接口返回格式异常: {type(data).__name__}",
                )

            status = str(data.get("status") or "").lower()
            message = str(data.get("message") or "").strip()
            if status != "ok":
                raise ValueError(f"获取命令列表失败: {message or '未知错误'}")

            data_node = data.get("data")
            if not isinstance(data_node, dict):
                raise ValueError(
                    f"命令接口中的 data 字段异常: {data_node}",
                )

            items = data_node.get("items", [])
            if not isinstance(items, list):
                raise ValueError(
                    f"命令接口中的 items 字段异常: {type(items).__name__}",
                )
            self._log_debug(f"命令总数(原始): {len(items)}")
            return items

    async def _get_or_refresh_token(self, force_login: bool = False) -> str:
        if not force_login and not self._is_token_expired():
            self._log_debug("复用内存中的 Token。")
            return self._auth_token
        self._log_debug("Token 不可用或已过期，尝试重新登录。")
        return await self._login_and_get_token()

    def _extract_allowed_items(self, raw_items: list[dict]) -> list[CommandDocItem]:
        collected: list[CommandDocItem] = []
        dedup: set[str] = set()

        def clean_text(value: str | None, default: str) -> str:
            text = re.sub(r"\s+", " ", (value or "").strip())
            if not text:
                return default
            if len(text) > 120:
                return f"{text[:117]}..."
            return text

        def walk(items: list[dict]) -> None:
            for item in items:
                if not isinstance(item, dict):
                    continue

                plugin_id = str(item.get("plugin") or "").strip()
                if plugin_id in self._EXCLUDED_PLUGINS:
                    continue

                item_type = item.get("type", "")
                enabled = item.get("enabled", False)
                permission = item.get("permission", "")

                if (
                    item_type in {"command", "sub_command"}
                    and enabled
                    and permission == "everyone"
                ):
                    command = clean_text(item.get("effective_command"), "")
                    if command:
                        plugin_name = clean_text(
                            item.get("plugin_display_name"), ""
                        ) or clean_text(item.get("plugin"), "未知插件")
                        description = clean_text(item.get("description"), "暂无说明。")
                        raw_aliases = item.get("aliases", [])
                        aliases: list[str] = []
                        if isinstance(raw_aliases, list):
                            for alias in raw_aliases:
                                if not isinstance(alias, str):
                                    continue
                                alias_text = clean_text(alias, "")
                                if alias_text:
                                    aliases.append(alias_text)
                        dedup_key = f"{plugin_name}|{command}"
                        if dedup_key not in dedup:
                            dedup.add(dedup_key)
                            collected.append(
                                CommandDocItem(
                                    plugin_name=plugin_name,
                                    command=command,
                                    description=description,
                                    aliases=aliases,
                                ),
                            )

                sub_commands = item.get("sub_commands", [])
                if isinstance(sub_commands, list) and sub_commands:
                    walk(sub_commands)

        walk(raw_items)
        collected.sort(key=lambda x: (x.plugin_name.lower(), x.command.lower()))
        return collected

    def _build_pages(
        self,
        items: list[CommandDocItem],
        total_items: int,
        last_update: str,
        page_size: int = 12,
    ) -> list[str]:
        if page_size <= 0:
            raise ValueError("page_size must be greater than 0")
        if not items:
            return ["当前暂无可展示命令，请先执行 /updateHelpMenu 刷新。"]

        pages: list[str] = []
        total_pages = (len(items) + page_size - 1) // page_size
        for page_index in range(total_pages):
            start = page_index * page_size
            current_items = items[start : start + page_size]
            grouped: dict[str, list[CommandDocItem]] = defaultdict(list)
            for item in current_items:
                grouped[item.plugin_name].append(item)

            lines = [
                "指令帮助菜单",
                (
                    f"第 {page_index + 1}/{total_pages} 页 | "
                    f"命令数: {total_items} | "
                    f"文档更新时间: {last_update}"
                ),
                "用法: /helpMenu <页码|next|prev> | /updateHelpMenu（仅限管理员）",
                "",
            ]

            for plugin_name in sorted(grouped.keys(), key=str.lower):
                lines.append(f"[{plugin_name}]")
                for entry in grouped[plugin_name]:
                    lines.append(f"/{entry.command} - {entry.description}")
                    if entry.aliases:
                        lines.append(f"  别名: {', '.join(entry.aliases)}")
                lines.append("")

            pages.append("\n".join(lines).strip())
        return pages

    async def _refresh_help_cache(self) -> tuple[bool, str]:
        async with self._refresh_lock:
            try:
                self._log("开始刷新帮助菜单缓存...")
                token = await self._get_or_refresh_token()
                try:
                    raw_items = await self._fetch_command_items(token)
                except PermissionError:
                    self._log_debug("命令接口返回 401，尝试用内存凭据重新登录后重试。")
                    token = await self._get_or_refresh_token(force_login=True)
                    raw_items = await self._fetch_command_items(token)
                parsed_items = self._extract_allowed_items(raw_items)
                self._log_debug(f"命令总数(过滤后): {len(parsed_items)}")

                total_items = len(parsed_items)
                last_update = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
                help_pages = self._build_pages(parsed_items, total_items, last_update)
                self._help_cache = HelpCacheSnapshot(
                    pages=tuple(help_pages),
                    total_items=total_items,
                    last_update=last_update,
                )
                async with self._session_page_lock:
                    self._session_page.clear()
                self._clear_sensitive_config_if_needed()
                return (
                    True,
                    f"帮助菜单刷新成功，共 {total_items} 条可用命令。",
                )
            except asyncio.TimeoutError:
                return False, "帮助菜单刷新失败：请求服务器超时，请稍后重试。"
            except aiohttp.ClientConnectionError as exc:
                return False, f"帮助菜单刷新失败：无法连接服务器（{exc}）。"
            except aiohttp.ClientError as exc:
                return False, f"帮助菜单刷新失败：网络请求异常（{exc}）。"
            except json.JSONDecodeError as exc:
                return False, f"帮助菜单刷新失败：服务器返回了无效 JSON（{exc.msg}）。"
            except PermissionError:
                return False, "帮助菜单刷新失败：登录状态失效，请检查账号配置后重试。"
            except ValueError as exc:
                return False, f"帮助菜单刷新失败：{exc}"
            except Exception as exc:  # noqa: BLE001
                if self._is_debug_enabled():
                    logger.exception("[helpmenu] Debug 模式下刷新失败。")
                return False, f"帮助菜单刷新失败：未知错误（{exc}）。"

    def _parse_help_arg(self, message: str) -> str:
        normalized = re.sub(r"\s+", " ", (message or "").strip())
        parts = normalized.split(" ", 1)
        if len(parts) < 2:
            return ""
        return parts[1].strip().lower()

    def _get_session_page(self, session_id: str) -> int:
        session_key = self._normalize_session_key(session_id)
        page = self._session_page.get(session_key)
        if page is None:
            return 1
        self._session_page.move_to_end(session_key)
        return page

    def _set_session_page(self, session_id: str, page: int) -> None:
        session_key = self._normalize_session_key(session_id)
        self._session_page[session_key] = page
        self._session_page.move_to_end(session_key)
        if len(self._session_page) <= self._SESSION_PAGE_CACHE_MAX_SIZE:
            return

        evicted_session, _ = self._session_page.popitem(last=False)
        evicted_session_safe = (
            f"{evicted_session[:16]}..."
            if len(evicted_session) > 16
            else evicted_session
        )
        self._log_debug(
            f"session page cache exceeded {self._SESSION_PAGE_CACHE_MAX_SIZE}, "
            f"evicted session: {evicted_session_safe}",
        )

    def _normalize_session_key(self, session_id: str) -> str:
        session_key = (session_id or "").strip()
        if not session_key:
            return "anonymous"
        if len(session_key) <= self._MAX_SESSION_KEY_LEN:
            return session_key
        digest = hashlib.sha256(session_key.encode("utf-8")).hexdigest()
        return f"sid:{digest}"

    def _resolve_page(self, arg: str, session_id: str) -> tuple[int, str]:
        total_pages = max(1, len(self._help_cache.pages))
        if not arg:
            page = self._get_session_page(session_id)
            return min(max(page, 1), total_pages), ""

        if arg in {"next", "n", "下页", "下一页"}:
            page = self._get_session_page(session_id) + 1
            return min(page, total_pages), ""
        if arg in {"prev", "p", "上页", "上一页"}:
            page = self._get_session_page(session_id) - 1
            return max(page, 1), ""
        if arg.isdigit():
            page = int(arg)
            if page < 1:
                page = 1
            if page > total_pages:
                page = total_pages
            return page, ""

        return 1, f"页码参数无效: {arg}，已显示第 1 页。\n\n"

    async def _resolve_and_set_session_page(
        self, arg: str, session_id: str
    ) -> tuple[int, str]:
        async with self._session_page_lock:
            page, warning = self._resolve_page(arg, session_id)
            self._set_session_page(session_id, page)
            return page, warning

    @filter.permission_type(filter.PermissionType.ADMIN)
    @filter.command("updateHelpMenu")
    async def update_helpmenu(self, event: AstrMessageEvent):
        """刷新已生成的帮助菜单文档。"""
        ok, message = await self._refresh_help_cache()
        if ok:
            yield event.plain_result(message)
            return
        yield event.plain_result(
            f"{message}\n请检查 Dashboard 地址、用户名和密码是否正确。"
        )

    @filter.command("helpMenu")
    async def helpmenu(self, event: AstrMessageEvent):
        """展示支持翻页的帮助菜单。"""
        if not self._help_cache.pages:
            ok, message = await self._refresh_help_cache()
            if not ok:
                yield event.plain_result(
                    f"{message}\n修正配置后请执行 /updateHelpMenu 重新刷新。",
                )
                return

        session_id = event.get_session_id()
        arg = self._parse_help_arg(event.message_str)
        page, warning = await self._resolve_and_set_session_page(arg, session_id)
        snapshot = self._help_cache
        text = snapshot.pages[page - 1]
        if warning:
            text = f"{warning}{text}"
        yield event.plain_result(text)

    async def terminate(self):
        async with self._http_session_lock:
            if self._http_session and not self._http_session.closed:
                await self._http_session.close()
            self._http_session = None
        async with self._session_page_lock:
            self._session_page.clear()
        self._auth_token = ""
        self._token_expire_at = 0
