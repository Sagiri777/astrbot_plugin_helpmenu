import json
from datetime import datetime, timedelta, timezone
from pathlib import Path

from astrbot.api import logger

DEFAULT_IMAGE_RENDER_OPTIONS = {
    "type": "png",
    "full_page": False,
    "omit_background": True,
    "animations": "disabled",
    "caret": "hide",
    "scale": 1.0,
}

DEFAULT_IMAGE_TEMPLATE = "classic"
MODE_API = "api"


def is_dark_time(dark_time_start: str = "18:00", dark_time_end: str = "06:00") -> bool:
    """判断当前是否为深色模式时间段。"""
    start_time_str = str(dark_time_start).strip()
    end_time_str = str(dark_time_end).strip()

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


def get_available_templates(templates_dir: Path) -> list[str]:
    """获取可用模板列表。"""
    if not templates_dir.exists():
        return []
    return [f.stem for f in templates_dir.glob("*.html")]


def get_image_template_name(
    templates_dir: Path,
    light_template: str | None = None,
    dark_template: str | None = None,
    dark_time_start: str = "18:00",
    dark_time_end: str = "06:00",
    is_debug: bool = False,
) -> str:
    """获取图片模板名称。"""

    def log_debug(message: str) -> None:
        if is_debug:
            logger.info(f"[helpmenu][debug] {message}")

    available = get_available_templates(templates_dir)
    log_debug(f"可用的模板列表: {available}")

    template_name = str(light_template or DEFAULT_IMAGE_TEMPLATE).strip().lower()
    log_debug(f"配置的浅色模板: {template_name}")

    if template_name not in available:
        logger.warning(
            f"[helpmenu] 未知 light_template={template_name}，将回退为 {DEFAULT_IMAGE_TEMPLATE}。"
        )
        template_name = DEFAULT_IMAGE_TEMPLATE

    if is_dark_time(dark_time_start, dark_time_end):
        dark_tmpl = str(dark_template or "").strip().lower()
        log_debug("当前为深色模式时间段")
        log_debug(f"配置的深色模板: {dark_tmpl if dark_tmpl else '未配置'}")

        if not dark_tmpl:
            dark_tmpl = f"{template_name}_dark"

        if dark_tmpl in available:
            template_name = dark_tmpl
            log_debug(f"使用深色模板: {template_name}")
        else:
            logger.warning(
                f"[helpmenu] 深色模板 {dark_tmpl} 不存在，降级使用浅色模板。"
            )
            log_debug(f"深色模板 {dark_tmpl} 不在可用列表中，继续使用: {template_name}")

    log_debug(f"最终使用的模板名称: {template_name}")
    return template_name


def get_image_template(
    templates_dir: Path,
    template_name: str | None = None,
    light_template: str | None = None,
    dark_template: str | None = None,
    dark_time_start: str = "18:00",
    dark_time_end: str = "06:00",
    is_debug: bool = False,
) -> str:
    """获取图片模板内容。"""

    def log_debug(message: str) -> None:
        if is_debug:
            logger.info(f"[helpmenu][debug] {message}")

    if template_name is None:
        template_name = get_image_template_name(
            templates_dir,
            light_template,
            dark_template,
            dark_time_start,
            dark_time_end,
            is_debug,
        )

    template_file = templates_dir / f"{template_name}.html"
    log_debug(f"模板文件路径: {template_file}")
    log_debug(f"模板文件是否存在: {template_file.exists()}")

    try:
        content = template_file.read_text(encoding="utf-8")
        log_debug(f"成功读取模板文件，内容长度: {len(content)} 字符")
        return content
    except Exception as e:
        logger.error(f"[helpmenu] 读取模板文件 {template_file} 失败: {e}")
        log_debug(f"模板文件读取异常: {type(e).__name__}: {e}")
        return "模板读取失败"


def mode_display_name(mode: str) -> str:
    """返回模式的显示名称。"""
    if mode == MODE_API:
        return "API 模式"
    return "元数据模式"


async def render_help_page_as_image(
    html_render_func,
    templates_dir: Path,
    cards: tuple[dict[str, object], ...],
    warning: str,
    page: int,
    total_pages: int,
    total_items: int,
    last_update: str,
    source_mode: str,
    light_template: str | None = None,
    dark_template: str | None = None,
    dark_time_start: str = "18:00",
    dark_time_end: str = "06:00",
    is_debug: bool = False,
) -> str:
    """渲染帮助页面为图片。"""

    def log_debug(message: str) -> None:
        if is_debug:
            logger.info(f"[helpmenu][debug] {message}")

    data = {
        "subtitle": (
            f"第 {page}/{total_pages} 页 | 命令数: {total_items} | "
            f"来源: {mode_display_name(source_mode)} | "
            f"文档更新时间: {last_update}"
        ),
        "warning": warning.strip(),
        "cards": cards,
    }

    # Debug logging for image rendering
    log_debug(f"开始渲染帮助菜单图片: 第 {page}/{total_pages} 页")
    log_debug(f"卡片数量: {len(cards)}")
    log_debug(
        f"渲染选项: {json.dumps(DEFAULT_IMAGE_RENDER_OPTIONS, ensure_ascii=False)}"
    )

    template_name = get_image_template_name(
        templates_dir,
        light_template,
        dark_template,
        dark_time_start,
        dark_time_end,
        is_debug,
    )
    log_debug(f"使用的模板: {template_name}")

    try:
        template_content = get_image_template(
            templates_dir,
            template_name,
            light_template,
            dark_template,
            dark_time_start,
            dark_time_end,
            is_debug,
        )
        log_debug(f"模板内容长度: {len(template_content)} 字符")

        # 调用 html_render 生成图片
        log_debug("调用 html_render 开始渲染...")
        result = await html_render_func(
            template_content,
            data,
            DEFAULT_IMAGE_RENDER_OPTIONS,
        )

        log_debug(f"图片渲染成功, 返回类型: {type(result).__name__}")
        if isinstance(result, str):
            log_debug(f"图片URL/路径长度: {len(result)} 字符")
            if not result:
                raise ValueError("html_render 返回了空字符串")
            return result
        elif result is None:
            raise ValueError("html_render 返回了 None")
        else:
            # 如果返回的是其他类型，尝试转换为字符串
            result_str = str(result)
            log_debug(f"将返回值转换为字符串，长度: {len(result_str)} 字符")
            return result_str
    except Exception as exc:
        log_debug(f"图片渲染失败: {type(exc).__name__}: {exc}")
        log_debug(f"渲染数据详情 - subtitle: {data.get('subtitle')}")
        log_debug(f"渲染数据详情 - warning: {data.get('warning')}")
        log_debug(f"渲染数据详情 - cards 数量: {len(cards)}")
        raise
