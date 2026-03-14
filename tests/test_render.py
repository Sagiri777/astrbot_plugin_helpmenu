import asyncio
from pathlib import Path
from playwright.async_api import async_playwright

TEMPLATES_DIR = Path(__file__).parent.parent / "templates"
OUTPUT_DIR = Path(__file__).parent / "output"
OUTPUT_DIR.mkdir(exist_ok=True)

test_data = {
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


def render_template(template_content: str, data: dict) -> str:
    # A simple jinja2-like replacement for testing
    import jinja2

    template = jinja2.Template(template_content)
    return template.render(**data)


async def main():
    async with async_playwright() as p:
        browser = await p.chromium.launch()
        page = await browser.new_page(device_scale_factor=2.0)

        for template_file in TEMPLATES_DIR.glob("*.html"):
            content = template_file.read_text(encoding="utf-8")
            html = render_template(content, test_data)

            # Save the test html
            test_html_path = OUTPUT_DIR / f"{template_file.stem}_test.html"
            test_html_path.write_text(html, encoding="utf-8")

            # Try to screenshot it
            try:
                await page.goto(f"file://{test_html_path.absolute()}")
                # Wait for any potential blur filters to render
                await asyncio.sleep(0.5)

                # Get the main container to screenshot just that part
                container = await page.query_selector("div[style*='width:fit-content']")
                if container:
                    await container.screenshot(
                        path=str(OUTPUT_DIR / f"{template_file.stem}.png"),
                        omit_background=True,
                    )
                    print(f"✅ Rendered {template_file.stem}")
                else:
                    print(f"❌ Failed to find container in {template_file.stem}")
            except Exception as e:
                print(f"❌ Error rendering {template_file.stem}: {e}")

        await browser.close()


if __name__ == "__main__":
    asyncio.run(main())
