#!/usr/bin/env python3
"""
국내 ETF 수급 데이터 수집 스크립트
Naver Finance API + KRX 투자자별 매매동향을 통해 ETF 데이터를 수집합니다.
GitHub Actions에서 매일 장 마감 후 자동 실행됩니다.
"""

import json
import sys
import time
from datetime import datetime, timedelta
from pathlib import Path
import requests

import re

HEADERS = {"User-Agent": "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7)"}

SECTOR_KEYWORDS = {
    "시장대표": ["KOSPI200", "KOSPI 200", "KOSDAQ150", "KOSDAQ 150", "KRX300", "코스피200", "코스닥150", "코스피 200", "코스닥 150", "TOP10플러스", "코리아TOP"],
    "반도체/HBM/AI": ["반도체", "HBM", "AI반도체", "소부장", "전공정", "메모리", "필라델피아"],
    "AI전력/변압기": ["전력", "변압기", "전선", "전력기기", "전력설비", "전력인프라", "전력핵심"],
    "2차전지/배터리": ["2차전지", "배터리", "리튬", "에너지저장", "ESS"],
    "바이오/헬스케어": ["바이오", "헬스케어", "제약", "의료", "게놈", "건강관리"],
    "조선/방산/우주": ["조선", "방산", "우주", "항공", "국방"],
    "자동차/로봇": ["자동차", "로봇", "자율주행", "모빌리티", "휴머노이드", "기계"],
    "금융/보험/증권": ["금융", "은행", "보험", "증권"],
    "배당/밸류업": ["배당", "밸류업", "고배당", "저변동", "퀄리티", "가치", "커버드콜"],
    "소비재/화장품": ["소비재", "화장품", "뷰티", "음식료", "필수소비", "미디어", "엔터"],
    "원전/신재생": ["원전", "신재생", "에너지", "태양광", "풍력", "수소", "탄소"],
    "레버리지/인버스": ["레버리지", "인버스", "2X", "곱버스", "단일종목"],
}


def classify_sector(name):
    upper = name.upper()
    for sector, keywords in SECTOR_KEYWORDS.items():
        for kw in keywords:
            if kw.upper() in upper:
                return sector
    return "기타/혼합"


def extract_mgr(name):
    prefixes = [
        ("KODEX", "삼성"), ("KoAct", "삼성"), ("TIGER", "미래에셋"),
        ("SOL", "신한"), ("ACE", "한투"), ("RISE", "KB"), ("KB ", "KB"),
        ("HANARO", "NH-A"), ("PLUS", "한화"), ("TIMEFOLIO", "타임폴리오"),
        ("ARIRANG", "한화"), ("BNK", "BNK"), ("히어로즈", "키움"),
    ]
    for prefix, mgr in prefixes:
        if name.startswith(prefix):
            return mgr
    return "기타"


def fetch_naver_etf_list():
    """Naver Finance ETF 전종목 시세 (국내 상장 전체)"""
    print("[1/3] Naver Finance ETF 전종목 시세 수집...")
    url = "https://finance.naver.com/api/sise/etfItemList.nhn"
    params = {"etfType": "0", "targetColumn": "market_sum", "sortOrder": "desc"}
    resp = requests.get(url, params=params, headers=HEADERS, timeout=30)
    data = resp.json()
    items = data.get("result", {}).get("etfItemList", [])
    print(f"  → {len(items)}개 ETF 수집 완료")
    return items


def fetch_naver_etf_detail(code):
    """개별 ETF 상세 (구성종목 등)"""
    url = f"https://finance.naver.com/api/sise/etfItemShareDetail.nhn?itemcode={code}"
    try:
        resp = requests.get(url, headers=HEADERS, timeout=10)
        return resp.json().get("result", {})
    except Exception:
        return {}


