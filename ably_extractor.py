"""
에이블리 상품 추출기
실시간 랭킹 / 이벤트 상품을 CSV로 추출합니다.
실행: streamlit run ably_extractor.py
"""

import streamlit as st
import requests
import csv
import io
import subprocess
import re
import time
import os
import tempfile
import pandas as pd
from urllib.parse import quote, urlparse, parse_qs

# ─────────────────────────────────────────────
# Page config
# ─────────────────────────────────────────────
st.set_page_config(
    page_title="에이블리 상품 추출기",
    page_icon=":material/shopping_bag:",
    layout="wide",
)

# ─────────────────────────────────────────────
# Session state init
# ─────────────────────────────────────────────
_defaults = {
    "jwt_token": "",
    "ranking_results": None,
    "ranking_label": "",
    "ranking_count": 0,
    "event_results": None,
    "event_title": "",
    "event_count": 0,
}
for k, v in _defaults.items():
    if k not in st.session_state:
        st.session_state[k] = v

# ─────────────────────────────────────────────
# Helpers
# ─────────────────────────────────────────────
BH_PATH = r"C:\Users\MADUP\.local\bin\browser-harness.exe"

def get_headers(jwt: str) -> dict:
    return {
        "Authorization": f"JWT {jwt}",
        "User-Agent": (
            "Mozilla/5.0 (iPhone; CPU iPhone OS 18_5 like Mac OS X) "
            "AppleWebKit/605.1.15 (KHTML, like Gecko) Mobile/15E148"
        ),
        "Accept": "application/json",
        "Referer": "https://m.a-bly.com/",
    }


def to_csv_bytes(rows: list) -> bytes:
    buf = io.StringIO()
    writer = csv.writer(buf)
    writer.writerow(["랭킹", "SNO", "브랜드명", "상품명"])
    writer.writerows(rows)
    return ("﻿" + buf.getvalue()).encode("utf-8")


def fetch_jwt_from_browser() -> str | None:
    """browser-harness를 통해 Chrome에서 JWT 자동 추출 (로컬 전용)."""
    script = """
import re
ensure_real_tab()
tabs = list_tabs()
ably_tab = next((t for t in tabs if 'a-bly.com' in t.get('url', '')), None)
if ably_tab:
    switch_tab(ably_tab['targetId'])
cookie_str = js("return document.cookie")
m = re.search(r'ably-jwt-token=([^;]+)', cookie_str or '')
if m:
    print('JWT_OK:' + m.group(1))
else:
    print('JWT_FAIL')
"""
    with tempfile.NamedTemporaryFile(
        mode="w", suffix=".py", delete=False, encoding="utf-8"
    ) as f:
        f.write(script)
        tmp = f.name
    try:
        env = {**os.environ, "PYTHONIOENCODING": "utf-8"}
        result = subprocess.run(
            [BH_PATH],
            stdin=open(tmp, "rb"),
            capture_output=True,
            text=True,
            timeout=20,
            env=env,
        )
        for line in result.stdout.splitlines():
            if line.startswith("JWT_OK:"):
                return line[7:].strip()
    finally:
        try:
            os.unlink(tmp)
        except OSError:
            pass
    return None


# ─────────────────────────────────────────────
# Ranking Extractor
# ─────────────────────────────────────────────
def extract_ranking(url: str, jwt: str):
    """v2 API 페이지네이션으로 전체 랭킹 상품 추출."""
    parsed = urlparse(url)
    params = parse_qs(parsed.query)
    init_token = params.get("next_token", [""])[0]
    cat_sno = params.get("category_sno", [""])[0]

    if not init_token:
        st.error("URL에 `next_token` 파라미터가 없습니다. URL을 다시 확인해주세요.")
        return None

    headers = get_headers(jwt)
    all_goods = []
    current_token = init_token
    page = 0

    progress_bar = st.progress(0.0, text="데이터 가져오는 중...")
    status_text = st.empty()

    try:
        while current_token:
            page += 1
            api_url = (
                f"https://api.a-bly.com/api/v2/screens/COMPONENT_LIST/"
                f"?next_token={quote(current_token, safe='')}&category_sno={cat_sno}"
            )
            resp = requests.get(api_url, headers=headers, timeout=15)

            if resp.status_code == 401:
                st.error("🔒 JWT 토큰이 만료됐습니다. 다시 가져와주세요.")
                return None
            resp.raise_for_status()
            data = resp.json()

            for comp in data.get("components", []):
                item_list = comp.get("entity", {}).get("item_list", [])
                if len(item_list) > 1:
                    goods = [
                        e["item"]
                        for e in item_list
                        if e.get("item", {}).get("sno")
                    ]
                    if goods:
                        all_goods.extend(goods)

            status_text.caption(
                f":material/downloading: 페이지 {page} — {len(all_goods)}개 수집 중…"
            )
            progress_bar.progress(min(page / 15, 0.95))

            next_tok = data.get("next_token")
            current_token = (
                next_tok if (next_tok and next_tok != current_token) else None
            )
            if page >= 25:
                break
            time.sleep(0.25)

    except requests.RequestException as e:
        st.error(f"네트워크 오류: {e}")
        return None

    progress_bar.progress(1.0, text="완료!")
    time.sleep(0.4)
    progress_bar.empty()
    status_text.empty()

    return [
        (i + 1, g["sno"], g.get("market_name", ""), g.get("name", ""))
        for i, g in enumerate(all_goods)
    ]


