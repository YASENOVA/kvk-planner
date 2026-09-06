"""
Battle Day logic for the KvK planner.

The upstream tool plans King's Buff appointments during the prep week.
This module adds the second half: who is actually online during the battle,
what troops they can field, and how leadership splits them into teams.

Kept in its own module so the existing prep-week code and its test suite
stay untouched.
"""

import json
import re

# --------------------------------------------------------------------------
# Defaults. Every event stores its own copy, so changing these later does not
# rewrite history for events that already exist.
# --------------------------------------------------------------------------

DEFAULT_START_HOUR = 12
DEFAULT_HOUR_COUNT = 5

# Strongest first. "Below" is the opt-out and never counts as fieldable.
DEFAULT_TIERS = ["T11", "TG5", "TG4", "TG3", "TG2", "TG1"]
BELOW_LABEL = "Below TG1"

DEFAULT_TROOP_TYPES = [
    {"key": "INF", "name": "Infantry"},
    {"key": "CAV", "name": "Cavalry"},
    {"key": "ARC", "name": "Archers"},
]

# Higher is better. Used for ranking, never shown to players.
DEFAULT_TIER_SCORES = {"T11": 100, "TG5": 70, "TG4": 55, "TG3": 42, "TG2": 30, "TG1": 20}

MAX_HOURS = 12
MAX_ALLIANCES = 12
MAX_NAME_LEN = 40
MAX_NOTE_LEN = 500
GAMER_ID_RE = re.compile(r"^\d{6,12}$")

# A team is a group of players sent to one alliance for one job.
# "required_hours" are hour indexes a player must all be available for.
# "size" caps the core; everyone else who qualifies becomes a reserve.
# "max_leads" caps how many rally leads land here: None = no limit, 0 = none.
# A garrison usually wants one lead of its own and the rest kept free.
DEFAULT_TEAMS = [
    {
        "key": "A",
        "name": "Garrison",
        "alliance": "",
        "min_tier": "TG5",
        "required_hours": [0, 1, 2],
        "size": 10,
        "max_leads": 1,
    },
    {
        "key": "B",
        "name": "Counter Rally",
        "alliance": "",
        "min_tier": "TG3",
        "required_hours": [],
        "size": None,
        "max_leads": None,
    },
]

# Sanctuary and Fortress rewards: a fixed number of pieces, capped per player.
DEFAULT_REWARDS = [
    {"key": "deployment", "name": "Deployment", "total": 60, "max_per": 2,
     "min_tier": "TG4", "required_hours": [0, 1]},
    {"key": "health", "name": "Health Up", "total": 60, "max_per": 2,
     "min_tier": "TG5", "required_hours": [0, 1]},
    {"key": "lethality", "name": "Lethality", "total": 60, "max_per": 2,
     "min_tier": "TG5", "required_hours": [0, 1]},
    {"key": "hero_gear", "name": "Hero Gear", "total": 60, "max_per": 2,
     "min_tier": "TG5", "required_hours": [0, 1]},
]


def default_config(alliances=None):
    """Config for a fresh battle event."""
    alliances = list(alliances or [])
    teams = [dict(t) for t in DEFAULT_TEAMS]
    # Point the default teams at the first two alliances, if we know them.
    for i, team in enumerate(teams):
        if i < len(alliances):
            team["alliance"] = alliances[i]
    return {
        "start_hour": DEFAULT_START_HOUR,
        "hour_count": DEFAULT_HOUR_COUNT,
        "tiers": list(DEFAULT_TIERS),
        "troop_types": [dict(t) for t in DEFAULT_TROOP_TYPES],
        "alliances": alliances,
        "tier_scores": dict(DEFAULT_TIER_SCORES),
        "teams": teams,
        "rewards": [dict(r) for r in DEFAULT_REWARDS],
    }


