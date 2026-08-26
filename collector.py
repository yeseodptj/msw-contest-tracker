from __future__ import annotations

import argparse
import asyncio
import re
from datetime import datetime, timezone, timedelta
from urllib.parse import urljoin, urlparse

from playwright.async_api import async_playwright, Page

from db import save_batch

BASE = "https://maplestoryworlds.nexon.com"
PLAY_URL = f"{BASE}/ko/play"
CONTEST_URL = f"{BASE}/events/ko/2026globalcontest"
KST = timezone(timedelta(hours=9))


def now_kst_iso() -> str:
    return datetime.now(KST).replace(microsecond=0).isoformat()


def compact_number(value: str | None) -> int | None:
    if not value:
        return None
    raw = value.strip().replace(",", "").lower()
    m = re.search(r"([0-9]+(?:\.[0-9]+)?)\s*([km]?)", raw)
    if not m:
        return None
    number = float(m.group(1))
    suffix = m.group(2)
    if suffix == "k":
        number *= 1_000
    elif suffix == "m":
        number *= 1_000_000
    return int(round(number))


def one(text: str, patterns: list[str], flags=re.I | re.M) -> str | None:
    for p in patterns:
        m = re.search(p, text, flags)
        if m:
            for g in m.groups():
                if g is not None:
                    return g.strip()
    return None


def extract_world_id(url: str) -> str | None:
    m = re.search(r"/play/([0-9a-fA-F]{20,64})", url)
    return m.group(1) if m else None


def clean_title(title: str | None) -> str | None:
    if not title:
        return None
    title = re.sub(r"\s*[:|]\s*MapleStory Worlds.*$", "", title, flags=re.I).strip()
    return title or None


async def extract_top_counters(page: Page) -> tuple[str | None, str | None]:
    """
    Read the two numeric counters rendered at the upper-right of the world detail card.
    Current MSW UI visually renders:
      [person icon] <total players>   [heart icon] <heart count>
    The icons do not reliably expose Korean text labels in body.inner_text(), so we
    use visible leaf text nodes + geometry instead of label regexes.
    """
    try:
        data = await page.evaluate("""
        () => {
          const isVisible = (el) => {
            const r = el.getBoundingClientRect();
            const st = getComputedStyle(el);
            return r.width > 0 && r.height > 0 &&
                   st.visibility !== 'hidden' && st.display !== 'none' &&
                   Number(st.opacity || 1) !== 0;
          };

          const headings = [...document.querySelectorAll('h1,h2,h3')]
            .filter(isVisible)
            .filter(el => {
              const t = (el.innerText || '').trim();
              return t && !t.includes('Privacy Preference Center');
            });

          const h = headings[0];
          if (!h) return [];

          const hr = h.getBoundingClientRect();
          const walker = document.createTreeWalker(
            document.body,
            NodeFilter.SHOW_TEXT,
            {
              acceptNode(node) {
                const t = (node.nodeValue || '').trim();
                if (!/^\\d[\\d,.]*[kKmM]?$/.test(t)) return NodeFilter.FILTER_REJECT;
                const el = node.parentElement;
                if (!el || !isVisible(el)) return NodeFilter.FILTER_REJECT;
                return NodeFilter.FILTER_ACCEPT;
              }
            }
          );

          const rows = [];
          while (walker.nextNode()) {
            const node = walker.currentNode;
            const el = node.parentElement;
            const r = el.getBoundingClientRect();
            // Counters sit on the same upper card band as the title, to its right.
            // Keep a generous vertical band but exclude lower metadata such as max players/age.
            if (r.top >= hr.top - 40 &&
                r.top <= hr.bottom + 95 &&
                r.left > hr.left + Math.min(300, hr.width * 0.45)) {
              rows.push({
                text: (node.nodeValue || '').trim(),
                x: r.left,
                y: r.top,
                area: r.width * r.height
              });
            }
          }

          // Text nodes may appear more than once through wrappers; unique by text+position.
          const seen = new Set();
          const uniq = [];
          for (const row of rows.sort((a,b) => a.x-b.x || a.y-b.y)) {
            const key = row.text + ':' + Math.round(row.x/3) + ':' + Math.round(row.y/3);
            if (!seen.has(key)) {
              seen.add(key);
              uniq.push(row);
            }
          }
          return uniq;
        }
        """)
        vals = []
        for row in data:
            t = str(row.get("text", "")).strip()
            if re.fullmatch(r"\d[\d,.]*[kKmM]?", t):
                vals.append(t)
        if len(vals) >= 2:
            return vals[0], vals[1]
        if len(vals) == 1:
            return vals[0], None
    except Exception:
        pass
    return None, None


