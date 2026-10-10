#!/usr/bin/env python3
"""Measure a Claude Code session's real token cost from its transcripts.

Counts the main transcript AND every subagent transcript, prices each API call
by its own model / cache-write TTL / speed, and attributes context to tools.

Usage:
  python3 analyze.py                    # current session ($CLAUDE_CODE_SESSION_ID), else newest
  python3 analyze.py <sessionId>        # a specific session (prefix match ok)
  python3 analyze.py /path/to.jsonl     # an explicit main transcript
  python3 analyze.py --list             # recent sessions, newest first
Options:
  --prices FILE    rates file (default: prices.json next to this script)
  --cutoff ISO     ignore calls at/after this timestamp; 'none' disables.
                   Default for the CURRENT session: the latest human prompt,
                   so the analysis does not measure itself.
  --report PATH    also write the output to a Markdown file
  --main-only      skip subagent transcripts (not a session total!)

Exit codes: 0 ok, 2 not found / bad args, 3 SCHEMA CHECK FAILED -- the
transcript format changed and the numbers cannot be trusted; inspect the
schema and fix this script rather than working around it.
"""
import json, sys, os, glob, collections, statistics, re, bisect, datetime

PROJECTS = os.path.expanduser("~/.claude/projects")
HERE = os.path.dirname(os.path.abspath(__file__))
PRICE_STALE_DAYS = 30
RATE_KEYS = ("input", "output", "cache_read", "cache_write_5m", "cache_write_1h")
USAGE_KEYS = {"input_tokens", "output_tokens", "cache_read_input_tokens",
              "cache_creation_input_tokens"}
KNOWN_TYPES = {"user", "assistant", "system", "attachment", "queue-operation",
               "last-prompt", "custom-title", "file-history-snapshot",
               "file-history-delta", "atis-latch", "agent-name", "cost-state",
               "summary", "progress", "mode", "pr-link"}
KNOWN_SPEEDS = {None, "standard", "fast"}
KNOWN_GEOS = {None, "global", "not_available", "us"}
DATED = re.compile(r"(-\d{8}|\[[^\]]*\])*$")   # snapshot date / [1m] suffixes


class SchemaError(Exception):
    pass


def ts(s):
    if not s:
        return None
    try:
        return datetime.datetime.fromisoformat(s.replace("Z", "+00:00"))
    except ValueError:
        return None


# ---------------------------------------------------------------- discovery

def main_transcripts():
    return sorted(glob.glob(os.path.join(PROJECTS, "*", "*.jsonl")),
                  key=os.path.getmtime, reverse=True)


def subagent_files(main_path):
    d = os.path.join(main_path[:-len(".jsonl")], "subagents")
    return sorted(glob.glob(os.path.join(d, "**", "agent-*.jsonl"), recursive=True))


def stray_jsonl(main_path):
    """.jsonl files in the session dir that are not where subagents should be:
    a sign the layout changed and something is being missed."""
    d = main_path[:-len(".jsonl")]
    known = set(subagent_files(main_path))
    return [p for p in glob.glob(os.path.join(d, "**", "*.jsonl"), recursive=True)
            if p not in known]


def resolve(arg):
    if arg and os.path.exists(arg):
        return arg
    all_t = main_transcripts()
    if not all_t:
        die(2, f"No transcripts under {PROJECTS}")
    want = arg or os.environ.get("CLAUDE_CODE_SESSION_ID")
    if want:
        hits = [t for t in all_t if os.path.basename(t).startswith(want)]
        if hits:
            return hits[0]
        if arg:
            die(2, f"No transcript matching {arg!r}")
    return all_t[0]


def die(code, msg):
    print(msg, file=sys.stderr)
    sys.exit(code)


# ---------------------------------------------------------------- parsing

def text_len(content):
    """(chars, images) of a tool_result's content."""
    if isinstance(content, str):
        return len(content), 0
    chars = imgs = 0
    for b in content or []:
        if isinstance(b, dict):
            if b.get("type") == "image":
                imgs += 1
            else:
                chars += len(b.get("text") or "")
    return chars, imgs