def parse_config(raw):
    """Read a stored config back, filling in anything missing."""
    cfg = default_config()
    if not raw:
        return cfg
    try:
        stored = json.loads(raw)
    except (ValueError, TypeError):
        return cfg
    if not isinstance(stored, dict):
        return cfg
    for key in cfg:
        if key in stored and stored[key] not in (None, ""):
            cfg[key] = stored[key]
    # Guard against a stored config that would break the UI.
    if not isinstance(cfg["hour_count"], int) or not 1 <= cfg["hour_count"] <= MAX_HOURS:
        cfg["hour_count"] = DEFAULT_HOUR_COUNT
    if not isinstance(cfg["start_hour"], int) or not 0 <= cfg["start_hour"] <= 23:
        cfg["start_hour"] = DEFAULT_START_HOUR
    if not cfg.get("tiers"):
        cfg["tiers"] = list(DEFAULT_TIERS)
    if not cfg.get("troop_types"):
        cfg["troop_types"] = [dict(t) for t in DEFAULT_TROOP_TYPES]
    # Older events stored a yes/no "exclude_leads" flag. A cap is more useful:
    # a garrison usually wants one lead of its own, not zero.
    for team in cfg.get("teams", []):
        if "max_leads" not in team:
            team["max_leads"] = 0 if team.pop("exclude_leads", False) else None
        team.pop("exclude_leads", None)
    return cfg


def build_config(form, alliances=None):
    """Build a config from submitted admin form data."""
    cfg = default_config(alliances)
    try:
        start = int(form.get("battle_start_hour", DEFAULT_START_HOUR))
        if 0 <= start <= 23:
            cfg["start_hour"] = start
    except (TypeError, ValueError):
        pass
    try:
        count = int(form.get("battle_hour_count", DEFAULT_HOUR_COUNT))
        if 1 <= count <= MAX_HOURS:
            cfg["hour_count"] = count
    except (TypeError, ValueError):
        pass
    raw_alliances = form.get("battle_alliances", "")
    parsed = [a.strip() for a in re.split(r"[,\n]", raw_alliances) if a.strip()]
    if parsed:
        cfg["alliances"] = parsed[:MAX_ALLIANCES]
    return cfg


# --------------------------------------------------------------------------
# Hour labels
# --------------------------------------------------------------------------


def hour_slots(cfg):
    """
    The battle hours as dicts, e.g.
    {"index": 0, "short": "12-13", "label": "12:00-13:00", "start": 12}
    """
    slots = []
    for i in range(cfg["hour_count"]):
        start = (cfg["start_hour"] + i) % 24
        end = (start + 1) % 24
        slots.append(
            {
                "index": i,
                "start": start,
                "short": "%d-%d" % (start, end),
                "label": "%02d:00-%02d:00" % (start, end),
            }
        )
    return slots


# --------------------------------------------------------------------------
# Submission parsing
# --------------------------------------------------------------------------


class SubmissionError(ValueError):
    """Raised when a player's submission cannot be accepted."""


def clean_gamer_id(value):
    """
    Gamer IDs are 6-12 digits. Anything else is a typo - one real player
    pasted his march sizes into this field, which would otherwise read as
    one giant ID and split him into two records.
    """
    digits = re.sub(r"[^0-9]", "", str(value or ""))
    return digits if GAMER_ID_RE.match(digits) else ""


def parse_submission(form, cfg):
    """
    Turn raw form data into a validated submission dict.
    Raises SubmissionError with a player-readable message.
    """
    player_name = str(form.get("player_name", "")).strip()
    if not player_name:
        raise SubmissionError("Please enter your in-game name.")
    if len(player_name) > MAX_NAME_LEN:
        raise SubmissionError("That name is too long.")

    gamer_id = clean_gamer_id(form.get("player_id"))
    if not gamer_id:
        raise SubmissionError("Your Gamer ID should be 6 to 12 digits, numbers only.")

    alliance = str(form.get("alliance_name", "")).strip()[:MAX_NAME_LEN]
    if cfg.get("alliances") and alliance not in cfg["alliances"]:
        raise SubmissionError("Please pick your alliance from the list.")

    tiers = {}
    for troop in cfg["troop_types"]:
        raw = str(form.get("tier_" + troop["key"], "")).strip()
        if not raw:
            raise SubmissionError("Please answer the troop tier for %s." % troop["name"])
        if raw == BELOW_LABEL:
            tiers[troop["key"]] = None
        elif raw in cfg["tiers"]:
            tiers[troop["key"]] = raw
        else:
            raise SubmissionError("Unknown troop tier for %s." % troop["name"])

    availability = []
    for slot in hour_slots(cfg):
        answer = str(form.get("hour_%d" % slot["index"], "")).strip().lower()
        if answer not in ("yes", "no"):
            raise SubmissionError("Please answer yes or no for %s UTC." % slot["label"])
        availability.append(1 if answer == "yes" else 0)

    notes = str(form.get("notes", "")).strip()[:MAX_NOTE_LEN]

    return {
        "player_id": gamer_id,
        "player_name": player_name,
        "alliance_name": alliance,
        "tiers": tiers,
        "availability": availability,
        "notes": notes,
    }


