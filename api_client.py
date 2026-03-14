import asyncio
import base64
import hashlib
import json
from collections.abc import Callable
from dataclasses import dataclass
from datetime import datetime
from typing import TYPE_CHECKING

import aiohttp

from astrbot.api import logger

from .page_builder import CommandDocItem

if TYPE_CHECKING:
    from astrbot.api import AstrBotConfig


class HelpMenuError(Exception):
    """Base exception for help menu errors."""

    def __init__(self, stage: str, message: str):
        super().__init__(message)
        self.stage = stage
        self.message = message


class HttpStatusError(HelpMenuError):
    """Exception for HTTP status errors."""

    def __init__(self, stage: str, status: int, detail: str):
        super().__init__(stage, f"HTTP {status}: {detail}")
        self.status = status


@dataclass
class ApiClientState:
    """Internal state for API client."""

    auth_token: str = ""
    token_expire_at: int = 0
    cached_admin_name: str = ""
    cached_admin_password: str = ""


class ApiClient:
    """Client for fetching command data from AstrBot API."""

    def __init__(
        self,
        config: "AstrBotConfig",
        log_callback: Callable[[str], None] | None = None,
        log_debug_callback: Callable[[str], None] | None = None,
    ):
        self.config = config
        self._state = ApiClientState()
        self._http_session: aiohttp.ClientSession | None = None
        self._http_session_lock = asyncio.Lock()
        self._log = log_callback or (lambda msg: logger.info(f"[helpmenu] {msg}"))
        self._log_debug = log_debug_callback or (lambda msg: None)

    async def _get_http_session(self) -> aiohttp.ClientSession:
        """Get or create HTTP session."""
        if self._http_session and not self._http_session.closed:
            return self._http_session

        async with self._http_session_lock:
            if self._http_session and not self._http_session.closed:
                return self._http_session
            self._http_session = aiohttp.ClientSession(trust_env=False)
            return self._http_session

    async def close(self) -> None:
        """Close HTTP session and cleanup."""
        async with self._http_session_lock:
            if self._http_session and not self._http_session.closed:
                await self._http_session.close()
            self._http_session = None
        self._state.auth_token = ""
        self._state.token_expire_at = 0

    def _get_base_url(self) -> str:
        """Get base URL for API requests."""
        return (
            self.config.get("ASTRHost")
            or self.config.get("dashboard_base_url")
            or "http://127.0.0.1:6185"
        ).rstrip("/")

    def _capture_credentials_from_config(self) -> tuple[str, str]:
        """Capture and cache admin credentials from config."""
        admin_name = (self.config.get("admin_name") or "").strip()
        admin_password = (self.config.get("admin_password") or "").strip()
        if admin_name and admin_password:
            self._state.cached_admin_name = admin_name
            self._state.cached_admin_password = admin_password
        return self._state.cached_admin_name, self._state.cached_admin_password

    def has_credentials(self) -> bool:
        """Check if API credentials are available."""
        admin_name, admin_password = self._capture_credentials_from_config()
        return bool(admin_name and admin_password)

    def _build_login_password(self, raw_password: str) -> str:
        """Build MD5 hashed password for login."""
        md5_password = hashlib.md5(raw_password.encode("utf-8")).hexdigest()  # noqa: S324
        self._log_debug("已将配置密码转换为 MD5 后提交登录。")
        return md5_password

    def _decode_token_expire_at(self, token: str) -> int:
        """Decode JWT token expiration timestamp."""
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
        """Check if current token is expired."""
        if not self._state.auth_token:
            return True
        if self._state.token_expire_at <= 0:
            return False
        return int(datetime.now().timestamp()) >= self._state.token_expire_at - 30

    def _build_safe_login_response_log(self, data: object) -> str:
        """Build safe log message for login response (without sensitive data)."""
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
        """Read and parse JSON response."""
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
        """Raise appropriate exception for non-2xx HTTP status."""
        if 200 <= response.status < 300:
            return
        if stage == "登录" and response.status in {401, 403}:
            raise PermissionError("login_unauthorized")
        if stage == "命令列表" and response.status == 401:
            raise PermissionError("token_unauthorized")
        raise HttpStatusError(stage, response.status, "服务返回非 2xx 状态")

    async def _login_and_get_token(self) -> str:
        """Login and get authentication token."""
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
            self._state.auth_token = token
            self._state.token_expire_at = self._decode_token_expire_at(token)
            if self._state.token_expire_at > 0:
                self._log_debug(
                    f"登录成功，Token 过期时间戳: {self._state.token_expire_at}"
                )
            else:
                self._log_debug("登录成功，未解析到 Token 过期时间。")
            return token

    async def _get_or_refresh_token(self, force_login: bool = False) -> str:
        """Get existing token or refresh if expired."""
        if not force_login and not self._is_token_expired():
            self._log_debug("复用内存中的 Token。")
            return self._state.auth_token
        self._log_debug("Token 不可用或已过期，尝试重新登录。")
        return await self._login_and_get_token()

    async def _fetch_command_items(self, token: str) -> list[dict]:
        """Fetch command list from API."""
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

    def _can_show_command(
        self, permission: str, include_admin_commands: bool = False
    ) -> bool:
        """Check if command should be shown based on permission."""
        normalized = (permission or "everyone").strip().lower()
        if normalized in {"", "everyone", "member"}:
            return True
        if include_admin_commands and normalized == "admin":
            return True
        return False

    def _clean_text(self, value: str | None, default: str) -> str:
        """Clean and normalize text."""
        import re

        text = re.sub(r"\s+", " ", (value or "").strip())
        if not text:
            return default
        if len(text) > 120:
            return f"{text[:117]}..."
        return text

    def _extract_allowed_items(
        self, raw_items: list[dict], include_admin_commands: bool = False
    ) -> list[CommandDocItem]:
        """Extract allowed command items from raw API response."""
        collected: list[CommandDocItem] = []
        dedup: set[str] = set()
        excluded_plugins = {"builtin_commands"}

        def walk(items: list[dict]) -> None:
            for item in items:
                if not isinstance(item, dict):
                    continue

                plugin_id = str(item.get("plugin") or "").strip()
                if plugin_id in excluded_plugins:
                    continue

                item_type = item.get("type", "")
                enabled = item.get("enabled", False)
                permission = str(item.get("permission") or "").strip().lower()

                if (
                    item_type in {"command", "sub_command"}
                    and enabled
                    and self._can_show_command(permission, include_admin_commands)
                ):
                    command = self._clean_text(item.get("effective_command"), "")
                    if command:
                        plugin_name = self._clean_text(
                            item.get("plugin_display_name"), ""
                        ) or self._clean_text(item.get("plugin"), "未知插件")
                        description = self._clean_text(
                            item.get("description"), "暂无说明。"
                        )
                        raw_aliases = item.get("aliases", [])
                        aliases: list[str] = []
                        if isinstance(raw_aliases, list):
                            for alias in raw_aliases:
                                if not isinstance(alias, str):
                                    continue
                                alias_text = self._clean_text(alias, "")
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

    async def fetch_commands(
        self, include_admin_commands: bool = False
    ) -> list[CommandDocItem]:
        """Fetch and parse commands from API.

        Args:
            include_admin_commands: Whether to include admin-only commands.

        Returns:
            List of parsed command items.

        Raises:
            ValueError: If credentials are missing or API returns invalid data.
            PermissionError: If authentication fails.
            HttpStatusError: If HTTP request fails.
            aiohttp.ClientError: If network request fails.
        """
        if not self.has_credentials():
            raise ValueError(
                "插件配置缺少 admin_name 或 admin_password，请先填写。",
            )

        token = await self._get_or_refresh_token()
        try:
            raw_items = await self._fetch_command_items(token)
        except PermissionError:
            self._log_debug("命令接口返回 401，尝试用内存凭据重新登录后重试。")
            token = await self._get_or_refresh_token(force_login=True)
            raw_items = await self._fetch_command_items(token)

        return self._extract_allowed_items(raw_items, include_admin_commands)

    def clear_cached_credentials(self) -> None:
        """Clear cached credentials from state."""
        self._state.cached_admin_name = ""
        self._state.cached_admin_password = ""
