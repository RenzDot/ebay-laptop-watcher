"""eBay UK laptop watcher -> ntfy phone alerts.

Runs on GitHub Actions (or locally). Standard library only.

Env vars:
  EBAY_CLIENT_ID, EBAY_CLIENT_SECRET  eBay Browse API keys (required)
  NTFY_TOPIC                          ntfy topic name (required unless DRY_RUN)
  STATE_FILE                          path of the seen-listings file (default state/seen.json)
  POLLS, POLL_GAP                     checks per run and seconds between them (default 3, 120)
  DRY_RUN=1                           print alerts instead of sending them
  SHOW_ALL=1                          with DRY_RUN: classify every listing found, ignore state
"""
import base64
import json
import os
import re
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
from datetime import datetime, timedelta, timezone

MAX_PRICE = 1200.0
CHEAP_LIMIT = 1000.0  # 5060 and "anything else" must be under this

# Searches run every poll. Browse API keyword OR syntax: "(a, b, c)".
QUERIES = [
    # Main GPUs: alerts up to £1200 whatever the build.
    "(3080, 3080ti, 4070, 4080, 5070, 5070ti, 5060, a5000)",
    # Other GPUs: only alert under £1000 with a metal build.
    "(3070, 3070ti, 3060, 4060, 4050, 2080, a4000, a3000)",
    # Metal-build models whose titles often leave out the GPU.
    "(legion 7, zephyrus duo, alienware x17, alienware m17, razer blade, p1 gen, "
    "x1 extreme, beast x25, aero, precision 7760, scar 17, strix 17, ge76, gs66, zbook studio)",
]

EXCLUDE = re.compile(
    r"\b(for parts|parts only|spares|spare or repair|repairs?|faulty|broken|not working|"
    r"no power|cracked|smashed|bios lock|bios password|motherboard only|screen only|"
    r"untested|read description|empty box|box only|chassis only|lid only|keyboard only|"
    r"motherboard|overheating|damaged)\b",
    re.I,
)

# Model list: the build of each laptop family, because listings almost never say.
# First match wins, so specific patterns come before general ones.
#   metal:  "all"     = metal lid, deck and base
#           "partial" = some metal (see note), e.g. metal lid with plastic base
#           "varies"  = depends on generation / colour / config
#           "plastic" = no meaningful metal
#   size:   inches (None = varies)
#   vram16: True = any RTX 3080 in this model is the 16GB version
#   src:    "review" = checked against reviews (Notebookcheck, LaptopMedia, Expert Reviews,
#           Laptop Mag, Lenovo PSREF) in Nov 2026; "brief" = from your research note;
#           "known" = widely documented but not re-checked
def M(pat, metal, size=None, vram16=None, note="", src="known"):
    return dict(pat=re.compile(pat, re.I), metal=metal, size=size, vram16=vram16, note=note, src=src)