def fetch_investor_data_krx(date_str):
    """KRX 투자자별 매매동향 (ETF) - OTP 방식 대신 open API 시도"""
    print(f"[2/3] 투자자별 매매동향 수집 시도 ({date_str})...")

    # KRX OPEN API (공개 데이터)
    url = "https://data-dbg.krx.co.kr/svc/apis/etp/etf_bydd_trd"
    params = {"basDd": date_str}
    try:
        resp = requests.get(url, params=params, headers=HEADERS, timeout=15)
        if resp.status_code == 200:
            data = resp.json()
            print(f"  → KRX OPEN API 성공: {len(data.get('OutBlock_1', []))}건")
            return data.get("OutBlock_1", [])
    except Exception as e:
        print(f"  → KRX OPEN API 실패: {e}")

    print("  → 투자자별 데이터 없이 진행")
    return []


def fetch_investor_from_naver(code):
    """Naver 외국인/기관 매매동향 페이지에서 최신일 순매수 주수 추출"""
    try:
        r = requests.get(
            f"https://finance.naver.com/item/frgn.naver?code={code}",
            headers=HEADERS, timeout=8,
        )
        tds = re.findall(r"<td[^>]*>(.*?)</td>", r.text, re.DOTALL)
        for i, td in enumerate(tds):
            if re.search(r"\d{4}\.\d{2}\.\d{2}", td) and i + 8 < len(tds):
                vals = [re.sub(r"<[^>]+>", "", tds[j]).strip() for j in range(i, i + 9)]
                def parse_int(s):
                    s = s.replace(",", "").replace("+", "")
                    return int(s) if s.lstrip("-").isdigit() else 0
                return {
                    "date": vals[0],
                    "inst_shares": parse_int(vals[5]),
                    "frgn_shares": parse_int(vals[6]),
                }
    except Exception:
        pass
    return None


def detect_latest_trade_date():
    """Naver에서 KODEX 200의 최신 거래일을 감지"""
    inv = fetch_investor_from_naver("069500")
    if inv and inv.get("date"):
        raw = inv["date"].replace(".", "")
        print(f"  → 최신 거래일 감지: {inv['date']}")
        return raw
    return None


def detect_price_date():
    """Naver 시세 API에서 실제 데이터 날짜 감지 (KODEX 200 일별 시세)"""
    try:
        r = requests.get(
            "https://api.finance.naver.com/siseJson.naver",
            params={"symbol": "069500", "requestType": 1, "startTime": "20260101",
                    "endTime": "20261231", "timeframe": "day"},
            headers=HEADERS, timeout=10,
        )
        dates = re.findall(r'"(\d{8})"', r.text)
        if dates:
            latest = dates[-1]
            print(f"  → 시세 최신 거래일 감지: {latest}")
            return latest
    except Exception:
        pass
    return None


def fetch_investor_batch(etf_items, max_count=120):
    """상위 ETF들의 기관/외국인 순매수 일괄 수집"""
    print(f"[3/4] 상위 {max_count}개 ETF 투자자별 순매수 수집 (Naver 스크래핑)...")
    result = {}
    detected_date = None
    for i, item in enumerate(etf_items[:max_count]):
        code = item["itemcode"]
        inv = fetch_investor_from_naver(code)
        if inv:
            close_price = item.get("nowVal", 0) or 1
            result[code] = {
                "inst_amt": round(inv["inst_shares"] * close_price / 100000000, 1),
                "frgn_amt": round(inv["frgn_shares"] * close_price / 100000000, 1),
                "inst_shares": inv["inst_shares"],
                "frgn_shares": inv["frgn_shares"],
            }
            if detected_date is None and inv.get("date"):
                detected_date = inv["date"].replace(".", "")
        if (i + 1) % 20 == 0:
            print(f"  → {i+1}/{max_count} 완료")
        time.sleep(0.2)
    print(f"  → {len(result)}개 ETF 투자자 데이터 수집 완료")
    return result, detected_date


def fetch_top_holdings(codes, max_count=50):
    """상위 ETF의 구성종목 수집 (Naver API 가용 시)"""
    print(f"[4/4] 구성종목 수집 스킵 (API 제한)")
    return {code: [] for code in codes[:max_count]}


