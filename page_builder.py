import re
from collections import defaultdict
from dataclasses import dataclass


@dataclass(slots=True)
class CommandDocItem:
    plugin_name: str
    command: str
    description: str
    aliases: list[str]
    permission: str = "everyone"


def extract_arg_lines(description: str) -> tuple[str, list[dict[str, str]]]:
    """从描述中提取参数行。"""
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


def mode_display_name(mode: str, mode_api: str = "api") -> str:
    """返回模式的显示名称。"""
    if mode == mode_api:
        return "API 模式"
    return "元数据模式"


def build_pages(
    items: list[CommandDocItem],
    total_items: int,
    last_update: str,
    source_mode: str,
    mode_api: str = "api",
    page_size: int = 32,
) -> list[str]:
    """构建文本帮助页面。"""
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
                _, args = extract_arg_lines(entry.description)
                estimated_units = 1 + (1 if entry.aliases else 0) + len(args)
                if current_units + estimated_units > page_size and current_units > 1:
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
                f"来源: {mode_display_name(source_mode, mode_api)} | "
                f"文档更新时间: {last_update}"
            ),
            "用法: /helpMenu <页码|next|prev> | /updateHelpMenu（仅限管理员）",
            "",
            *block_lines,
        ]
        pages.append("\n".join(lines).strip())
    return pages


def build_image_pages(
    items: list[CommandDocItem],
    page_size: int = 42,
    card_size: int = 14,
) -> list[tuple[dict[str, object], ...]]:
    """构建图片帮助页面数据结构。"""
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
                clean_desc, args = extract_arg_lines(entry.description)
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