async def extract_english_detail_metrics(context, world_id: str) -> tuple[str | None, str | None]:
    """
    MSW's English detail page exposes stable text labels such as:
      4.2k
      Total Players
      299
      Notifications ON
    Use it as a fallback for the top counters.
    """
    page = await context.new_page()
    try:
        url = f"{BASE}/en/play/{world_id}/"
        await page.goto(url, wait_until="domcontentloaded", timeout=60_000)
        await page.wait_for_timeout(700)
        body = await page.locator("body").inner_text(timeout=15_000)
        players = one(body, [
            r"([0-9][0-9.,]*\\s*[kKmM]?)\\s*\\n+\\s*Total\\s*Players",
            r"Total\\s*Players\\s*\\n+\\s*([0-9][0-9.,]*\\s*[kKmM]?)",
        ])
        hearts = one(body, [
            r"([0-9][0-9.,]*\\s*[kKmM]?)\\s*\\n+\\s*Notifications?\\s*ON",
            r"Notifications?\\s*ON\\s*\\n+\\s*([0-9][0-9.,]*\\s*[kKmM]?)",
        ])
        return players, hearts
    except Exception:
        return None, None
    finally:
        await page.close()


async def discover_world_urls(page: Page, debug: bool = False) -> list[str]:
    """
    공식 글로벌 개발 콘테스트 페이지에서
    '모든 참가 월드 보러가기'를 통해 참가작 목록으로 이동한 뒤,
    참가 월드의 /play/<world_id> 링크만 수집한다.
    """

    print("[목록] 글로벌 개발 콘테스트 공식 페이지 접속")
    await page.goto(
        CONTEST_URL,
        wait_until="domcontentloaded",
        timeout=90_000,
    )
    await page.wait_for_timeout(2500)

    # 공식 페이지 하단의 '모든 참가 월드 보러가기' 클릭
    button = page.get_by_text("모든 참가 월드 보러가기", exact=False)

    if await button.count() == 0:
        raise RuntimeError(
            "'모든 참가 월드 보러가기' 버튼을 찾지 못했습니다."
        )

    print("[목록] 모든 참가 월드 목록 열기")
    await button.first.evaluate("""
        el => {
        const target = el.closest('a, button') || el;
        target.click();
        }
    """)

    # 목록 페이지가 완전히 열린 뒤 수집
    await page.wait_for_timeout(3000)

    print(f"[목록] 이동된 주소: {page.url}")

    found: dict[str, str] = {}

    last_count = 0
    stagnant = 0

    # 무한 스크롤 대응
    for _ in range(120):
        hrefs = await page.locator(
            'a[href*="/play/"]'
        ).evaluate_all(
            "els => els.map(e => e.href).filter(Boolean)"
        )

        for href in hrefs:
            wid = extract_world_id(href)

            if wid:
                # 상세 페이지는 반드시 한국어 주소로 통일
                found[wid] = f"{BASE}/ko/play/{wid}/"

        current_count = len(found)

        print(
            f"[목록] 현재 발견된 참가 월드: {current_count}개",
            end="\r",
        )

        if current_count == last_count:
            stagnant += 1
        else:
            stagnant = 0
            last_count = current_count

        # 스크롤을 여러 번 해도 더 이상 월드가 안 늘어나면 종료
        if stagnant >= 8:
            break

        await page.evaluate(
            "window.scrollTo(0, document.body.scrollHeight)"
        )
        await page.wait_for_timeout(900)

    print()
    print(f"[목록] 최종 발견: {len(found)}개")

    if len(found) < 90:
        print(
            "[경고] 참가작 수가 예상보다 적습니다. "
            "현재 콘테스트 페이지 로딩 상태를 확인하세요."
        )

    if debug:
        for url in list(found.values())[:10]:
            print("  ", url)

    return list(found.values())


