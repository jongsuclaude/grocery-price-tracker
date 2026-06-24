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


# 대형 쇼핑몰(자체배송·무료배송 기준) — 작은 스마트스토어는 배송비로 표시가가 의미없어 제외
MAJOR_MALLS = [
    "이마트", "홈플러스", "쿠팡", "마켓컬리", "롯데마트", "롯데슈퍼", "롯데on", "롯데온",
    "ssg", "신세계몰", "gs the fresh", "gs프레시", "gs프레쉬", "현대hmall",
    "농협몰", "하나로마트", "오아시스마켓",
]


def is_major(mall):
    ml = (mall or "").lower()
    return any(k in ml for k in MAJOR_MALLS)


def parse_qty(title):
    """제목에서 용량/개수를 추출 → (기준수량, 단위유형). 못 찾으면 None.
    단위유형: 'g'(무게, g기준), 'ml'(부피, ml기준), 'ct'(개수)"""
    t = title.lower()
    m = re.search(r"(\d+\.?\d*)\s?kg", t)
    if m:
        return (float(m.group(1)) * 1000, "g")
    m = re.search(r"(\d+\.?\d*)\s?ml", t)
    if m:
        return (float(m.group(1)), "ml")
    m = re.search(r"(\d+\.?\d*)\s?l(?![a-z])", t)
    if m:
        return (float(m.group(1)) * 1000, "ml")
    m = re.search(r"(\d+\.?\d*)\s?g(?![a-z])", t)
    if m:
        return (float(m.group(1)), "g")
    m = re.search(r"(\d+)\s?(구|개|매|입|포|봉|통|모|미|알|장|수)", t)
    if m:
        return (float(m.group(1)), "ct")
    return None


def unit_label(price, amount, utype):
    """단가 문구 (100g당/100ml당/개당)"""
    if amount <= 0:
        return ""
    if utype == "g":
        return f"100g당 {round(price / amount * 100):,}원"
    if utype == "ml":
        return f"100ml당 {round(price / amount * 100):,}원"
    return f"개당 {round(price / amount):,}원"


