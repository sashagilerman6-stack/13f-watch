"""
Fetches the latest 13F-HR holdings for a curated list of institutional managers
PLUS up to MAX_TOTAL_FUNDS discovered automatically from SEC's own quarterly
filer index, and writes the result to data.json.

Runs server-side (e.g. in GitHub Actions) so there's no browser CORS restriction.

SEC asks automated tools to identify themselves with a descriptive User-Agent
(name + contact email).
"""

import json
import re
import time
import urllib.request
from datetime import datetime, timezone, timedelta
from pathlib import Path

CONTACT_EMAIL = "Sashagilerman6@gmail.com"
HEADERS = {"User-Agent": f"13F-Watch-Dashboard {CONTACT_EMAIL}"}

# Always included, fetched precisely via SEC's submissions API.
PRIORITY_FUNDS = [
    {"name": "Bridgewater Associates", "cik": "1350694"},
    {"name": "Berkshire Hathaway", "cik": "1067983"},
    {"name": "Citadel Advisors", "cik": "1423053"},
    {"name": "Millennium Management", "cik": "1273087"},
]

# Total fund cap (priority + auto-discovered). Tune down if a run times out
# or you want a faster/lighter site.
MAX_TOTAL_FUNDS = 500

DATA_FILE = Path(__file__).parent / "data.json"
TOP_N = 10
REQUEST_DELAY = 0.15  # seconds between every HTTP request, to stay well under SEC's rate limits


def fetch(url: str) -> bytes:
    req = urllib.request.Request(url, headers=HEADERS)
    with urllib.request.urlopen(req, timeout=30) as resp:
        data = resp.read()
    time.sleep(REQUEST_DELAY)
    return data


def get_latest_13f(cik: str):
    cik_padded = cik.zfill(10)
    data = json.loads(fetch(f"https://data.sec.gov/submissions/CIK{cik_padded}.json"))
    recent = data["filings"]["recent"]
    for i, form in enumerate(recent["form"]):
        if form.startswith("13F-HR"):
            return {
                "name": data.get("name"),
                "accession": recent["accessionNumber"][i],
                "filed": recent["filingDate"][i],
                "period": recent["reportDate"][i],
            }
    return None


def get_info_table_url(cik: str, accession: str):
    cik_num = str(int(cik))
    acc_nodash = accession.replace("-", "")
    idx_url = f"https://www.sec.gov/Archives/edgar/data/{cik_num}/{acc_nodash}/index.json"
    idx = json.loads(fetch(idx_url))
    items = idx["directory"]["item"]
    candidate = next(
        (it for it in items if "info" in it["name"].lower() and it["name"].endswith(".xml")),
        None,
    )
    if not candidate:
        candidate = next(
            (it for it in items if it["name"].endswith(".xml") and "primary_doc" not in it["name"]),
            None,
        )
    if not candidate:
        return None
    return f"https://www.sec.gov/Archives/edgar/data/{cik_num}/{acc_nodash}/{candidate['name']}"


def parse_info_table(xml_text: str, top_n: int = TOP_N):
    blocks = re.split(r"<(?:\w+:)?infoTable>", xml_text, flags=re.IGNORECASE)[1:]
    holdings = {}

    def get(block, tag):
        m = re.search(rf"<(?:\w+:)?{tag}>([^<]*)<", block, flags=re.IGNORECASE)
        return m.group(1).strip() if m else ""

    for block in blocks:
        name = get(block, "nameOfIssuer")
        if not name:
            continue
        value = float(get(block, "value") or 0) * 1000  # reported in thousands
        shares = float(get(block, "sshPrnamt") or 0)
        if name not in holdings:
            holdings[name] = {"name": name, "value": 0.0, "shares": 0.0}
        holdings[name]["value"] += value
        holdings[name]["shares"] += shares

    ranked = sorted(holdings.values(), key=lambda h: -h["value"])
    total = sum(h["value"] for h in ranked)
    top = ranked[:top_n]
    for h in top:
        h["weight"] = round(h["value"] / total * 100, 2) if total else 0
    return top, total


def diff_holdings(old_top, new_top):
    old_by_name = {h["name"]: h for h in (old_top or [])}
    new_by_name = {h["name"]: h for h in new_top}
    added, reduced, new_pos, exited = [], [], [], []
    for name, h in new_by_name.items():
        if name in old_by_name:
            delta = h["value"] - old_by_name[name]["value"]
            if delta > 0:
                added.append({"name": name, "delta": round(delta, 2)})
            elif delta < 0:
                reduced.append({"name": name, "delta": round(delta, 2)})
        else:
            new_pos.append(name)
    for name in old_by_name:
        if name not in new_by_name:
            exited.append(name)
    added.sort(key=lambda x: -x["delta"])
    reduced.sort(key=lambda x: x["delta"])
    return {"added": added, "reduced": reduced, "newPos": new_pos, "exited": exited}