MODELS = [
    # --- Lenovo ---
    M(r"16achg6|16ithg6|legion 7i? (gen ?6|16)", "all", 16, True, src="brief"),
    M(r"16arha7", "all", 16, note="AMD Radeon GPU, no RTX 3080"),
    M(r"16iax7|legion 7i? gen ?7", "all", 16, note="3070 Ti / 3080 Ti"),
    M(r"legion pro 7|16irx[89]h", "varies", 16, src="review",
      note="aluminium body; some reviews mention a plastic deck"),
    M(r"legion slim 7", "all", 16, note="aluminium top and bottom (PSREF)", src="review"),
    M(r"legion slim 5", "partial", 16, note="aluminium lid, plastic base (PSREF)", src="review"),
    M(r"legion 5 ?pro|16ach6h|16arh7h|16iah7h", "partial", 16, src="review",
      note="aluminium lid and bottom, plastic deck"),
    M(r"legion pro 5|16arx8|16irx8\b|16irx9\b", "partial", 16, note="aluminium lid", src="review"),
    M(r"\bloq\b|ideapad gaming", "plastic"),
    M(r"(p1|x1 extreme) gen ?4", "all", 16, True, src="brief"),
    M(r"(p1|x1 extreme) gen ?5", "all", 16, note="same chassis family as Gen 4; 3080 Ti option"),
    # --- ASUS ---
    M(r"gx551|zephyrus duo 15", "all", 15.6, True, src="brief"),
    M(r"gu604|zephyrus m16 2023", "all", 16, note="all-metal chassis", src="review"),
    M(r"gu603v", "varies", 16, note="2023 Zephyrus G16, not checked"),
    M(r"gu603|zephyrus m16", "partial", 16, src="review",
      note="magnesium-aluminium lid and deck (same chassis as G15)"),
    M(r"ga503|zephyrus g15", "partial", 15.6, src="review",
      note="magnesium-aluminium lid and deck; its 3080 is 8GB"),
    M(r"ga403|gu605|zephyrus g1[46] 202[45]", "all", None, note="CNC aluminium"),
    M(r"zephyrus g14|ga40[12]", "varies", 14, note="2024+ all aluminium, older metal lid"),
    M(r"zephyrus g16", "varies", 16, note="2024+ all aluminium"),
    M(r"zephyrus s17|gx703", "all", 17),
    M(r"g634|g834|scar 16|scar 18", "partial", None, note="aluminium lid, plastic base", src="review"),
    M(r"scar 17|g733", "varies", 17, True, src="brief"),
    M(r"scar 15|g533", "varies", 15.6, True),
    M(r"g614|g814|strix g16|strix g18", "partial", None, note="aluminium lid, plastic base", src="review"),
    M(r"strix (g|scar )?17|strix g17|g713", "partial", 17, True, note="metal lid", src="brief"),
    M(r"strix g15|g513", "varies", 15.6),
    M(r"\btuf\b", "varies", None, note="some colours have a metal lid"),
    M(r"proart studiobook", "all", 16),
    # --- MSI ---
    M(r"gs66|gs76|stealth", "all", None, note="aluminium; 3080 was 8GB or 16GB"),
    M(r"ge66|ge76|ge68|ge78|raider", "partial", None, True, src="review",
      note="aluminium lid and deck, plastic base"),
    M(r"vector 1[67]", "partial", None, note="metal lid only", src="review"),
    M(r"gp66|gp76|vector", "partial", None, note="metal lid and deck, plastic base", src="review"),
    M(r"creator (z1[67]|15)", "all", None),
    M(r"titan", "partial", None, note="metal lid"),
    M(r"gf63|thin gf", "partial", 15.6, note="brushed aluminium lid"),
    M(r"katana|cyborg|sword|crosshair|pulse|bravo|alpha 1[57]|gf65|gf75", "plastic"),
    # --- Razer ---
    M(r"razer blade 14", "all", 14),
    M(r"razer blade 15", "all", 15.6),
    M(r"razer blade 16", "all", 16),
    M(r"razer blade 17", "all", 17),
    M(r"razer blade", "all", None),
    # --- Gigabyte ---
    M(r"aero 15", "all", 15.6, src="brief"),
    M(r"aero 16", "all", 16, src="brief"),
    M(r"aero 17", "all", 17, src="brief"),
    M(r"\baero\b", "all", None, src="brief"),
    M(r"aorus 17g x", "all", 17, note="CNC aluminium", src="review"),
    M(r"aorus 15p", "plastic", 15.6, src="review"),
    M(r"aorus 1[67]x", "partial", None, note="aluminium lid, plastic base", src="review"),
    M(r"gigabyte (g5|g6|a16|a5)\b", "plastic"),
    # --- Dell / Alienware ---
    M(r"x17 r[12]|x15 r[12]", "varies", None, True, src="brief"),
    M(r"m17 r[34]", "partial", 17, True, note="metal lid", src="brief"),
    M(r"m15 r[3-7]", "partial", 15.6, note="metal lid"),
    M(r"m16 r[12]", "partial", 16, note="metal lid (not re-checked)"),
    M(r"precision 77[67]0", "partial", 17, True, note="metal lid", src="brief"),
    M(r"precision 5[5-7][67]0|precision 5680", "all", None, note="XPS-style aluminium"),
    M(r"\bxps 1[567]\b", "all", None),
    M(r"dell g16", "varies", 16, note="some versions have a metal lid"),
    M(r"dell g15", "plastic", 15.6),
    # --- HP ---
    M(r"zbook studio", "all", None),
    M(r"zbook fury", "partial", None, note="metal lid"),
    M(r"omen 16", "varies", 16, src="review",
      note="2021-22: metal deck and base, plastic lid; later years not checked"),
    M(r"victus", "plastic"),
    # --- Acer ---
    M(r"helios 16|ph16", "partial", 16, note="mostly brushed aluminium, plastic base", src="review"),
    M(r"helios 300|ph315", "partial", 15.6, note="aluminium lid and deck", src="review"),
    M(r"triton", "all", None),
    M(r"\bnitro\b", "plastic"),
    # --- Others ---
    M(r"galaxy book\s?\d? (ultra|pro)", "all", None),
    M(r"beast x25", "all", 15.6, True, src="brief"),
    M(r"beast x[34]0", "all", 17, note="metal chassis per reviews", src="review"),
    M(r"surface laptop studio", "all", 14.4),
]

