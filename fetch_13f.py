"""
Fetches the latest 13F-HR holdings for a curated list of institutional managers
from SEC EDGAR and writes the result to data.json.

Runs server-side (e.g. in GitHub Actions) so there's no browser CORS restriction --
this is the same trick every real 13F tracker uses under the hood.

SEC asks automated tools to identify themselves with a descriptive User-Agent
(name + contact email). Edit CONTACT_EMAIL below before deploying.
"""

import json
import re
import time
import urllib.request
from datetime import datetime, timezone
from pathlib import Path

CONTACT_EMAIL = "Sashagilerman6@gmail.com"  
HEADERS = {"User-Agent": f"13F-Watch-Dashboard {CONTACT_EMAIL}"}

# Add or remove funds here. Find a CIK by searching https://www.sec.gov/cgi-bin/browse-edgar
FUNDS = [
    {"name": "Bridgewater Associates", "cik": "1350694"},
    {"name": "Berkshire Hathaway", "cik": "1067983"},
    {"name": "Citadel Advisors", "cik": "1423053"},
    {"name": "Millennium Management", "cik": "1273087"},
]

DATA_FILE = Path(__file__).parent / "data.json"
TOP_N = 10


def fetch(url: str) -> bytes:
    req = urllib.request.Request(url, headers=HEADERS)
    with urllib.request.urlopen(req, timeout=30) as resp:
        return resp.read()


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
    """Compare two top-N snapshots to flag adds/reduces/new/exited positions."""
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


def main():
    previous = {}
    if DATA_FILE.exists():
        try:
            old = json.loads(DATA_FILE.read_text())
            previous = {f["cik"]: f for f in old.get("funds", []) if not f.get("error")}
        except Exception:
            pass

    results = []
    for fund in FUNDS:
        try:
            latest = get_latest_13f(fund["cik"])
            if not latest:
                raise RuntimeError("no 13F-HR filing found")
            info_url = get_info_table_url(fund["cik"], latest["accession"])
            if not info_url:
                raise RuntimeError("info table document not found")
            xml_text = fetch(info_url).decode("utf-8", errors="ignore")
            top, total = parse_info_table(xml_text)

            prev_fund = previous.get(fund["cik"])
            prev_period = prev_fund.get("period") if prev_fund else None
            moves = None
            if prev_fund and prev_period != latest["period"]:
                moves = diff_holdings(prev_fund.get("holdings"), top)

            results.append({
                "key": fund["cik"],
                "name": latest["name"] or fund["name"],
                "cik": fund["cik"],
                "period": latest["period"],
                "filed": latest["filed"],
                "totalValue": total,
                "holdings": top,
                "moves": moves,
                "error": None,
            })
            print(f"OK: {fund['name']} -> {latest['period']} ({len(top)} holdings)")
        except Exception as e:
            results.append({
                "key": fund["cik"],
                "name": fund["name"],
                "cik": fund["cik"],
                "error": str(e),
            })
            print(f"FAILED: {fund['name']}: {e}")
        time.sleep(0.3)  # be polite to SEC's servers

    output = {
        "generatedAt": datetime.now(timezone.utc).isoformat(),
        "funds": results,
    }
    DATA_FILE.write_text(json.dumps(output, indent=2))
    print(f"Wrote {DATA_FILE}")


if __name__ == "__main__":
    main()