# ─────────────────────────────────────────────
# Event Extractor
# ─────────────────────────────────────────────
def extract_event(url: str, jwt: str):
    """이벤트 API로 전체 세그먼트 상품 추출.

    API 구조:
      GET /webview/events/{id}/  →  { event: { sno, name, segment_meta_data: [...] } }
      GET /webview/events/segments/{sno}/goods/?per_page=100  →  { goods_list: [{item: {...}}], next_token }
    """
    m = re.search(r"/events/([a-zA-Z0-9]+)", url)
    if not m:
        st.error("URL에서 이벤트 ID를 찾을 수 없습니다. URL을 다시 확인해주세요.")
        return None, ""

    event_id = m.group(1)
    headers = get_headers(jwt)

    progress_bar = st.progress(0.0, text="이벤트 정보 가져오는 중...")
    status_text = st.empty()

    try:
        # 이벤트 정보 + 세그먼트 목록
        event_resp = requests.get(
            f"https://api.a-bly.com/webview/events/{event_id}/",
            headers=headers,
            timeout=15,
        )
        if event_resp.status_code == 401:
            st.error("🔒 JWT 토큰이 만료됐습니다. 다시 가져와주세요.")
            return None, ""
        event_resp.raise_for_status()
        outer = event_resp.json()

        # 응답 구조: { "event": { "sno", "name", "segment_meta_data": [...] } }
        event = outer.get("event", outer)
        event_title = event.get("name", "이벤트")
        segments = event.get("segment_meta_data", [])
        total_segs = max(len(segments), 1)

        all_goods = []

        for seg_idx, seg in enumerate(segments):
            sno = seg.get("sno")
            if not sno:
                continue
            seg_name = seg.get("title", f"세그먼트{seg_idx+1}")
            next_token = None

            while True:
                seg_url = (
                    f"https://api.a-bly.com/webview/events/segments/{sno}/goods/"
                    f"?per_page=100"
                )
                if next_token:
                    seg_url += f"&next_token={quote(str(next_token), safe='')}"

                seg_resp = requests.get(seg_url, headers=headers, timeout=15)
                seg_resp.raise_for_status()
                seg_data = seg_resp.json()

                # goods_list 안에 { item: {...} } 구조
                goods_list = seg_data.get("goods_list", [])
                for entry in goods_list:
                    item = entry.get("item", {})
                    if item.get("sno"):
                        all_goods.append(item)

                pct = (seg_idx + 1) / total_segs
                progress_bar.progress(min(pct, 0.99))
                status_text.caption(
                    f":material/downloading: [{seg_name}] {len(goods_list)}개 — "
                    f"총 {len(all_goods)}개 수집 중…"
                )

                next_token = seg_data.get("next_token")
                if not next_token or not goods_list:
                    break

    except requests.RequestException as e:
        st.error(f"네트워크 오류: {e}")
        return None, ""

    progress_bar.progress(1.0, text="완료!")
    time.sleep(0.4)
    progress_bar.empty()
    status_text.empty()

    rows = []
    for i, item in enumerate(all_goods):
        sno = item.get("sno", "")
        name = item.get("name", "")
        brand = item.get("market_name", "") or (item.get("market") or {}).get("name", "")
        rows.append((i + 1, sno, brand, name))

    return rows, event_title


# ─────────────────────────────────────────────
# Bookmarklet JS (어느 PC에서나 동작)
# ─────────────────────────────────────────────
_BOOKMARKLET = (
    "javascript:(function(){"
    "var m=document.cookie.match(/ably-jwt-token=([^;]+)/);"
    "if(m){"
    "var t=decodeURIComponent(m[1]);"
    "prompt('✅ 아래 토큰을 Ctrl+A → Ctrl+C 로 복사하세요:', t);"
    "}else{"
    "alert('⚠️ 에이블리에 로그인되어 있지 않습니다.\\n\\nm.a-bly.com 에서 로그인 후 다시 클릭해주세요.');"
    "}"
    "})();"
)

