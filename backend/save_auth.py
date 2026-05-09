"""
Run once to save logged-in browser state for X and LinkedIn.

Usage:
    python save_auth.py x
    python save_auth.py linkedin
"""
import asyncio
import sys
from pathlib import Path
from playwright.async_api import async_playwright

AUTH_DIR = Path(__file__).parent / "auth"
URLS = {
    "x": "https://x.com/login",
    "linkedin": "https://www.linkedin.com/login",
}


async def main(platform: str) -> None:
    if platform not in URLS:
        print(f"Unknown platform '{platform}'. Choose: x, linkedin")
        return

    AUTH_DIR.mkdir(exist_ok=True)
    state_file = AUTH_DIR / f"{platform}_state.json"

    async with async_playwright() as p:
        browser = await p.chromium.launch(headless=False)
        context = await browser.new_context()
        page = await context.new_page()
        await page.goto(URLS[platform])

        print(f"\nLog in to {platform} in the browser window.")
        print("Press Enter here once you are fully logged in...")
        await asyncio.get_event_loop().run_in_executor(None, input)

        await context.storage_state(path=str(state_file))
        print(f"Saved auth state to {state_file}")
        await browser.close()


if __name__ == "__main__":
    if len(sys.argv) != 2:
        print("Usage: python save_auth.py <x|linkedin>")
        sys.exit(1)
    asyncio.run(main(sys.argv[1]))
