"""Utilities for rendering checks and interactive test runs."""

from __future__ import annotations

import asyncio
import json
from pathlib import Path
from typing import Any

import aiohttp
from astrbot.core.utils.http_ssl import build_tls_connector

FALLBACK_T2I_ENDPOINT = "https://t2i.soulter.top/text2img"
DEFAULT_RENDER_OPTIONS = {
    "type": "png",
    "full_page": False,
    "omit_background": True,
    "animations": "disabled",
    "caret": "hide",
    "scale": 1.0,
}
DEFAULT_TEMPLATE_NAMES = (
    "classic.html",
    "classic_dark.html",
    "sakura.html",
    "sakura_dark.html",
    "compact.html",
    "compact_dark.html",
    "frost.html",
    "frost_dark.html",
    "ember_industrial.html",
    "ember_industrial_dark.html",
)


def get_templates_dir() -> Path:
    """Return the template directory path."""
    return Path(__file__).resolve().parent / "templates"


def load_template(template_name: str, templates_dir: Path | None = None) -> str:
    """Load template content by file name."""
    resolved_templates_dir = templates_dir or get_templates_dir()
    template_path = resolved_templates_dir / template_name
    if not template_path.exists():
        raise FileNotFoundError(f"Template not found: {template_path}")
    return template_path.read_text(encoding="utf-8")


def get_sample_data() -> dict[str, Any]:
    """Return shared sample data for rendering checks."""
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
                        "args": [{"name": "test", "detail": "测试参数"}],
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
                "commands": [{"name": "status", "description": "查看状态"}],
            },
            {
                "plugin": "插件3",
                "commands": [
                    {
                        "name": "help",
                        "description": "显示帮助信息",
                        "args": [{"name": "plugin", "detail": "插件名称"}],
                    }
                ],
            },
        ],
    }


def render_template(template_content: str, data: dict[str, Any]) -> str:
    """Render HTML using Jinja2 template syntax."""
    import jinja2

    template = jinja2.Template(template_content)
    return template.render(**data)


async def render_all_templates_locally(
    output_dir: Path | None = None,
) -> list[tuple[str, bool, str]]:
    """Render all templates locally with Playwright screenshots."""
    from playwright.async_api import async_playwright

    save_dir = output_dir or (Path(__file__).resolve().parent / "tests" / "output")
    save_dir.mkdir(parents=True, exist_ok=True)
    data = {
        "subtitle": "第 1/1 页 | 命令数: 5 | 来源: 元数据模式 | 文档更新时间: 2026-03-11",
        "warning": "",
        "cards": [
            {
                "plugin": "TestPlugin",
                "continued": False,
                "commands": [
                    {
                        "name": "hello",
                        "description": "Say hello to the bot",
                        "args": [],
                        "aliases": "hi",
                    },
                    {
                        "name": "calc",
                        "description": "Calculate a math expression",
                        "args": [
                            {
                                "name": "expression",
                                "detail": "The math expression to evaluate",
                            }
                        ],
                        "aliases": "",
                    },
                ],
            }
        ],
    }

    results: list[tuple[str, bool, str]] = []
    async with async_playwright() as p:
        browser = await p.chromium.launch()
        page = await browser.new_page(device_scale_factor=2.0)

        for template_file in get_templates_dir().glob("*.html"):
            html = render_template(template_file.read_text(encoding="utf-8"), data)
            test_html_path = save_dir / f"{template_file.stem}_test.html"
            test_html_path.write_text(html, encoding="utf-8")
            try:
                await page.goto(f"file://{test_html_path.absolute()}")
                await asyncio.sleep(0.5)
                container = await page.query_selector("div[style*='width:fit-content']")
                if container:
                    await container.screenshot(
                        path=str(save_dir / f"{template_file.stem}.png"),
                        omit_background=True,
                    )
                    print(f"✅ Rendered {template_file.stem}")
                    results.append((template_file.name, True, "ok"))
                else:
                    msg = "container not found"
                    print(f"❌ Failed to find container in {template_file.stem}")
                    results.append((template_file.name, False, msg))
            except Exception as exc:  # noqa: BLE001
                print(f"❌ Error rendering {template_file.stem}: {exc}")
                results.append((template_file.name, False, str(exc)))

        await browser.close()

    return results


