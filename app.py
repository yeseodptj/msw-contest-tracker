from __future__ import annotations

import sqlite3
import subprocess
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pandas as pd
import streamlit as st

from db import DB_PATH, init_db

KST = timezone(timedelta(hours=9))

GENRE_KO = {
    "Action": "액션",
    "Adventure": "어드벤처",
    "Platformer": "플랫포머",
    "Bullet Heaven": "불릿 헤븐",
    "Casual & Arcade": "캐주얼·아케이드",
    "Defense": "디펜스",
    "Horror": "호러",
    "Idle": "방치형",
    "RPG": "RPG",
    "Role Playing": "RPG",
    "Simulation": "시뮬레이션",
    "Strategy": "전략",
    "Puzzle": "퍼즐",
    "Social": "소셜",
    "Sports": "스포츠",
    "Racing": "레이싱",
    "Shooter": "슈팅",
    "Party": "파티",
}

def genre_ko(v):
    if v is None:
        return v
    t = str(v).strip()
    return GENRE_KO.get(t, t)


st.set_page_config(page_title="MSW 글로벌 개발 콘테스트 Tracker", page_icon="🎮", layout="wide")

CUSTOM_CSS = """
<style>
.block-container {padding-top: 1.3rem; padding-bottom: 3rem;}
[data-testid="stMetricValue"] {font-size: 1.7rem;}
.small-muted {color:#888; font-size:0.88rem;}
</style>
"""
st.markdown(CUSTOM_CSS, unsafe_allow_html=True)


def load_data():
    init_db()
    con = sqlite3.connect(DB_PATH)
    worlds = pd.read_sql_query("SELECT * FROM worlds WHERE is_contest=1", con)
    snaps = pd.read_sql_query("SELECT * FROM snapshots", con)
    con.close()
    if not snaps.empty:
        snaps["observed_at"] = pd.to_datetime(snaps["observed_at"], errors="coerce", utc=True)
    return worlds, snaps


def closest_prior(group: pd.DataFrame, latest_time: pd.Timestamp, hours: int):
    target = latest_time - pd.Timedelta(hours=hours)
    prior = group[group["observed_at"] <= target]
    if prior.empty:
        return None
    return prior.sort_values("observed_at").iloc[-1]


def build_latest(worlds: pd.DataFrame, snaps: pd.DataFrame) -> pd.DataFrame:
    if worlds.empty or snaps.empty:
        return pd.DataFrame()
    rows = []
    for world_id, group in snaps.groupby("world_id"):
        group = group.dropna(subset=["observed_at"]).sort_values("observed_at")
        if group.empty:
            continue
        latest = group.iloc[-1].copy()
        for label, hours in [("1h", 1), ("24h", 24), ("7d", 24 * 7)]:
            prior = closest_prior(group, latest["observed_at"], hours)
            for col in ["total_players", "favorites", "likes_count", "comments"]:
                latest[f"{col}_delta_{label}"] = (
                    latest[col] - prior[col]
                    if prior is not None and pd.notna(latest[col]) and pd.notna(prior[col])
                    else pd.NA
                )
        rows.append(latest)
    latest_df = pd.DataFrame(rows)
    merged = worlds.merge(latest_df, on="world_id", how="left", suffixes=("", "_snap"))
    merged["total_players"] = pd.to_numeric(merged["total_players"], errors="coerce")
    merged["favorites"] = pd.to_numeric(merged["favorites"], errors="coerce")
    merged["likes_count"] = pd.to_numeric(merged["likes_count"], errors="coerce")
    merged["comments"] = pd.to_numeric(merged["comments"], errors="coerce")
    merged["rank"] = merged["total_players"].rank(method="min", ascending=False, na_option="bottom").astype("Int64")
    return merged


def fmt_int(v):
    if pd.isna(v):
        return "-"
    return f"{int(v):,}"


def fmt_delta(v):
    if pd.isna(v):
        return "-"
    v = int(v)
    return f"+{v:,}" if v > 0 else f"{v:,}"


worlds, snaps = load_data()
latest = build_latest(worlds, snaps)
if not latest.empty and "genre" in latest.columns:
    latest["genre"] = latest["genre"].map(genre_ko)

