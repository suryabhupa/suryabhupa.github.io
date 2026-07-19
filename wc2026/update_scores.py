#!/usr/bin/env python3
"""
Fetch live World Cup 2026 knockout scores from ESPN's public scoreboard API
and rewrite results.json. Designed to run from GitHub Actions on a cron.

No API key needed — site.api.espn.com is public JSON.
"""
import json
import sys
import urllib.request
from datetime import datetime, timezone
from pathlib import Path

HERE = Path(__file__).parent
RESULTS_PATH = HERE / "results.json"

# Match number → (top-slot team, bottom-slot team). R32 only — later rounds
# are resolved dynamically once feeders complete.
R32 = {
    73: ("South Africa", "Canada"),
    74: ("Germany", "Paraguay"),
    75: ("Netherlands", "Morocco"),
    76: ("Brazil", "Japan"),
    77: ("France", "Sweden"),
    78: ("Ivory Coast", "Norway"),
    79: ("Mexico", "Ecuador"),
    80: ("England", "DR Congo"),
    81: ("United States", "Bosnia and Herzegovina"),
    82: ("Belgium", "Senegal"),
    83: ("Portugal", "Croatia"),
    84: ("Spain", "Austria"),
    85: ("Switzerland", "Algeria"),
    86: ("Argentina", "Cape Verde"),
    87: ("Colombia", "Ghana"),
    88: ("Australia", "Egypt"),
}
# Later rounds: matchId → (feederA, feederB) where each feeder is a matchId
# whose winner fills that slot.
FEED = {
    89: (74, 77), 90: (73, 75), 91: (76, 78), 92: (79, 80),
    93: (83, 84), 94: (81, 82), 95: (86, 88), 96: (85, 87),
    97: (89, 90), 98: (93, 94), 99: (91, 92), 100: (95, 96),
    101: (97, 98), 102: (99, 100),
    104: (101, 102),
}
# 3rd place: losers of 101, 102
THIRD = 103

# ESPN uses some different display names — normalize to ours.
ALIASES = {
    "USA": "United States",
    "Côte d'Ivoire": "Ivory Coast",
    "Cote d'Ivoire": "Ivory Coast",
    "Congo DR": "DR Congo",
    "DR Congo": "DR Congo",
    "Bosnia-Herzegovina": "Bosnia and Herzegovina",
    "Cape Verde Islands": "Cape Verde",
}


def norm(name: str) -> str:
    name = name.strip()
    return ALIASES.get(name, name)


def fetch_scoreboard():
    """Fetch all knockout-stage games (Jun 28 – Jul 19)."""
    url = (
        "https://site.api.espn.com/apis/site/v2/sports/soccer/"
        "fifa.world/scoreboard?dates=20260628-20260719&limit=200"
    )
    req = urllib.request.Request(url, headers={"User-Agent": "wc2026-updater/1.0"})
    with urllib.request.urlopen(req, timeout=20) as r:
        return json.load(r)


def winner_name(mid: int, results: dict) -> str | None:
    """Resolve the team name that won match `mid`, or None if not yet decided."""
    r = results.get(str(mid))
    if not r or r.get("status") != "FT":
        return None
    a, b = slot_teams(mid, results)
    if a is None or b is None:
        return None
    s = r["score"]
    if s[0] != s[1]:
        return a if s[0] > s[1] else b
    p = r.get("pens")
    if p:
        return a if p[0] > p[1] else b
    return None


def loser_name(mid: int, results: dict) -> str | None:
    w = winner_name(mid, results)
    if not w:
        return None
    a, b = slot_teams(mid, results)
    return b if w == a else a


def slot_teams(mid: int, results: dict) -> tuple[str | None, str | None]:
    if mid in R32:
        return R32[mid]
    if mid == THIRD:
        return loser_name(101, results), loser_name(102, results)
    fa, fb = FEED[mid]
    return winner_name(fa, results), winner_name(fb, results)


def build_match_index(results: dict) -> dict[frozenset, int]:
    """Map {teamA, teamB} → matchId for every match whose participants are known."""
    idx = {}
    for mid in list(R32) + list(FEED) + [THIRD]:
        a, b = slot_teams(mid, results)
        if a and b:
            idx[frozenset({a, b})] = mid
    return idx


def main() -> int:
    # Load existing results so we can resolve later-round participants.
    try:
        current = json.loads(RESULTS_PATH.read_text())
        results = dict(current.get("matches", {}))
    except Exception:
        current = {}
        results = {}

    try:
        data = fetch_scoreboard()
    except Exception as e:
        print(f"fetch failed: {e}", file=sys.stderr)
        return 0  # don't fail the workflow; keep existing file

    events = data.get("events", [])
    idx = build_match_index(results)
    changed = False

    for ev in events:
        comp = (ev.get("competitions") or [{}])[0]
        teams = comp.get("competitors") or []
        if len(teams) != 2:
            continue
        home = next((t for t in teams if t.get("homeAway") == "home"), teams[0])
        away = next((t for t in teams if t.get("homeAway") == "away"), teams[1])
        n_home = norm(home.get("team", {}).get("displayName", ""))
        n_away = norm(away.get("team", {}).get("displayName", ""))
        key = frozenset({n_home, n_away})
        mid = idx.get(key)
        if not mid:
            continue

        # Determine slot order (our top/bottom) for this match.
        a, b = slot_teams(mid, results)
        # ESPN home/away doesn't map to our top/bottom — map by name.
        by_name = {n_home: home, n_away: away}
        top, bot = by_name.get(a), by_name.get(b)
        if not top or not bot:
            continue

        status = comp.get("status", {}).get("type", {})
        state = status.get("state")  # "pre" | "in" | "post"
        detail = status.get("shortDetail", "")  # e.g. "58'", "FT", "HT"

        try:
            g_top = int(top.get("score") or 0)
            g_bot = int(bot.get("score") or 0)
        except (TypeError, ValueError):
            continue

        entry = {"score": [g_top, g_bot]}
        if state == "post":
            entry["status"] = "FT"
            so_top = top.get("shootoutScore")
            so_bot = bot.get("shootoutScore")
            if so_top is not None and so_bot is not None:
                entry["pens"] = [int(so_top), int(so_bot)]
            elif g_top == g_bot:
                # Tied at FT with no shootout data — fall back to ESPN's winner flag.
                w = 0 if top.get("winner") else 1 if bot.get("winner") else None
                if w is not None:
                    # encode as pens 1-0 so downstream derives the right winner
                    entry["pens"] = [1, 0] if w == 0 else [0, 1]
        elif state == "in":
            entry["status"] = "LIVE"
            entry["minute"] = detail or ""
        else:
            continue  # pre-match, skip

        prev = results.get(str(mid))
        if prev != entry:
            results[str(mid)] = entry
            changed = True
            print(f"M{mid}: {a} {g_top}-{g_bot} {b} [{entry['status']}]")
            # Rebuild index so newly-completed matches unlock later-round mappings
            # within the same run.
            idx = build_match_index(results)

    if not changed:
        print("no changes")
        return 0

    out = {
        "updated": datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%MZ"),
        "matches": dict(sorted(results.items(), key=lambda kv: int(kv[0]))),
    }
    # Preserve manually-set tiebreaker actual (goals+corners+yellows in the final).
    if "tiebreaker" in current:
        out["tiebreaker"] = current["tiebreaker"]
    RESULTS_PATH.write_text(json.dumps(out, indent=2) + "\n")
    print(f"wrote {RESULTS_PATH}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