def tool_label(b):
    i = b.get("input") or {}
    lab = i.get("command") or i.get("file_path") or i.get("pattern") \
        or i.get("skill") or i.get("description") or i.get("url") or ""
    lab = re.sub(r"\s+", " ", str(lab))
    return ("…" + lab[-59:]) if i.get("file_path") and len(lab) > 60 else lab[:60]


def parse(path, thread, cutoff, stats):
    """One transcript -> dict(calls, results, compacts, prompts).

    calls: one per UNIQUE requestId. One API call is split over several
    assistant lines (thinking / text / tool_use) that each repeat the same
    usage object -- counting lines overcounts ~15x.
    """
    calls, results, compacts, prompts = [], [], [], []
    by_rid = {}
    uses = {}                    # tool_use id -> (name, label, call)
    for idx, line in enumerate(open(path, errors="replace")):
        try:
            d = json.loads(line)
        except Exception:
            stats["malformed"] += 1
            continue
        t = d.get("type")
        if t not in KNOWN_TYPES:
            stats["unknown_types"][t] += 1
        when = ts(d.get("timestamp"))
        if cutoff and when and when >= cutoff:
            stats["after_cutoff"] += 1
            continue
        if t == "system" and d.get("subtype") == "compact_boundary":
            compacts.append(idx)
        elif t == "user":
            c = (d.get("message") or {}).get("content")
            blocks = c if isinstance(c, list) else []
            res = [b for b in blocks if isinstance(b, dict) and b.get("type") == "tool_result"]
            for b in res:
                chars, imgs = text_len(b.get("content"))
                name, label, call = uses.get(b.get("tool_use_id"), ("?", "", None))
                results.append(dict(idx=idx, name=name, label=label, chars=chars,
                                    imgs=imgs, turn=call["turn"] if call else None,
                                    thread=thread))
            if not res and not d.get("isMeta") and thread == "main":
                txt = c if isinstance(c, str) else " ".join(
                    b.get("text", "") for b in blocks if isinstance(b, dict))
                txt = re.sub(r"<system-reminder>.*?</system-reminder>", "", txt, flags=re.S)
                if txt.strip():
                    prompts.append(dict(idx=idx, when=when,
                                        text=txt.strip()[:70].replace("\n", " ")))
        elif t == "assistant":
            stats["assistant_lines"] += 1
            m = d.get("message") or {}
            rid = d.get("requestId") or m.get("id") or f"line{idx}"
            if not d.get("requestId"):
                stats["no_request_id"] += 1
            tools = [b for b in (m.get("content") or [])
                     if isinstance(b, dict) and b.get("type") == "tool_use"]
            if m.get("model") == "<synthetic>":
                stats["synthetic"] += 1     # client-generated, never billed
                continue
            call = by_rid.get(rid)
            if call is None:
                u = m.get("usage")
                if u is None:
                    stats["no_usage"] += 1
                    u = {}
                stats["usage_keys"].update(u.keys())
                its = u.get("iterations") or [u]
                g = lambda k: sum(i.get(k) or 0 for i in its)
                cc = lambda k: sum((i.get("cache_creation") or {}).get(k) or 0 for i in its)
                cw = g("cache_creation_input_tokens")
                cw5, cw1 = cc("ephemeral_5m_input_tokens"), cc("ephemeral_1h_input_tokens")
                if cw and cw5 + cw1 != cw:
                    stats["cw_unsplit_calls"] += 1
                    cw5 = max(0, cw - cw1)      # unsplit remainder priced as 5m
                speed = u.get("speed")
                geo = u.get("inference_geo")
                if geo not in KNOWN_GEOS:
                    stats["unknown_geo"][geo] += 1
                if speed not in KNOWN_SPEEDS:
                    stats["unknown_speed"][speed] += 1
                call = dict(
                    idx=idx, rid=rid, thread=thread, when=when, turn=len(prompts),
                    model=m.get("model") or "unknown",
                    effort=d.get("effort") or "unknown", speed=speed or "standard", geo=geo,
                    skill=d.get("attributionSkill"),
                    inp=g("input_tokens"), cr=g("cache_read_input_tokens"),
                    cw5=cw5, cw1=cw1, out=g("output_tokens"),
                    think=(u.get("output_tokens_details") or {}).get("thinking_tokens") or 0,
                    server=dict((k, v) for k, v in (u.get("server_tool_use") or {}).items() if v),
                    tools=[], cmds=[])
                by_rid[rid] = call
                calls.append(call)
            for b in tools:
                call["tools"].append(b.get("name"))
                if b.get("name") == "Bash":
                    call["cmds"].append((b.get("input") or {}).get("command", ""))
                uses[b.get("id")] = (b.get("name"), tool_label(b), call)
    stats["files"] += 1
    return dict(calls=calls, results=results, compacts=compacts, prompts=prompts,
                uses=uses)