async def scrape_world(page: Page, context, url: str, debug: bool = False) -> dict | None:
    world_id = extract_world_id(url)
    if not world_id:
        return None

    try:
        await page.goto(url, wait_until="domcontentloaded", timeout=60_000)
        await page.wait_for_timeout(1200)
        body = await page.locator("body").inner_text(timeout=15_000)
    except Exception as e:
        print(f"[실패] {world_id}: {e}")
        return None

    # OneTrust cookie banner has an H1 named "Privacy Preference Center".
    # Never use the first H1/H2 as the world name. Prefer page metadata,
    # which is tied to the world detail page and is not affected by overlays.
    title = None
    for selector, attr in [
        ('meta[property="og:title"]', "content"),
        ('meta[name="twitter:title"]', "content"),
    ]:
        try:
            loc = page.locator(selector)
            if await loc.count():
                candidate = clean_title(await loc.first.get_attribute(attr))
                if candidate and "Privacy Preference Center" not in candidate:
                    title = candidate
                    break
        except Exception:
            pass

    if not title:
        candidate = clean_title(await page.title())
        if candidate and "Privacy Preference Center" not in candidate:
            title = candidate

    if not title:
        # Last-resort visible heading, with known cookie/privacy UI excluded.
        blocked = {"플레이", "Play", "댓글", "Comments", "Privacy Preference Center"}
        for sel in ["h1", "h2", "h3"]:
            try:
                vals = [v.strip() for v in await page.locator(sel).all_inner_texts() if v.strip()]
                vals = [v for v in vals if v not in blocked and "Privacy Preference" not in v]
                if vals:
                    title = vals[0]
                    break
            except Exception:
                pass

    title = title or world_id

    # First try the actual visible counter pills at the upper-right of the card.
    ui_players_raw, ui_heart_raw = await extract_top_counters(page)

    # Also read the English detail page as a fallback because it exposes textual labels
    # such as "Total Players" and "Notifications ON".
    en_players_raw, en_heart_raw = await extract_english_detail_metrics(context, world_id)

    players_raw = ui_players_raw or en_players_raw
    heart_raw = ui_heart_raw or en_heart_raw

    # Some pages may expose like/rating metadata through accessibility strings.
    likes_rate_raw = None
    try:
        accessibility_texts = await page.locator(
            '[aria-label], [title]'
        ).evaluate_all(
            """els => els.map(e => [
                e.getAttribute('aria-label') || '',
                e.getAttribute('title') || ''
            ].join(' ')).filter(Boolean).join('\\n')"""
        )
        likes_rate_raw = one(accessibility_texts, [
            r"(?:좋아요율|좋아요|Likes?|Rating)\D{0,20}([0-9]+(?:\.[0-9]+)?)%",
            r"([0-9]+(?:\.[0-9]+)?)%\D{0,20}(?:좋아요|Likes?|Rating)",
        ])
    except Exception:
        pass

    # Comments are still text-based when a count is exposed on the tab.
    comments_raw = one(body, [
        r"(?:댓글|Comments)\s*\(?\s*([0-9][0-9.,]*\s*[kKmM]?)\s*\)?",
    ])

    # The visible star/favorite button itself has no count in the current detail UI.
    favorites_raw = None
    likes_count_raw = heart_raw

    release_date = one(body, [
        r"(?:출시일|Release\s*Date)\s*[:：]?\s*([^\n]+)",
    ])
    updated_date = one(body, [
        r"(?:최근\s*업데이트|업데이트일|업데이트|Last\s*Updated|Update\s*Date)\s*[:：]?\s*([^\n]+)",
    ])
    genre = one(body, [
        r"(?:장르|Genre)\s*[:：]?\s*([^\n]+)",
    ])
    max_players_raw = one(body, [
        r"(?:최대\s*플레이어|Max(?:imum)?\s*Players?)\s*[:：]?\s*([0-9]+)\s*명?",
    ])

    creator = one(body, [
        r"(?:제작자|Creator)\s*\n?\s*([^\n]+)",
    ])

    platforms = []
    # 플랫폼 섹션이 있으면 PC/Mobile만 기록. 본문 전체에서의 오탐을 줄이기 위해 라벨 주변도 허용.
    if re.search(r"\bPC\b", body, re.I):
        platforms.append("PC")
    if re.search(r"모바일|Mobile", body, re.I):
        platforms.append("Mobile")

    thumbnail_url = None
    try:
        og = page.locator('meta[property="og:image"]')
        if await og.count():
            thumbnail_url = await og.first.get_attribute("content")
    except Exception:
        pass

    result = {
        "world_id": world_id,
        "name": title,
        "creator": creator,
        "genre": genre,
        "release_date": release_date,
        "updated_date": updated_date,
        "url": url,
        "thumbnail_url": thumbnail_url,
        "max_players": int(max_players_raw) if max_players_raw and max_players_raw.isdigit() else None,
        "platforms": ", ".join(platforms) if platforms else None,
        "total_players": compact_number(players_raw),
        "favorites": compact_number(favorites_raw),
        "likes_count": compact_number(likes_count_raw),
        "likes_rate": float(likes_rate_raw) if likes_rate_raw else None,
        "comments": compact_number(comments_raw),
        "observed_at": now_kst_iso(),
    }

    if debug:
        print("[DEBUG]", result)
    else:
        print(
            f"[수집] {result['name'][:35]:35} | "
            f"players={result['total_players']} hearts={result['likes_count']} "
            f"like_rate={result['likes_rate']} comments={result['comments']}"
        )
    return result


