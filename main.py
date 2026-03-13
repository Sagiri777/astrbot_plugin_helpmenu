import asyncio
import base64
import hashlib
import json
import re
from collections import OrderedDict, defaultdict
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from pathlib import Path

import aiohttp

from astrbot.api import AstrBotConfig, logger
from astrbot.api.event import AstrMessageEvent, filter
from astrbot.api.star import Context, Star, register
from astrbot.core.star.filter.command import CommandFilter
from astrbot.core.star.filter.command_group import CommandGroupFilter
from astrbot.core.star.filter.permission import PermissionType, PermissionTypeFilter
from astrbot.core.star.star_handler import star_handlers_registry


@dataclass(slots=True)
class CommandDocItem:
    plugin_name: str
    command: str
    description: str
    aliases: list[str]
    permission: str = "everyone"


@dataclass(slots=True, frozen=True)
class HelpCacheSnapshot:
    pages: tuple[str, ...]
    image_pages: tuple[tuple[dict[str, object], ...], ...]
    total_items: int
    last_update: str
    source_mode: str


class HelpMenuError(Exception):
    def __init__(self, stage: str, message: str):
        super().__init__(message)
        self.stage = stage
        self.message = message


class HttpStatusError(HelpMenuError):
    def __init__(self, stage: str, status: int, detail: str):
        super().__init__(stage, f"HTTP {status}: {detail}")
        self.status = status