def load_session(main_path, cutoff, main_only, stats):
    main = parse(main_path, "main", cutoff, stats)
    threads = {"main": main}
    owner = {k: v[2] for k, v in main["uses"].items()}     # tool_use id -> call
    subs = [] if main_only else subagent_files(main_path)
    metas = []
    for p in subs:
        mp = p[:-len(".jsonl")] + ".meta.json"
        try:
            meta = json.load(open(mp))
        except Exception:
            meta = {}
            stats["missing_meta"] += 1
        metas.append((meta.get("spawnDepth") or 1, p, meta))
    metas.sort(key=lambda x: x[0])                          # parents before children
    for _, p, meta in metas:
        aid = os.path.basename(p)[len("agent-"):-len(".jsonl")]
        label = f"{meta.get('agentType', '?')}: {(meta.get('description') or aid)[:40]}"
        th = parse(p, aid, cutoff, stats)
        th["label"], th["agentType"] = label, meta.get("agentType", "?")
        parent = owner.get(meta.get("toolUseId"))
        if parent is None:
            stats["orphan_subagents"] += 1
        th["parent_turn"] = parent["turn"] if parent else None
        for c in th["calls"]:
            c["turn"] = th["parent_turn"]
        for r in th["results"]:
            r["turn"] = th["parent_turn"]
        owner.update({k: v[2] for k, v in th["uses"].items()})
        threads[aid] = th
    spawned = {k for k, v in main["uses"].items() if v[0] == "Agent"}
    linked = {m.get("toolUseId") for _, _, m in metas}
    stats["agent_calls_without_transcript"] = 0 if main_only else len(spawned - linked)
    return threads


def schema_check(threads, stats, main_path):
    """Fail LOUD when the format no longer matches what this script expects.
    Silent drift is how the previous version under-reported by ~80%."""
    fatal, warn = [], []
    calls = [c for th in threads.values() for c in th["calls"]]
    if not threads["main"]["calls"]:
        fatal.append("main transcript has no assistant calls")
    if calls and not (stats["usage_keys"] & USAGE_KEYS):
        fatal.append(f"no known usage fields; saw {sorted(stats['usage_keys'])[:12]}")
    zero = sum(1 for c in calls if not (c["inp"] or c["cr"] or c["cw5"] or c["cw1"] or c["out"]))
    if calls and zero / len(calls) > 0.9:
        fatal.append(f"{zero}/{len(calls)} calls have all-zero usage -- fields moved?")
    stray = stray_jsonl(main_path)
    if stray:
        fatal.append(f"{len(stray)} .jsonl file(s) outside subagents/ not analysed, "
                     f"e.g. {stray[0]} -- subagent layout changed?")
    if stats["malformed"]:
        warn.append(f"{stats['malformed']} malformed line(s) skipped")
    if stats["no_usage"]:
        warn.append(f"{stats['no_usage']} assistant call(s) without a usage object")
    if stats["no_request_id"]:
        warn.append(f"{stats['no_request_id']} assistant line(s) without requestId "
                    "(deduped by message id)")
    if stats["cw_unsplit_calls"]:
        warn.append(f"{stats['cw_unsplit_calls']} call(s) whose 5m/1h cache-write split "
                    "does not sum to the total; remainder priced at the 5m rate")
    if stats["unknown_geo"]:
        warn.append(f"unknown usage.inference_geo values (no multiplier applied): "
                    f"{dict(stats['unknown_geo'])}")
    if stats["unknown_speed"]:
        warn.append(f"unknown usage.speed values: {dict(stats['unknown_speed'])}")
    if stats["agent_calls_without_transcript"]:
        warn.append(f"{stats['agent_calls_without_transcript']} Agent call(s) in the main "
                    "thread with no subagent transcript (remote/background, or missing)")
    if stats["orphan_subagents"]:
        warn.append(f"{stats['orphan_subagents']} subagent transcript(s) whose parent "
                    "call was not found (turn attribution unknown)")
    if stats["missing_meta"]:
        warn.append(f"{stats['missing_meta']} subagent(s) without a .meta.json")
    if stats["unknown_types"]:
        warn.append("unrecognised record types (ignored): "
                    + ", ".join(f"{k} {v}" for k, v in stats["unknown_types"].most_common(5)))
    return fatal, warn