# ─────────────────────────────────────────────
# Sidebar — JWT
# ─────────────────────────────────────────────
with st.sidebar:
    st.header("인증 설정", anchor=False)

    has_jwt = bool(st.session_state.jwt_token)
    if has_jwt:
        st.badge("토큰 있음", icon=":material/check:", color="green")
    else:
        st.badge("토큰 없음", icon=":material/key_off:", color="red")

    st.space("small")

    # ── Step 1: 북마크릿 ──────────────────────
    with st.container(border=True):
        st.markdown("**① 북마크릿 설치** (최초 1회)")
        st.markdown(
            f"""<a href="{_BOOKMARKLET}"
                style="display:inline-block;padding:9px 18px;
                       background:#FF4B4B;color:white;border-radius:8px;
                       text-decoration:none;font-size:14px;font-weight:600;
                       cursor:grab;user-select:none;"
                draggable="true"
                title="북마크바로 드래그해서 저장하세요">
                🔖 JWT 복사기
            </a>""",
            unsafe_allow_html=True,
        )
        st.caption(
            "위 빨간 버튼을 **북마크바로 드래그**해서 저장하세요.\n\n"
            "북마크바가 안 보이면: Chrome에서 **Ctrl+Shift+B**"
        )

    st.space("small")

    # ── Step 2: 북마크릿 사용 안내 ──────────
    with st.container(border=True):
        st.markdown("**② JWT 복사**")
        st.caption(
            "1. Chrome에서 **m.a-bly.com** 열기\n"
            "2. 에이블리 **로그인** 확인\n"
            "3. 북마크바의 **JWT 복사기** 클릭\n"
            "4. 팝업 뜨면 **Ctrl+A → Ctrl+C** 로 복사"
        )

    st.space("small")

    # ── Step 3: 붙여넣기 ─────────────────────
    jwt_input = st.text_area(
        "**③ 여기에 붙여넣기** (Ctrl+V)",
        value=st.session_state.jwt_token,
        height=80,
        placeholder="북마크릿 클릭 후 Ctrl+V …",
        key="_jwt_area",
    )
    if jwt_input != st.session_state.jwt_token:
        st.session_state.jwt_token = jwt_input

    # ── 이 기기 전용: browser-harness 자동 추출 ──
    bh_available = os.path.exists(BH_PATH)
    if bh_available:
        st.space("small")
        if st.button(
            "이 기기: Chrome에서 자동 가져오기",
            icon=":material/sync:",
            help="browser-harness가 설치된 이 기기에서만 동작합니다",
        ):
            with st.spinner("Chrome에서 JWT 추출 중…"):
                try:
                    jwt = fetch_jwt_from_browser()
                    if jwt:
                        st.session_state.jwt_token = jwt
                        st.toast("JWT 토큰을 가져왔습니다!", icon=":material/check_circle:")
                        st.rerun()
                    else:
                        st.warning("에이블리 탭을 Chrome에서 먼저 열어주세요.")
                except Exception as e:
                    st.error(f"오류: {e}")


# ─────────────────────────────────────────────
# Header
# ─────────────────────────────────────────────
st.title("에이블리 상품 추출기")
st.caption("실시간 랭킹 · 이벤트 상품 목록을 CSV로 추출합니다")

st.space("small")

# ─────────────────────────────────────────────
# Tabs
# ─────────────────────────────────────────────
tab_ranking, tab_event = st.tabs([
    ":material/bar_chart: 실시간 랭킹 추출",
    ":material/campaign: 이벤트 상품 추출",
])