# --------------------------------------------------------------------------
# Derived values
# --------------------------------------------------------------------------


def tier_score(tier, cfg):
    if not tier:
        return 0
    return cfg.get("tier_scores", DEFAULT_TIER_SCORES).get(tier, 0)


def best_tier(tiers, cfg):
    """The strongest tier a player can field across all troop types."""
    best, best_score = None, 0
    for troop in cfg["troop_types"]:
        value = tiers.get(troop["key"])
        score = tier_score(value, cfg)
        if value and score > best_score:
            best, best_score = value, score
    return best


def strength(tiers, cfg):
    """
    Best tier plus a quarter of the others - rewards players who can field
    more than one troop type, without letting depth outweigh raw tier.
    """
    scores = sorted(
        (tier_score(tiers.get(t["key"]), cfg) for t in cfg["troop_types"]),
        reverse=True,
    )
    if not scores:
        return 0
    return scores[0] + 0.25 * sum(scores[1:])


def hours_available(availability):
    return sum(1 for a in availability if a)


def covers_hours(availability, indexes):
    """True if the player is available in every one of the given hours."""
    return all(
        idx < len(availability) and availability[idx] for idx in indexes
    )


def pattern(availability):
    """Compact visual, e.g. '##.##'."""
    return "".join("#" if a else "." for a in availability)


def enrich(row, cfg):
    """Add the derived fields the dashboard needs to a stored submission."""
    tiers = row.get("tiers") or {}
    availability = row.get("availability") or []
    out = dict(row)
    out["best_tier"] = best_tier(tiers, cfg)
    out["strength"] = strength(tiers, cfg)
    out["hours"] = hours_available(availability)
    out["pattern"] = pattern(availability)
    out["fieldable"] = out["best_tier"] is not None and out["hours"] > 0
    out["note_flag"] = flag_note(row.get("notes"), availability)
    return out


# --------------------------------------------------------------------------
# Note screening
#
# Players routinely tick five hours and then write "I'll be on and off".
# We cannot read intent, so we never silently override what they ticked -
# we surface the note for a human to judge. Everything here is a hint,
# not a verdict.
# --------------------------------------------------------------------------

# Phrases that suggest the ticked hours are softer than they look.
_DOUBT_PATTERNS = [
    (r"\bon and off\b", "says they will be on and off"),
    (r"\bin and out\b", "says they will be in and out"),
    (r"\bnot sure\b|\bunsure\b|\bno idea\b", "says they are not sure"),
    (r"\bmight\b|\bmaybe\b|\bprobably\b|\bperhaps\b", "hedges with might/maybe"),
    (r"\btry to\b|\bwill try\b", "only says they will try"),
    (r"\bbest guess\b|\bguess\b", "calls the times a guess"),
    (r"\bdepends\b", "says it depends"),
    (r"\bif i\b|\bif my\b", "attaches a condition"),
    (r"\bhave to leave\b|\bleaving\b|\bleave early\b", "may have to leave"),
    (r"\bcalled away\b|\bengagement\b|\bappointment\b", "has another commitment"),
    (r"\bunstable\b|\bconnection\b|\bwifi\b", "mentions connection trouble"),
    (r"\bwork\b|\bworking\b|\bshift\b", "mentions work"),
    (r"\bsleep\b|\bkids\b|\bbirthday\b|\bparty\b", "mentions a personal conflict"),
    (r"\btravel\b|\btravelling\b|\btraveling\b|\bholiday\b", "mentions travelling"),
    (r"\bupgrade\b|\bafter friday\b|\bwill be\b.{0,12}\bafter\b", "tier may change before the battle"),
    # KvK servers are international and people answer in their own language.
    # Two real submissions were written in French and sailed straight through
    # an English-only screen, which is exactly the kind of miss that puts an
    # unreliable player into the opening wave.
    (r"\bpeut.?être\b|\bpeut etre\b|\bpar moment\b|\bje serai\b.{0,20}\bpuis\b",
     "hedges in French"),
    (r"\bquizá\b|\bquizas\b|\btal vez\b|\ba veces\b", "hedges in Spanish"),
    (r"\btalvez\b|\bàs vezes\b|\bas vezes\b", "hedges in Portuguese"),
    (r"\bmungkin\b|\bkadang\b", "hedges in Indonesian"),
    (r"\bvielleicht\b|\bkann sein\b|\bab und zu\b", "hedges in German"),
    (r"\bbelki\b|\bbazen\b", "hedges in Turkish"),
    (r"\bربما\b|\bاحيانا\b", "hedges in Arabic"),
]


