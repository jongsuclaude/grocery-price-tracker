#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
식재료 최저가 추적기 (개인용)

- 네이버 쇼핑 검색 API로 watchlist 품목의 실구매 최저가를 조회한다.
- 목표가 이하로 떨어지면 맥 알림센터로 알림 + 로컬 대시보드(dashboard.html)를 만든다.
- API 키가 없으면 자동으로 '목업 모드'로 돌아가 화면만 미리 볼 수 있다.

실행:  python3 tracker.py      (그 다음  open dashboard.html)
설정:  config.json 의 naver.client_id / client_secret / watchlist 를 수정
"""

import json
import os
import re
import sys
import csv
import html
import random
import subprocess
import time
import urllib.request
import urllib.parse
import urllib.error
from datetime import datetime, timezone, timedelta

BASE = os.path.dirname(os.path.abspath(__file__))
CONFIG_PATH = os.path.join(BASE, "config.json")
OUTPUT_HTML = os.path.join(BASE, "dashboard.html")
HISTORY_CSV = os.path.join(BASE, "history.csv")
NAVER_SHOP_URL = "https://openapi.naver.com/v1/search/shop.json"
KST = timezone(timedelta(hours=9))   # 클라우드 러너는 UTC라 한국시간으로 표기/기록


# ---------------------------------------------------------------- 설정/유틸

def load_config():
    if not os.path.exists(CONFIG_PATH):
        print(f"[오류] config.json 이 없습니다: {CONFIG_PATH}")
        sys.exit(1)
    with open(CONFIG_PATH, encoding="utf-8") as f:
        return json.load(f)


def is_placeholder(v):
    """키가 비어있거나 예시 그대로면 True (→ 목업 모드)"""
    return (not v) or str(v).startswith("여기에")


def resolve_keys(cfg):
    """네이버 키 우선순위: 환경변수 → 로컬 keys.json → config.json
    - 환경변수: GitHub Actions Secrets 가 주입 (클라우드)
    - keys.json: 로컬 전용 비공개 파일(.gitignore 됨, repo 에 안 올라감)
    - config.json: 마지막 폴백 (repo 에는 빈 칸으로 올림)"""
    cid = os.environ.get("NAVER_CLIENT_ID", "").strip()
    csec = os.environ.get("NAVER_CLIENT_SECRET", "").strip()
    if not (cid and csec):
        kp = os.path.join(BASE, "keys.json")
        if os.path.exists(kp):
            try:
                with open(kp, encoding="utf-8") as f:
                    k = json.load(f)
                cid = cid or k.get("client_id", "")
                csec = csec or k.get("client_secret", "")
            except (ValueError, OSError):
                pass
    if not (cid and csec):
        n = cfg.get("naver", {})
        cid = cid or n.get("client_id", "")
        csec = csec or n.get("client_secret", "")
    return cid, csec


def strip_tags(s):
    return re.sub(r"<[^>]+>", "", s or "").strip()


# ---------------------------------------------------------------- 네이버 조회

# 가격 낮은 순으로 뽑으면 양파망·에그트레이 같은 부자재가 걸린다.
# 그래서 정확도순으로 받고, 아래 단어가 제목에 있으면 식재료가 아니라고 보고 제외한다.
BLOCK_WORDS = [
    "망", "계란판", "에그트레이", "난좌", "트레이", "네트", "보관", "케이스", "소스",
    "씨앗", "모종", "종자", "모형", "스티커", "봉투", "비닐", "받침",
    "커터", "슬라이서", "다지기", "정리", "거치", "수납", "행거", "집게",
    "파충류", "도마뱀", "사료", "즙", "환",
]


def query_naver(item, client_id, client_secret):
    """정확도순으로 받아 부자재를 거른 뒤, 검색어 단어를 모두 포함한 항목 중 최저가 1개 반환"""
    query = item.get("query", item["name"])
    tokens = query.lower().split()
    block = BLOCK_WORDS + item.get("exclude", [])  # config 에서 품목별 추가 제외어 가능

    params = urllib.parse.urlencode({"query": query, "display": 40, "sort": "sim"})
    req = urllib.request.Request(NAVER_SHOP_URL + "?" + params)
    req.add_header("X-Naver-Client-Id", client_id)
    req.add_header("X-Naver-Client-Secret", client_secret)
    data = None
    for attempt in range(4):  # 429(요청 과다) 시 백오프 후 재시도
        try:
            with urllib.request.urlopen(req, timeout=10) as resp:
                data = json.loads(resp.read().decode("utf-8"))
            break
        except urllib.error.HTTPError as e:
            if e.code == 429 and attempt < 3:
                time.sleep(0.6 * (attempt + 1))
                continue
            raise

    cleans, anys = [], []
    for it in data.get("items", []):
        try:
            price = int(it.get("lprice") or 0)
        except ValueError:
            continue
        if price <= 0:
            continue
        title = strip_tags(it.get("title"))
        tl = title.lower()
        if not all(tok in tl for tok in tokens):   # 검색어 단어를 모두 포함해야 진짜 매칭
            continue
        if any(b in title for b in block):          # 부자재 단어 있으면 제외
            continue
        # 옵션 낚시 감지: 서로 다른 '무게/용량'이 2개 이상이면 lprice=최소옵션 가격
        # (들어가서 원하는 용량 고르면 오름). '골라담기/택1'류도 더 싼 옵션이 숨은 낚시성.
        sizes = set(re.findall(r"\d+\.?\d*\s?(?:kg|g|ml|l)", tl))
        bait = len(sizes) >= 2 or any(k in title for k in ("골라담기", "택1", "택일", "모음전"))
        cand = {
            "price": price,
            "title": title,
            "mall": it.get("mallName") or "쇼핑몰",
            "link": it.get("link") or "",
        }
        anys.append(cand)
        if not bait:
            cleans.append(cand)
    pool = sorted(cleans or anys, key=lambda c: c["price"])  # 낚시 아닌 단일용량 우선, 없으면 폴백
    if not pool:
        return None
    best = dict(pool[0])
    best["alts"] = pool[1:6]   # 비슷한 상품(가격 오름차순 대안) 최대 5개
    return best


def mock_result(item):
    """키가 없을 때 화면 확인용 가짜 결과"""
    target = item.get("target_price") or 5000
    price = int(target * random.uniform(0.7, 1.3))
    search = urllib.parse.quote(item.get("query", item["name"]))
    return {
        "price": price,
        "title": item["name"] + " (목업)",
        "mall": random.choice(["쿠팡", "마켓컬리", "이마트몰", "11번가"]),
        "link": "https://search.shopping.naver.com/search/all?query=" + search,
    }


# ---------------------------------------------------------------- 가격 기록(이력)

def log_history(results):
    """이번 실행의 최저가를 history.csv 에 한 줄씩 추가 (매일 누적)"""
    now = datetime.now(KST)
    date = now.strftime("%Y-%m-%d")
    ts = now.strftime("%Y-%m-%d %H:%M")
    exists = os.path.exists(HISTORY_CSV)
    with open(HISTORY_CSV, "a", newline="", encoding="utf-8") as f:
        w = csv.writer(f)
        if not exists:
            w.writerow(["date", "datetime", "name", "price", "mall", "link"])
        for r in results:
            b = r["best"]
            if b:
                w.writerow([date, ts, r["item"]["name"], b["price"], b["mall"], b["link"]])


def load_history():
    rows = []
    if not os.path.exists(HISTORY_CSV):
        return rows
    with open(HISTORY_CSV, newline="", encoding="utf-8") as f:
        for row in csv.DictReader(f):
            try:
                row["price"] = int(row["price"])
            except (ValueError, KeyError, TypeError):
                continue
            rows.append(row)
    return rows


def history_stats(rows, name, days=30):
    """품목별 '하루 최저가' 시계열에서 최근 N일 평균/최저 계산
    (하루에 여러 번 돌려도 그날 최저가로 합쳐서 1일=1포인트)"""
    daily = {}
    for r in rows:
        if r.get("name") != name:
            continue
        d, p = r.get("date"), r.get("price")
        if d is None or p is None:
            continue
        if d not in daily or p < daily[d]:
            daily[d] = p
    if not daily:
        return None
    dates = sorted(daily)
    recent = dates[-days:]
    vals = [daily[d] for d in recent]
    min_date = min(daily, key=lambda k: daily[k])
    return {
        "days": len(daily),
        "series": vals,                          # 추이(스파크라인)용 일별 최저가
        "avg": round(sum(vals) / len(vals)),
        "min": daily[min_date],
        "min_date": min_date,
        "prev": daily[dates[-2]] if len(dates) >= 2 else None,       # 이전 갱신(전일) 가격
        "prev_date": dates[-2] if len(dates) >= 2 else None,
    }


# ---------------------------------------------------------------- 실행 본체

def run():
    cfg = load_config()
    cid, csec = resolve_keys(cfg)
    mock_mode = is_placeholder(cid) or is_placeholder(csec)

    if mock_mode:
        print("[안내] 네이버 키가 없어 목업 모드로 실행합니다. (config.json 에 키 입력 시 실데이터)")

    results = []
    for item in cfg.get("watchlist", []):
        name = item.get("name", "?")
        target = item.get("target_price")
        query = item.get("query", name)
        best, error = None, None

        if mock_mode:
            best = mock_result(item)
        else:
            try:
                best = query_naver(item, cid, csec)
            except urllib.error.HTTPError as e:
                error = f"HTTP {e.code} {e.reason}"
            except Exception as e:  # noqa
                error = str(e)

        hit = bool(best and target and best["price"] <= target)
        results.append({"item": item, "best": best, "error": error, "hit": hit})
        # 콘솔 한 줄 요약
        if best:
            mark = "🟢 알림" if hit else "  대기"
            print(f"{mark}  {name:<14} {best['price']:>8,}원  (목표 {target:,}원)  [{best['mall']}]")
        else:
            print(f"  오류  {name:<14} {error}")
        if not mock_mode:
            time.sleep(0.1)  # 초당 호출 제한 회피

    log_history(results)                      # 오늘 가격을 history.csv 에 누적
    history = load_history()
    stats_map = {r["item"]["name"]: history_stats(history, r["item"]["name"])
                 for r in results}

    write_dashboard(results, stats_map, mock_mode)
    notify(results, cfg)

    hits = sum(1 for r in results if r["hit"])
    logged_days = len({h["date"] for h in history})
    print(f"\n총 {len(results)}개 중 {hits}개 목표가 이하. (가격 기록 {logged_days}일째) → {OUTPUT_HTML}")
    print("대시보드 열기:  open dashboard.html")


def notify(results, cfg):
    if not cfg.get("alert", {}).get("macos_notification", True):
        return
    hits = [r for r in results if r["hit"]]
    if not hits:
        return
    names = ", ".join(r["item"]["name"] for r in hits[:5]).replace('"', "'")
    title = f"식재료 최저가 알림 ({len(hits)}건)"
    msg = f"{names} — 목표가 이하!"
    try:
        subprocess.run(
            ["osascript", "-e", f'display notification "{msg}" with title "{title}"'],
            check=False,
        )
    except Exception:
        pass


# ---------------------------------------------------------------- 대시보드 HTML

PAGE = """<!DOCTYPE html>
<html lang="ko"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>식재료 최저가</title>
<style>
  :root { color-scheme: light dark; }
  body { font-family: -apple-system, system-ui, sans-serif; max-width: 860px;
         margin: 32px auto; padding: 0 16px; color: #1d1d1f; background: #fbfbfd; }
  h1 { font-size: 22px; margin: 0 0 4px; }
  .meta { color: #6e6e73; font-size: 13px; margin-bottom: 20px; }
  .badge { display: inline-block; padding: 2px 9px; border-radius: 999px;
           font-size: 12px; font-weight: 600; }
  .live { background: #e3f6e9; color: #1a7f37; }
  .mock { background: #fff3cd; color: #8a6d00; }
  .buy  { background: #e3f6e9; color: #1a7f37; }
  .wait { background: #eee; color: #6e6e73; }
  table { width: 100%; border-collapse: collapse; background: #fff;
          border-radius: 12px; overflow: hidden; box-shadow: 0 1px 3px rgba(0,0,0,.06); }
  th, td { text-align: left; padding: 12px 14px; border-bottom: 1px solid #f0f0f2; font-size: 14px; }
  th { background: #f5f5f7; font-size: 12px; color: #6e6e73; text-transform: uppercase; letter-spacing: .03em; }
  tr:last-child td { border-bottom: none; }
  .name { font-weight: 600; }
  .price { font-variant-numeric: tabular-nums; }
  .hit { color: #1a7f37; }
  .target { color: #6e6e73; font-variant-numeric: tabular-nums; }
  .err { color: #c0392b; font-size: 12px; }
  a { color: #0066cc; text-decoration: none; }
  .summary { font-size: 15px; margin: 18px 0 8px; }
  .prod { color: #86868b; font-weight: 400; font-size: 12px; margin-top: 3px; }
  .note { color: #86868b; font-size: 12px; margin-top: 16px; line-height: 1.6; }
  details.alts { margin-top: 5px; }
  details.alts summary { font-size: 12px; color: #0066cc; cursor: pointer; }
  .alt { display: block; font-size: 12px; color: #0066cc; padding: 5px 0 0; text-decoration: none; line-height: 1.4; }
  .alt .amall { color: #86868b; }
  .alt.more { color: #0066cc; font-weight: 600; }
  .avg { color: #1d1d1f; font-variant-numeric: tabular-nums; }
  .dn { color: #1a7f37; font-weight: 600; }
  .up { color: #c0392b; font-weight: 600; }
  .tabs { display: flex; flex-wrap: wrap; gap: 6px; margin: 10px 0 14px; }
  .tab { font-size: 13px; padding: 5px 13px; border-radius: 999px; border: 1px solid #ddd;
         background: #fff; cursor: pointer; user-select: none; }
  .tab.active { background: #1d1d1f; color: #fff; border-color: #1d1d1f; }
  .toggle { font-size: 13px; padding: 5px 13px; border-radius: 999px; border: 1px solid #ddd;
            background: #fff; cursor: pointer; user-select: none; display: inline-block; }
  .toggle.active { background: #1a7f37; color: #fff; border-color: #1a7f37; }
  .controls { display: flex; flex-wrap: wrap; align-items: center; gap: 10px 14px; margin-bottom: 14px; }
  .sortbar { font-size: 12px; color: #86868b; display: inline-flex; flex-wrap: wrap; align-items: center; gap: 5px; }
  .sort { font-size: 12px; padding: 4px 10px; border-radius: 999px; border: 1px solid #ddd;
          background: #fff; cursor: pointer; user-select: none; color: #1d1d1f; }
  .sort.active { background: #1d1d1f; color: #fff; border-color: #1d1d1f; }
  .spark { vertical-align: middle; }
  @media (max-width: 640px) {
    body { margin: 14px auto; }
    h1 { font-size: 21px; }
    table, thead, tbody, tr { display: block; width: auto; }
    thead { display: none; }
    tbody tr { background: #fff; border-radius: 12px; box-shadow: 0 1px 3px rgba(0,0,0,.06);
               padding: 12px 14px; margin-bottom: 10px; }
    td.name { display: block; font-size: 15px; padding: 0 0 8px; margin-bottom: 6px;
              border-bottom: 1px solid #f0f0f2; }
    td:not(.name) { display: flex; justify-content: space-between; align-items: baseline;
                    gap: 12px; padding: 5px 0; border: none; font-size: 14px; }
    td:not(.name)::before { content: attr(data-label); color: #86868b; font-size: 12px; flex: none; }
    td .cv { text-align: right; min-width: 0; }
    td .cv .prod { margin-top: 1px; }
  }
</style></head><body>
<h1>🥬 식재료 최저가</h1>
<div class="meta">__UPDATED__ (KST) 기준 · __MODE__</div>
<div class="summary">__SUMMARY__</div>
<div class="tabs">__TABS__</div>
<div class="controls">
  <span class="toggle" id="dropToggle">📉 어제보다 싸진 것만</span>
  <span class="sortbar">정렬
    <span class="sort active" data-sort="idx">기본</span>
    <span class="sort" data-sort="price">가격 낮은순</span>
    <span class="sort" data-sort="drop">낙폭순</span>
  </span>
</div>
<table>
  <thead><tr>
    <th>품목</th><th>오늘 가격</th><th>전일 대비</th><th>역대 최저</th><th>30일 평균</th><th>추이</th><th>쇼핑몰</th><th>링크</th>
  </tr></thead>
  <tbody>__ROWS__</tbody>
</table>
<p class="note">※ 검색어 기준 최저가예요. 가공·중량·옵션(예: 냉동 다이스, 500g 옵션) 차이로 실제와 다를 수 있어요.
품목 아래 회색 글씨가 실제 매칭된 상품이니 같이 확인하세요.</p>
<script>
  var activeCat = '전체', dropOnly = false;
  function applyFilter() {
    document.querySelectorAll('tbody tr').forEach(function (tr) {
      var okCat = (activeCat === '전체' || tr.dataset.cat === activeCat);
      var okDrop = (!dropOnly || tr.dataset.drop === 'y');
      tr.style.display = (okCat && okDrop) ? '' : 'none';
    });
  }
  document.querySelectorAll('.tab').forEach(function (t) {
    t.addEventListener('click', function () {
      activeCat = t.dataset.cat;
      document.querySelectorAll('.tab').forEach(function (x) { x.classList.toggle('active', x === t); });
      applyFilter();
    });
  });
  var dt = document.getElementById('dropToggle');
  if (dt) dt.addEventListener('click', function () {
    dropOnly = !dropOnly;
    dt.classList.toggle('active', dropOnly);
    applyFilter();
  });
  function sortRows(mode) {
    var tbody = document.querySelector('tbody');
    var rows = Array.prototype.slice.call(tbody.querySelectorAll('tr'));
    rows.sort(function (a, b) {
      if (mode === 'price') return (+a.dataset.price) - (+b.dataset.price);
      if (mode === 'drop')  return (+a.dataset.delta) - (+b.dataset.delta);
      return (+a.dataset.idx) - (+b.dataset.idx);
    });
    rows.forEach(function (r) { tbody.appendChild(r); });
  }
  document.querySelectorAll('.sort').forEach(function (s) {
    s.addEventListener('click', function () {
      document.querySelectorAll('.sort').forEach(function (x) { x.classList.toggle('active', x === s); });
      sortRows(s.dataset.sort);
    });
  });
</script>
</body></html>"""


def sparkline_svg(series):
    """일별 최저가 시계열을 작은 인라인 SVG 추이선으로 (내림=초록, 오름=빨강)"""
    series = [v for v in (series or []) if v is not None]
    if len(series) < 2:
        return '<span class="prod">–</span>'
    w, h, pad = 72, 22, 3
    lo, hi = min(series), max(series)
    rng = (hi - lo) or 1
    n = len(series)
    pts = []
    for i, v in enumerate(series):
        x = pad + (w - 2 * pad) * (i / (n - 1))
        y = pad + (h - 2 * pad) * (1 - (v - lo) / rng)
        pts.append(f"{x:.1f},{y:.1f}")
    color = ("#1a7f37" if series[-1] < series[0]
             else "#c0392b" if series[-1] > series[0] else "#86868b")
    lx, ly = pts[-1].split(",")
    return (f'<svg class="spark" width="{w}" height="{h}" viewBox="0 0 {w} {h}">'
            f'<polyline points="{" ".join(pts)}" fill="none" stroke="{color}" '
            f'stroke-width="1.5" stroke-linejoin="round" stroke-linecap="round"/>'
            f'<circle cx="{lx}" cy="{ly}" r="2" fill="{color}"/></svg>')


def write_dashboard(results, stats_map, mock_mode):
    now = datetime.now(KST).strftime("%Y-%m-%d %H:%M")
    mode = ('<span class="badge mock">목업 데이터</span>' if mock_mode
            else '<span class="badge live">실시간 · 네이버 쇼핑</span>')
    cheaper = pricier = lows = 0
    for r in results:
        b = r["best"]
        s = stats_map.get(r["item"].get("name", "?"))
        if not (b and s):
            continue
        if b["price"] <= s["min"]:
            lows += 1
        if s.get("prev") is not None:
            if b["price"] < s["prev"]:
                cheaper += 1
            elif b["price"] > s["prev"]:
                pricier += 1
    summary = (f'총 <b>{len(results)}</b>개 · 매일 오전 10시 갱신 · '
               f'전일 대비 <span class="dn">▼{cheaper}</span> / <span class="up">▲{pricier}</span>'
               f' · 역대최저 <b>{lows}</b>개')

    rows = []
    for idx, r in enumerate(results):
        item, best, error = r["item"], r["best"], r["error"]
        name = html.escape(item.get("name", "?"))
        prod = html.escape(best["title"][:42]) if best else ""
        cat = html.escape(item.get("category", "기타"))
        stats = stats_map.get(item.get("name", "?"))
        cur = best["price"] if best else None
        is_low = bool(best and stats and cur <= stats["min"])  # 오늘이 역대 최저면 강조
        dropped = bool(best and stats and stats.get("prev") is not None
                       and cur < stats["prev"])  # 어제보다 싸짐

        if error:
            price_html = f'<span class="err">조회실패: {html.escape(error)}</span>'
            mall, link = "-", ""
        elif best:
            mall = html.escape(best["mall"])
            link = best["link"]
            price_html = (f'<b class="hit">{cur:,}원</b> 📉' if is_low
                          else f'{cur:,}원')
        else:
            price_html = '<span class="err">결과 없음</span>'
            mall, link = "-", ""

        link_html = (f'<a href="{html.escape(link)}" target="_blank">보기 ↗</a>'
                     if link else "-")

        # 비슷한 상품(가격순 대안) + 네이버 검색 더보기
        alts_html = ""
        if best and best.get("alts"):
            lis = "".join(
                f'<a class="alt" target="_blank" href="{html.escape(a["link"] or ("https://search.shopping.naver.com/search/all?query=" + urllib.parse.quote(a["title"])))}">'
                f'{a["price"]:,}원 · {html.escape(a["title"][:30])} '
                f'<span class="amall">{html.escape(a["mall"])}</span> ↗</a>'
                for a in best["alts"]
            )
            q = urllib.parse.quote(item.get("query", item.get("name", "")))
            lis += (f'<a class="alt more" target="_blank" '
                    f'href="https://search.shopping.naver.com/search/all?query={q}">'
                    f'네이버에서 더 보기 ↗</a>')
            alts_html = (f'<details class="alts"><summary>비슷한 상품 {len(best["alts"])}개</summary>'
                         f'{lis}</details>')

        # 전일(이전 갱신) 대비 변동
        if best and stats and stats.get("prev") is not None:
            d = cur - stats["prev"]
            if d > 0:
                delta_html = f'<span class="up">▲ +{d:,}원</span>'
            elif d < 0:
                delta_html = f'<span class="dn">▼ -{abs(d):,}원</span>'
            else:
                delta_html = '<span class="prod">– 동일</span>'
        else:
            delta_html = '<span class="prod">첫 기록</span>'

        # 역대 최저 / 30일 평균
        if stats:
            low_html = f'{stats["min"]:,}원<div class="prod">{stats["min_date"]}</div>'
            avg_html = f'{stats["avg"]:,}원<div class="prod">{stats["days"]}일 기록</div>'
        else:
            low_html = "-"
            avg_html = '<span class="prod">기록 시작</span>'

        # 추이(스파크라인) + 정렬용 값
        spark_html = sparkline_svg(stats["series"]) if stats else '<span class="prod">–</span>'
        price_sort = cur if cur is not None else 99999999
        delta_sort = (cur - stats["prev"]) if (best and stats and stats.get("prev") is not None) else 0

        rows.append(
            f'<tr data-cat="{cat}" data-drop="{"y" if dropped else "n"}" '
            f'data-price="{price_sort}" data-delta="{delta_sort}" data-idx="{idx}">'
            f'<td class="name">{name}<div class="prod">{prod}</div>{alts_html}</td>'
            f'<td class="price" data-label="오늘 가격"><span class="cv">{price_html}</span></td>'
            f'<td data-label="전일 대비"><span class="cv">{delta_html}</span></td>'
            f'<td class="avg" data-label="역대 최저"><span class="cv">{low_html}</span></td>'
            f'<td class="avg" data-label="30일 평균"><span class="cv">{avg_html}</span></td>'
            f'<td class="avg" data-label="추이"><span class="cv">{spark_html}</span></td>'
            f'<td data-label="쇼핑몰"><span class="cv">{mall}</span></td>'
            f'<td data-label="링크"><span class="cv">{link_html}</span></td>'
            "</tr>"
        )

    cat_order = ["채소", "과일", "고기", "계란", "두부", "조미료"]
    present = [c for c in cat_order if any(r["item"].get("category") == c for r in results)]
    for r in results:
        c = r["item"].get("category")
        if c and c not in present:
            present.append(c)
    tabs = ['<span class="tab active" data-cat="전체">전체</span>']
    tabs += [f'<span class="tab" data-cat="{html.escape(c)}">{html.escape(c)}</span>'
             for c in present]

    page = (PAGE
            .replace("__UPDATED__", now)
            .replace("__MODE__", mode)
            .replace("__SUMMARY__", summary)
            .replace("__TABS__", "".join(tabs))
            .replace("__ROWS__", "\n".join(rows)))
    with open(OUTPUT_HTML, "w", encoding="utf-8") as f:
        f.write(page)


if __name__ == "__main__":
    run()
