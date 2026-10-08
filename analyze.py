#!/usr/bin/env python3
"""Measure a Claude Code session's real token cost from its transcript.

Usage:
  python3 analyze.py                 # most recently modified transcript
  python3 analyze.py <sessionId>     # a specific session
  python3 analyze.py /path/to.jsonl  # an explicit transcript path
  python3 analyze.py --list          # recent transcripts, newest first
"""
import json, sys, os, glob, collections, statistics, re

PROJECTS = os.path.expanduser("~/.claude/projects")

# $ per 1M tokens. 'cw1h' = 1-hour-TTL cache write (2x base write).
PRICES = {
    "opus":   dict(inp=5,    out=25,   cr=0.5,   cw=6.25,  cw1h=10),
    "sonnet": dict(inp=3,    out=15,   cr=0.3,   cw=3.75,  cw1h=6),
    "haiku":  dict(inp=0.8,  out=4,    cr=0.08,  cw=1.0,   cw1h=1.6),
}

def family(model):
    m = (model or "").lower()
    for k in PRICES:
        if k in m:
            return k
    return None

def find_transcripts():
    return sorted(glob.glob(os.path.join(PROJECTS, "*", "*.jsonl")),
                  key=os.path.getmtime, reverse=True)

def resolve(arg):
    if arg and os.path.exists(arg):
        return arg
    all_t = find_transcripts()
    if not all_t:
        sys.exit(f"No transcripts under {PROJECTS}")
    if arg:
        hits = [t for t in all_t if arg in os.path.basename(t)]
        if not hits:
            sys.exit(f"No transcript matching {arg!r}")
        return hits[0]
    return all_t[0]

def load(path):
    """Return (calls, turns, snapshots, model).

    calls: one entry per UNIQUE requestId. Several assistant lines share a
    requestId (thinking / text / tool_use blocks of one API call) and each
    repeats the same usage object -- counting lines instead of requestIds
    overcounts by ~15x.
    """
    calls, snaps, turns = [], [], []
    seen = set()
    turn = 0
    model = None
    for line in open(path, errors="replace"):
        try:
            d = json.loads(line)
        except Exception:
            continue
        t = d.get("type")
        if t == "cost-state":
            snaps.append(d)
        elif t == "user":
            c = (d.get("message") or {}).get("content")
            is_result = isinstance(c, list) and any(
                isinstance(b, dict) and b.get("type") == "tool_result" for b in c)
            if not is_result:
                txt = c if isinstance(c, str) else " ".join(
                    b.get("text", "") for b in c if isinstance(b, dict))
                if txt.strip():
                    turn += 1
                    turns.append((turn, txt.strip()[:70].replace("\n", " ")))
        elif t == "assistant":
            rid = d.get("requestId")
            m = d.get("message") or {}
            model = model or m.get("model")
            tools = [b for b in (m.get("content") or [])
                     if isinstance(b, dict) and b.get("type") == "tool_use"]
            if rid in seen:
                # same API call: tools already counted below, usage identical
                if calls and tools:
                    calls[-1]["tools"] += [b.get("name") for b in tools]
                    calls[-1]["cmds"] += [(b.get("input") or {}).get("command", "")
                                          for b in tools if b.get("name") == "Bash"]
                continue
            seen.add(rid)
            u = m.get("usage") or {}
            its = u.get("iterations") or [u]
            calls.append(dict(
                turn=turn, rid=rid,
                cr=sum(i.get("cache_read_input_tokens", 0) for i in its),
                cw=sum(i.get("cache_creation_input_tokens", 0) for i in its),
                out=sum(i.get("output_tokens", 0) for i in its),
                inp=sum(i.get("input_tokens", 0) for i in its),
                think=(u.get("output_tokens_details") or {}).get("thinking_tokens", 0),
                cw1h=sum((i.get("cache_creation") or {}).get("ephemeral_1h_input_tokens", 0)
                         for i in its),
                tools=[b.get("name") for b in tools],
                cmds=[(b.get("input") or {}).get("command", "")
                      for b in tools if b.get("name") == "Bash"],
            ))
    return calls, turns, snaps, model

def verify_pricing(snaps, fam):
    """cost-state records go stale, but early ones are exact -- use them to
    confirm the price table instead of trusting either blindly."""
    if not fam:
        return None, "unknown model; cost cannot be priced"
    if not snaps:
        return PRICES[fam]["cw1h"], "no cost-state records; pricing ASSUMED, not verified"
    p = PRICES[fam]
    for s in snaps:
        mu = (s.get("modelUsage") or {}).get(
            next(iter(s.get("modelUsage") or {}), ""), {})
        want = s.get("totalCostUSD")
        if not want or not mu:
            continue
        for label, cwrate in (("1h", p["cw1h"]), ("5m", p["cw"])):
            got = (mu.get("inputTokens", 0) * p["inp"]
                   + mu.get("outputTokens", 0) * p["out"]
                   + mu.get("cacheReadInputTokens", 0) * p["cr"]
                   + mu.get("cacheCreationInputTokens", 0) * cwrate) / 1e6
            if want and abs(got - want) / want < 0.01:
                return cwrate, f"verified against a cost-state snapshot ({label} cache write)"
    return p["cw1h"], "could NOT reconcile with cost-state; treat cost as approximate"

def money(calls, fam, cwrate):
    if not fam or cwrate is None:
        return None
    p = PRICES[fam]
    return sum(c["cr"] * p["cr"] + c["cw"] * cwrate + c["out"] * p["out"]
               + c["inp"] * p["inp"] for c in calls) / 1e6