TOP_GPUS = {"3080 16GB", "3080 Ti", "4070", "4080", "5070", "5070 Ti", "A5000"}
STAR_GPUS = {"3080 16GB", "3080 Ti", "A5000"}
METAL_OK = ("all", "partial", "varies")  # builds that count as metal for the under-£1000 rule
# GPUs too weak to be worth an alert, even all-metal and cheap. Edit to taste.
WEAK_GPU = re.compile(r"^(T\d+|A[12]000|GTX.*|MX.*|30[5]0.*|20[56]0.*|1[0-6]\d0.*)$")
WORKSTATION = re.compile(r"thinkpad|\bp1 gen|x1 extreme|precision|zbook", re.I)


def http(url, data=None, headers=None, method=None):
    req = urllib.request.Request(url, data=data, headers=headers or {}, method=method)
    with urllib.request.urlopen(req, timeout=30) as r:
        return json.loads(r.read().decode() or "{}")


def ebay_token(cid, secret):
    auth = base64.b64encode(f"{cid}:{secret}".encode()).decode()
    body = urllib.parse.urlencode({
        "grant_type": "client_credentials",
        "scope": "https://api.ebay.com/oauth/api_scope",
    }).encode()
    r = http("https://api.ebay.com/identity/v1/oauth2/token", body, {
        "Authorization": "Basic " + auth,
        "Content-Type": "application/x-www-form-urlencoded",
    })
    return r["access_token"]


def ebay_search(token, q):
    params = {
        "q": q,
        "category_ids": "177",
        "sort": "newlyListed",
        "limit": "200",
        "filter": f"price:[..{int(MAX_PRICE)}],priceCurrency:GBP,itemLocationCountry:GB,"
                  "conditionIds:{1000|1500|2000|2010|2020|2030|2500|3000}",
    }
    url = "https://api.ebay.com/buy/browse/v1/item_summary/search?" + urllib.parse.urlencode(params)
    r = http(url, headers={"Authorization": "Bearer " + token, "X-EBAY-C-MARKETPLACE-ID": "EBAY_GB"})
    return r.get("itemSummaries", [])


def detect_gpu(t):
    t = t.lower().replace("-", " ")
    if re.search(r"\ba5[05]00\b", t):
        return "A5000"
    m = re.search(r"\b(?:rtx )?(a[1-4]000)\b", t)  # workstation RTX A1000-A4000
    if m:
        return m.group(1).upper()
    m = re.search(r"\b(t[56]00|t1[02]00|t2000|gtx ?\d{4}|mx ?\d{3})\b", t)  # weak GPUs
    if m:
        return m.group(1).upper()
    if re.search(r"5070\s*ti", t):
        return "5070 Ti"
    if re.search(r"\b5070\b", t):
        return "5070"
    if re.search(r"\b5060\b", t):
        return "5060"
    if re.search(r"\b4080\b", t):
        return "4080"
    if re.search(r"\b4070\b", t):
        return "4070"
    if re.search(r"3080\s*ti", t):
        return "3080 Ti"
    if re.search(r"\b3080\b", t):
        if re.search(r"3080\s*\(?\s*16\s*gb|16\s*gb\s*(gddr6\s*)?(rtx\s*)?3080|16gb vram", t):
            return "3080 16GB"
        if re.search(r"3080\s*\(?\s*8\s*gb|8\s*gb\s*(gddr6\s*)?(rtx\s*)?3080|8gb vram", t):
            return "3080 8GB"
        return "3080"
    m = re.search(r"\b(30[5-7]0(\s*ti)?|40[5-6]0|2080(\s*(ti|super))?|2070(\s*super)?)\b", t)
    return m.group(1).upper() if m else None


def detect_size(t):
    m = re.search(r"\b(1[3-8](?:\.\d)?)\s*(\"|''|”|inch|in\b|-inch)", t, re.I)
    if m:
        return float(m.group(1))
    # Model names like "Nitro 17", "Strix G17", "Titan 18" with no inch mark.
    m = re.search(r"(?<![\d.])(17|18)(?![\d.])(?!\s*(gb|tb|th\b))", t, re.I)
    return float(m.group(1)) if m else None