def build_dataset(date_str):
    """전체 데이터셋 빌드. 실제 데이터 날짜를 감지하여 반환."""
    # 1. Naver ETF 시세
    naver_items = fetch_naver_etf_list()
    time.sleep(1)

    # 1.5. 실제 시세 날짜 감지
    price_date = detect_price_date()
    time.sleep(0.5)

    # 2. 투자자별 순매수 (Naver 스크래핑)
    inv_map, investor_date = fetch_investor_batch(naver_items, max_count=120)
    time.sleep(1)

    # 실제 데이터 날짜 결정 (투자자 데이터 날짜 > 시세 날짜 > 입력 날짜)
    actual_date = investor_date or price_date or date_str
    if actual_date != date_str:
        print(f"\n⚠️  입력 날짜({date_str}) ≠ 실제 데이터 날짜({actual_date}) → 실제 날짜로 저장")
        date_str = actual_date

    # 3. 구성종목 (상위 ETF만 - 현재 스킵)
    top_codes = [item["itemcode"] for item in naver_items[:80]]
    holdings_map = fetch_top_holdings(top_codes)

    # 데이터셋 빌드
    etf_list = []
    for item in naver_items:
        code = item["itemcode"]
        name = item["itemname"]
        sector = classify_sector(name)
        mgr = extract_mgr(name)

        aum = item.get("marketSum", 0) or 0  # 억원
        change_rate = item.get("changeRate", 0) or 0
        three_month_ret = item.get("threeMonthEarnRate", None)
        trade_val = item.get("amonut", 0) or 0  # 백만원 → 억원
        trade_val_billion = round(trade_val / 100, 1) if trade_val else 0
        close_price = item.get("nowVal", 0) or 0

        # 투자자별 순매수 (Naver 스크래핑 - 기관/외국인만)
        inv = inv_map.get(code, {})
        institutional = inv.get("inst_amt", 0)
        foreign = inv.get("frgn_amt", 0)
        individual = 0  # Naver에서 개인은 직접 제공 안 함
        fin_invest = 0

        # 자금유입 추정 = 기관 + 외국인 순매수 합계 (proxy)
        flow_estimate = round(institutional + foreign, 1)

        # 구성종목
        holdings = holdings_map.get(code, [])

        entry = {
            "code": code,
            "name": name,
            "sector": sector,
            "mgr": mgr,
            "close": close_price,
            "changeRate": change_rate,
            "aum": aum,
            "ret1d": change_rate,
            "ret1w": None,
            "ret1m": None,
            "ret3m": three_month_ret,
            "tradeVal": trade_val_billion,
            "individual": individual,
            "foreign": foreign,
            "institutional": institutional,
            "finInvest": fin_invest,
            "flowEstimate": flow_estimate,
            "holdings": holdings,
        }
        etf_list.append(entry)

    # 순자산 100억 이상만
    etf_list = [e for e in etf_list if (e.get("aum") or 0) >= 100]
    etf_list.sort(key=lambda x: abs(x.get("aum") or 0), reverse=True)

    print(f"\n필터링 후 ETF 수: {len(etf_list)}")
    return etf_list, date_str


def build_sector_summary(etf_list):
    sectors = {}
    for e in etf_list:
        s = e["sector"]
        if s not in sectors:
            sectors[s] = {"sector": s, "count": 0, "totalAum": 0, "totalFlow": 0,
                          "totalTradeVal": 0, "returns": []}
        sectors[s]["count"] += 1
        sectors[s]["totalAum"] += e.get("aum") or 0
        sectors[s]["totalFlow"] += e.get("flowEstimate") or 0
        sectors[s]["totalTradeVal"] += e.get("tradeVal") or 0
        if e.get("ret1d") is not None:
            sectors[s]["returns"].append(e["ret1d"])

    result = []
    for s, d in sectors.items():
        avg_ret = round(sum(d["returns"]) / len(d["returns"]), 2) if d["returns"] else None
        result.append({
            "sector": s, "count": d["count"],
            "totalAum": round(d["totalAum"]), "totalFlow": round(d["totalFlow"], 1),
            "totalTradeVal": round(d["totalTradeVal"], 1), "avgReturn1d": avg_ret,
        })
    result.sort(key=lambda x: x["totalFlow"], reverse=True)
    return result


