import asyncio
import hashlib
import json
import re
from collections import OrderedDict, defaultdict
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path

from astrbot.api import AstrBotConfig, logger
from astrbot.api.event import AstrMessageEvent, filter
from astrbot.api.star import Context, Star, register
from astrbot.core.star.filter.command import CommandFilter
from astrbot.core.star.filter.command_group import CommandGroupFilter
from astrbot.core.star.filter.permission import PermissionType, PermissionTypeFilter
from astrbot.core.star.star_handler import star_handlers_registry

from .api_client import ApiClient, HttpStatusError
from .image_post_processor import crop_outer_white_background
from .image_renderer import render_help_page_as_image
from .page_builder import CommandDocItem, build_image_pages, build_pages


@dataclass(slots=True, frozen=True)
class HelpCacheSnapshot:
    pages: tuple[str, ...]
    image_pages: tuple[tuple[dict[str, object], ...], ...]
    total_items: int
    last_update: str
    source_mode: str


@register("helpmenu", "Sagiri777", "自动生成可翻页的指令帮助菜单", "1.0.8")
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
        "animations": "disabled",
        "caret": "hide",
        "scale": "css",
    }

    def __init__(self, context: Context, config: AstrBotConfig):
        super().__init__(context)
        self.config = config
        self._refresh_lock = asyncio.Lock()
        self._session_page_lock = asyncio.Lock()
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
        self._api_client: ApiClient | None = None
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
        return self._api_client is not None and self._api_client.has_credentials()

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

    def _is_image_post_process_enabled(self) -> bool:
        return bool(self.config.get("post_process_image", True))

    @property
    def _templates_dir(self) -> Path:
        return Path(__file__).parent / "templates"

    async def initialize(self):
        # Log default image render options on initialization
        self._log_debug(
            f"默认图片渲染选项: {json.dumps(self._DEFAULT_IMAGE_RENDER_OPTIONS, ensure_ascii=False)}"
        )

        # Initialize API client if in API mode
        if self._get_fetch_mode() == self._MODE_API:
            self._api_client = ApiClient(
                self.config,
                log_callback=self._log,
                log_debug_callback=self._log_debug,
            )

        ok, message = await self._refresh_help_cache(force=True)
        if ok:
            self._log(message)
        else:
            logger.warning(f"[helpmenu] {message}")

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

    async def _fetch_commands_from_api(
        self, include_admin_commands: bool = False
    ) -> list[CommandDocItem]:
        """Fetch commands from API using ApiClient."""
        if self._api_client is None:
            raise ValueError("API client is not initialized")
        return await self._api_client.fetch_commands(include_admin_commands)

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
                    parsed_items_public = await self._fetch_commands_from_api(
                        include_admin_commands=False
                    )
                    parsed_items_admin_private = await self._fetch_commands_from_api(
                        include_admin_commands=True
                    )
                self._log_debug(f"命令总数(普通): {len(parsed_items_public)}")
                self._log_debug(
                    f"命令总数(管理员私聊): {len(parsed_items_admin_private)}"
                )

                last_update = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
                help_pages_public = build_pages(
                    parsed_items_public,
                    len(parsed_items_public),
                    last_update,
                    mode,
                    self._MODE_API,
                )
                help_pages_admin_private = build_pages(
                    parsed_items_admin_private,
                    len(parsed_items_admin_private),
                    last_update,
                    mode,
                    self._MODE_API,
                )
                image_pages_public = build_image_pages(parsed_items_public)
                image_pages_admin_private = build_image_pages(
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
            except Exception as exc:  # noqa: BLE001
                # Handle specific exception types
                exc_class = exc.__class__.__name__
                if exc_class == "ClientConnectionError":
                    self._log_debug(f"刷新失败阶段: connect ({exc})")
                    return False, f"帮助菜单刷新失败：无法连接服务器（{exc}）。"
                if exc_class == "ClientError":
                    self._log_debug(f"刷新失败阶段: client_error ({exc})")
                    return False, f"帮助菜单刷新失败：网络请求异常（{exc}）。"
                if isinstance(exc, HttpStatusError):
                    status_error: HttpStatusError = exc
                    self._log_debug(
                        f"刷新失败阶段: {status_error.stage} status={status_error.status}"
                    )
                    return (
                        False,
                        f"帮助菜单刷新失败：{status_error.stage}接口异常（HTTP {status_error.status}）。",
                    )
                if isinstance(exc, PermissionError):
                    self._log_debug(f"刷新失败阶段: permission ({exc})")
                    return (
                        False,
                        "帮助菜单刷新失败：登录状态失效，请检查账号配置后重试。",
                    )
                if isinstance(exc, ValueError):
                    self._log_debug(f"刷新失败阶段: value_error ({exc})")
                    return False, f"帮助菜单刷新失败：{exc}"
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
    async def on_plugin_loaded(self, metadata=None):
        if metadata is None:
            return
        await self._auto_refresh_for_plugin_change(
            str(getattr(metadata, "name", "") or "").strip(),
            "加载",
        )

    @filter.on_plugin_unloaded()
    async def on_plugin_unloaded(self, metadata=None):
        if metadata is None:
            return
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

    async def _handle_image_test(self, event: AstrMessageEvent):
        """测试文转图功能，使用系统 html_render 渲染示例帮助菜单图片。"""
        self._log_debug("收到 helpMenu imageTest 命令请求")

        try:
            from .tests.image_test import run_image_test_command

            image_url, fallback_message = await run_image_test_command(
                self.html_render,
                self._templates_dir,
                self.config,
                self._is_debug_enabled(),
                self._log_debug,
            )

            if fallback_message:
                yield event.plain_result(fallback_message)

            yield event.image_result(image_url)

        except FileNotFoundError as exc:
            self._log_debug(f"模板文件不存在: {exc}")
            yield event.plain_result(f"模板文件不存在: {exc}")
        except Exception as exc:
            self._log_debug(f"文转图测试失败: {type(exc).__name__}: {exc}")
            import traceback

            self._log_debug(f"异常堆栈: {traceback.format_exc()}")
            yield event.plain_result(f"文转图测试失败: {exc}")

    @filter.command("helpMenu")
    async def helpmenu(self, event: AstrMessageEvent):
        """展示支持翻页的帮助菜单。"""
        self._log_debug("收到 helpMenu 命令请求")
        self._log_debug(
            f"是否为管理员私聊: {event.is_admin() and event.is_private_chat()}"
        )

        # 检测是否为 imageTest 参数
        arg = self._parse_help_arg(event.message_str)
        if arg == "imagetest":
            async for result in self._handle_image_test(event):
                yield result
            return

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
                self._log_debug("准备调用 render_help_page_as_image...")
                try:
                    image_url = await render_help_page_as_image(
                        self.html_render,
                        self._templates_dir,
                        image_page_bucket[page - 1],
                        warning,
                        page,
                        len(image_page_bucket),
                        snapshot.total_items,
                        snapshot.last_update,
                        snapshot.source_mode,
                        self.config.get("light_template")
                        or self.config.get("image_template"),
                        self.config.get("dark_template"),
                        str(self.config.get("dark_time_start", "18:00")),
                        str(self.config.get("dark_time_end", "06:00")),
                        self._is_debug_enabled(),
                    )
                except Exception as exc:  # noqa: BLE001
                    self._log_debug(
                        f"首轮图片渲染失败，准备使用经典模板重试: {type(exc).__name__}: {exc}"
                    )
                    image_url = await render_help_page_as_image(
                        self.html_render,
                        self._templates_dir,
                        image_page_bucket[page - 1],
                        warning,
                        page,
                        len(image_page_bucket),
                        snapshot.total_items,
                        snapshot.last_update,
                        snapshot.source_mode,
                        self._DEFAULT_IMAGE_TEMPLATE,
                        None,
                        str(self.config.get("dark_time_start", "18:00")),
                        str(self.config.get("dark_time_end", "06:00")),
                        self._is_debug_enabled(),
                    )

                self._log_debug(
                    f"图片渲染完成，URL: {image_url[:100] if len(image_url) > 100 else image_url}"
                )
                if not image_url:
                    raise ValueError("html_render 返回了空的图片 URL/路径")
                if self._is_image_post_process_enabled():
                    self._log_debug("已启用图片后处理，尝试裁剪主卡片外白色背景。")
                    image_url = crop_outer_white_background(image_url)
                yield event.image_result(image_url)
                return
            except Exception as exc:  # noqa: BLE001
                self._log_debug(f"帮助菜单图片渲染异常类型: {type(exc).__name__}")
                self._log_debug(f"帮助菜单图片渲染异常详情: {exc}")
                import traceback

                self._log_debug(f"异常堆栈: {traceback.format_exc()}")
                logger.warning(f"[helpmenu] 帮助菜单图片渲染失败，回退文本输出：{exc}")
                if self._is_debug_enabled():
                    yield event.plain_result(
                        f"图片渲染失败: {exc}\n\n已回退到文本模式。"
                    )
                else:
                    yield event.plain_result("图片渲染失败，已回退到文本模式。")

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
        if self._api_client is not None:
            await self._api_client.close()
        async with self._session_page_lock:
            self._session_page.clear()