# ══════════════════════════════════════════════
# Tab 1 — 실시간 랭킹
# ══════════════════════════════════════════════
with tab_ranking:
    st.subheader("실시간 랭킹 상품 추출", anchor=False)
    st.caption("에이블리 카테고리 랭킹 URL → 전체 상품 SNO · 브랜드 · 상품명 추출")

    st.space("small")

    col_url, col_label = st.columns([4, 1])
    with col_url:
        ranking_url = st.text_input(
            "랭킹 URL",
            placeholder="https://m.a-bly.com/screens?screen_name=COMPONENT_LIST&next_token=eyJ…",
            key="ranking_url",
        )
    with col_label:
        ranking_label = st.text_input(
            "레이블",
            placeholder="예: 스킨케어",
            key="ranking_label_input",
            help="CSV 파일명에 사용됩니다 (예: ably_screen_스킨케어.csv)",
        )

    with st.container(horizontal=True):
        run_ranking = st.button(
            "추출 시작",
            icon=":material/play_arrow:",
            type="primary",
            key="btn_ranking",
        )
        if st.session_state.ranking_results:
            st.button(
                "초기화",
                icon=":material/refresh:",
                key="btn_ranking_clear",
                on_click=lambda: st.session_state.update(
                    ranking_results=None, ranking_label="", ranking_count=0
                ),
            )

    if run_ranking:
        if not ranking_url:
            st.error("URL을 입력해주세요.", icon=":material/error:")
        elif not st.session_state.jwt_token:
            st.error(
                "JWT 토큰이 필요합니다. 사이드바에서 토큰을 가져와주세요.",
                icon=":material/key_off:",
            )
        else:
            rows = extract_ranking(ranking_url, st.session_state.jwt_token)
            if rows is not None:
                st.session_state.ranking_results = rows
                st.session_state.ranking_label = ranking_label or "ranking"
                st.session_state.ranking_count = len(rows)
                st.toast(f"{len(rows)}개 추출 완료!", icon=":material/check_circle:")

    # 결과 표시
    if st.session_state.ranking_results:
        rows = st.session_state.ranking_results
        label = st.session_state.ranking_label

        st.space("small")

        col_m1, col_m2, col_dl = st.columns([1, 1, 2])
        col_m1.metric("총 상품 수", f"{len(rows):,}개")
        col_m2.metric("카테고리", label)

        with col_dl:
            csv_bytes = to_csv_bytes(rows)
            st.download_button(
                label="CSV 다운로드",
                data=csv_bytes,
                file_name=f"ably_screen_{label}.csv",
                mime="text/csv",
                icon=":material/download:",
                type="primary",
                key="dl_ranking",
            )

        df = pd.DataFrame(rows, columns=["랭킹", "SNO", "브랜드명", "상품명"])
        st.dataframe(
            df,
            height=520,
            hide_index=True,
            column_config={
                "랭킹": st.column_config.NumberColumn(width="small"),
                "SNO": st.column_config.TextColumn(width="medium"),
                "브랜드명": st.column_config.TextColumn(width="medium"),
                "상품명": st.column_config.TextColumn(width="large"),
            },
        )


# ══════════════════════════════════════════════
# Tab 2 — 이벤트
# ══════════════════════════════════════════════
with tab_event:
    st.subheader("이벤트 상품 추출", anchor=False)
    st.caption("'전체보기 / 펼쳐보기' 포함 모든 세그먼트 상품을 추출합니다")

    st.space("small")

    event_url = st.text_input(
        "이벤트 URL",
        placeholder="https://m.a-bly.com/events/dca0667e",
        key="event_url",
    )

    with st.container(horizontal=True):
        run_event = st.button(
            "추출 시작",
            icon=":material/play_arrow:",
            type="primary",
            key="btn_event",
        )
        if st.session_state.event_results:
            st.button(
                "초기화",
                icon=":material/refresh:",
                key="btn_event_clear",
                on_click=lambda: st.session_state.update(
                    event_results=None, event_title="", event_count=0
                ),
            )

    if run_event:
        if not event_url:
            st.error("이벤트 URL을 입력해주세요.", icon=":material/error:")
        elif not st.session_state.jwt_token:
            st.error(
                "JWT 토큰이 필요합니다. 사이드바에서 토큰을 가져와주세요.",
                icon=":material/key_off:",
            )
        else:
            rows, title = extract_event(event_url, st.session_state.jwt_token)
            if rows is not None:
                st.session_state.event_results = rows
                st.session_state.event_title = title
                st.session_state.event_count = len(rows)
                st.toast(f"{len(rows)}개 추출 완료!", icon=":material/check_circle:")

    # 결과 표시
    if st.session_state.event_results:
        rows = st.session_state.event_results
        title = st.session_state.event_title
        safe_title = re.sub(r'[\\/:*?"<>|]', "_", title)[:40]

        st.space("small")

        short_title = title if len(title) <= 20 else title[:19] + "…"
        col_m1, col_m2, col_dl = st.columns([1, 1, 2])
        col_m1.metric("총 상품 수", f"{len(rows):,}개")
        col_m2.metric("이벤트", short_title)

        with col_dl:
            csv_bytes = to_csv_bytes(rows)
            st.download_button(
                label="CSV 다운로드",
                data=csv_bytes,
                file_name=f"ably_event_{safe_title}.csv",
                mime="text/csv",
                icon=":material/download:",
                type="primary",
                key="dl_event",
            )

        df = pd.DataFrame(rows, columns=["랭킹", "SNO", "브랜드명", "상품명"])
        st.dataframe(
            df,
            height=520,
            hide_index=True,
            column_config={
                "랭킹": st.column_config.NumberColumn(width="small"),
                "SNO": st.column_config.TextColumn(width="medium"),
                "브랜드명": st.column_config.TextColumn(width="medium"),
                "상품명": st.column_config.TextColumn(width="large"),
            },
        )
