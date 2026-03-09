from datetime import datetime

from astrbot.api import AstrBotConfig
from astrbot.api.event import AstrMessageEvent, MessageChain, filter
from astrbot.api.star import Context, Star
from astrbot.core.star import StarMetadata, command_management


class AlterationNotifierPlugin(Star):
    def __init__(self, context: Context, config: AstrBotConfig):
        super().__init__(context)
        self.UNIFIED_MSG_ORIGIN_PREFIX = "default:GroupMessage:"
        self.SELF = "astrbot_plugin_alteration_notifier"

        self.ctx = context

        self.exclude_mode = config.get("role_range", {}).get("exclude_mode", True)
        self.group = config.get("role_range", {}).get("group_list", [])
        self.monitor_self = config.get("monitor_self", False)

        self.activated_plugins: dict[str, set[str]] = {}
        self.unloaded_cache: dict[str, int] = {}
        self.role_range: set[str] = set() if self.exclude_mode else set(self.group)

    async def initialize(self):
        """可选择实现异步的插件初始化方法，当实例化该插件类之后会自动调用该方法。"""
        await self.save_external_activated_plugins()

    async def terminate(self):
        """可选择实现异步的插件销毁方法，当插件被卸载/停用时会调用。"""
        pass

    @staticmethod
    def collect_command_names(data: dict):
        names = set()
        if data["type"] == "group":
            for sub in data["sub_commands"]:
                names |= AlterationNotifierPlugin.collect_command_names(sub)
        elif data["type"] == "sub_command":
            par = data["parent_signature"]
            names.add(f"{par} {data['current_fragment']}")
            for al in data["aliases"]:
                names.add(f"{par} {al}")
        elif data["type"] == "command":
            names.add(data["current_fragment"])
            names |= set(data["aliases"])
        else:
            raise NotImplementedError(f"未解析的类型：{data['type']}")

        return names

    async def save_external_activated_plugins(
        self,
    ):
        external_list = self.ctx.get_all_stars()
        all_commands = await command_management.list_commands()

        for cmd in external_list:
            if cmd.activated:
                names = set()
                for check in all_commands:
                    if cmd.name == check["plugin"]:
                        names |= AlterationNotifierPlugin.collect_command_names(check)
                self.activated_plugins[cmd.name] = names

    async def notify(self, msg: str):
        for group in self.role_range:
            await self.ctx.send_message(
                self.UNIFIED_MSG_ORIGIN_PREFIX + group, MessageChain().message(msg)
            )

    @filter.on_plugin_loaded()
    async def plugin_loaded(self, metadata: StarMetadata):
        """监测新插件启用、加载"""

        names = set()
        all_commands = await command_management.list_commands()

        plugin = metadata.name
        for check in all_commands:
            if plugin == check["plugin"]:
                added = AlterationNotifierPlugin.collect_command_names(check)
                names |= added
                cache = self.unloaded_cache
                for one in added:
                    if one in cache:
                        del self.unloaded_cache[one]
        self.activated_plugins[plugin] = names

        if not self.monitor_self and plugin == self.SELF:
            return
        await self.notify(f"新增插件：{plugin}")

    @filter.on_plugin_unloaded()
    async def plugin_unloaded(self, metadata: StarMetadata):
        """监测插件禁用、卸载"""

        plugin = metadata.name
        now = int(datetime.now().timestamp())
        names = self.activated_plugins.pop(plugin)
        for name in names:
            self.unloaded_cache[name] = now

        if not self.monitor_self and plugin == self.SELF:
            return
        await self.notify(f"移除插件：{plugin}")

    @filter.event_message_type(filter.EventMessageType.ALL, priority=-10)
    async def monitor(self, event: AstrMessageEvent):
        """监控唤醒机器人的消息"""

        if event._has_send_oper or not event.is_at_or_wake_command:
            return

        pure_text = event.message_str
        for name in self.unloaded_cache:
            if pure_text.startswith(name):
                yield event.plain_result(
                    f"指令【{name}】已经于【{datetime.fromtimestamp(self.unloaded_cache[name])}】被移除了！"
                )
                break

    @filter.event_message_type(filter.EventMessageType.GROUP_MESSAGE, priority=10)
    async def collect_group(self, event: AstrMessageEvent):
        """自动收集群聊id"""

        group_id = event.get_group_id()
        if self.exclude_mode and group_id not in self.group:
            self.role_range.add(group_id)