import json
from datetime import datetime, timedelta, timezone
from pathlib import Path

from astrbot.api import logger

DEFAULT_IMAGE_RENDER_OPTIONS = {
    "type": "png",
    "full_page": False,
    "animations": "disabled",
    "caret": "hide",
    "omit_background": True,
    "scale": "css",
}

MINIMAL_IMAGE_RENDER_OPTIONS = {
    "type": "png",
    "full_page": False,
    "omit_background": True,
}

LEGACY_IMAGE_RENDER_OPTIONS = {
    "type": "png",
    "full_page": False,
    "animations": "disabled",
    "omit_background": True,
    "caret": "hide",
    "scale": "css",
}

ULTRA_MINIMAL_IMAGE_RENDER_OPTIONS = {
    "type": "png",
}

DEFAULT_IMAGE_TEMPLATE = "classic"
MODE_API = "api"

# Fallback template to keep image rendering working even when template files are
# missing in runtime package deployments.
FALLBACK_IMAGE_TEMPLATES: dict[str, str] = {
    "classic": """
<div style="background:#eef4ff;padding:24px;font-family:'Inter','Segoe UI','PingFang SC','Microsoft YaHei',sans-serif;">
  <div style="max-width:940px;margin:0 auto;background:#fff;border:1px solid #d8e5ff;border-radius:16px;padding:16px;box-sizing:border-box;">
    <div style="font-size:14px;color:#334155;line-height:1.5;font-weight:520;">{{ subtitle }}</div>
    {% if warning %}
    <div style="margin-top:10px;padding:8px 10px;border:1px solid #f5d08a;background:#fff7e8;border-radius:10px;font-size:12px;color:#8a5a00;">{{ warning }}</div>
    {% endif %}
    <div style="margin-top:12px;column-count:2;column-gap:10px;">
      {% for card in cards %}
      <div style="display:block;width:100%;margin:0 0 10px;padding:10px;box-sizing:border-box;break-inside:avoid-column;border:1px solid #dbe7ff;border-radius:12px;background:#f8fbff;">
        <div style="font-size:15px;color:#1e3a8a;font-weight:700;word-break:break-word;">{{ card.plugin }}</div>
        {% if card.continued %}
        <div style="font-size:11px;color:#64748b;margin-top:2px;">本页续接</div>
        {% endif %}
        {% for command in card.commands %}
        <div style="margin-top:6px;padding:6px;border:1px solid #e2e8f0;border-radius:8px;background:#fff;">
          <div style="font-size:13px;color:#0f172a;font-weight:650;">/{{ command.name }}</div>
          <div style="margin-top:3px;font-size:11px;color:#334155;line-height:1.4;">{{ command.description }}</div>
          {% if command.aliases %}
          <div style="margin-top:3px;font-size:10px;color:#64748b;line-height:1.35;">别名: {{ command.aliases }}</div>
          {% endif %}
        </div>
        {% endfor %}
      </div>
      {% endfor %}
    </div>
  </div>
</div>
""",
}


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
    fs_templates: list[str] = []
    if templates_dir.exists():
        fs_templates = [f.stem for f in templates_dir.glob("*.html")]

    # Keep old behavior compatibility: built-in templates should still work
    # even when templates folder is not packaged.
    return sorted(set(fs_templates + list(FALLBACK_IMAGE_TEMPLATES.keys())))


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
        logger.warning(f"[helpmenu] 读取模板文件 {template_file} 失败: {e}")
        log_debug(f"模板文件读取异常: {type(e).__name__}: {e}")

    fallback_template = FALLBACK_IMAGE_TEMPLATES.get(template_name)
    if fallback_template:
        logger.warning(
            "[helpmenu] 已回退到内置 %s 模板，确保文转图功能可用。",
            template_name,
        )
        return fallback_template

    logger.warning(
        "[helpmenu] 未找到模板 %s，对应内置模板也不存在，回退到内置 %s 模板。",
        template_name,
        DEFAULT_IMAGE_TEMPLATE,
    )
    return FALLBACK_IMAGE_TEMPLATES[DEFAULT_IMAGE_TEMPLATE]


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

    def is_http_422_error(exc: Exception) -> bool:
        status = getattr(exc, "status", None)
        if status == 422:
            return True

        status_code = getattr(exc, "status_code", None)
        if status_code == 422:
            return True

        response = getattr(exc, "response", None)
        if response is not None and getattr(response, "status", None) == 422:
            return True

        message = str(exc)
        return "422" in message and "Unprocessable" in message

    def normalize_image_result(result: object) -> str:
        log_debug(f"图片渲染成功, 返回类型: {type(result).__name__}")
        if isinstance(result, str):
            log_debug(f"图片URL/路径长度: {len(result)} 字符")
            if not result:
                raise ValueError("html_render 返回了空字符串")
            return result
        if result is None:
            raise ValueError("html_render 返回了 None")

        result_str = str(result)
        log_debug(f"将返回值转换为字符串，长度: {len(result_str)} 字符")
        return result_str

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
        option_attempts = [
            ("default", DEFAULT_IMAGE_RENDER_OPTIONS),
            ("minimal", MINIMAL_IMAGE_RENDER_OPTIONS),
            ("legacy", LEGACY_IMAGE_RENDER_OPTIONS),
            ("ultra_minimal", ULTRA_MINIMAL_IMAGE_RENDER_OPTIONS),
        ]

        last_error: Exception | None = None
        result = None
        for attempt_name, options in option_attempts:
            try:
                log_debug(
                    f"尝试文转图参数[{attempt_name}]: {json.dumps(options, ensure_ascii=False)}"
                )
                result = await html_render_func(
                    template_content,
                    data,
                    options=options,
                )
                break
            except Exception as exc:  # noqa: BLE001
                last_error = exc
                if is_http_422_error(exc):
                    logger.warning(
                        "[helpmenu] 文转图参数[%s]被端点拒绝(422)，尝试下一组兼容参数。",
                        attempt_name,
                    )
                else:
                    logger.warning(
                        "[helpmenu] 文转图参数[%s]渲染失败: %s: %s，尝试下一组参数。",
                        attempt_name,
                        type(exc).__name__,
                        exc,
                    )
                log_debug(f"参数[{attempt_name}]失败: {type(exc).__name__}: {exc}")
        if result is None:
            if last_error is not None:
                raise last_error
            raise RuntimeError("html_render 未返回结果")

        return normalize_image_result(result)
    except Exception as exc:
        log_debug(f"图片渲染失败: {type(exc).__name__}: {exc}")
        log_debug(f"渲染数据详情 - subtitle: {data.get('subtitle')}")
        log_debug(f"渲染数据详情 - warning: {data.get('warning')}")
        log_debug(f"渲染数据详情 - cards 数量: {len(cards)}")
        raise