def flag_note(note, availability=None):
    """
    Return None, or a dict describing why this note deserves a second look.

    level 'contradicts' - they ticked a lot of hours but hedge in the text
    level 'caution'     - worth reading, but consistent with what they ticked
    """
    text = (note or "").strip()
    if not text:
        return None

    reasons = []
    lowered = text.lower()
    for expr, reason in _DOUBT_PATTERNS:
        if re.search(expr, lowered):
            reasons.append(reason)
    if not reasons:
        return None

    # Hedging is only a contradiction when they actually claimed a lot.
    claimed = hours_available(availability or [])
    total = len(availability or [])
    level = "contradicts" if total and claimed >= max(3, total - 1) else "caution"

    return {"level": level, "reasons": reasons[:3], "note": text}


# --------------------------------------------------------------------------
# Dashboard analysis
# --------------------------------------------------------------------------


def coverage_by_troop(players, cfg):
    """
    For each troop type: how many players of each tier are online each hour.

    Returns {troop_key: {"name": str, "rows": [{"tier","counts","total"}],
                         "totals": [...], "max": int}}
    """
    slots = hour_slots(cfg)
    result = {}
    for troop in cfg["troop_types"]:
        rows, peak = [], 1
        for tier in cfg["tiers"]:
            group = [p for p in players if (p.get("tiers") or {}).get(troop["key"]) == tier]
            counts = []
            for slot in slots:
                n = sum(
                    1
                    for p in group
                    if slot["index"] < len(p["availability"])
                    and p["availability"][slot["index"]]
                )
                counts.append(n)
                peak = max(peak, n)
            rows.append({"tier": tier, "counts": counts, "total": len(group)})
        totals = []
        with_type = [p for p in players if (p.get("tiers") or {}).get(troop["key"])]
        for slot in slots:
            totals.append(
                sum(
                    1
                    for p in with_type
                    if slot["index"] < len(p["availability"])
                    and p["availability"][slot["index"]]
                )
            )
        result[troop["key"]] = {
            "name": troop["name"],
            "rows": rows,
            "totals": totals,
            "total_players": len(with_type),
            "max": peak,
        }
    return result


def headcount_by_hour(players, cfg):
    """
    Per hour: who is online, who arrives, who leaves. This is the view that
    tells leadership whether the plan survives a battle that drags on.
    """
    slots = hour_slots(cfg)
    online = []
    for slot in slots:
        names = {
            p["player_name"]
            for p in players
            if slot["index"] < len(p["availability"]) and p["availability"][slot["index"]]
        }
        online.append(names)

    out = []
    for i, slot in enumerate(slots):
        joined = sorted(online[i] - online[i - 1]) if i else []
        left = sorted(online[i - 1] - online[i]) if i else []
        out.append(
            {
                "slot": slot,
                "count": len(online[i]),
                "joined": joined,
                "left": left,
                "delta": len(online[i]) - len(online[i - 1]) if i else 0,
            }
        )
    return out


def weakest_hour(headcounts):
    """The hour with the fewest players. Cover this one first."""
    if not headcounts:
        return None
    return min(headcounts, key=lambda h: h["count"])


def alliance_breakdown(players):
    counts = {}
    for p in players:
        key = p.get("alliance_name") or "(none)"
        counts[key] = counts.get(key, 0) + 1
    return sorted(counts.items(), key=lambda kv: (-kv[1], kv[0]))


# --------------------------------------------------------------------------
# Teams
#
# Leadership decides; the tool only proposes. Anything a human pinned by hand
# is honoured first and never moved by a later re-run.
# --------------------------------------------------------------------------


def meets(player, rule, cfg):
    """Does this player satisfy a team's or reward's entry conditions?"""
    if not player.get("fieldable"):
        return False
    floor = tier_score(rule.get("min_tier"), cfg)
    if floor and tier_score(player.get("best_tier"), cfg) < floor:
        return False
    return covers_hours(player.get("availability") or [], rule.get("required_hours") or [])


def _priority(player):
    """
    Most hours first, then people whose own comment did not undercut them,
    then raw strength. Someone who is there all five hours is worth more to a
    garrison than a stronger player who leaves after two.
    """
    flag = player.get("note_flag")
    doubt = 2 if flag and flag["level"] == "contradicts" else (1 if flag else 0)
    return (-player["hours"], doubt, -player["strength"], player["player_name"].lower())