@register("helpmenu", "Sagiri777", "自动生成可翻页的指令帮助菜单", "1.0.0")
class MyPlugin(Star):
    _SESSION_PAGE_CACHE_MAX_SIZE = 1024
    _MAX_SESSION_KEY_LEN = 128
    _EXCLUDED_PLUGINS = {"builtin_commands"}
    _MODE_METADATA = "metadata"
    _MODE_API = "api"
    _OUTPUT_TEXT = "text"
    _OUTPUT_IMAGE = "image"
    _DEFAULT_IMAGE_TEMPLATE = "classic"
    _DEFAULT_IMAGE_RENDER_OPTIONS = {
        "type": "png",
        "full_page": True,
        "omit_background": True,
        "animations": "disabled",
        "caret": "hide",
        "scale": "css",
    }

    def __init__(self, context: Context, config: AstrBotConfig):
        super().__init__(context)
        self.config = config
        self._refresh_lock = asyncio.Lock()
        self._session_page_lock = asyncio.Lock()
        self._http_session_lock = asyncio.Lock()
        self._help_cache = HelpCacheSnapshot(
            pages=(),
            image_pages=(),
            total_items=0,
            last_update="从未",
            source_mode=self._MODE_METADATA,
        )
        self._help_cache_admin_private = HelpCacheSnapshot(
            pages=(),
            image_pages=(),
            total_items=0,
            last_update="从未",
            source_mode=self._MODE_METADATA,
        )
        self._session_page: OrderedDict[str, int] = OrderedDict()
        self._auth_token: str = ""
        self._token_expire_at: int = 0
        self._cached_admin_name: str = ""
        self._cached_admin_password: str = ""
        self._http_session: aiohttp.ClientSession | None = None
        self._plugin_change_pending = False
        self._plugin_refresh_task: asyncio.Task | None = None

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

    def _get_fetch_mode(self) -> str:
        mode = str(self.config.get("fetch_mode") or self._MODE_METADATA).strip().lower()
        if mode in {self._MODE_METADATA, self._MODE_API}:
            return mode
        logger.warning(
            f"[helpmenu] 未知 fetch_mode={mode}，将回退为 {self._MODE_METADATA} 模式。"
        )
        return self._MODE_METADATA

    def _mode_display_name(self, mode: str) -> str:
        if mode == self._MODE_API:
            return "API 模式"
        return "元数据模式"

    def _has_api_credentials(self) -> bool:
        admin_name, admin_password = self._capture_credentials_from_config()
        return bool(admin_name and admin_password)

    def _get_output_mode(self) -> str:
        mode = str(self.config.get("output_mode") or self._OUTPUT_TEXT).strip().lower()
        self._log_debug(f"配置的输出模式: {mode}")

        if mode in {self._OUTPUT_TEXT, self._OUTPUT_IMAGE}:
            self._log_debug(f"最终输出模式: {mode}")
            return mode
        logger.warning(
            f"[helpmenu] 未知 output_mode={mode}，将回退为 {self._OUTPUT_TEXT} 模式。"
        )
        self._log_debug(f"回退到默认输出模式: {self._OUTPUT_TEXT}")
        return self._OUTPUT_TEXT

    def _is_dark_time(self) -> bool:
        start_time_str = str(self.config.get("dark_time_start", "18:00")).strip()
        end_time_str = str(self.config.get("dark_time_end", "06:00")).strip()

        try:
            start_hour, start_minute = map(int, start_time_str.split(":"))
            end_hour, end_minute = map(int, end_time_str.split(":"))
        except ValueError:
            logger.warning("[helpmenu] 深色模式时间格式有误，默认不启用。")
            return False

        # Get current Beijing Time (UTC+8)
        tz_bj = timezone(timedelta(hours=8))
        now = datetime.now(tz_bj)
        current_minutes = now.hour * 60 + now.minute
        start_minutes = start_hour * 60 + start_minute
        end_minutes = end_hour * 60 + end_minute

        if start_minutes < end_minutes:
            return start_minutes <= current_minutes <= end_minutes
        else:  # Over midnight
            return current_minutes >= start_minutes or current_minutes <= end_minutes

    def _get_available_templates(self) -> list[str]:
        templates_dir = Path(__file__).parent / "templates"
        if not templates_dir.exists():
            return []
        return [f.stem for f in templates_dir.glob("*.html")]

    def _get_image_template_name(self) -> str:
        light_template = (
            str(
                self.config.get("light_template")
                or self.config.get("image_template")
                or self._DEFAULT_IMAGE_TEMPLATE
            )
            .strip()
            .lower()
        )

        available = self._get_available_templates()
        self._log_debug(f"可用的模板列表: {available}")
        self._log_debug(f"配置的浅色模板: {light_template}")

        template_name = light_template
        if light_template not in available:
            logger.warning(
                f"[helpmenu] 未知 light_template={light_template}，将回退为 {self._DEFAULT_IMAGE_TEMPLATE}。"
            )
            template_name = self._DEFAULT_IMAGE_TEMPLATE

        if self._is_dark_time():
            dark_template = str(self.config.get("dark_template") or "").strip().lower()
            self._log_debug("当前为深色模式时间段")
            self._log_debug(
                f"配置的深色模板: {dark_template if dark_template else '未配置'}"
            )

            if not dark_template:
                dark_template = f"{template_name}_dark"

            if dark_template in available:
                template_name = dark_template
                self._log_debug(f"使用深色模板: {template_name}")
            else:
                logger.warning(
                    f"[helpmenu] 深色模板 {dark_template} 不存在，降级使用浅色模板。"
                )
                self._log_debug(
                    f"深色模板 {dark_template} 不在可用列表中，继续使用: {template_name}"
                )

        self._log_debug(f"最终使用的模板名称: {template_name}")
        return template_name

    def _get_image_template(self) -> str:
        template_name = self._get_image_template_name()
        template_file = Path(__file__).parent / "templates" / f"{template_name}.html"
        self._log_debug(f"模板文件路径: {template_file}")
        self._log_debug(f"模板文件是否存在: {template_file.exists()}")

        try:
            content = template_file.read_text(encoding="utf-8")
            self._log_debug(f"成功读取模板文件，内容长度: {len(content)} 字符")
            return content
        except Exception as e:
            logger.error(f"[helpmenu] 读取模板文件 {template_file} 失败: {e}")
            self._log_debug(f"模板文件读取异常: {type(e).__name__}: {e}")
            return "模板读取失败"

    async def _render_help_page_as_image(
        self,
        cards: tuple[dict[str, object], ...],
        warning: str,
        page: int,
        total_pages: int,
        snapshot: HelpCacheSnapshot,
    ) -> str:
        data = {
            "subtitle": (
                f"第 {page}/{total_pages} 页 | 命令数: {snapshot.total_items} | "
                f"来源: {self._mode_display_name(snapshot.source_mode)} | "
                f"文档更新时间: {snapshot.last_update}"
            ),
            "warning": warning.strip(),
            "cards": cards,
        }

        # Debug logging for image rendering
        self._log_debug(f"开始渲染帮助菜单图片: 第 {page}/{total_pages} 页")
        self._log_debug(f"卡片数量: {len(cards)}")
        self._log_debug(
            f"渲染选项: {json.dumps(self._DEFAULT_IMAGE_RENDER_OPTIONS, ensure_ascii=False)}"
        )

        template_name = self._get_image_template_name()
        self._log_debug(f"使用的模板: {template_name}")

        try:
            template_content = self._get_image_template()
            self._log_debug(f"模板内容长度: {len(template_content)} 字符")

            result = await self.html_render(
                template_content,
                data,
                options=self._DEFAULT_IMAGE_RENDER_OPTIONS,
            )

            self._log_debug(f"图片渲染成功, 返回类型: {type(result).__name__}")
            if isinstance(result, str):
                self._log_debug(f"图片URL/路径长度: {len(result)} 字符")
            return result
        except Exception as exc:
            self._log_debug(f"图片渲染失败: {type(exc).__name__}: {exc}")
            self._log_debug(f"渲染数据详情 - subtitle: {data.get('subtitle')}")
            self._log_debug(f"渲染数据详情 - warning: {data.get('warning')}")
            self._log_debug(f"渲染数据详情 - cards 数量: {len(cards)}")
            raise

    def _extract_arg_lines(self, description: str) -> tuple[str, list[dict[str, str]]]:
        matches = list(
            re.finditer(
                r"(Arg[\w\-\u4e00-\u9fa5]*)\s*[:：]\s*([^,，;；。]+)",
                description,
                flags=re.IGNORECASE,
            )
        )
        args: list[dict[str, str]] = []
        for match in matches:
            args.append(
                {
                    "name": match.group(1).strip(),
                    "detail": match.group(2).strip(),
                }
            )
        cleaned = re.sub(
            r"(Arg[\w\-\u4e00-\u9fa5]*)\s*[:：]\s*([^,，;；。]+)",
            "",
            description,
            flags=re.IGNORECASE,
        )
        cleaned = re.sub(r"[，,;；]+", "，", cleaned).strip("，,;；。 ")
        return (cleaned or description), args

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
        # Log default image render options on initialization
        self._log_debug(
            f"默认图片渲染选项: {json.dumps(self._DEFAULT_IMAGE_RENDER_OPTIONS, ensure_ascii=False)}"
        )

        if self._get_fetch_mode() == self._MODE_API:
            await self._get_http_session()
        ok, message = await self._refresh_help_cache(force=True)
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
            self._http_session = aiohttp.ClientSession(trust_env=False)
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
        md5_password = hashlib.md5(raw_password.encode("utf-8")).hexdigest()  # noqa: S324
        self._log_debug("已将配置密码转换为 MD5 后提交登录。")
        return md5_password

    def _get_base_url(self) -> str:
        return (
            self.config.get("ASTRHost")
            or self.config.get("dashboard_base_url")
            or "http://127.0.0.1:6185"
        ).rstrip("/")

    def _build_safe_login_response_log(self, data: object) -> str:
        if not isinstance(data, dict):
            return f"响应类型: {type(data).__name__}"

        status = str(data.get("status") or "").strip()
        message = str(data.get("message") or "").strip()
        data_node = data.get("data")
        data_node_type = type(data_node).__name__
        data_node_keys: list[str] = []
        if isinstance(data_node, dict):
            data_node_keys = sorted(data_node.keys())

        safe_payload = {
            "status": status,
            "message": message,
            "data_type": data_node_type,
            "data_keys": data_node_keys,
        }
        return json.dumps(safe_payload, ensure_ascii=False)

    async def _read_json_response(
        self, response: aiohttp.ClientResponse, stage: str
    ) -> object:
        try:
            return await response.json(content_type=None)
        except json.JSONDecodeError as exc:
            body_preview = (await response.text()).strip().replace("\n", " ")[:200]
            raise ValueError(
                f"{stage}返回了无效 JSON（HTTP {response.status}，响应片段: {body_preview or '空'}）"
            ) from exc

    def _raise_for_http_status(
        self, response: aiohttp.ClientResponse, stage: str
    ) -> None:
        if 200 <= response.status < 300:
            return
        if stage == "登录" and response.status in {401, 403}:
            raise PermissionError("login_unauthorized")
        if stage == "命令列表" and response.status == 401:
            raise PermissionError("token_unauthorized")
        raise HttpStatusError(stage, response.status, "服务返回非 2xx 状态")

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

        timeout = aiohttp.ClientTimeout(total=12)
        session = await self._get_http_session()
        async with session.post(login_url, json=payload, timeout=timeout) as response:
            self._log_debug(f"登录状态码: {response.status}")
            self._raise_for_http_status(response, "登录")
            data = await self._read_json_response(response, "登录接口")
            self._log_debug(
                f"登录响应(脱敏): {self._build_safe_login_response_log(data)}"
            )

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
            self._log_debug(f"命令列表状态码: {response.status}")
            self._raise_for_http_status(response, "命令列表")
            data = await self._read_json_response(response, "命令接口")
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

    def _can_show_command(
        self, permission: str, include_admin_commands: bool = False
    ) -> bool:
        normalized = (permission or "everyone").strip().lower()
        if normalized in {"", "everyone", "member"}:
            return True
        if include_admin_commands and normalized == "admin":
            return True
        return False

    def _extract_allowed_items(
        self, raw_items: list[dict], include_admin_commands: bool = False
    ) -> list[CommandDocItem]:
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
                permission = str(item.get("permission") or "").strip().lower()

                if (
                    item_type in {"command", "sub_command"}
                    and enabled
                    and self._can_show_command(permission, include_admin_commands)
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
                                    permission=permission or "everyone",
                                ),
                            )

                sub_commands = item.get("sub_commands", [])
                if isinstance(sub_commands, list) and sub_commands:
                    walk(sub_commands)

        walk(raw_items)
        collected.sort(key=lambda x: (x.plugin_name.lower(), x.command.lower()))
        return collected

    def _collect_items_from_metadata(
        self, include_admin_commands: bool = False
    ) -> list[CommandDocItem]:
        collected: list[CommandDocItem] = []
        dedup: set[str] = set()
        # Type ignore: context is actually Context instance with get_all_stars method
        all_stars_metadata = [
            star
            for star in self.context.get_all_stars()
            if star.activated  # type: ignore[attr-defined]
        ]
        if not all_stars_metadata:
            return collected

        handlers_by_module: defaultdict[str, list] = defaultdict(list)
        for handler in star_handlers_registry:
            module_path = getattr(handler, "handler_module_path", None)
            event_filters = getattr(handler, "event_filters", None)
            if not isinstance(module_path, str) or not isinstance(event_filters, list):
                continue
            handlers_by_module[module_path].append(handler)

        for star in all_stars_metadata:
            plugin_id = str(getattr(star, "name", "") or "").strip()
            module_path = str(getattr(star, "module_path", "") or "").strip()
            if not plugin_id or not module_path or plugin_id in self._EXCLUDED_PLUGINS:
                continue

            plugin_name = (
                str(getattr(star, "display_name", "") or "").strip()
                or plugin_id
                or "未知插件"
            )

            for handler in handlers_by_module.get(module_path, []):
                handler_desc = getattr(handler, "desc", "")
                event_filters = getattr(handler, "event_filters", [])
                if not isinstance(event_filters, list):
                    continue

                description = (
                    re.sub(r"\s+", " ", str(handler_desc or "").strip()) or "暂无说明。"
                )
                permission = "everyone"
                for event_filter in event_filters:
                    if not isinstance(event_filter, PermissionTypeFilter):
                        continue
                    if event_filter.permission_type == PermissionType.ADMIN:
                        permission = "admin"
                    else:
                        permission = "member"
                    break

                if not self._can_show_command(permission, include_admin_commands):
                    continue

                for event_filter in event_filters:
                    command = ""
                    aliases: list[str] = []

                    if isinstance(event_filter, CommandFilter):
                        full_names = [
                            re.sub(r"\s+", " ", name.strip())
                            for name in event_filter.get_complete_command_names()
                            if isinstance(name, str) and name.strip()
                        ]
                        if full_names:
                            command = full_names[0]
                            aliases = full_names[1:]
                    elif isinstance(event_filter, CommandGroupFilter):
                        full_names = [
                            re.sub(r"\s+", " ", name.strip())
                            for name in event_filter.get_complete_command_names()
                            if isinstance(name, str) and name.strip()
                        ]
                        if full_names:
                            command = full_names[0]
                            aliases = full_names[1:]

                    if not command:
                        continue

                    dedup_key = f"{plugin_name}|{command}"
                    if dedup_key in dedup:
                        continue
                    dedup.add(dedup_key)
                    collected.append(
                        CommandDocItem(
                            plugin_name=plugin_name,
                            command=command,
                            description=description,
                            aliases=aliases,
                            permission=permission,
                        )
                    )

        collected.sort(
            key=lambda item: (item.plugin_name.lower(), item.command.lower())
        )
        return collected

    def _build_pages(
        self,
        items: list[CommandDocItem],
        total_items: int,
        last_update: str,
        source_mode: str,
        page_size: int = 32,
    ) -> list[str]:
        if page_size <= 0:
            raise ValueError("page_size must be greater than 0")
        if not items:
            return ["当前暂无可展示命令，请先执行 /updateHelpMenu 刷新。"]

        grouped: dict[str, list[CommandDocItem]] = defaultdict(list)
        for item in items:
            grouped[item.plugin_name].append(item)

        page_blocks: list[list[str]] = []
        current_page: list[str] = []
        current_units = 0
        for plugin_name in sorted(grouped.keys(), key=str.lower):
            plugin_items = grouped[plugin_name]
            pointer = 0
            is_continued = False
            while pointer < len(plugin_items):
                if current_units >= page_size:
                    page_blocks.append(current_page)
                    current_page = []
                    current_units = 0

                title = f"[{plugin_name}{'(续)' if is_continued else ''}]"
                current_page.append(title)
                current_units += 1

                while pointer < len(plugin_items):
                    entry = plugin_items[pointer]
                    _, args = self._extract_arg_lines(entry.description)
                    estimated_units = 1 + (1 if entry.aliases else 0) + len(args)
                    if (
                        current_units + estimated_units > page_size
                        and current_units > 1
                    ):
                        break
                    current_page.append(f"/{entry.command} - {entry.description}")
                    current_units += 1
                    if entry.aliases:
                        current_page.append(f"  别名: {', '.join(entry.aliases)}")
                        current_units += 1
                    pointer += 1

                current_page.append("")
                current_units += 1
                is_continued = pointer < len(plugin_items)

        if current_page:
            page_blocks.append(current_page)

        pages: list[str] = []
        total_pages = len(page_blocks)
        for page_index, block_lines in enumerate(page_blocks, start=1):
            lines = [
                "指令帮助菜单",
                (
                    f"第 {page_index}/{total_pages} 页 | "
                    f"命令数: {total_items} | "
                    f"来源: {self._mode_display_name(source_mode)} | "
                    f"文档更新时间: {last_update}"
                ),
                "用法: /helpMenu <页码|next|prev> | /updateHelpMenu（仅限管理员）",
                "",
                *block_lines,
            ]
            pages.append("\n".join(lines).strip())
        return pages

    def _build_image_pages(
        self,
        items: list[CommandDocItem],
        page_size: int = 42,
        card_size: int = 14,
    ) -> list[tuple[dict[str, object], ...]]:
        if page_size <= 0 or card_size <= 0:
            raise ValueError("image page_size and card_size must be greater than 0")
        if not items:
            return []

        grouped: dict[str, list[CommandDocItem]] = defaultdict(list)
        for item in items:
            grouped[item.plugin_name].append(item)

        pages: list[list[dict[str, object]]] = []
        current_page: list[dict[str, object]] = []
        current_units = 0

        for plugin_name in sorted(grouped.keys(), key=str.lower):
            plugin_items = grouped[plugin_name]
            pointer = 0
            is_continued = False
            while pointer < len(plugin_items):
                card_commands: list[dict[str, object]] = []
                card_units = 0
                while pointer < len(plugin_items):
                    entry = plugin_items[pointer]
                    clean_desc, args = self._extract_arg_lines(entry.description)
                    command_data = {
                        "name": entry.command,
                        "description": clean_desc,
                        "args": args,
                        "aliases": ", ".join(entry.aliases),
                    }
                    command_units = 1 + (1 if entry.aliases else 0) + len(args)
                    if card_commands and card_units + command_units > card_size:
                        break
                    card_commands.append(command_data)
                    card_units += command_units
                    pointer += 1

                card = {
                    "plugin": plugin_name,
                    "continued": is_continued,
                    "commands": card_commands,
                }

                if current_page and current_units + card_units > page_size:
                    pages.append(current_page)
                    current_page = []
                    current_units = 0

                current_page.append(card)
                current_units += card_units
                is_continued = pointer < len(plugin_items)

        if current_page:
            pages.append(current_page)

        return [tuple(page) for page in pages]

    def _resolve_snapshot_for_event(self, event: AstrMessageEvent) -> HelpCacheSnapshot:
        if event.is_admin() and event.is_private_chat():
            if self._help_cache_admin_private.pages:
                return self._help_cache_admin_private
        return self._help_cache

    async def _refresh_help_cache(self, force: bool = False) -> tuple[bool, str]:
        async with self._refresh_lock:
            try:
                if not force and self._help_cache.pages:
                    self._log_debug("帮助菜单缓存已就绪，跳过重复刷新。")
                    return True, "帮助菜单缓存已就绪，已跳过重复刷新。"

                mode = self._get_fetch_mode()
                self._log(f"开始刷新帮助菜单缓存（{self._mode_display_name(mode)}）...")

                if mode == self._MODE_METADATA:
                    parsed_items_public = self._collect_items_from_metadata(
                        include_admin_commands=False
                    )
                    parsed_items_admin_private = self._collect_items_from_metadata(
                        include_admin_commands=True
                    )
                else:
                    if not self._has_api_credentials():
                        return (
                            False,
                            "帮助菜单刷新已跳过：当前为 API 模式，但未配置可用的 admin_name/admin_password。",
                        )
                    token = await self._get_or_refresh_token()
                    try:
                        raw_items = await self._fetch_command_items(token)
                    except PermissionError:
                        self._log_debug(
                            "命令接口返回 401，尝试用内存凭据重新登录后重试。"
                        )
                        token = await self._get_or_refresh_token(force_login=True)
                        raw_items = await self._fetch_command_items(token)
                    parsed_items_public = self._extract_allowed_items(
                        raw_items, include_admin_commands=False
                    )
                    parsed_items_admin_private = self._extract_allowed_items(
                        raw_items, include_admin_commands=True
                    )
                self._log_debug(f"命令总数(普通): {len(parsed_items_public)}")
                self._log_debug(
                    f"命令总数(管理员私聊): {len(parsed_items_admin_private)}"
                )

                last_update = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
                help_pages_public = self._build_pages(
                    parsed_items_public, len(parsed_items_public), last_update, mode
                )
                help_pages_admin_private = self._build_pages(
                    parsed_items_admin_private,
                    len(parsed_items_admin_private),
                    last_update,
                    mode,
                )
                image_pages_public = self._build_image_pages(parsed_items_public)
                image_pages_admin_private = self._build_image_pages(
                    parsed_items_admin_private
                )
                self._help_cache = HelpCacheSnapshot(
                    pages=tuple(help_pages_public),
                    image_pages=tuple(image_pages_public),
                    total_items=len(parsed_items_public),
                    last_update=last_update,
                    source_mode=mode,
                )
                self._help_cache_admin_private = HelpCacheSnapshot(
                    pages=tuple(help_pages_admin_private),
                    image_pages=tuple(image_pages_admin_private),
                    total_items=len(parsed_items_admin_private),
                    last_update=last_update,
                    source_mode=mode,
                )
                async with self._session_page_lock:
                    self._session_page.clear()
                if mode == self._MODE_API:
                    self._clear_sensitive_config_if_needed()
                return (
                    True,
                    (
                        "帮助菜单刷新成功"
                        f"（{self._mode_display_name(mode)}），"
                        f"普通 {len(parsed_items_public)} 条，"
                        f"管理员私聊 {len(parsed_items_admin_private)} 条可用命令。"
                    ),
                )
            except asyncio.TimeoutError:
                self._log_debug("刷新失败阶段: network_timeout")
                return False, "帮助菜单刷新失败：请求服务器超时，请稍后重试。"
            except aiohttp.ClientConnectionError as exc:
                self._log_debug(f"刷新失败阶段: connect ({exc})")
                return False, f"帮助菜单刷新失败：无法连接服务器（{exc}）。"
            except HttpStatusError as exc:
                self._log_debug(f"刷新失败阶段: {exc.stage} status={exc.status}")
                return (
                    False,
                    f"帮助菜单刷新失败：{exc.stage}接口异常（HTTP {exc.status}）。",
                )
            except aiohttp.ClientError as exc:
                self._log_debug(f"刷新失败阶段: client_error ({exc})")
                return False, f"帮助菜单刷新失败：网络请求异常（{exc}）。"
            except PermissionError as exc:
                self._log_debug(f"刷新失败阶段: permission ({exc})")
                return False, "帮助菜单刷新失败：登录状态失效，请检查账号配置后重试。"
            except ValueError as exc:
                self._log_debug(f"刷新失败阶段: value_error ({exc})")
                return False, f"帮助菜单刷新失败：{exc}"
            except Exception as exc:  # noqa: BLE001
                logger.exception("[helpmenu] 刷新失败（未知异常）。")
                return False, f"帮助菜单刷新失败：未知错误（{exc}）。"

    async def _run_debounced_auto_refresh(self) -> None:
        await asyncio.sleep(1.0)
        if not self._plugin_change_pending:
            return
        self._plugin_change_pending = False

        ok, message = await self._refresh_help_cache(force=False)
        if ok:
            self._log("检测到插件变更，已合并触发一次自动刷新帮助文档。")
            return
        logger.warning(f"[helpmenu] 检测到插件变更，自动刷新帮助文档失败：{message}")

    async def _auto_refresh_for_plugin_change(
        self, plugin_name: str, action: str
    ) -> None:
        if not plugin_name or plugin_name == "helpmenu":
            return

        mode = self._get_fetch_mode()
        if mode == self._MODE_API and not self._has_api_credentials():
            logger.warning(
                "[helpmenu] 检测到插件%s：%s，但当前为 API 模式且无可用账号密码，已跳过自动刷新。",
                action,
                plugin_name,
            )
            return

        self._plugin_change_pending = True
        if self._plugin_refresh_task and not self._plugin_refresh_task.done():
            self._log_debug(
                f"检测到插件{action}：{plugin_name}，已并入待执行刷新批次。"
            )
            return

        self._log_debug(f"检测到插件{action}：{plugin_name}，将在短暂去抖后自动刷新。")
        self._plugin_refresh_task = asyncio.create_task(
            self._run_debounced_auto_refresh()
        )

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

    def _resolve_page(
        self, arg: str, session_id: str, total_pages: int
    ) -> tuple[int, str]:
        total_pages = max(1, total_pages)
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
        self, arg: str, session_id: str, total_pages: int
    ) -> tuple[int, str]:
        async with self._session_page_lock:
            page, warning = self._resolve_page(arg, session_id, total_pages)
            self._set_session_page(session_id, page)
            return page, warning

    @filter.on_plugin_loaded()
    async def on_plugin_loaded(self, metadata):
        await self._auto_refresh_for_plugin_change(
            str(getattr(metadata, "name", "") or "").strip(),
            "加载",
        )

    @filter.on_plugin_unloaded()
    async def on_plugin_unloaded(self, metadata):
        await self._auto_refresh_for_plugin_change(
            str(getattr(metadata, "name", "") or "").strip(),
            "卸载",
        )

    @filter.permission_type(filter.PermissionType.ADMIN)
    @filter.command("updateHelpMenu")
    async def update_helpmenu(self, event: AstrMessageEvent):
        """刷新已生成的帮助菜单文档。"""
        ok, message = await self._refresh_help_cache(force=True)
        if ok:
            yield event.plain_result(message)
            return
        if self._get_fetch_mode() == self._MODE_API:
            yield event.plain_result(
                f"{message}\n请检查 Dashboard 地址、用户名和密码是否正确。"
            )
            return
        yield event.plain_result(message)

    @filter.command("helpMenu")
    async def helpmenu(self, event: AstrMessageEvent):
        """展示支持翻页的帮助菜单。"""
        self._log_debug("收到 helpMenu 命令请求")
        self._log_debug(
            f"是否为管理员私聊: {event.is_admin() and event.is_private_chat()}"
        )

        if not self._help_cache.pages:
            self._log_debug("帮助缓存为空，尝试刷新")
            ok, message = await self._refresh_help_cache()
            if not ok:
                if self._get_fetch_mode() == self._MODE_API:
                    yield event.plain_result(
                        f"{message}\n修正配置后请执行 /updateHelpMenu 重新刷新。",
                    )
                    return
                yield event.plain_result(message)
                return

        session_id = event.get_session_id()
        arg = self._parse_help_arg(event.message_str)
        self._log_debug(
            f"会话ID: {session_id[:32] if len(session_id) > 32 else session_id}"
        )
        self._log_debug(f"命令参数: {arg if arg else '(无)'}")

        snapshot = self._resolve_snapshot_for_event(event)
        if not snapshot.pages:
            snapshot = self._help_cache
        output_mode = self._get_output_mode()
        self._log_debug(f"输出模式: {output_mode}")
        self._log_debug(
            f"快照总页数: {len(snapshot.pages)}, 图片页数: {len(snapshot.image_pages)}"
        )

        page_bucket = snapshot.pages
        image_page_bucket = snapshot.image_pages
        paging_session_id = session_id
        if output_mode == self._OUTPUT_IMAGE and image_page_bucket:
            page_bucket = image_page_bucket
            paging_session_id = f"{session_id}:image"
            self._log_debug("使用图片分页模式")

        page, warning = await self._resolve_and_set_session_page(
            arg, paging_session_id, len(page_bucket)
        )
        page = max(1, min(page, len(page_bucket)))
        self._log_debug(f"解析后的页码: {page}")

        if output_mode == self._OUTPUT_IMAGE and image_page_bucket:
            self._log_debug("输出模式: 图片模式")
            self._log_debug(f"图片页面桶大小: {len(image_page_bucket)}")
            self._log_debug(f"当前页码: {page}")
            try:
                image_url = await self._render_help_page_as_image(
                    image_page_bucket[page - 1],
                    warning,
                    page,
                    len(image_page_bucket),
                    snapshot,
                )
                self._log_debug(
                    f"图片渲染完成，URL: {image_url[:100] if len(image_url) > 100 else image_url}"
                )
                yield event.image_result(image_url)
                return
            except Exception as exc:  # noqa: BLE001
                self._log_debug(f"帮助菜单图片渲染异常类型: {type(exc).__name__}")
                self._log_debug(f"帮助菜单图片渲染异常详情: {exc}")
                logger.warning(f"[helpmenu] 帮助菜单图片渲染失败，回退文本输出：{exc}")

        text = snapshot.pages[page - 1]
        if warning:
            text = f"{warning}{text}"
        yield event.plain_result(text)

    async def terminate(self):
        if self._plugin_refresh_task and not self._plugin_refresh_task.done():
            self._plugin_refresh_task.cancel()
            try:
                await self._plugin_refresh_task
            except asyncio.CancelledError:
                pass
        self._plugin_refresh_task = None
        self._plugin_change_pending = False
        async with self._http_session_lock:
            if self._http_session and not self._http_session.closed:
                await self._http_session.close()
            self._http_session = None
        async with self._session_page_lock:
            self._session_page.clear()
        self._auth_token = ""
        self._token_expire_at = 0