async def run_t2i_endpoint_check(
    templates: tuple[str, ...] | None = None,
    endpoint: str = FALLBACK_T2I_ENDPOINT,
) -> list[tuple[str, str | None]]:
    """Render templates through fallback t2i endpoint and download images."""
    selected_templates = templates or DEFAULT_TEMPLATE_NAMES
    tmpl_data = get_sample_data()
    headers = {"Accept-Encoding": "gzip, deflate"}

    print(f"Testing endpoint: {endpoint}")
    print(f"Templates to test: {len(selected_templates)}")
    print("=" * 60)

    async with aiohttp.ClientSession(
        trust_env=True,
        connector=build_tls_connector(),
        headers=headers,
    ) as session:
        results: list[tuple[str, str | None]] = []
        for template_name in selected_templates:
            print(f"\n[Rendering] Template: {template_name}")
            tmpl_str = load_template(template_name)
            post_data = {
                "tmpl": tmpl_str,
                "json": "true",
                "tmpldata": tmpl_data,
                "options": {"full_page": False, "type": "png", "omit_background": True},
            }
            async with session.post(f"{endpoint}/generate", json=post_data) as resp:
                print(f"  Status: {resp.status}")
                if resp.status != 200:
                    text = await resp.text()
                    print(f"  Error: {text[:200]}")
                    results.append((template_name, None))
                    continue

                data = await resp.json()
                if "data" not in data or "id" not in data["data"]:
                    print("  Error: No image ID in response")
                    results.append((template_name, None))
                    continue

                image_url = f"{endpoint}/{data['data']['id']}"
                tmp_dir = Path(__file__).resolve().parent / "tests" / "tmp"
                tmp_dir.mkdir(parents=True, exist_ok=True)
                image_filename = f"{template_name.replace('.html', '')}_{data['data']['id'].split('/')[-1]}"
                image_path = tmp_dir / image_filename
                async with session.get(image_url) as img_resp:
                    if img_resp.status == 200:
                        image_path.write_bytes(await img_resp.read())
                        print(f"  Saved: {image_path}")
                        results.append((template_name, str(image_path)))
                    else:
                        print(f"  Failed to download: {img_resp.status}")
                        results.append((template_name, None))

    print("\n" + "=" * 60)
    print("Summary:")
    success = sum(1 for _, path in results if path)
    print(f"  Success: {success}/{len(selected_templates)}")
    for template_name, image_path in results:
        status = "OK" if image_path else "FAIL"
        print(f"    [{status}] {template_name}")
    print("=" * 60)

    return results


async def render_with_fallback_t2i(
    template_content: str,
    tmpl_data: dict[str, Any],
    log_debug_callback=None,
) -> tuple[str, str]:
    """Render image by the online fallback t2i service."""

    def _log(msg: str) -> None:
        if log_debug_callback:
            log_debug_callback(msg)

    _log(f"Using fallback t2i service: {FALLBACK_T2I_ENDPOINT}")

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

    _log(
        f"Fallback request payload size: {len(json.dumps(post_data, ensure_ascii=False))}"
    )

    async with aiohttp.ClientSession(
        trust_env=True,
        connector=build_tls_connector(),
        headers={"Accept-Encoding": "gzip, deflate"},
    ) as session:
        async with session.post(
            f"{FALLBACK_T2I_ENDPOINT}/generate", json=post_data
        ) as resp:
            _log(f"Fallback response status: {resp.status}")
            if resp.status != 200:
                text = await resp.text()
                raise RuntimeError(
                    f"Fallback service error (HTTP {resp.status}): {text[:200]}"
                )

            data = await resp.json()
            if "data" not in data or "id" not in data["data"]:
                raise RuntimeError("No image ID found in fallback response")

            image_url = f"{FALLBACK_T2I_ENDPOINT}/{data['data']['id']}"
            return image_url, "系统文转图失败，已切换到备用文转图服务生成图片"