def main():
    arg = next((a for a in sys.argv[1:] if not a.startswith("-")), None)
    if "--list" in sys.argv:
        for t in find_transcripts()[:15]:
            print(f"{os.path.getmtime(t):.0f}  {os.path.getsize(t)//1024:>7}K  {t}")
        return
    path = resolve(arg)
    calls, turns, snaps, model = load(path)
    if not calls:
        sys.exit("No assistant messages with usage data in that transcript.")
    fam = family(model)
    cwrate, note = verify_pricing(snaps, fam)
    p = PRICES.get(fam)
    T = collections.Counter()
    for c in calls:
        for k in ("cr", "cw", "out", "inp", "think"):
            T[k] += c[k]
    total = money(calls, fam, cwrate)
    n = len(calls)

    print(f"\n=== SESSION COST REVIEW ===")
    print(f"transcript : {path}")
    print(f"model      : {model}  ({note})")
    if snaps:
        last = snaps[-1].get("totalCostUSD")
        print(f"cost-state : ${last:.2f} <- stale/partial, shown only for contrast")

    print(f"\n-- WHERE THE MONEY WENT --")
    rows = [("cache read", T["cr"], p and p["cr"]), ("cache write", T["cw"], cwrate),
            ("output", T["out"], p and p["out"]), ("input", T["inp"], p and p["inp"])]
    print(f"{'component':13}{'tokens':>16}{'$':>10}{'%':>8}")
    for name, tok, rate in rows:
        cost = tok * rate / 1e6 if rate else 0  # 0 when unpriced
        pct = (100 * cost / total) if total else 0
        print(f"{name:13}{tok:>16,}{cost:>10.2f}{pct:>7.1f}%")
    print(f"{'TOTAL':13}{sum(r[1] for r in rows):>16,}{(total or 0):>10.2f}")
    print(f"\nthinking {T['think']:,} tok (subset of output)")
    print(f"{n} API calls | {len(turns)} turns | avg context {T['cr']//n:,}/call "
          f"| avg ${ (total/n) if total else 0:.3f}/call")

    # ---- batching: the usual #1 lever ----
    with_tools = [c for c in calls if c["tools"]]
    ntools = sum(len(c["tools"]) for c in with_tools)
    dist = collections.Counter(len(c["tools"]) for c in with_tools)
    print(f"\n-- BATCHING --")
    print(f"tools per call: {dict(sorted(dist.items()))}")
    if with_tools:
        avg = ntools / len(with_tools)
        print(f"{len(with_tools)} calls issued {ntools} tools (avg {avg:.2f})")
        solo = dist.get(1, 0)
        print(f"{solo} of {len(with_tools)} calls carried exactly ONE tool "
              f"({100*solo/len(with_tools):.0f}%)")
        if avg < 1.5 and total:
            saved = (len(with_tools) - ntools / 3) * (total / n)
            print(f"  -> batching independent calls ~3/msg could save "
                  f"roughly ${saved:.0f} of ${total:.0f}")

    names = collections.Counter(t for c in calls for t in c["tools"])
    print(f"\ntool calls ({sum(names.values())}): "
          + ", ".join(f"{k} {v}" for k, v in names.most_common(8)))
    cmds = [x for c in calls for x in c["cmds"] if x]
    if cmds:
        verbs = collections.Counter()
        for cmd in cmds:
            c2 = re.sub(r"^cd \S+ && ", "", cmd.strip())
            mm = re.match(r"([\w.-]+)", c2)
            verbs[mm.group(1) if mm else "?"] += 1
        print("top bash verbs: " + ", ".join(f"{k} {v}" for k, v in verbs.most_common(8)))
        dupes = len(cmds) - len(set(cmds))
        if dupes:
            print(f"exact duplicate commands: {dupes} of {len(cmds)}")

    # ---- context growth ----
    crs = [c["cr"] for c in calls]
    third = max(1, n // 3)
    print(f"\n-- CONTEXT GROWTH --")
    print(f"first third avg {statistics.mean(crs[:third]):,.0f} | "
          f"last third avg {statistics.mean(crs[-third:]):,.0f} | peak {max(crs):,}")
    if total:
        early = (money(calls[:third], fam, cwrate) or 0) / third
        late = (money(calls[-third:], fam, cwrate) or 0) / third
        if early > 0:
            print(f"$/call: {early:.3f} early -> {late:.3f} late  ({late/early:.1f}x)")

    # ---- cold-cache spikes ----
    spikes = sorted(calls, key=lambda c: -c["cw"])[:5]
    if spikes and spikes[0]["cw"] > 100_000:
        print(f"\n-- CACHE-WRITE SPIKES (cold cache / 1h TTL expiry; writes cost ~20x reads) --")
        for c in spikes:
            if c["cw"] > 50_000:
                cost = (c["cw"] * cwrate / 1e6) if p else 0
                print(f"  turn {c['turn']:>3}  {c['cw']:>9,} tok written  ${cost:.2f}")

    # ---- per-turn ----
    agg = collections.defaultdict(lambda: collections.Counter())
    for c in calls:
        a = agg[c["turn"]]
        a["n"] += 1
        for k in ("cr", "cw", "out", "inp"):
            a[k] += c[k]
    pm = dict(turns)
    scored = []
    for t, a in agg.items():
        cost = (a["cr"] * p["cr"] + a["cw"] * cwrate + a["out"] * p["out"]
                + a["inp"] * p["inp"]) / 1e6 if p else 0
        scored.append((cost, t, a["n"]))
    scored.sort(reverse=True)
    print(f"\n-- MOST EXPENSIVE TURNS --")
    print(f"{'turn':>5}{'calls':>7}{'$':>9}  prompt")
    for cost, t, cn in scored[:10]:
        print(f"{t:>5}{cn:>7}{cost:>9.2f}  {pm.get(t,'')[:58]}")
    print()

if __name__ == "__main__":
    main()