def _reward_priority(player):
    """Rewards follow strength first - they buy damage, not attendance."""
    flag = player.get("note_flag")
    doubt = 2 if flag and flag["level"] == "contradicts" else (1 if flag else 0)
    return (-player["strength"], doubt, -player["hours"], player["player_name"].lower())


def assign_teams(players, cfg, overrides=None):
    """
    Build the teams.

    overrides maps player_id -> {"team": key or "", "locked": bool,
                                 "is_lead": bool, "excluded": bool}

    Returns {"teams": [...], "unassigned": [...]}.
    """
    overrides = overrides or {}
    teams_cfg = cfg.get("teams") or []
    buckets = {t["key"]: {"cfg": t, "core": [], "reserve": []} for t in teams_cfg}
    taken = set()

    def ov(p, key, default=None):
        return overrides.get(p["player_id"], {}).get(key, default)

    pool = [p for p in players if not ov(p, "excluded")]
    for p in pool:
        p["role"] = ov(p, "role", "")
        p["is_lead"] = p["role"] == "lead"
        p["locked"] = bool(ov(p, "locked"))

    # 1. Anything pinned by hand goes in first and stays there. A pinned lead
    #    counts against that team's lead cap - leadership already decided.
    for p in sorted(pool, key=_priority):
        forced = ov(p, "team")
        if forced and forced in buckets:
            buckets[forced]["core"].append(p)
            taken.add(p["player_id"])

    # 2. Fill the rest in team order, so the first team gets first pick.
    for team in teams_cfg:
        bucket = buckets[team["key"]]
        size = team.get("size")
        max_leads = team.get("max_leads")
        leads_here = sum(1 for p in bucket["core"] if p["is_lead"])

        candidates = [
            p for p in pool if p["player_id"] not in taken and meets(p, team, cfg)
        ]
        candidates.sort(key=_priority)
        for p in candidates:
            if p["is_lead"] and max_leads is not None and leads_here >= max_leads:
                continue
            if size is not None and len(bucket["core"]) >= size:
                bucket["reserve"].append(p)
            else:
                bucket["core"].append(p)
                if p["is_lead"]:
                    leads_here += 1
            taken.add(p["player_id"])

    out = []
    for team in teams_cfg:
        bucket = buckets[team["key"]]
        bucket["core"].sort(key=_priority)
        bucket["reserve"].sort(key=_priority)
        out.append(
            {
                "cfg": team,
                "core": bucket["core"],
                "reserve": bucket["reserve"],
                "hourly": [
                    sum(
                        1
                        for p in bucket["core"]
                        if slot["index"] < len(p["availability"])
                        and p["availability"][slot["index"]]
                    )
                    for slot in hour_slots(cfg)
                ],
                "swaps": [
                    p
                    for p in bucket["core"]
                    if team.get("alliance") and p.get("alliance_name") != team["alliance"]
                ],
                "leads": [p for p in bucket["core"] if p.get("is_lead")],
                "joiners": [p for p in bucket["core"] if p.get("role") == "joiner"],
            }
        )

    unassigned = [p for p in pool if p["player_id"] not in taken]
    unassigned.sort(key=_priority)
    return {"teams": out, "unassigned": unassigned}


# --------------------------------------------------------------------------
# Rewards
# --------------------------------------------------------------------------


def allocate_reward(players, rule, cfg, already=None):
    """
    Hand out one reward type.

    Everyone eligible gets one piece, then the remainder goes back down the
    same list as a second piece, and so on up to max_per. `already` maps
    player_id -> pieces already delivered in game, so a re-run never
    hands the same player their share twice.

    Returns {"grants": [...], "left": int, "short": [...], "eligible": int}
    """
    already = already or {}
    total = int(rule.get("total") or 0)
    max_per = max(1, int(rule.get("max_per") or 1))

    pool = [p for p in players if meets(p, rule, cfg)]
    pool.sort(key=_reward_priority)

    given = {p["player_id"]: int(already.get(p["player_id"], 0)) for p in pool}
    left = total - sum(already.values())

    for rnd in range(1, max_per + 1):
        for p in pool:
            if left <= 0:
                break
            if given[p["player_id"]] == rnd - 1:
                given[p["player_id"]] += 1
                left -= 1
        if left <= 0:
            break

    grants = []
    for p in pool:
        amount = given[p["player_id"]]
        grants.append(
            {
                "player": p,
                "amount": amount,
                "delivered": int(already.get(p["player_id"], 0)),
                "outstanding": amount - int(already.get(p["player_id"], 0)),
            }
        )

    return {
        "rule": rule,
        "grants": grants,
        "left": max(0, left),
        "short": [g for g in grants if g["amount"] == 0],
        "eligible": len(pool),
        "handed_out": sum(g["amount"] for g in grants),
    }