def query_naver(item, client_id, client_secret):
    """정확도순으로 받아 부자재·낚시를 거른 뒤, 단가(그램당/개당) 최저를 우선 반환"""
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
    pool = [c for c in (cleans or anys) if is_major(c["mall"])]   # 대형몰만 고려 (소형몰 제외)
    if not pool:
        return None
    for c in pool:                       # 후보별 단가(그램당/개당) 계산 — 표시용
        q = parse_qty(c["title"])
        if q and q[0] > 0:
            amt, ut = q
            c["amount"], c["utype"] = amt, ut
            c["unit_key"] = c["price"] / amt
            c["unit_label"] = unit_label(c["price"], amt, ut)
        else:
            c["amount"] = c["utype"] = c["unit_key"] = None
            c["unit_label"] = ""

    # 가정용 크기 범위 안에서 '단가(그램당/개당) 최저'를 고른다.
    # 검색어에 용량 있으면 그 크기의 0.5~2.5배, 없으면 기본 상한(대용량 업소용 제외).
    # 기준 용량(config의 size, 없으면 검색어에서 추출)의 0.5~1.5배 범위에서 '단가 최저'를 고른다.
    # → 소량(200g) 함정도, 1~2배 대용량도 피하고 기준 크기 근처에서 그램당 싼 걸 선택.
    anchor = parse_qty(item.get("size") or item.get("query", ""))

    def in_band(c):
        if not (anchor and c["amount"] and c["utype"]):
            return False
        a_amt, a_ut = anchor
        return c["utype"] == a_ut and a_amt * 0.4 <= c["amount"] <= a_amt * 2.5

    band = [c for c in pool if in_band(c)]
    if not band:
        return None                              # 대형몰에 기준 크기 매물 없으면 노출 안 함
    utypes = [c["utype"] for c in band]
    dom = max(set(utypes), key=utypes.count)
    grp = sorted((c for c in band if c["utype"] == dom), key=lambda c: c["unit_key"])
    gids = {id(c) for c in grp}
    ordered = grp + sorted((c for c in pool if id(c) not in gids), key=lambda c: c["price"])
    best = dict(ordered[0])
    # 다른 대형몰 비교: 같은 크기(grp)만, 베스트 몰 제외, 묶음 이상치(>2.5배) 제외, 몰별 최저 1개
    best_price, best_mall = ordered[0]["price"], ordered[0]["mall"]
    by_mall = {}
    for c in grp:
        if c["mall"] == best_mall or c["price"] > best_price * 2.5:
            continue
        if c["mall"] not in by_mall or c["price"] < by_mall[c["mall"]]["price"]:
            by_mall[c["mall"]] = c
    best["alts"] = sorted(by_mall.values(), key=lambda c: c["price"])[:5]
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
  h1 { font-size: 22px; margin: 0; }
  .meta { color: #6e6e73; font-size: 11px; text-align: right; line-height: 1.5; }
  .hdr { display: flex; justify-content: space-between; align-items: center;
         gap: 8px 12px; flex-wrap: wrap; margin-bottom: 16px; }
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
  .nhead { display: flex; justify-content: space-between; align-items: flex-start; gap: 10px; }
  .nm { font-weight: 600; }
  .np { display: flex; flex-direction: column; align-items: flex-end;
        font-variant-numeric: tabular-nums; font-weight: 700; flex: none; }
  .unit { font-size: 11px; color: #86868b; font-weight: 400; }
  .price { font-variant-numeric: tabular-nums; }
  .hit { color: #1a7f37; }
  .target { color: #6e6e73; font-variant-numeric: tabular-nums; }
  .err { color: #c0392b; font-size: 12px; }
  a { color: #0066cc; text-decoration: none; }
  .summary { font-size: 15px; margin: 18px 0 8px; }
  .prod { color: #86868b; font-weight: 400; font-size: 12px; margin-top: 3px; }
  .prodlink { display: block; color: #0066cc; font-size: 12px; margin-top: 4px; text-decoration: none; line-height: 1.4; }
  .cmp { font-size: 12px; color: #515154; margin-top: 5px; line-height: 1.7; }
  .cmp .cmpl { color: #86868b; margin-right: 4px; }
  .cmp .alt { color: #0066cc; text-decoration: none; white-space: nowrap; }
  .cmpmore { display: inline-block; margin-top: 5px; font-size: 12px; color: #0066cc; text-decoration: none; }
  .note { color: #86868b; font-size: 12px; margin-top: 16px; line-height: 1.6; }
  details.alts { margin-top: 5px; }
  details.alts summary { font-size: 12px; color: #0066cc; cursor: pointer; }
  .alt { display: block; font-size: 12px; color: #0066cc; padding: 5px 0 0; text-decoration: none; line-height: 1.4; }
  .alt .amall { color: #86868b; }
  .alt.more { color: #0066cc; font-weight: 600; }
  .avg { color: #1d1d1f; font-variant-numeric: tabular-nums; }
  .dn { color: #1a7f37; font-weight: 600; }
  .up { color: #c0392b; font-weight: 600; }
  .tabs { display: flex; flex-wrap: nowrap; overflow-x: auto; gap: 6px; margin: 10px 0 14px;
          -webkit-overflow-scrolling: touch; scrollbar-width: none; }
  .tabs::-webkit-scrollbar { display: none; }
  .tab { font-size: 13px; padding: 5px 13px; border-radius: 999px; border: 1px solid #ddd;
         background: #fff; cursor: pointer; user-select: none; flex: none; white-space: nowrap; }
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
    body { margin: 12px auto; }
    h1 { font-size: 20px; }
    .meta { font-size: 10.5px; }
    table, thead, tbody, tr { display: block; width: auto; }
    thead { display: none; }
    tbody tr { background: #fff; border-radius: 14px; box-shadow: 0 1px 4px rgba(0,0,0,.05);
               padding: 13px 15px; margin-bottom: 9px; }
    td.name { display: block; padding: 0; margin: 0; border: none; }
    td:not(.name) { display: flex; justify-content: space-between; align-items: baseline;
                    gap: 12px; padding: 3px 0; border: none; font-size: 13px; }
    td:not(.name)::before { content: attr(data-label); color: #a1a1a6; font-size: 12px; flex: none; }
    td:not(.name) .cv { text-align: right; color: #3a3a3c; }
    td[data-label="전일 대비"] { margin-top: 9px; padding-top: 9px; border-top: 1px solid #f2f2f4; }
  }
</style></head><body>
<div class="hdr"><h1>🥬 식재료 최저가</h1>
  <div class="meta">__UPDATED__ (KST)<br>매일 오전 10시 갱신</div></div>
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
    <th>품목 · 오늘가격</th><th>전일 대비</th><th>역대 최저</th><th>30일 평균</th>
  </tr></thead>
  <tbody>__ROWS__</tbody>
</table>
<p class="note">※ 이마트몰·홈플러스·쿠팡·롯데·컬리·SSG·농협몰 등 대형몰에 올라온 품목만 표시해요
(소형 스마트스토어는 배송비 변수로 제외 — 그래서 대형몰에 없는 품목은 안 보여요).
가공·중량·옵션 차이가 있을 수 있으니 품목 아래 상품명도 같이 확인하세요.</p>
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


def write_dashboard(results, stats_map, mock_mode):
    now = datetime.now(KST).strftime("%Y-%m-%d %H:%M")
    mode = ('<span class="badge mock">목업 데이터</span>' if mock_mode
            else '<span class="badge live">실시간 · 네이버 쇼핑</span>')

    rows = []
    for idx, r in enumerate(results):
        item, best, error = r["item"], r["best"], r["error"]
        if best is None:                         # 대형몰 매물 없는 품목은 노출 안 함
            continue
        name = html.escape(item.get("name", "?"))
        prod = html.escape(best["title"][:42]) if best else ""
        cat = html.escape(item.get("category", "기타"))
        stats = stats_map.get(item.get("name", "?"))
        cur = best["price"] if best else None
        is_low = bool(best and stats and cur <= stats["min"])  # 오늘이 역대 최저면 강조
        dropped = bool(best and stats and stats.get("prev") is not None
                       and cur < stats["prev"])  # 어제보다 싸짐
        unit_top = (f'<span class="unit">{html.escape(best["unit_label"])}</span>'
                    if best and best.get("unit_label") else "")

        if error:
            price_html = f'<span class="err">조회실패: {html.escape(error)}</span>'
            mall, link = "-", ""
        elif best:
            mall = html.escape(best["mall"])
            link = best["link"]
            price_html = (f'<b class="hit">{cur:,}원</b>' if is_low
                          else f'{cur:,}원')
        else:
            price_html = '<span class="err">결과 없음</span>'
            mall, link = "-", ""

        # 매칭 상품 + 쇼핑몰 + 링크를 한 줄 클릭 링크로 통합
        if best:
            inner = f'{prod} · {mall}' if prod else mall
            prod_line = (f'<a class="prodlink" href="{html.escape(link)}" target="_blank">{inner} ↗</a>'
                         if link else f'<div class="prod">{inner}</div>')
        else:
            prod_line = ""

        # 다른 대형몰 가격 비교(몰별 1개)
        alts_html = ""
        if best and best.get("alts"):
            chips = " · ".join(
                f'<a class="alt" target="_blank" href="{html.escape(a["link"])}">'
                f'{html.escape(a["mall"])} <b>{a["price"]:,}원</b></a>'
                for a in best["alts"]
            )
            alts_html = f'<div class="cmp"><span class="cmpl">다른 대형몰</span> {chips}</div>'

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

        # 역대 최저 / 30일 평균 (가격만, 부가 표기 없음)
        if stats:
            low_html = f'{stats["min"]:,}원'
            avg_html = f'{stats["avg"]:,}원'
        else:
            low_html = "-"
            avg_html = "-"

        # 정렬용 값
        price_sort = cur if cur is not None else 99999999
        delta_sort = (cur - stats["prev"]) if (best and stats and stats.get("prev") is not None) else 0
        major_flag = "y" if (best and is_major(best["mall"])) else "n"

        rows.append(
            f'<tr data-cat="{cat}" data-drop="{"y" if dropped else "n"}" '
            f'data-major="{major_flag}" data-price="{price_sort}" data-delta="{delta_sort}" data-idx="{idx}">'
            f'<td class="name">'
            f'<div class="nhead"><span class="nm">{name}</span>'
            f'<span class="np">{price_html}{unit_top}</span></div>'
            f'{prod_line}{alts_html}</td>'
            f'<td data-label="전일 대비"><span class="cv">{delta_html}</span></td>'
            f'<td class="avg" data-label="역대 최저"><span class="cv">{low_html}</span></td>'
            f'<td class="avg" data-label="30일 평균"><span class="cv">{avg_html}</span></td>'
            "</tr>"
        )

    cat_order = ["채소", "과일", "고기", "계란", "두부", "조미료"]
    shown = [r for r in results if r["best"]]
    present = [c for c in cat_order if any(r["item"].get("category") == c for r in shown)]
    for r in shown:
        c = r["item"].get("category")
        if c and c not in present:
            present.append(c)
    tabs = ['<span class="tab active" data-cat="전체">전체</span>']
    tabs += [f'<span class="tab" data-cat="{html.escape(c)}">{html.escape(c)}</span>'
             for c in present]

    page = (PAGE
            .replace("__UPDATED__", now)
            .replace("__MODE__", mode)
            .replace("__TABS__", "".join(tabs))
            .replace("__ROWS__", "\n".join(rows)))
    with open(OUTPUT_HTML, "w", encoding="utf-8") as f:
        f.write(page)


if __name__ == "__main__":
    run()
