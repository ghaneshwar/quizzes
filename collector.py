"""Weekly collector: researches each active watchlist entry and updates questions.json.

Only answers that pass the gate (high confidence + 2 independent source domains) become
"verified". Changed answers are held in "proposed" until you approve them.
"""
import datetime
import json
import os
import re
from urllib.parse import urlparse

import anthropic

MODEL = os.environ.get("MODEL", "claude-sonnet-5-5")  # change here if the model name is retired
MAX_ENTRIES = int(os.environ.get("MAX_ENTRIES", "60"))  # cost safety cap per run
today = datetime.date.today()
client = anthropic.Anthropic(api_key=os.environ["ANTHROPIC_API_KEY"].strip())  # strip guards against pasted whitespace

PROMPT = """Today is {date}. Research this pub-quiz question using web search:
{q}
{hint}
Prefer official or major news sources. Reply with ONLY a JSON object, no other text:
{{"answer": "short answer", "detail": "one factual sentence", "confidence": "high|medium|low", "sources": ["urls you relied on"]}}
If the event has not happened yet or no answer is confirmed, use "answer": null and "confidence": "low"."""


def load(path, default):
    try:
        with open(path, encoding="utf-8") as f:
            return json.load(f)
    except FileNotFoundError:
        return default


def save(path, obj):
    with open(path, "w", encoding="utf-8") as f:
        json.dump(obj, f, indent=1, ensure_ascii=False)


def due(e):
    if "months" in e and today.month not in e["months"] and not os.environ.get("BACKFILL"):
        return False
    if "years" in e and today.year not in e["years"]:
        return False
    return True


def norm(s):
    return re.sub(r"[^a-z0-9]", "", (s or "").lower())


def ask(q, hint):
    msg = client.messages.create(
        model=MODEL,
        max_tokens=1000,
        tools=[{"type": "web_search_20250305", "name": "web_search", "max_uses": 3}],
        messages=[{"role": "user", "content": PROMPT.format(date=today.isoformat(), q=q, hint=hint)}],
    )
    seen, text = set(), ""
    for b in msg.content:
        if b.type == "web_search_tool_result" and isinstance(b.content, list):
            seen.update(getattr(x, "url", "") for x in b.content)
        elif b.type == "text":
            text += b.text
    m = re.search(r"\{.*\}", text, re.S)
    if not m:
        return None
    data = json.loads(m.group(0))
    # keep only sources that really appeared in the search results
    data["sources"] = [u for u in data.get("sources", []) if u in seen]
    return data


def main():
    watch = load("watchlist.json", [])
    recs = {r["id"]: r for r in load("questions.json", [])}
    report, runs, fails, aborted = [], 0, 0, False

    # apply approvals: ids listed in approve.json promote their proposed answer
    for rid in load("approve.json", []):
        r = recs.get(rid)
        if r and r.get("proposed"):
            p = r.pop("proposed")
            r.update(answer=p["answer"], detail=p.get("detail", ""), sources=p["sources"], status="verified")

    for e in watch:
        if not due(e) or runs >= MAX_ENTRIES:
            continue
        yearly = "{year}" in e["q"]
        rid = f"{e['id']}-{today.year}" if yearly else e["id"]
        q = e["q"].replace("{year}", str(today.year))
        rec = recs.get(rid)
        # dated events: once verified, stop spending on them
        if rec and rec["status"] == "verified" and "months" in e and not e.get("recheck"):
            continue
        runs += 1
        try:
            r = ask(q, e.get("hint", ""))
            fails = 0
        except Exception as ex:  # includes running out of credit
            print(f"FAILED {rid}: {ex!r} | cause: {ex.__cause__!r}")
            fails += 1
            if fails >= 3:
                print("Three failures in a row; stopping (check credit/key).")
                aborted = True
                break
            continue
        if not r or not r.get("answer"):
            print(f"no answer yet: {rid}")
            continue
        domains = {urlparse(u).netloc.removeprefix("www.") for u in r["sources"]}
        ok = r.get("confidence") == "high" and len(domains) >= 2
        new = {"answer": r["answer"], "detail": r.get("detail", ""), "sources": r["sources"]}
        now = today.isoformat()
        if rec is None:
            recs[rid] = dict(id=rid, category=e["cat"], question=q, year=today.year,
                             checked=now, status="verified" if ok else "review", **new)
            if not ok:
                report.append(f"- NEEDS REVIEW `{rid}`: {q} -> {r['answer']} ({r.get('confidence')}, {len(domains)} source domains)")
        elif rec["status"] == "verified":
            rec["checked"] = now
            if ok and norm(r["answer"]) == norm(rec["answer"]):
                rec["sources"] = r["sources"]
            elif ok:
                rec["proposed"] = new
                report.append(f"- CHANGED `{rid}`: {q} was '{rec['answer']}', now '{r['answer']}'. Approve by adding \"{rid}\" to approve.json")
        else:  # was in review
            rec["checked"] = now
            if ok:
                rec.update(status="verified", **new)

    save("questions.json", sorted(recs.values(), key=lambda r: r["id"]))
    save("approve.json", [])
    if report:
        with open("review_report.md", "w", encoding="utf-8") as f:
            f.write("Items needing your attention:\n\n" + "\n".join(report) + "\n")
    print(f"Done: {runs} checked, {len(report)} flagged.")
    if aborted:
        raise SystemExit(1)


if __name__ == "__main__":
    main()