def update_rules(cfg, form):
    """
    Apply edited team and reward settings from the admin screen.

    Leadership decides the split at the end, once they know how many people
    actually answered - so every one of these numbers has to stay editable
    for the whole life of the event. Returns a new config; the caller stores it.
    """
    out = json.loads(json.dumps(cfg))  # deep copy, config is plain JSON
    valid_tiers = set(out["tiers"])
    hour_count = out["hour_count"]

    def read_hours(prefix, key):
        raw = form.getlist(prefix + key) if hasattr(form, "getlist") else form.get(prefix + key, [])
        hours = []
        for value in raw:
            try:
                idx = int(value)
            except (TypeError, ValueError):
                continue
            if 0 <= idx < hour_count and idx not in hours:
                hours.append(idx)
        return sorted(hours)

    # Unchecked boxes are simply absent from a POST, so "no hours" and "this
    # block was not on the page" look identical. The hidden _present marker
    # tells them apart, which keeps a partial update from silently wiping
    # settings the caller never saw.
    def submitted(prefix):
        return form.get(prefix + "present") == "1"

    for team in out.get("teams", []):
        p = "team_%s_" % team["key"]
        if not submitted(p):
            continue
        name = (form.get(p + "name") or "").strip()
        if name:
            team["name"] = name[:MAX_NAME_LEN]
        alliance = (form.get(p + "alliance") or "").strip()
        team["alliance"] = alliance[:MAX_NAME_LEN]
        tier = (form.get(p + "min_tier") or "").strip()
        if tier in valid_tiers:
            team["min_tier"] = tier
        raw_size = (form.get(p + "size") or "").strip()
        if raw_size == "":
            team["size"] = None
        else:
            try:
                size = int(raw_size)
                team["size"] = size if size > 0 else None
            except ValueError:
                pass
        team["required_hours"] = read_hours(p, "hours")
        raw_leads = (form.get(p + "max_leads") or "").strip()
        if raw_leads == "":
            team["max_leads"] = None          # no limit
        else:
            try:
                cap = int(raw_leads)
                team["max_leads"] = cap if cap >= 0 else None
            except ValueError:
                pass

    for reward in out.get("rewards", []):
        p = "reward_%s_" % reward["key"]
        if not submitted(p):
            continue
        name = (form.get(p + "name") or "").strip()
        if name:
            reward["name"] = name[:MAX_NAME_LEN]
        for field, lo, hi in (("total", 0, 100000), ("max_per", 1, 1000)):
            raw = (form.get(p + field) or "").strip()
            if raw == "":
                continue
            try:
                value = int(raw)
            except ValueError:
                continue
            if lo <= value <= hi:
                reward[field] = value
        tier = (form.get(p + "min_tier") or "").strip()
        if tier in valid_tiers:
            reward["min_tier"] = tier
        reward["required_hours"] = read_hours(p, "hours")

    return out


def allocate_all_rewards(players, cfg, already_by_reward=None):
    already_by_reward = already_by_reward or {}
    return [
        allocate_reward(players, rule, cfg, already_by_reward.get(rule["key"]))
        for rule in (cfg.get("rewards") or [])
    ]


def analyse(rows, cfg):
    """Everything the battle dashboard needs, from raw database rows."""
    players = [enrich(r, cfg) for r in rows]
    fieldable = [p for p in players if p["fieldable"]]
    players.sort(
        key=lambda p: (-p["strength"], -p["hours"], p["player_name"].lower())
    )
    headcounts = headcount_by_hour(fieldable, cfg)
    return {
        "players": players,
        "fieldable": fieldable,
        "not_fielded": [p for p in players if not p["fieldable"]],
        "coverage": coverage_by_troop(fieldable, cfg),
        "headcounts": headcounts,
        "weakest": weakest_hour(headcounts),
        "alliances": alliance_breakdown(fieldable),
        "watchlist": sorted(
            [p for p in players if p["note_flag"]],
            key=lambda p: (p["note_flag"]["level"] != "contradicts", -p["strength"]),
        ),
        "slots": hour_slots(cfg),
    }