# ---------------------------------------------------------------- pricing

def load_prices(path):
    if not os.path.exists(path):
        return None, f"no prices file at {path} -- tokens only, cost UNPRICED"
    try:
        P = json.load(open(path))
    except Exception as e:
        return None, f"prices file unreadable ({e}) -- cost UNPRICED"
    note = f"rates from {P.get('source', '?')} retrieved {P.get('retrieved', '?')}"
    got = ts((P.get("retrieved") or "") + "T00:00:00+00:00")
    if not got:
        note += " -- NO RETRIEVAL DATE, treat as unverified"
    else:
        age = (datetime.datetime.now(datetime.timezone.utc) - got).days
        if age > PRICE_STALE_DAYS:
            note += f" -- STALE ({age} days old): refresh before quoting dollars"
    return P, note


def rates_for(P, c):
    """Rates for one call: exact model id, else a configured id followed only by
    a snapshot date or [..] suffix. No family guessing -- 'claude-opus-5-7'
    must NOT inherit 'claude-opus-5' rates; an unknown model stays unpriced."""
    if not P:
        return None
    models, model = P.get("models") or {}, c["model"]
    r = models.get(model)
    if r is None:
        keys = [k for k in models if model.startswith(k) and DATED.fullmatch(model[len(k):])]
        r = models[max(keys, key=len)] if keys else None
    if r is None:
        return None
    lc = r.get("long_context")
    if lc and c["inp"] + c["cr"] + c["cw5"] + c["cw1"] > lc.get("threshold", float("inf")):
        r = lc
    if c["speed"] != "standard":
        r = r.get(c["speed"])         # e.g. "fast": {...}; absent -> unpriced
    if not r or any(k not in r for k in RATE_KEYS):
        return None
    mult = (P.get("geo_multipliers") or {}).get(c["geo"], 1)
    return {k: r[k] * mult for k in RATE_KEYS}


def price(calls, P):
    for c in calls:
        r = rates_for(P, c)
        if r is None:
            c["cost"] = None
            continue
        c["cost"] = (c["inp"] * r["input"] + c["cr"] * r["cache_read"]
                     + c["cw5"] * r["cache_write_5m"] + c["cw1"] * r["cache_write_1h"]
                     + c["out"] * r["output"]) / 1e6
        c["rates"] = r


def server_cost(calls, P):
    per = (P or {}).get("server_tools") or {}
    n = collections.Counter()
    for c in calls:
        n.update(c["server"])
    return n, {k: v * per[k] for k, v in n.items() if k in per}


# ---------------------------------------------------------------- report

