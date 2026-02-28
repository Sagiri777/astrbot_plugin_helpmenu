import asyncio
import re
from collections import defaultdict
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


@register("helpmenu", "Sagiri777", "自动生成可翻页的指令帮助菜单", "0.1.0")
class MyPlugin(Star):
    def __init__(self, context: Context, config: AstrBotConfig):
        super().__init__(context)
        self.config = config
        self._refresh_lock = asyncio.Lock()
        self._help_pages: list[str] = []
        self._total_items = 0
        self._last_update = "never"
        self._session_page: dict[str, int] = {}

    def _is_debug_enabled(self) -> bool:
        return bool(self.config.get("debug", False))

    def _log(self, message: str) -> None:
        logger.info(f"[helpmenu] {message}")

    def _log_debug(self, message: str) -> None:
        if self._is_debug_enabled():
            logger.info(f"[helpmenu][debug] {message}")

    async def initialize(self):
        ok, message = await self._refresh_help_cache()
        if ok:
            self._log(message)
        else:
            logger.warning(f"[helpmenu] {message}")

    def _clear_sensitive_config_if_needed(self) -> None:
        if self._is_debug_enabled():
            self._log_debug("Skip clearing admin credentials because debug is enabled.")
            return

        self.config["admin_name"] = ""
        self.config["admin_password"] = ""
        self.config.save_config()
        self._log("Refresh succeeded, cleared admin_name/admin_password from plugin config.")

    async def _login_and_get_token(self) -> str:
        admin_name = (self.config.get("admin_name") or "").strip()
        admin_password = (self.config.get("admin_password") or "").strip()
        if not admin_name or not admin_password:
            raise ValueError(
                "Missing admin_name/admin_password in plugin config (_conf_schema.json).",
            )

        base_url = (
            self.config.get("dashboard_base_url") or "http://127.0.0.1:6185"
        ).rstrip(
            "/",
        )
        login_url = f"{base_url}/api/auth/login"
        payload = {"username": admin_name, "password": admin_password}
        self._log_debug(f"Login URL: {login_url}")
        self._log_debug(f"Login username: {admin_name}")

        timeout = aiohttp.ClientTimeout(total=12)
        async with aiohttp.ClientSession(trust_env=True, timeout=timeout) as session:
            async with session.post(login_url, json=payload) as response:
                data = await response.json(content_type=None)
                self._log_debug(f"Login HTTP status: {response.status}")
                self._log_debug(f"Login response: {data}")

                if not isinstance(data, dict):
                    raise ValueError(
                        f"Login response is not a JSON object: {type(data).__name__}",
                    )

                data_node = data.get("data")
                if not isinstance(data_node, dict):
                    raise ValueError(
                        f"Login response field 'data' is invalid: {data_node}",
                    )

                token = str(data_node.get("token") or "").strip()
                if not token:
                    raise ValueError(
                        f"Login failed: status={response.status}, body={data}",
                    )
                return token

    async def _fetch_command_items(self, token: str) -> list[dict]:
        base_url = (
            self.config.get("dashboard_base_url") or "http://127.0.0.1:6185"
        ).rstrip(
            "/",
        )
        commands_url = f"{base_url}/api/commands"
        headers = {"Authorization": f"Bearer {token}"}
        timeout = aiohttp.ClientTimeout(total=18)
        self._log_debug(f"Commands URL: {commands_url}")

        async with aiohttp.ClientSession(trust_env=True, timeout=timeout) as session:
            async with session.get(commands_url, headers=headers) as response:
                data = await response.json(content_type=None)
                self._log_debug(f"Commands HTTP status: {response.status}")
                if not isinstance(data, dict):
                    raise ValueError(
                        f"Commands response is not a JSON object: {type(data).__name__}",
                    )

                data_node = data.get("data")
                if not isinstance(data_node, dict):
                    raise ValueError(
                        f"Commands response field 'data' is invalid: {data_node}",
                    )

                items = data_node.get("items", [])
                if not isinstance(items, list):
                    raise ValueError(
                        f"Commands response field 'items' is invalid: {type(items).__name__}",
                    )
                self._log_debug(f"Commands items count (raw): {len(items)}")
                return items

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
                        ) or clean_text(item.get("plugin"), "unknown")
                        description = clean_text(
                            item.get("description"), "No description."
                        )
                        aliases = [
                            clean_text(alias, "")
                            for alias in item.get("aliases", [])
                            if clean_text(alias, "")
                        ]
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
        self, items: list[CommandDocItem], page_size: int = 12
    ) -> list[str]:
        if not items:
            return ["No visible commands yet. Use /updateHelpMenu to refresh."]

        pages: list[str] = []
        total_pages = (len(items) + page_size - 1) // page_size
        for page_index in range(total_pages):
            start = page_index * page_size
            current_items = items[start : start + page_size]
            grouped: dict[str, list[CommandDocItem]] = defaultdict(list)
            for item in current_items:
                grouped[item.plugin_name].append(item)

            lines = [
                "Help Menu",
                (
                    f"Page {page_index + 1}/{total_pages} | "
                    f"Commands: {self._total_items} | "
                    f"Updated: {self._last_update}"
                ),
                "Use: /helpMenu <page|next|prev> | /updateHelpMenu",
                "",
            ]

            for plugin_name in sorted(grouped.keys(), key=str.lower):
                lines.append(f"[{plugin_name}]")
                for entry in grouped[plugin_name]:
                    lines.append(f"/{entry.command} - {entry.description}")
                    if entry.aliases:
                        lines.append(f"  aliases: {', '.join(entry.aliases)}")
                lines.append("")

            pages.append("\n".join(lines).strip())
        return pages

    async def _refresh_help_cache(self) -> tuple[bool, str]:
        async with self._refresh_lock:
            try:
                self._log("Refreshing help menu cache...")
                token = await self._login_and_get_token()
                raw_items = await self._fetch_command_items(token)
                parsed_items = self._extract_allowed_items(raw_items)
                self._log_debug(f"Commands items count (filtered): {len(parsed_items)}")

                self._total_items = len(parsed_items)
                self._last_update = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
                self._help_pages = self._build_pages(parsed_items)
                self._session_page.clear()
                self._clear_sensitive_config_if_needed()
                return (
                    True,
                    f"Help menu refreshed successfully ({self._total_items} commands).",
                )
            except Exception as exc:  # noqa: BLE001
                if self._is_debug_enabled():
                    logger.exception("[helpmenu] Refresh failed in debug mode.")
                return False, f"Failed to refresh help menu: {exc}"

    def _parse_help_arg(self, message: str) -> str:
        normalized = re.sub(r"\s+", " ", (message or "").strip())
        parts = normalized.split(" ", 1)
        if len(parts) < 2:
            return ""
        return parts[1].strip().lower()

    def _resolve_page(self, arg: str, session_id: str) -> tuple[int, str]:
        total_pages = max(1, len(self._help_pages))
        if not arg:
            page = self._session_page.get(session_id, 1)
            return min(max(page, 1), total_pages), ""

        if arg in {"next", "n", "下页", "下一页"}:
            page = self._session_page.get(session_id, 1) + 1
            return min(page, total_pages), ""
        if arg in {"prev", "p", "上页", "上一页"}:
            page = self._session_page.get(session_id, 1) - 1
            return max(page, 1), ""
        if arg.isdigit():
            page = int(arg)
            if page < 1:
                page = 1
            if page > total_pages:
                page = total_pages
            return page, ""

        return 1, f"Invalid page argument: {arg}. Showing page 1.\n\n"

    @filter.command("updateHelpMenu")
    async def update_helpmenu(self, event: AstrMessageEvent):
        """Refresh the generated help menu document."""
        ok, message = await self._refresh_help_cache()
        if ok:
            yield event.plain_result(message)
            return
        yield event.plain_result(
            f"{message}\nPlease check dashboard address and admin credentials."
        )

    @filter.command("helpMenu")
    async def helpmenu(self, event: AstrMessageEvent):
        """Show generated help menu with pagination support."""
        if not self._help_pages:
            ok, message = await self._refresh_help_cache()
            if not ok:
                yield event.plain_result(
                    f"{message}\nPlease run /updateHelpMenu after fixing config.",
                )
                return

        arg = self._parse_help_arg(event.message_str)
        page, warning = self._resolve_page(arg, event.get_session_id())
        self._session_page[event.get_session_id()] = page
        text = self._help_pages[page - 1]
        if warning:
            text = f"{warning}{text}"
        yield event.plain_result(text)

    async def terminate(self):
        self._session_page.clear()