async def run(headless: bool = True, limit: int | None = None, debug: bool = False) -> int:
    async with async_playwright() as p:
        browser = await p.chromium.launch(headless=headless)
        context = await browser.new_context(
            locale="ko-KR",
            user_agent=(
                "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
                "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/151 Safari/537.36"
            ),
        )
        list_page = await context.new_page()
        urls = await discover_world_urls(list_page, debug=debug)
        if limit:
            urls = urls[:limit]
        print(f"\n총 {len(urls)}개 월드를 수집합니다.\n")

        results = []
        detail_page = await context.new_page()
        for i, url in enumerate(urls, 1):
            print(f"[{i}/{len(urls)}]", end=" ")
            item = await scrape_world(detail_page, context, url, debug=debug)
            if item:
                results.append(item)

        await browser.close()

    saved = save_batch(results)
    print(f"\n완료: {saved}개 월드의 현재 값을 tracker.db에 저장했습니다.")
    return saved


def main() -> None:
    parser = argparse.ArgumentParser(description="MapleStory Worlds 글로벌 개발 콘테스트 참가작 수집기")
    parser.add_argument("--show-browser", action="store_true", help="브라우저 창을 보이게 실행")
    parser.add_argument("--limit", type=int, default=None, help="테스트용 최대 수집 개수")
    parser.add_argument("--debug", action="store_true")
    args = parser.parse_args()
    asyncio.run(run(headless=not args.show_browser, limit=args.limit, debug=args.debug))


if __name__ == "__main__":
    main()
