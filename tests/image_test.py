"""Image rendering test functionality for helpmenu plugin."""

from pathlib import Path


# 默认渲染选项
DEFAULT_RENDER_OPTIONS = {
    "type": "png",
    "full_page": False,
    "omit_background": True,
    "animations": "disabled",
    "caret": "hide",
    "scale": 1.0,
}


def get_sample_data() -> dict:
    """Get sample data for template rendering (参考 test_t2i_endpoint.py)."""
    return {
        "subtitle": "使用 /help 获取帮助 | 来源: 文转图测试 | 文档更新时间: 2026-03-14",
        "warning": "提示：带 * 的指令表示需要管理员权限",
        "cards": [
            {
                "plugin": "插件1",
                "commands": [
                    {
                        "name": "hello",
                        "description": "你好呀",
                        "args": [
                            {"name": "test", "detail": "测试参数"},
                        ],
                        "aliases": "hello",
                    },
                    {
                        "name": "search",
                        "description": "搜索内容",
                    },
                ],
            },
            {
                "plugin": "插件2",
                "continued": True,
                "commands": [
                    {
                        "name": "status",
                        "description": "查看状态",
                    },
                ],
            },
            {
                "plugin": "插件3",
                "commands": [
                    {
                        "name": "help",
                        "description": "显示帮助信息",
                        "args": [
                            {"name": "plugin", "detail": "插件名称"},
                        ],
                    },
                ],
            },
        ],
    }


def load_template(templates_dir: Path, template_name: str = "classic.html") -> str:
    """Load template from templates folder.
    
    Args:
        templates_dir: 模板文件夹路径
        template_name: 模板文件名，默认为 classic.html
        
    Returns:
        模板内容字符串
        
    Raises:
        FileNotFoundError: 模板文件不存在
    """
    template_path = templates_dir / template_name
    if not template_path.exists():
        raise FileNotFoundError(f"Template not found: {template_path}")
    return template_path.read_text(encoding="utf-8")


async def render_test_image(
    html_render_func,
    templates_dir: Path,
    template_name: str = "classic.html",
    log_debug_callback=None,
) -> str:
    """渲染测试图片。
    
    Args:
        html_render_func: html_render 方法（来自 Star 类）
        templates_dir: 模板文件夹路径
        template_name: 模板文件名，默认为 classic.html
        log_debug_callback: 可选的调试日志回调函数，接收字符串参数
        
    Returns:
        生成的图片 URL 或路径
        
    Raises:
        FileNotFoundError: 模板文件不存在
        ValueError: html_render 返回空结果
        Exception: 渲染过程中的其他异常
    """
    def _log(msg: str) -> None:
        if log_debug_callback:
            log_debug_callback(msg)
    
    # 加载模板
    _log(f"开始加载模板: {template_name}")
    template_content = load_template(templates_dir, template_name)
    _log(f"成功加载模板，长度: {len(template_content)} 字符")
    
    # 准备示例数据
    sample_data = get_sample_data()
    _log(f"示例数据准备完成，包含 {len(sample_data['cards'])} 个插件卡片")
    
    # 渲染选项
    render_options = DEFAULT_RENDER_OPTIONS.copy()
    _log(f"渲染选项: {render_options}")
    
    # 调用系统文转图服务
    _log("开始调用 html_render 渲染图片...")
    image_url = await html_render_func(
        template_content,
        sample_data,
        options=render_options,
    )
    
    if not image_url:
        raise ValueError("html_render 返回了空的图片 URL")
    
    _log(f"图片渲染成功，URL: {image_url[:100] if len(image_url) > 100 else image_url}")
    
    return image_url