def approx_period(filed_str: str) -> str:
    """13F filings don't carry the period in the bulk index -- estimate the
    quarter-end date from the filing date (filed ~45 days after quarter end)."""
    try:
        filed_dt = datetime.strptime(filed_str, "%Y-%m-%d")
    except ValueError:
        return ""
    guess = filed_dt - timedelta(days=45)
    candidates = []
    for y in (guess.year - 1, guess.year, guess.year + 1):
        for m, d in ((3, 31), (6, 30), (9, 30), (12, 31)):
            candidates.append(datetime(y, m, d))
    best = min(candidates, key=lambda d: abs((d - guess).days))
    return best.strftime("%Y-%m-%d")


def discover_bulk_funds(max_new: int, exclude_ciks: set):
    """Pull CIK/name/accession for 13F-HR filers from SEC's official quarterly
    full-index -- the real master list of every institutional manager that
    filed, not a hand-picked guess."""
    today = datetime.now(timezone.utc)
    quarter = (today.month - 1) // 3 + 1
    url = f"https://www.sec.gov/Archives/edgar/full-index/{today.year}/QTR{quarter}/form.idx"
    try:
        raw = fetch(url).decode("latin-1", errors="ignore")
    except Exception as e:
        print(f"Could not fetch full-index ({url}): {e}")
        return []

    line_re = re.compile(r"^(\S+)\s+(.+?)\s+(\d+)\s+(\d{4}-\d{2}-\d{2})\s+(\S+)\s*$")
    found = []
    seen = set(exclude_ciks)
    for line in raw.splitlines():
        if not line.startswith("13F-HR "):  # excludes 13F-HR/A and 13F-NT
            continue
        m = line_re.match(line)
        if not m:
            continue
        form_type, company, cik, filed, filename = m.groups()
        if form_type != "13F-HR" or cik in seen:
            continue
        fm = re.search(r"/(\d{10}-\d{2}-\d{6})\.txt$", filename)
        if not fm:
            continue
        seen.add(cik)
        found.append({
            "name": company.strip(),
            "cik": cik,
            "accession": fm.group(1),
            "filed": filed,
        })
        if len(found) >= max_new:
            break
    print(f"Discovered {len(found)} funds from {url}")
    return found


def process_fund(fund: dict, previous: dict):
    cik = fund["cik"]
    try:
        if fund.get("accession"):
            accession = fund["accession"]
            filed = fund.get("filed", "")
            name = fund.get("name") or cik
            period = approx_period(filed)
        else:
            latest = get_latest_13f(cik)
            if not latest:
                raise RuntimeError("no 13F-HR filing found")
            accession = latest["accession"]
            filed = latest["filed"]
            period = latest["period"]
            name = latest["name"] or fund["name"]

        info_url = get_info_table_url(cik, accession)
        if not info_url:
            raise RuntimeError("info table document not found")
        xml_text = fetch(info_url).decode("utf-8", errors="ignore")
        top, total = parse_info_table(xml_text)

        prev_fund = previous.get(cik)
        prev_period = prev_fund.get("period") if prev_fund else None
        moves = None
        if prev_fund and prev_period != period:
            moves = diff_holdings(prev_fund.get("holdings"), top)

        return {
            "key": cik, "name": name, "cik": cik, "period": period, "filed": filed,
            "totalValue": total, "holdings": top, "moves": moves, "error": None,
        }
    except Exception as e:
        return {"key": cik, "name": fund.get("name", cik), "cik": cik, "error": str(e)}


def main():
    previous = {}
    if DATA_FILE.exists():
        try:
            old = json.loads(DATA_FILE.read_text())
            previous = {f["cik"]: f for f in old.get("funds", []) if not f.get("error")}
        except Exception:
            pass

    priority_ciks = {f["cik"] for f in PRIORITY_FUNDS}
    remaining_slots = max(0, MAX_TOTAL_FUNDS - len(PRIORITY_FUNDS))
    bulk_funds = discover_bulk_funds(remaining_slots, priority_ciks)
    all_funds = PRIORITY_FUNDS + bulk_funds

    results = []
    ok_count, fail_count = 0, 0
    for i, fund in enumerate(all_funds, 1):
        r = process_fund(fund, previous)
        results.append(r)
        if r.get("error"):
            fail_count += 1
        else:
            ok_count += 1
        if i % 25 == 0:
            print(f"...{i}/{len(all_funds)} processed ({ok_count} ok, {fail_count} failed)")

    print(f"Done: {ok_count} ok, {fail_count} failed, {len(results)} total")

    output = {
        "generatedAt": datetime.now(timezone.utc).isoformat(),
        "funds": results,
    }
    DATA_FILE.write_text(json.dumps(output, indent=2))
    print(f"Wrote {DATA_FILE}")


if __name__ == "__main__":
    main()