st.title("🎮 MapleStory Worlds 글로벌 개발 콘테스트 Tracker")
st.caption("콘테스트 참가 월드의 플레이어·하트·댓글을 주기적으로 수집해 증가량과 순위를 추적합니다.")

with st.sidebar:
    st.header("수집")
    st.write(f"DB: `{DB_PATH.name}`")
    if not snaps.empty:
        last = snaps["observed_at"].max()
        st.caption(f"마지막 스냅샷: {last.tz_convert('Asia/Seoul'):%Y-%m-%d %H:%M:%S}")
    if st.button("🔄 지금 한 번 수집", use_container_width=True):
        with st.spinner("메월드 참가작을 수집 중입니다..."):
            proc = subprocess.run([sys.executable, str(Path(__file__).with_name("collector.py"))], capture_output=True, text=True)
        if proc.returncode == 0:
            st.success("수집 완료. 페이지를 새로고침합니다.")
            st.rerun()
        else:
            st.error("수집에 실패했습니다.")
            st.code(proc.stderr[-4000:] or proc.stdout[-4000:])
    st.info("온라인 버전은 GitHub Actions가 매시간 자동으로 새 스냅샷을 저장합니다.")

if latest.empty:
    st.warning("아직 수집된 데이터가 없습니다. 왼쪽의 **지금 한 번 수집**을 누르거나 `collect_once.bat`을 실행하세요.")
    st.stop()

# 파싱된 출시일은 사이트 표기 형식이 다양하므로 원문을 유지하고, first_seen을 신작 보조 기준으로 쓴다.
latest["first_seen_dt"] = pd.to_datetime(latest["first_seen"], errors="coerce", utc=True)
now_utc = pd.Timestamp.now(tz="UTC")
latest["is_new_72h"] = latest["first_seen_dt"] >= now_utc - pd.Timedelta(hours=72)

c1, c2, c3, c4 = st.columns(4)
c1.metric("참가작", f"{len(latest):,}개")
c2.metric("총 플레이어", fmt_int(latest["total_players"].sum(min_count=1)))
c3.metric("72시간 내 신규 발견", f"{int(latest['is_new_72h'].sum()):,}개")
d24 = pd.to_numeric(latest.get("total_players_delta_24h"), errors="coerce").sum(min_count=1)
c4.metric("24시간 플레이 증가", fmt_delta(d24))

st.divider()

filters1, filters2, filters3, filters4 = st.columns([2, 1.3, 1.4, 1.4])
query = filters1.text_input("게임명 검색", placeholder="월드명 검색")
genres = sorted([g for g in latest["genre"].dropna().astype(str).unique() if g.strip() and g.strip() != "-"])
genre = filters2.selectbox("장르", ["전체"] + genres)
new_only = filters3.toggle("🆕 신규만", value=False)
sort_label = filters4.selectbox("정렬", ["플레이어", "24시간 증가", "7일 증가", "즐겨찾기", "좋아요 수", "댓글", "신작"])

view = latest.copy()
if query:
    view = view[view["name"].str.contains(query, case=False, na=False)]
if genre != "전체":
    view = view[view["genre"] == genre]
if new_only:
    view = view[view["is_new_72h"]]

sort_map = {
    "플레이어": "total_players",
    "24시간 증가": "total_players_delta_24h",
    "7일 증가": "total_players_delta_7d",
    "즐겨찾기": "favorites",
    "좋아요 수": "likes_count",
    "댓글": "comments",
    "신작": "first_seen_dt",
}
view = view.sort_values(sort_map[sort_label], ascending=False, na_position="last")

display = pd.DataFrame({
    "순위": view["rank"],
    "게임명": view["name"],
    "장르": view["genre"].fillna("-"),
    "플레이어": view["total_players"].map(fmt_int),
    "1H": view.get("total_players_delta_1h", pd.Series(index=view.index, dtype="object")).map(fmt_delta),
    "24H": view.get("total_players_delta_24h", pd.Series(index=view.index, dtype="object")).map(fmt_delta),
    "7D": view.get("total_players_delta_7d", pd.Series(index=view.index, dtype="object")).map(fmt_delta),
    "즐겨찾기": view["favorites"].map(fmt_int),
    "하트": view["likes_count"].map(fmt_int),
    "좋아요율": view["likes_rate"].apply(lambda x: f"{x:.0f}%" if pd.notna(x) else "-"),
    "댓글": view["comments"].map(fmt_int),
    "최근 업데이트": view["updated_date"].fillna("-"),
    "URL": view["url"],
})