def build_market_summary(etf_list, date_str):
    total_individual = sum(e.get("individual") or 0 for e in etf_list)
    total_foreign = sum(e.get("foreign") or 0 for e in etf_list)
    total_institutional = sum(e.get("institutional") or 0 for e in etf_list)

    flow_sorted = sorted(etf_list, key=lambda x: x.get("flowEstimate") or 0, reverse=True)
    ret_sorted = sorted([e for e in etf_list if e.get("ret1d") is not None],
                        key=lambda x: x["ret1d"], reverse=True)

    return {
        "date": date_str,
        "totalIndividual": round(total_individual, 1),
        "totalForeign": round(total_foreign, 1),
        "totalInstitutional": round(total_institutional, 1),
        "topInflow": [{"name": e["name"], "sector": e["sector"], "flow": e.get("flowEstimate", 0)} for e in flow_sorted[:10]],
        "topOutflow": [{"name": e["name"], "sector": e["sector"], "flow": e.get("flowEstimate", 0)} for e in flow_sorted[-10:][::-1]],
        "topGainers": [{"name": e["name"], "sector": e["sector"], "ret1d": e["ret1d"]} for e in ret_sorted[:10]],
        "topLosers": [{"name": e["name"], "sector": e["sector"], "ret1d": e["ret1d"]} for e in ret_sorted[-10:][::-1]],
    }


def main():
    if len(sys.argv) > 1:
        date_str = sys.argv[1]
    else:
        now = datetime.now()
        if now.weekday() == 5:
            now -= timedelta(days=1)
        elif now.weekday() == 6:
            now -= timedelta(days=2)
        date_str = now.strftime("%Y%m%d")

    print(f"=== ETF 수급 데이터 수집 시작 (입력: {date_str}) ===\n")

    etf_list, actual_date = build_dataset(date_str)
    date_str = actual_date  # 실제 데이터 날짜 사용
    sector_summary = build_sector_summary(etf_list)
    market_summary = build_market_summary(etf_list, date_str)

    weekday_kr = ["월", "화", "수", "목", "금", "토", "일"]
    dt = datetime.strptime(date_str, "%Y%m%d")
    day_name = weekday_kr[dt.weekday()]
    formatted = f"{date_str[:4]}.{date_str[4:6]}.{date_str[6:8]} ({day_name})"

    result = {
        "meta": {
            "date": date_str,
            "dateFormatted": formatted,
            "fetchedAt": datetime.now().isoformat(),
            "filteredCount": len(etf_list),
        },
        "market": market_summary,
        "sectors": sector_summary,
        "etfs": etf_list,
    }

    data_dir = Path(__file__).parent.parent / "data"
    data_dir.mkdir(exist_ok=True)

    latest_path = data_dir / "latest.json"
    archive_path = data_dir / f"{date_str}.json"

    with open(latest_path, "w", encoding="utf-8") as f:
        json.dump(result, f, ensure_ascii=False, indent=2)
    print(f"\n✅ {latest_path}")

    with open(archive_path, "w", encoding="utf-8") as f:
        json.dump(result, f, ensure_ascii=False, indent=2)
    print(f"✅ {archive_path}")

    index_path = data_dir / "index.json"
    if index_path.exists():
        with open(index_path) as f:
            index = json.load(f)
    else:
        index = {"dates": []}

    if date_str not in index["dates"]:
        index["dates"].insert(0, date_str)
        index["dates"] = index["dates"][:90]

    with open(index_path, "w", encoding="utf-8") as f:
        json.dump(index, f, ensure_ascii=False, indent=2)

    print(f"\n=== 완료! ETF {len(etf_list)}개, 섹터 {len(sector_summary)}개 ===")


if __name__ == "__main__":
    main()