def classify(item):
    """Return None if rejected, else dict(level, gpu, notes). level: star / maybe_star / match."""
    title = item.get("title", "")
    tl = title.lower()
    if EXCLUDE.search(tl):
        return None
    price = float(item.get("price", {}).get("value", 1e9))
    if price > MAX_PRICE:
        return None

    metal, size, vram16, build_note = None, None, None, ""
    for m in MODELS:
        if m["pat"].search(tl):
            metal, size, vram16, build_note = m["metal"], m["size"], m["vram16"], m["note"]
            break
    size = detect_size(title) or size
    if metal in (None, "varies", "plastic"):
        # A seller's own description of the build beats the model list.
        if re.search(r"\b(metal|aluminium|aluminum|magnesium) (lid|top|cover)\b", tl):
            metal, build_note = "partial", "seller says metal lid"
        elif re.search(r"\b(all[- ]metal|aluminium|aluminum|magnesium)\b", tl):
            metal, build_note = "all", "seller says metal"

    gpu = detect_gpu(title)
    notes = []
    if gpu == "3080":
        if vram16:
            gpu = "3080 16GB"
            notes.append("16GB from model")
        else:
            notes.append("VRAM not stated")

    if size and size >= 18:
        return None
    if size and size >= 17 and gpu not in ("3080 16GB", "3080 Ti", "A5000", "5070", "5070 Ti", "3080"):
        if gpu is not None:
            return None

    if gpu is None:
        # GPU missing from title: only flag gaming models known to come with a 3080 16GB.
        # (Workstations like ThinkPad P1 / Precision mostly ship with weak GPUs.)
        if metal in METAL_OK and vram16 and not WORKSTATION.search(tl):
            return {"level": "match", "gpu": "GPU not in title", "metal": metal, "size": size,
                    "build_note": build_note, "notes": ["check GPU"], "price": price}
        return None

    if gpu in TOP_GPUS or gpu == "3080":
        pass
    elif gpu == "5060":
        if price >= CHEAP_LIMIT:
            return None
    elif WEAK_GPU.search(gpu):
        return None
    else:
        # Partial metal (e.g. metal lid) counts too.
        if not (price < CHEAP_LIMIT and metal in METAL_OK):
            return None

    if gpu in STAR_GPUS and metal == "all":
        level = "star"
    elif (gpu in STAR_GPUS and metal == "varies") or (gpu == "3080" and metal in ("all", "varies")):
        level = "maybe_star"
        notes.append("check build" if metal == "varies" else "check VRAM")
    else:
        level = "match"
    return {"level": level, "gpu": gpu, "metal": metal, "size": size, "build_note": build_note,
            "notes": notes, "price": price}


BUILD_TEXT = {"all": "all metal", "partial": "partly metal", "varies": "metal varies by version",
              "plastic": "plastic", None: "unknown (not in model list)"}


def build_line(c):
    text = "Build: " + BUILD_TEXT[c["metal"]]
    return text + (f" ({c['build_note']})" if c.get("build_note") else "")


def notify(topic, item, c, dry):
    star = {"star": "⭐ ", "maybe_star": "❔⭐ ", "match": ""}[c["level"]]
    prio = {"star": 5, "maybe_star": 4, "match": 2}[c["level"]]
    opts = item.get("buyingOptions", [])
    kind = "Auction (current bid)" if opts == ["AUCTION"] else ("Best offer" if "BEST_OFFER" in opts else "Buy it now")
    lines = [
        f"£{c['price']:.0f} · {kind}",
        f"GPU: {c['gpu']}" + (f" · {c['size']:g}\"" if c["size"] else ""),
        build_line(c),
        f"Condition: {item.get('condition', '?')}",
    ]
    if c["notes"]:
        lines.append("Note: " + ", ".join(c["notes"]))
    payload = {
        "topic": topic,
        "title": (star + item.get("title", ""))[:200],
        "message": "\n".join(lines),
        "priority": prio,
        "click": item.get("itemWebUrl", "").split("?")[0],
        "tags": ["computer"],
    }
    if dry:
        print(f"[ALERT p{prio}] {payload['title']}\n    " + " | ".join(lines) + f"\n    {payload['click']}")
    return payload


QUIET_START, QUIET_END = 0, 8  # no alerts 00:00-08:00 UK time; held alerts go out at 08:00
MORNING_MAX = 20                # most held alerts sent one by one; the rest are summarised


def uk_time(utc):
    """UK local time: GMT, or BST (UTC+1) from the last Sunday of March to the last Sunday
    of October, switching at 01:00 UTC. Written out so no timezone package is needed."""
    def last_sunday(month):
        d = datetime(utc.year, month, 31, 1, tzinfo=timezone.utc)
        return d - timedelta(days=(d.weekday() + 1) % 7)
    bst = last_sunday(3) <= utc < last_sunday(10)
    return utc + timedelta(hours=1 if bst else 0)


