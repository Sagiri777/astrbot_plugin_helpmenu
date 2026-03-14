"""Image rendering test functionality for helpmenu plugin."""

import asyncio
from pathlib import Path

import aiohttp
from astrbot.core.utils.http_ssl import build_tls_connector


# 备用文转图服务端点
FALLBACK_T2I_ENDPOINT = "https://t2i.soulter.top/text2img"

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


async def render_with_fallback_t2i(
    template_content: str,
    tmpl_data: dict,
    log_debug_callback=None,
) -> tuple[str, str]:
    """使用备用文转图服务渲染图片。
    
    Args:
        template_content: HTML 模板内容
        tmpl_data: 模板渲染数据
        log_debug_callback: 可选的调试日志回调函数
        
    Returns:
        tuple: (image_url, message) 图片URL和提示信息
    """
    def _log(msg: str) -> None:
        if log_debug_callback:
            log_debug_callback(msg)
    
    _log(f"使用备用文转图服务: {FALLBACK_T2I_ENDPOINT}")
    
    # 准备请求数据
    post_data = {
        "tmpl": template_content,
        "json": "true",
        "tmpldata": tmpl_data,
        "options": {
            "full_page": False,
            "type": "png",
            "omit_background": True,
        },
    }
    
    # 调试：打印请求数据的关键信息
    import json
    _log(f"请求数据 - tmpl 长度: {len(template_content)} 字符")
    _log(f"请求数据 - tmpldata keys: {list(tmpl_data.keys())}")
    _log(f"请求数据 - tmpldata subtitle: {tmpl_data.get('subtitle', '')[:50]}")
    _log(f"请求数据 - tmpldata cards 数量: {len(tmpl_data.get('cards', []))}")
    _log(f"请求数据 - options: {post_data['options']}")
    _log(f"请求数据 - json 序列化后大小: {len(json.dumps(post_data))} 字节")
    
    timeout = aiohttp.ClientTimeout(total=60)
    headers = {
        "Accept-Encoding": "gzip, deflate",
    }
    
    async with aiohttp.ClientSession(
        timeout=timeout,
        trust_env=True,
        connector=build_tls_connector(),
        headers=headers,
    ) as session:
        # 请求图片生成
        _log("发送图片生成请求到备用服务...")
        async with session.post(f"{FALLBACK_T2I_ENDPOINT}/generate", json=post_data) as resp:
            _log(f"备用服务响应状态: {resp.status}")
            
            if resp.status != 200:
                text = await resp.text()
                _log(f"备用服务错误详情 (HTTP {resp.status}): {text[:500]}")
                try:
                    error_json = await resp.json()
                    _log(f"备用服务错误JSON: {error_json}")
                except:
                    pass
                raise RuntimeError(f"备用服务返回错误 (HTTP {resp.status}): {text[:200]}")
            
            data = await resp.json()
            _log(f"备用服务响应数据: {data}")
            
            if "data" not in data or "id" not in data["data"]:
                raise RuntimeError("备用服务响应中未找到图片ID")
            
            image_url = f"{FALLBACK_T2I_ENDPOINT}/{data['data']['id']}"
            _log(f"备用服务图片URL: {image_url}")
            
            return image_url, "系统文转图失败，已切换到备用文转图服务生成图片"


async def render_test_image(
    html_render_func,
    templates_dir: Path,
    template_name: str = "classic.html",
    log_debug_callback=None,
    use_fallback_on_failure: bool = True,
) -> tuple[str, str]:
    """渲染测试图片。
    
    首先尝试使用系统文转图服务，如果失败且允许备用，
    则自动切换到 https://t2i.soulter.top/text2img 在线服务。
    
    Args:
        html_render_func: html_render 方法（来自 Star 类）
        templates_dir: 模板文件夹路径
        template_name: 模板文件名，默认为 classic.html
        log_debug_callback: 可选的调试日志回调函数，接收字符串参数
        use_fallback_on_failure: 系统文转图失败时是否使用备用服务，默认为 True
        
    Returns:
        tuple: (image_url, message) 图片URL和提示信息（如果有）
        
    Raises:
        FileNotFoundError: 模板文件不存在
        ValueError: html_render 返回空结果且备用也失败
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
    
    # 首先尝试系统文转图服务
    _log("开始调用系统 html_render 渲染图片...")
    try:
        image_url = await html_render_func(
            template_content,
            sample_data,
            options=render_options,
        )
        
        if not image_url:
            raise ValueError("html_render 返回了空的图片 URL")
        
        _log(f"系统文转图成功，URL: {image_url[:100] if len(image_url) > 100 else image_url}")
        return image_url, ""
        
    except Exception as primary_exc:
        _log(f"系统文转图失败: {type(primary_exc).__name__}: {primary_exc}")
        
        if not use_fallback_on_failure:
            _log("备用服务已禁用，重新抛出异常")
            raise
        
        # 尝试备用服务
        _log("尝试使用备用文转图服务...")
        try:
            fallback_url, fallback_msg = await render_with_fallback_t2i(
                template_content,
                sample_data,
                log_debug_callback,
            )
            _log(f"备用服务渲染成功: {fallback_msg}")
            return fallback_url, fallback_msg
            
        except Exception as fallback_exc:
            _log(f"备用文转图也失败: {type(fallback_exc).__name__}: {fallback_exc}")
            # 两次都失败了，抛出组合错误信息
            error_msg = f"系统文转图失败: {primary_exc}；备用服务也失败: {fallback_exc}"
            _log(f"最终错误消息: {error_msg}")
            raise RuntimeError(error_msg) from fallback_exc