async def run_image_test_command(
    html_render_func,
    templates_dir: Path,
    config,
    is_debug_enabled: bool,
    log_debug_callback=None,
) -> tuple[str, str]:
    """Execute the imageTest flow used by /helpMenu imageTest."""

    def _log(msg: str) -> None:
        if log_debug_callback:
            log_debug_callback(msg)

    from .image_renderer import get_image_template
    from .page_builder import CommandDocItem, build_image_pages

    sample_items = [
        CommandDocItem(
            plugin_name="插件1",
            command="hello",
            description="你好呀",
            aliases=[],
            permission="everyone",
        ),
        CommandDocItem(
            plugin_name="插件1",
            command="search",
            description="搜索内容",
            aliases=[],
            permission="everyone",
        ),
        CommandDocItem(
            plugin_name="插件2",
            command="status",
            description="查看状态",
            aliases=[],
            permission="everyone",
        ),
    ]

    image_pages = build_image_pages(sample_items)
    if not image_pages:
        raise ValueError("无法生成测试图片：无可用命令")

    template_name = (
        config.get("light_template") or config.get("image_template") or "classic"
    )
    _log(f"Using template: {template_name}")

    template_content = get_image_template(
        templates_dir,
        template_name,
        config.get("light_template"),
        config.get("dark_template"),
        str(config.get("dark_time_start", "18:00")),
        str(config.get("dark_time_end", "06:00")),
        is_debug_enabled,
    )

    render_data = {
        "subtitle": "文转图测试 | 第 1/1 页 | 命令数: 3 | 文档更新时间: 2026-03-14",
        "warning": "",
        "cards": image_pages[0],
    }

    try:
        image_url = await html_render_func(
            template_content,
            render_data,
            options=DEFAULT_RENDER_OPTIONS,
        )
        if not image_url:
            raise ValueError("html_render returned an empty image URL")
        return image_url, ""
    except Exception as primary_exc:  # noqa: BLE001
        _log(f"System renderer failed: {type(primary_exc).__name__}: {primary_exc}")
        return await render_with_fallback_t2i(
            template_content, render_data, log_debug_callback
        )


def _print_tui_menu() -> None:
    print("\n=== HelpMenu Render Toolkit ===")
    print("1) Run local Playwright render check")
    print("2) Run fallback t2i endpoint check")
    print("3) Run both checks")
    print("4) Run fallback check with one template")
    print("q) Quit")


def _ask_single_template() -> tuple[str, ...] | None:
    print("Available templates:")
    for index, template_name in enumerate(DEFAULT_TEMPLATE_NAMES, start=1):
        print(f"  {index}. {template_name}")

    raw = input("Choose template index: ").strip()
    if not raw.isdigit():
        print("Invalid index")
        return None

    index = int(raw)
    if index < 1 or index > len(DEFAULT_TEMPLATE_NAMES):
        print("Index out of range")
        return None

    return (DEFAULT_TEMPLATE_NAMES[index - 1],)


async def run_tui() -> None:
    """Run an interactive TUI for manual rendering checks."""
    while True:
        _print_tui_menu()
        choice = input("Select an option: ").strip().lower()

        if choice == "1":
            results = await render_all_templates_locally()
            success = sum(1 for _, ok, _ in results if ok)
            print(f"[Local Render] Success: {success}/{len(results)}")
        elif choice == "2":
            await run_t2i_endpoint_check()
        elif choice == "3":
            results = await render_all_templates_locally()
            success = sum(1 for _, ok, _ in results if ok)
            print(f"[Local Render] Success: {success}/{len(results)}")
            await run_t2i_endpoint_check()
        elif choice == "4":
            selected = _ask_single_template()
            if selected is not None:
                await run_t2i_endpoint_check(selected)
        elif choice == "q":
            print("Bye")
            return
        else:
            print("Unknown option")


if __name__ == "__main__":
    asyncio.run(run_tui())