def is_quiet(utc):
    return QUIET_START <= uk_time(utc).hour < QUIET_END


def send_held(topic, state):
    """Send alerts held overnight, most important first."""
    held = sorted(state.pop("held", []), key=lambda p: -p["priority"])
    for p in held[:MORNING_MAX]:
        p["title"] = ("🌙 " + p["title"])[:200]
        p["message"] = "Listed overnight, may be gone\n" + p["message"]
        send(p)
    if len(held) > MORNING_MAX:
        send({"topic": topic, "title": f"🌙 {len(held) - MORNING_MAX} more overnight matches",
              "priority": 2, "message": f"Only the {MORNING_MAX} most important were sent."})
    return len(held)


def send(payload):
    http("https://ntfy.sh", json.dumps(payload).encode(), {"Content-Type": "application/json"}, "POST")


def load_state(path):
    try:
        with open(path, encoding="utf-8") as f:
            return json.load(f)
    except (FileNotFoundError, json.JSONDecodeError):
        return None


def save_state(path, state):
    os.makedirs(os.path.dirname(path) or ".", exist_ok=True)
    with open(path, "w", encoding="utf-8") as f:
        json.dump(state, f)


def parse_time(s):
    try:
        return datetime.fromisoformat(s.replace("Z", "+00:00"))
    except Exception:
        return None


def run():
    cid, secret = os.environ["EBAY_CLIENT_ID"], os.environ["EBAY_CLIENT_SECRET"]
    dry = os.environ.get("DRY_RUN") == "1"
    show_all = dry and os.environ.get("SHOW_ALL") == "1"
    topic = os.environ.get("NTFY_TOPIC", "")
    if not dry and not topic:
        sys.exit("NTFY_TOPIC is not set")
    path = os.environ.get("STATE_FILE", "state/seen.json")
    polls = int(os.environ.get("POLLS", "3"))
    gap = int(os.environ.get("POLL_GAP", "120"))

    state = None if show_all else load_state(path)
    seeding = state is None
    state = state or {"seen": {}, "heartbeat": ""}
    now = datetime.now(timezone.utc)

    token = ebay_token(cid, secret)
    sent = 0
    for i in range(polls):
        if i:
            time.sleep(gap)
        now = datetime.now(timezone.utc)
        items = {}
        for q in QUERIES:
            try:
                for it in ebay_search(token, q):
                    items[it["itemId"]] = it
            except urllib.error.HTTPError as e:
                print(f"search failed ({e.code}): {q[:40]}", file=sys.stderr)
        for iid, it in items.items():
            if iid in state["seen"]:
                continue
            state["seen"][iid] = now.isoformat(timespec="seconds")
            created = parse_time(it.get("itemCreationDate", ""))
            # On first run (or lost state) only alert for very fresh listings, to avoid a flood.
            max_age = timedelta(minutes=30) if seeding else timedelta(days=2)
            if not show_all and created and now - created > max_age:
                continue
            c = classify(it)
            if c:
                payload = notify(topic, it, c, dry)
                if dry:
                    pass
                elif is_quiet(now):
                    state.setdefault("held", []).append(payload)  # sent at 08:00
                else:
                    send(payload)
                sent += 1
        if show_all:
            break
        if not dry:
            save_state(path, state)

    # Forget listings after 30 days.
    cutoff = now - timedelta(days=30)
    state["seen"] = {k: v for k, v in state["seen"].items() if (parse_time(v) or now) > cutoff}

    quiet = is_quiet(now)
    held_sent = 0
    if not dry and not quiet and state.get("held"):
        held_sent = send_held(topic, state)

    # Daily heartbeat once quiet hours end.
    uk = uk_time(now)
    today = uk.date().isoformat()
    if not dry and uk.hour >= QUIET_END and state.get("heartbeat") != today:
        state["heartbeat"] = today
        send({"topic": topic, "title": "Laptop watcher running", "priority": 1, "tags": ["white_check_mark"],
              "message": f"Tracking {len(state['seen'])} listings. {held_sent} overnight alert(s) delivered."})
    if not dry:
        save_state(path, state)
    print(f"done: {sent} match(es){' held for morning' if quiet else ''}, {held_sent} overnight sent, "
          f"{len(state['seen'])} listings tracked, seeding={seeding}")


if __name__ == "__main__":
    if len(sys.argv) > 1 and sys.argv[1] == "test":
        send({"topic": os.environ["NTFY_TOPIC"], "title": "⭐ Test alert", "priority": 5,
              "message": "If you see this, laptop alerts work.", "click": "https://www.ebay.co.uk"})
        print("test notification sent")
    else:
        run()
