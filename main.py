from astrbot.api.event import filter, AstrMessageEvent, MessageEventResult
from astrbot.core.utils.astrbot_path import get_astrbot_data_path
from astrbot.api.star import Context, Star, register
from astrbot.api import logger

@register("helpmenu", "Sagiri777", "一个简单的 HelpMenu 插件", "0.0.1")
class MyPlugin(Star):
    def __init__(self, context: Context):
        super().__init__(context)

    async def initialize(self):
        """可选择实现异步的插件初始化方法，当实例化该插件类之后会自动调用该方法。"""

    # 注册指令的装饰器。指令名为 helpmenu。注册成功后，发送 `/helpmenu` 就会触发这个指令，并回复 `你好, {user_name}!`
    @filter.command("helpmenu")
    async def helpmenu(self, event: AstrMessageEvent):
        """这是一个帮助菜单指令""" # 这是 handler 的描述，将会被解析方便用户了解插件内容。建议填写。
        user_name = event.get_sender_name()
        message_str = event.message_str
        plugins = self.context.get_all_stars()
        plugin_data_path = get_astrbot_data_path()
        with open(f"{plugin_data_path}/example.txt", "w") as f:
            f.write(plugins)
        yield event.plain_result(f"Hello, {user_name}, 你发了 {message_str}!") # 发送一条纯文本消息

    async def terminate(self):
        """可选择实现异步的插件销毁方法，当插件被卸载/停用时会调用。"""