rank_tab, rising_tab, genre_tab, detail_tab = st.tabs(["🏆 전체 랭킹", "🚀 급상승", "🎯 장르 분석", "⭐ 월드 상세"])

with rank_tab:
    st.dataframe(display, hide_index=True, use_container_width=True, height=620, column_config={
        "URL": st.column_config.LinkColumn("월드", display_text="열기"),
    })
    export = view[[
        "rank", "name", "creator", "genre", "total_players", "total_players_delta_1h",
        "total_players_delta_24h", "total_players_delta_7d", "favorites", "likes_count",
        "likes_rate", "comments", "release_date", "updated_date", "url", "world_id"
    ]].copy()
    export.columns = [
        "순위", "게임명", "제작자", "장르", "플레이어", "1시간증가", "24시간증가", "7일증가",
        "즐겨찾기", "좋아요수", "좋아요율", "댓글", "출시일", "최근업데이트", "URL", "월드ID"
    ]
    st.download_button(
        "⬇️ 현재 표 CSV 다운로드 (Excel에서 열기)",
        export.to_csv(index=False, encoding="utf-8-sig"),
        file_name=f"msw_contest_{datetime.now(KST):%Y%m%d_%H%M}.csv",
        mime="text/csv",
    )

with rising_tab:
    rising = latest.sort_values("total_players_delta_24h", ascending=False, na_position="last").head(20)
    if rising["total_players_delta_24h"].notna().any():
        chart = rising.set_index("name")["total_players_delta_24h"].dropna()
        st.bar_chart(chart, horizontal=True)
        st.dataframe(
            rising[["name", "total_players", "total_players_delta_24h", "total_players_delta_7d", "favorites", "url"]],
            hide_index=True, use_container_width=True,
            column_config={"url": st.column_config.LinkColumn("월드", display_text="열기")},
        )
    else:
        st.info("24시간 전 스냅샷이 아직 없습니다. 데이터를 계속 쌓으면 자동으로 급상승 순위가 생깁니다.")

with genre_tab:
    genre_df = latest.copy()
    genre_df["genre"] = genre_df["genre"].fillna("미분류")
    agg = genre_df.groupby("genre", as_index=False).agg(
        게임수=("world_id", "count"),
        총플레이어=("total_players", "sum"),
        평균플레이어=("total_players", "mean"),
        평균즐겨찾기=("favorites", "mean"),
    ).sort_values("총플레이어", ascending=False)
    st.dataframe(agg, hide_index=True, use_container_width=True)
    if not agg.empty:
        st.bar_chart(agg.set_index("genre")["게임수"])

with detail_tab:
    options = latest.sort_values("total_players", ascending=False)["name"].tolist()
    selected = st.selectbox("월드 선택", options)
    row = latest[latest["name"] == selected].iloc[0]
    d1, d2, d3, d4, d5 = st.columns(5)
    d1.metric("전체 순위", f"#{fmt_int(row['rank'])}")
    d2.metric("플레이어", fmt_int(row["total_players"]), fmt_delta(row.get("total_players_delta_24h")))
    d3.metric("즐겨찾기", fmt_int(row["favorites"]))
    d4.metric("하트", fmt_int(row["likes_count"]), f"{row['likes_rate']:.0f}%" if pd.notna(row["likes_rate"]) else None)
    d5.metric("댓글", fmt_int(row["comments"]))
    st.link_button("메월드에서 열기", row["url"])

    history = snaps[snaps["world_id"] == row["world_id"]].sort_values("observed_at").copy()
    if len(history) >= 2:
        history = history.set_index("observed_at")
        st.subheader("플레이어 추이")
        st.line_chart(history[["total_players"]])
        st.subheader("즐겨찾기 / 좋아요 / 댓글 추이")
        st.line_chart(history[["favorites", "likes_count", "comments"]])
    else:
        st.info("스냅샷이 2개 이상 쌓이면 추이 그래프가 표시됩니다.")

st.caption("데이터 출처: MapleStory Worlds 공개 웹페이지. 사이트 구조 변경 시 collector.py의 파서가 조정될 수 있습니다.")