class Out:
    def __init__(self):
        self.lines = []

    def __call__(self, s=""):
        self.lines.append(s)
        print(s)

    def table(self, head, rows, align=None):
        align = align or ["l"] + ["r"] * (len(head) - 1)
        self("| " + " | ".join(head) + " |")
        self("|" + "|".join("---:" if a == "r" else "---" for a in align) + "|")
        for r in rows:
            self("| " + " | ".join(str(x) for x in r) + " |")
        self()


def usd(x):
    return "unpriced" if x is None else f"${x:,.2f}"


def ssum(xs):
    """Sum of costs; None if every element is unpriced."""
    xs = list(xs)
    priced = [x for x in xs if x is not None]
    return sum(priced) if priced else None


def report(main_path, threads, stats, fatal, warn, P, pnote, cutoff, cut_reason, out):
    calls = [c for th in threads.values() for c in th["calls"]]
    price(calls, P)
    total = ssum(c["cost"] for c in calls)
    unpriced = [c for c in calls if c["cost"] is None]
    n = len(calls)
    main = threads["main"]
    subs = [th for k, th in threads.items() if k != "main"]

    out(f"# Session cost review — {os.path.basename(main_path)[:-6]}")
    out()
    out(f"- transcript: `{main_path}`")
    out(f"- threads: main + {len(subs)} subagent(s); {stats['files']} file(s) parsed")
    out(f"- cutoff: {cutoff.isoformat() if cutoff else 'none'} ({cut_reason}); "
        f"{stats['after_cutoff']} record(s) excluded")
    out(f"- pricing: {pnote}")
    if unpriced:
        missing = collections.Counter((c["model"], c["speed"]) for c in unpriced)
        out(f"- **UNPRICED calls: {len(unpriced)}/{n}** — "
            + ", ".join(f"{m} ({s}) ×{k}" for (m, s), k in missing.items())
            + ". Dollar totals below are PARTIAL.")
    for w in warn:
        out(f"- warning: {w}")
    out()

    # -- component matrix
    T = collections.Counter()
    C = collections.defaultdict(float)
    for c in calls:
        for k in ("inp", "cr", "cw5", "cw1", "out", "think"):
            T[k] += c[k]
        r = c.get("rates")
        if r:
            C["cr"] += c["cr"] * r["cache_read"] / 1e6
            C["cw5"] += c["cw5"] * r["cache_write_5m"] / 1e6
            C["cw1"] += c["cw1"] * r["cache_write_1h"] / 1e6
            C["out"] += c["out"] * r["output"] / 1e6
            C["inp"] += c["inp"] * r["input"] / 1e6
    out("## Where the money went")
    out()
    rows = []
    for k, name in (("cr", "cache read"), ("cw5", "cache write 5m"), ("cw1", "cache write 1h"),
                    ("out", "output"), ("inp", "input (uncached)")):
        share = f"{100 * C[k] / total:.1f}%" if total else "—"
        rows.append((name, f"{T[k]:,}", usd(C[k]) if total is not None else "unpriced", share))
    rows.append(("**TOTAL**", f"{sum(T[k] for k in ('cr','cw5','cw1','out','inp')):,}",
                 f"**{usd(total)}**", ""))
    out.table(["component", "tokens", "cost", "share"], rows)
    srv, srv_cost = server_cost(calls, P)
    if srv:
        out(f"server tools: {dict(srv)}; priced {usd(sum(srv_cost.values()) if srv_cost else None)}"
            " (separate from token cost)")
    tin = T["inp"] + T["cr"] + T["cw5"] + T["cw1"]
    out(f"thinking {T['think']:,} tok (subset of output) | cache hit rate "
        f"{100 * T['cr'] / tin:.1f}% | {n} API calls | {len(main['prompts'])} turns | "
        f"avg context {(tin // n) if n else 0:,}/call | avg {usd(total / n if total else None)}/call")
    out()

    # -- thread x model x effort x speed
    out("## Matrix: thread × model × effort × speed")
    out()
    grp = collections.defaultdict(list)
    for key, th in threads.items():
        kind = "main" if key == "main" else f"sub:{th['agentType']}"
        for c in th["calls"]:
            grp[(kind, c["model"], c["effort"], c["speed"])].append(c)
    rows = []
    for (kind, model, eff, sp), cs in sorted(grp.items(), key=lambda kv: -(ssum(c["cost"] for c in kv[1]) or 0)):
        cost = ssum(c["cost"] for c in cs)
        cin = sum(c["inp"] + c["cr"] + c["cw5"] + c["cw1"] for c in cs)
        rows.append((kind, model, eff, sp, len(cs), f"{sum(c['cr'] for c in cs):,}",
                     f"{sum(c['cw5'] + c['cw1'] for c in cs):,}", f"{sum(c['out'] for c in cs):,}",
                     f"{100 * sum(c['cr'] for c in cs) / cin:.0f}%" if cin else "—",
                     usd(cost), f"{100 * cost / total:.1f}%" if total and cost else "—"))
    out.table(["thread", "model", "effort", "speed", "calls", "cache read", "cache write",
               "output", "hit", "cost", "share"], rows, ["l"] * 4 + ["r"] * 7)
    if subs:
        sub_cost = ssum(c["cost"] for th in subs for c in th["calls"])
        out(f"subagents: {len(subs)} transcripts, {usd(sub_cost)} "
            f"({100 * sub_cost / total:.0f}% of total)" if total and sub_cost else
            f"subagents: {len(subs)} transcripts")
        top = sorted(subs, key=lambda th: -(ssum(c["cost"] for c in th["calls"]) or 0))[:5]
        out.table(["most expensive subagents", "turn", "calls", "cost"],
                  [(th["label"], th["parent_turn"], len(th["calls"]),
                    usd(ssum(c["cost"] for c in th["calls"]))) for th in top])

    # -- skills
    sk = collections.defaultdict(list)
    for c in calls:
        if c["skill"]:
            sk[c["skill"]].append(c)
    if sk:
        out("## Cost while a skill was active (attributionSkill)")
        out()
        out.table(["skill", "calls", "cost"],
                  [(k, len(v), usd(ssum(c["cost"] for c in v)))
                   for k, v in sorted(sk.items(), key=lambda kv: -(ssum(c["cost"] for c in kv[1]) or 0))[:8]])

    # -- batching
    out("## Batching")
    out()
    with_tools = [c for c in calls if c["tools"]]
    ntools = sum(len(c["tools"]) for c in with_tools)
    dist = collections.Counter(len(c["tools"]) for c in with_tools)
    out(f"tools per call: {dict(sorted(dist.items()))}")
    if with_tools:
        avg = ntools / len(with_tools)
        solo = dist.get(1, 0)
        out(f"{len(with_tools)} calls issued {ntools} tools (avg {avg:.2f}); "
            f"{solo} ({100 * solo / len(with_tools):.0f}%) carried exactly ONE tool")
        if avg < 1.5 and total:
            saved = (len(with_tools) - ntools / 3) * (total / n)
            out(f"-> batching independent calls ~3/msg could save roughly ${saved:.0f} "
                f"of ${total:.0f} (estimate; overlaps with other levers)")
    names = collections.Counter(t for c in calls for t in c["tools"])
    out(f"tool calls ({sum(names.values())}): "
        + ", ".join(f"{k} {v}" for k, v in names.most_common(10)))
    cmds = [x for c in calls for x in c["cmds"] if x]
    if cmds:
        verbs = collections.Counter()
        for cmd in cmds:
            c2 = re.sub(r"^cd \S+ && ", "", cmd.strip())
            mm = re.match(r"([\w.-]+)", c2)
            verbs[mm.group(1) if mm else "?"] += 1
        out("top bash verbs: " + ", ".join(f"{k} {v}" for k, v in verbs.most_common(8)))
        dupes = len(cmds) - len(set(cmds))
        if dupes:
            out(f"exact duplicate commands: {dupes} of {len(cmds)}")
    out()

    # -- tool results: what fills the context
    out("## Tool results: what fills the context")
    out()
    out("Measured: result characters. Estimated (~4 chars/token, APPROXIMATE): tokens, and "
        "re-read exposure = result tokens × later calls in the same thread before compaction. "
        "Exact per-tool billing is not in the log; do not add these to the totals above.")
    out()
    per = collections.defaultdict(lambda: dict(n=0, chars=0, imgs=0, exp=0, mx=0))
    allres = []
    for th in threads.values():
        cidx = [c["idx"] for c in th["calls"]]
        comp = th["compacts"]
        for r in th["results"]:
            nxt = comp[bisect.bisect_right(comp, r["idx"])] if bisect.bisect_right(comp, r["idx"]) < len(comp) else float("inf")
            later = bisect.bisect_left(cidx, nxt) - bisect.bisect_right(cidx, r["idx"])
            r["exp"] = (r["chars"] // 4) * max(0, later)
            a = per[r["name"]]
            a["n"] += 1; a["chars"] += r["chars"]; a["imgs"] += r["imgs"]
            a["exp"] += r["exp"]; a["mx"] = max(a["mx"], r["chars"])
            allres.append(r)
    allchars = sum(a["chars"] for a in per.values()) or 1
    out.table(["tool", "results", "chars", "share", "~tokens", "largest", "~re-read exposure", "images"],
              [(k, a["n"], f"{a['chars']:,}", f"{100 * a['chars'] / allchars:.0f}%",
                f"{a['chars'] // 4:,}", f"{a['mx']:,}", f"{a['exp']:,}", a["imgs"] or "")
               for k, a in sorted(per.items(), key=lambda kv: -kv[1]["exp"])[:12]])
    out.table(["largest results", "thread", "turn", "chars", "~re-read exposure"],
              [(f"{r['name']} `{r['label']}`", "main" if r["thread"] == "main" else "sub",
                r["turn"], f"{r['chars']:,}", f"{r['exp']:,}")
               for r in sorted(allres, key=lambda r: -r["exp"])[:8]], ["l", "l", "r", "r", "r"])

    # -- context growth (main thread)
    mc = main["calls"]
    if len(mc) >= 3:
        third = max(1, len(mc) // 3)
        ctx = [c["cr"] + c["cw5"] + c["cw1"] + c["inp"] for c in mc]
        out("## Context growth (main thread)")
        out()
        out(f"first third avg {statistics.mean(ctx[:third]):,.0f} | last third avg "
            f"{statistics.mean(ctx[-third:]):,.0f} | peak {max(ctx):,} | "
            f"{len(main['compacts'])} compaction(s)")
        e, l = ssum(c["cost"] for c in mc[:third]), ssum(c["cost"] for c in mc[-third:])
        if e and l:
            out(f"$/call: {e / third:.3f} early -> {l / third:.3f} late ({l / e:.1f}x)")
        out()

    # -- cold-cache spikes, with the idle gap that preceded them
    for th in threads.values():
        prev = None
        for c in th["calls"]:
            c["gap"] = (c["when"] - prev).total_seconds() / 60 if prev and c["when"] else None
            prev = c["when"] or prev
    spikes = [c for c in sorted(calls, key=lambda c: -(c["cw5"] + c["cw1"]))[:6]
              if c["cw5"] + c["cw1"] > 50_000]
    if spikes:
        out("## Cache-write spikes (cold cache; a write costs 12-40x a read)")
        out()
        out.table(["turn", "thread", "tokens written", "idle gap before", "cost"],
                  [(c["turn"], "main" if c["thread"] == "main" else "sub",
                    f"{c['cw5'] + c['cw1']:,}",
                    f"{c['gap']:.0f} min" if c["gap"] is not None else "first call",
                    usd((c["cw5"] * c["rates"]["cache_write_5m"] + c["cw1"] * c["rates"]["cache_write_1h"]) / 1e6
                        if c.get("rates") else None)) for c in spikes])

    # -- per turn (subagent cost rolled into the turn that spawned it)
    agg = collections.defaultdict(list)
    for c in calls:
        agg[c["turn"]].append(c)
    pm = {i + 1: p["text"] for i, p in enumerate(main["prompts"])}
    out("## Most expensive turns (incl. their subagents)")
    out()
    out.table(["turn", "calls", "cost", "prompt"],
              [(t if t is not None else "?", len(cs), usd(ssum(c["cost"] for c in cs)),
                pm.get(t, "")[:58].replace("|", "/"))
               for t, cs in sorted(agg.items(), key=lambda kv: -(ssum(c["cost"] for c in kv[1]) or 0))[:10]],
              ["r", "r", "r", "l"])


def list_sessions():
    cur = os.environ.get("CLAUDE_CODE_SESSION_ID")
    for t in main_transcripts()[:15]:
        subs = subagent_files(t)
        size = os.path.getsize(t) + sum(os.path.getsize(s) for s in subs)
        when = datetime.datetime.fromtimestamp(os.path.getmtime(t)).strftime("%Y-%m-%d %H:%M")
        mark = "  <- current" if cur and os.path.basename(t).startswith(cur) else ""
        print(f"{when}  {size // 1024:>7}K  {len(subs):>3} sub  {t}{mark}")


def main():
    args = sys.argv[1:]
    opt = lambda k: args[args.index(k) + 1] if k in args and args.index(k) + 1 < len(args) else None
    if "--list" in args:
        return list_sessions()
    valued = {opt(k) for k in ("--prices", "--cutoff", "--report")}
    pos = [a for a in args if not a.startswith("-") and a not in valued]
    path = resolve(pos[0] if pos else None)
    stats = collections.Counter()
    stats["usage_keys"], stats["unknown_types"], stats["unknown_speed"] = set(), collections.Counter(), collections.Counter()
    stats["unknown_geo"] = collections.Counter()

    # cutoff: never let the analysis measure itself
    cut_arg, cutoff, reason = opt("--cutoff"), None, "none requested"
    is_current = os.path.basename(path).startswith(os.environ.get("CLAUDE_CODE_SESSION_ID") or "\0")
    if cut_arg and cut_arg != "none":
        cutoff, reason = ts(cut_arg), "explicit"
        if not cutoff:
            die(2, f"bad --cutoff {cut_arg!r}; use ISO 8601")
    elif is_current and cut_arg != "none":
        probe = parse(path, "main", None, collections.Counter(
            usage_keys=set(), unknown_types=collections.Counter(), unknown_speed=collections.Counter(),
            unknown_geo=collections.Counter()))
        last = next((p["when"] for p in reversed(probe["prompts"]) if p["when"]), None)
        if last:
            cutoff, reason = last, "current session: excludes the latest prompt (this analysis)"

    threads = load_session(path, cutoff, "--main-only" in args, stats)
    if not stats["assistant_lines"]:
        die(2, f"{path}: no assistant messages{' before the cutoff' if cutoff else ''} "
               "-- nothing to analyse; pick another session (--list)")
    fatal, warn = schema_check(threads, stats, path)
    if "--main-only" in args:
        warn.insert(0, "--main-only: subagents excluded, this is NOT a session total")
    if fatal:
        print("SCHEMA CHECK FAILED — the transcript format no longer matches this script; "
              "numbers would be wrong, so none are printed.", file=sys.stderr)
        for f in fatal:
            print(f"  - {f}", file=sys.stderr)
        print("Inspect a few records (compact aggregates, not whole files), fix analyze.py, "
              "and re-run.", file=sys.stderr)
        sys.exit(3)
    P, pnote = load_prices(opt("--prices") or os.path.join(HERE, "prices.json"))
    out = Out()
    report(path, threads, stats, fatal, warn, P, pnote, cutoff, reason, out)
    rp = opt("--report")
    if rp:
        with open(rp, "w") as f:
            f.write("\n".join(out.lines) + "\n")
        print(f"report written: {rp}")


if __name__ == "__main__":
    main()
