"""Test script for t2i endpoint with templates from project."""

import asyncio
import sys
from pathlib import Path

# Add project root to path
sys.path.insert(0, str(Path(__file__).parent.parent.parent.parent.parent))

import aiohttp
from astrbot.core.utils.http_ssl import build_tls_connector


def load_template(template_name: str) -> str:
    """Load template from templates folder."""
    templates_dir = Path(__file__).parent.parent / "templates"
    template_path = templates_dir / template_name
    if not template_path.exists():
        raise FileNotFoundError(f"Template not found: {template_path}")
    return template_path.read_text(encoding="utf-8")


def get_sample_data() -> dict:
    """Get sample data for template rendering."""
    return {
        "subtitle": "使用 /help 获取帮助",
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


async def render_template(
    session: aiohttp.ClientSession, endpoint: str, template_name: str, tmpl_data: dict
) -> str:
    """Render a template and download the image. Returns the saved image path."""
    print(f"\n[Rendering] Template: {template_name}")

    # Load template
    tmpl_str = load_template(template_name)

    # Prepare request data
    post_data = {
        "tmpl": tmpl_str,
        "json": "true",
        "tmpldata": tmpl_data,
        "options": {"full_page": False, "type": "png", "omit_background": True},
    }

    # Request image generation
    async with session.post(f"{endpoint}/generate", json=post_data) as resp:
        print(f"  Status: {resp.status}")

        if resp.status != 200:
            text = await resp.text()
            print(f"  Error: {text[:200]}")
            return None

        data = await resp.json()
        print(f"  Response: {data}")

        if "data" not in data or "id" not in data["data"]:
            print("  Error: No image ID in response")
            return None

        image_url = f"{endpoint}/{data['data']['id']}"
        print(f"  Image URL: {image_url}")

        # Download image
        tmp_dir = Path(__file__).parent / "tmp"
        tmp_dir.mkdir(exist_ok=True)

        image_filename = (
            f"{template_name.replace('.html', '')}_{data['data']['id'].split('/')[-1]}"
        )
        image_path = tmp_dir / image_filename

        async with session.get(image_url) as img_resp:
            if img_resp.status == 200:
                with open(image_path, "wb") as f:
                    f.write(await img_resp.read())
                print(f"  Saved: {image_path}")
                return str(image_path)
            else:
                print(f"  Failed to download: {img_resp.status}")
                return None


async def test_t2i_endpoint():
    """Test the t2i endpoint with project templates."""

    endpoint = "https://t2i.soulter.top/text2img"
    tmpl_data = get_sample_data()

    # Available templates
    templates = [
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
    ]

    print(f"Testing endpoint: {endpoint}")
    print(f"Templates to test: {len(templates)}")
    print("=" * 60)

    headers = {
        "Accept-Encoding": "gzip, deflate",
    }

    async with aiohttp.ClientSession(
        trust_env=True,
        connector=build_tls_connector(),
        headers=headers,
    ) as session:
        results = []
        for template_name in templates:
            try:
                path = await render_template(
                    session, endpoint, template_name, tmpl_data
                )
                results.append((template_name, path))
            except Exception as e:
                print(f"  Exception: {e}")
                results.append((template_name, None))

    print("\n" + "=" * 60)
    print("Summary:")
    success = sum(1 for _, p in results if p)
    print(f"  Success: {success}/{len(templates)}")
    for name, path in results:
        status = "OK" if path else "FAIL"
        print(f"    [{status}] {name}")
    print("=" * 60)


if __name__ == "__main__":
    asyncio.run(test_t2i_endpoint())
