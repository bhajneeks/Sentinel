"""
Run once to save logged-in browser state for X and LinkedIn.

Usage:
    .venv/bin/python3 save_auth.py x
    .venv/bin/python3 save_auth.py linkedin
"""
import asyncio
import sys
from pathlib import Path

from browser_use import Browser

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

    browser = Browser(headless=False)
    await browser.start()
    await browser.navigate_to(URLS[platform])

    print(f"\nLog in to {platform} in the browser window, then press Enter here...")
    await asyncio.get_event_loop().run_in_executor(None, input)

    await browser.export_storage_state(output_path=state_file)
    print(f"Saved auth state to {state_file}")
    await browser.stop()


if __name__ == "__main__":
    if len(sys.argv) != 2:
        print("Usage: .venv/bin/python3 save_auth.py <x|linkedin>")
        sys.exit(1)
    asyncio.run(main(sys.argv[1]))
