import asyncio
import os
from datetime import datetime

from collector import run

INTERVAL_MINUTES = int(os.getenv("MSW_INTERVAL_MINUTES", "30"))

async def main():
    while True:
        print(f"\n=== {datetime.now():%Y-%m-%d %H:%M:%S} 수집 시작 ===")
        try:
            await run(headless=True)
        except Exception as e:
            print("수집 중 오류:", e)
        print(f"다음 수집: {INTERVAL_MINUTES}분 후")
        await asyncio.sleep(INTERVAL_MINUTES * 60)

if __name__ == "__main__":
    asyncio.run(main())
