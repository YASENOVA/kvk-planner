"""Tests for the Battle Day half of the planner."""

import json

import pytest

from app import battle


# --------------------------------------------------------------------------
# Pure logic
# --------------------------------------------------------------------------


def test_hour_slots_default():
    cfg = battle.default_config()
    slots = battle.hour_slots(cfg)
    assert len(slots) == 5
    assert slots[0]["short"] == "12-13"
    assert slots[-1]["short"] == "16-17"
    assert slots[2]["label"] == "14:00-15:00"


def test_hour_slots_wrap_around_midnight():
    cfg = battle.default_config()
    cfg["start_hour"] = 22
    cfg["hour_count"] = 4
    slots = battle.hour_slots(cfg)
    assert [s["short"] for s in slots] == ["22-23", "23-0", "0-1", "1-2"]


@pytest.mark.parametrize(
    "raw,expected",
    [
        ("189820095", "189820095"),
        ("189 820 095", "189820095"),
        ("  190314875  ", "190314875"),
        # A real player pasted his march sizes into the Gamer ID field.
        # Those digits must not read as one giant ID.
        ("Infantry: 318 000 Cavalry: 269 000 Archers: 282 000", ""),
        ("Jimmysandhu", ""),
        ("12345", ""),
        ("", ""),
        (None, ""),
    ],
)
def test_clean_gamer_id(raw, expected):
    assert battle.clean_gamer_id(raw) == expected


def test_best_tier_and_strength():
    cfg = battle.default_config()
    tiers = {"INF": "T11 TG6", "CAV": "TG5", "ARC": "T11 TG6"}
    assert battle.best_tier(tiers, cfg) == "T11 TG6"
    # 120 + 0.25 * (120 + 70)
    assert battle.strength(tiers, cfg) == pytest.approx(167.5)


def test_best_tier_ignores_missing():
    cfg = battle.default_config()
    assert battle.best_tier({"INF": None, "CAV": None, "ARC": None}, cfg) is None
    assert battle.best_tier({"INF": None, "CAV": "TG3", "ARC": None}, cfg) == "TG3"


def test_covers_hours_and_pattern():
    availability = [1, 1, 1, 0, 0]
    assert battle.covers_hours(availability, [0, 1, 2]) is True
    assert battle.covers_hours(availability, [0, 1, 2, 3]) is False
    assert battle.pattern(availability) == "###.."
    assert battle.hours_available(availability) == 3


def test_enrich_marks_unfieldable():
    cfg = battle.default_config()
    row = {"tiers": {"INF": None, "CAV": None, "ARC": None}, "availability": [1, 1, 0, 0, 0]}
    out = battle.enrich(row, cfg)
    assert out["fieldable"] is False

    row2 = {"tiers": {"INF": "TG4", "CAV": "TG4", "ARC": "TG4"}, "availability": [0] * 5}
    assert battle.enrich(row2, cfg)["fieldable"] is False


def test_parse_config_survives_garbage():
    assert battle.parse_config("not json")["hour_count"] == 5
    assert battle.parse_config(None)["start_hour"] == 12
    assert battle.parse_config(json.dumps({"hour_count": 99}))["hour_count"] == 5
    assert battle.parse_config(json.dumps({"hour_count": 3}))["hour_count"] == 3


def test_build_config_reads_alliances():
    cfg = battle.build_config(
        {"battle_start_hour": "9", "battle_hour_count": "3", "battle_alliances": "ICE, FYR\nMeo"}
    )
    assert cfg["start_hour"] == 9
    assert cfg["hour_count"] == 3
    assert cfg["alliances"] == ["ICE", "FYR", "Meo"]


# --------------------------------------------------------------------------
# Submission parsing
# --------------------------------------------------------------------------


def _valid_form(**overrides):
    form = {
        "player_name": "Yasenova",
        "player_id": "188755088",
        "alliance_name": "ICE",
        "tier_INF": "T11",
        "tier_CAV": "TG5",
        "tier_ARC": "T11",
        "hour_0": "yes",
        "hour_1": "yes",
        "hour_2": "yes",
        "hour_3": "no",
        "hour_4": "no",
        "notes": "",
    }
    form.update(overrides)
    return form


def test_parse_submission_happy_path():
    cfg = battle.default_config(["ICE", "FYR"])
    parsed = battle.parse_submission(_valid_form(), cfg)
    assert parsed["player_id"] == "188755088"
    assert parsed["tiers"] == {"INF": "T11", "CAV": "TG5", "ARC": "T11"}
    assert parsed["availability"] == [1, 1, 1, 0, 0]


def test_parse_submission_below_tier_becomes_none():
    cfg = battle.default_config()
    parsed = battle.parse_submission(_valid_form(tier_CAV=battle.BELOW_LABEL), cfg)
    assert parsed["tiers"]["CAV"] is None


@pytest.mark.parametrize(
    "overrides,fragment",
    [
        ({"player_name": "   "}, "in-game name"),
        ({"player_id": "abc"}, "6 to 12 digits"),
        ({"tier_INF": ""}, "Infantry"),
        ({"tier_ARC": "TG99"}, "Unknown troop tier"),
        ({"hour_2": ""}, "yes or no"),
        ({"hour_2": "maybe"}, "yes or no"),
    ],
)
def test_parse_submission_rejects(overrides, fragment):
    cfg = battle.default_config()
    with pytest.raises(battle.SubmissionError) as err:
        battle.parse_submission(_valid_form(**overrides), cfg)
    assert fragment in str(err.value)


def test_parse_submission_rejects_unknown_alliance():
    cfg = battle.default_config(["ICE", "FYR"])
    with pytest.raises(battle.SubmissionError):
        battle.parse_submission(_valid_form(alliance_name="Nope"), cfg)


def test_parse_submission_truncates_long_note():
    cfg = battle.default_config()
    parsed = battle.parse_submission(_valid_form(notes="x" * 900), cfg)
    assert len(parsed["notes"]) == battle.MAX_NOTE_LEN


# --------------------------------------------------------------------------
# Routes
# --------------------------------------------------------------------------


def _create_battle_event(client, **extra):
    data = {
        "event_name": "KvK Battle",
        "event_type": "battle",
        "battle_start_hour": "12",
        "battle_hour_count": "5",
        "battle_alliances": "ICE,FYR,Meo",
    }
    data.update(extra)
    resp = client.post("/create", data=data, follow_redirects=False)
    assert resp.status_code == 302
    uid = resp.headers["Location"].split("/success/")[1].split("?")[0]
    return uid


def test_create_battle_event_and_render_form(client):
    uid = _create_battle_event(client)
    page = client.get(f"/event/{uid}")
    assert page.status_code == 200
    body = page.get_data(as_text=True)
    assert "Availability" in body
    assert "12:00-13:00" in body
    assert "16:00-17:00" in body
    # alliance dropdown, not free text
    assert "<select" in body and "FYR" in body


def test_prep_event_still_renders_old_form(client):
    """The original behaviour must be untouched."""
    resp = client.post("/create", data={"event_name": "Prep"}, follow_redirects=False)
    uid = resp.headers["Location"].split("/success/")[1].split("?")[0]
    page = client.get(f"/event/{uid}")
    assert page.status_code == 200
    assert "Troop Tiers" not in page.get_data(as_text=True)


def test_battle_submission_is_stored(client, app):
    uid = _create_battle_event(client)
    resp = client.post(f"/event/{uid}/submit", data=_valid_form(), follow_redirects=True)
    assert resp.status_code == 200

    with app.app_context():
        from app import database

        rows = database.get_db().execute(
            "SELECT * FROM battle_submissions WHERE event_uid = ?", (uid,)
        ).fetchall()
    assert len(rows) == 1
    assert json.loads(rows[0][6]) == [1, 1, 1, 0, 0]


def test_resubmission_replaces_instead_of_duplicating(client, app):
    """The single biggest source of manual cleanup in the old workflow."""
    uid = _create_battle_event(client)
    client.post(f"/event/{uid}/submit", data=_valid_form(), follow_redirects=True)
    client.post(
        f"/event/{uid}/submit",
        data=_valid_form(player_name="Yasenova renamed", hour_3="yes"),
        follow_redirects=True,
    )

    with app.app_context():
        from app import database

        rows = database.get_db().execute(
            "SELECT player_name, availability FROM battle_submissions WHERE event_uid = ?",
            (uid,),
        ).fetchall()
    assert len(rows) == 1
    assert rows[0][0] == "Yasenova renamed"
    assert json.loads(rows[0][1]) == [1, 1, 1, 1, 0]


def test_bad_submission_returns_form_with_error(client):
    uid = _create_battle_event(client)
    resp = client.post(f"/event/{uid}/submit", data=_valid_form(player_id="nope"))
    assert resp.status_code == 400
    body = resp.get_data(as_text=True)
    assert "6 to 12 digits" in body
    # the player's other answers survive the round trip
    assert "Yasenova" in body


def test_density_counts_previous_answers(client):
    uid = _create_battle_event(client)
    client.post(f"/event/{uid}/submit", data=_valid_form(), follow_redirects=True)
    client.post(
        f"/event/{uid}/submit",
        data=_valid_form(player_id="190314875", player_name="Giraffe"),
        follow_redirects=True,
    )
    body = client.get(f"/event/{uid}").get_data(as_text=True)
    assert "2 submitted so far" in body


# --------------------------------------------------------------------------
# Note screening
# --------------------------------------------------------------------------


def test_flag_note_ignores_harmless_notes():
    assert battle.flag_note("", [1, 1, 1, 1, 1]) is None
    assert battle.flag_note(None, [1, 1, 1, 1, 1]) is None
    assert battle.flag_note("Thanks for organising!", [1, 1, 1, 1, 1]) is None
    assert battle.flag_note("Elmo is burning", [1, 1, 1, 1, 1]) is None


def test_flag_note_catches_hedging_against_full_availability():
    """Real note from the spreadsheet: ticked all five, hedged in the text."""
    flag = battle.flag_note("I will be on and off during the battle.", [1, 1, 1, 1, 1])
    assert flag is not None
    assert flag["level"] == "contradicts"
    assert "on and off" in flag["reasons"][0]


def test_flag_note_downgrades_when_few_hours_claimed():
    """Same words, but they only claimed one hour - nothing to contradict."""
    flag = battle.flag_note("I might be in and out", [1, 0, 0, 0, 0])
    assert flag is not None
    assert flag["level"] == "caution"


def test_flag_note_real_examples():
    cases = [
        "I might be in and out coz i have an engagement at that time",
        "Best guess on times, as I might be called away",
        "I work at 10:30 I will be off and on during the castle",
        "On and off on the battle but will try to be online the whole time",
        "Not sure if I will be available for one schedule in particular",
        "If im out of resources ill be leaving",
        "Sometimes need to put kids to sleep at 13 UTC",
    ]
    for note in cases:
        assert battle.flag_note(note, [1, 1, 1, 1, 1]) is not None, note


# --------------------------------------------------------------------------
# Dashboard analysis
# --------------------------------------------------------------------------


def _row(name, inf, cav, arc, avail, alliance="ICE", notes="", pid=None):
    # Every player needs their own Gamer ID - the tool keys on it, so sharing
    # one would make the whole test pool look like a single person.
    if pid is None:
        pid = "9%08d" % (abs(hash(name)) % 100000000)
    return {
        "player_id": pid,
        "player_name": name,
        "alliance_name": alliance,
        "tiers": {"INF": inf, "CAV": cav, "ARC": arc},
        "availability": avail,
        "notes": notes,
    }


def _sample():
    return [
        _row("Alpha", "T11 TG5", "TG5", "T11 TG5", [1, 1, 1, 1, 1]),
        _row("Bravo", "TG5", "TG5", "TG5", [1, 1, 1, 0, 0]),
        _row("Charlie", "TG4", "TG4", "TG4", [0, 0, 1, 1, 1], alliance="FYR"),
        _row("Delta", None, None, None, [1, 1, 1, 1, 1]),          # below tier
        _row("Echo", "TG3", "TG3", "TG3", [0, 0, 0, 0, 0]),        # never online
    ]


def test_analyse_splits_fieldable():
    cfg = battle.default_config()
    out = battle.analyse(_sample(), cfg)
    names = [p["player_name"] for p in out["fieldable"]]
    assert names == ["Alpha", "Bravo", "Charlie"]
    assert {p["player_name"] for p in out["not_fielded"]} == {"Delta", "Echo"}


def test_headcount_tracks_joins_and_leaves():
    cfg = battle.default_config()
    out = battle.analyse(_sample(), cfg)
    counts = [h["count"] for h in out["headcounts"]]
    assert counts == [2, 2, 3, 2, 2]
    assert out["headcounts"][2]["joined"] == ["Charlie"]
    assert out["headcounts"][3]["left"] == ["Bravo"]
    assert out["headcounts"][3]["delta"] == -1


def test_weakest_hour_is_the_lowest():
    cfg = battle.default_config()
    out = battle.analyse(_sample(), cfg)
    assert out["weakest"]["count"] == 2


def test_coverage_counts_per_tier_and_hour():
    cfg = battle.default_config()
    out = battle.analyse(_sample(), cfg)
    inf = out["coverage"]["INF"]
    by_tier = {r["tier"]: r for r in inf["rows"]}
    assert by_tier["T11 TG5"]["counts"] == [1, 1, 1, 1, 1]
    assert by_tier["TG5"]["counts"] == [1, 1, 1, 0, 0]
    assert by_tier["TG4"]["counts"] == [0, 0, 1, 1, 1]
    assert inf["totals"] == [2, 2, 3, 2, 2]


def test_coverage_uses_the_right_troop_type():
    """Alpha is T11 infantry but only TG5 cavalry."""
    cfg = battle.default_config()
    out = battle.analyse(_sample(), cfg)
    cav = {r["tier"]: r for r in out["coverage"]["CAV"]["rows"]}
    assert cav["T11 TG5"]["counts"] == [0, 0, 0, 0, 0]
    assert cav["TG5"]["counts"] == [2, 2, 2, 1, 1]


def test_alliance_breakdown():
    cfg = battle.default_config()
    out = battle.analyse(_sample(), cfg)
    assert out["alliances"] == [("ICE", 2), ("FYR", 1)]


def test_watchlist_only_contains_flagged_players():
    cfg = battle.default_config()
    rows = _sample()
    rows[1]["notes"] = "I will be on and off"
    out = battle.analyse(rows, cfg)
    assert [p["player_name"] for p in out["watchlist"]] == ["Bravo"]


def test_admin_dashboard_renders_for_battle_event(client):
    uid = _create_battle_event(client)
    client.post(f"/event/{uid}/submit", data=_valid_form(), follow_redirects=True)

    # admin secret is required
    assert client.get(f"/admin/{uid}").status_code == 403

    from app import database

    with client.application.app_context():
        secret = database.get_db().execute(
            "SELECT admin_secret FROM events WHERE uid = ?", (uid,)
        ).fetchone()[0]

    page = client.get(f"/admin/{uid}?secret={secret}")
    assert page.status_code == 200
    body = page.get_data(as_text=True)
    assert "Headcount per hour" in body
    assert "Coverage &mdash; Infantry" in body or "Coverage" in body
    assert "Yasenova" in body


def test_admin_dashboard_handles_zero_responses(client):
    uid = _create_battle_event(client)
    from app import database

    with client.application.app_context():
        secret = database.get_db().execute(
            "SELECT admin_secret FROM events WHERE uid = ?", (uid,)
        ).fetchone()[0]
    page = client.get(f"/admin/{uid}?secret={secret}")
    assert page.status_code == 200
    assert "No responses yet" in page.get_data(as_text=True)


# --------------------------------------------------------------------------
# Teams
# --------------------------------------------------------------------------


def _cfg_with_teams():
    cfg = battle.default_config(["ICE", "FYR"])
    return cfg


def _team_pool():
    """Two clear garrison candidates, one weaker, one lead, one late arrival."""
    return [
        _row("Strong5h", "TG5", "TG5", "TG5", [1, 1, 1, 1, 1]),
        _row("Strong3h", "TG5", "TG5", "TG5", [1, 1, 1, 0, 0]),
        _row("LeadT11", "T11", "T11", "T11", [1, 1, 1, 1, 1]),
        _row("Mid4", "TG4", "TG4", "TG4", [1, 1, 1, 1, 1], alliance="FYR"),
        _row("Late5", "TG5", "TG5", "TG5", [0, 0, 1, 1, 1]),
        _row("Low3", "TG3", "TG3", "TG3", [1, 1, 1, 0, 0], alliance="FYR"),
    ]


def _analysed(rows, cfg):
    return battle.analyse(rows, cfg)["fieldable"]


def test_meets_checks_tier_and_hours():
    cfg = _cfg_with_teams()
    p = battle.enrich(_row("X", "TG5", "TG5", "TG5", [1, 1, 0, 0, 0]), cfg)
    assert battle.meets(p, {"min_tier": "TG5", "required_hours": [0, 1]}, cfg) is True
    assert battle.meets(p, {"min_tier": "TG5", "required_hours": [0, 1, 2]}, cfg) is False
    assert battle.meets(p, {"min_tier": "T11", "required_hours": []}, cfg) is False


def test_garrison_takes_tier_and_opening_hours_only():
    cfg = _cfg_with_teams()
    plan = battle.assign_teams(_analysed(_team_pool(), cfg), cfg)
    garrison = plan["teams"][0]
    names = [p["player_name"] for p in garrison["core"]]
    # TG5+ and available 12-15
    assert set(names) == {"Strong5h", "Strong3h", "LeadT11"}
    # Late5 misses the opening hours, Mid4 and Low3 are below TG5
    assert "Late5" not in names and "Mid4" not in names


def test_counter_rally_picks_up_the_rest():
    cfg = _cfg_with_teams()
    plan = battle.assign_teams(_analysed(_team_pool(), cfg), cfg)
    # Anyone the earlier teams did not take has to land somewhere, and
    # nobody may be counted twice.
    later = []
    for team in plan["teams"][1:]:
        later += [p["player_name"] for p in team["core"]]
    assert set(later) == {"Mid4", "Late5", "Low3"}
    assert len(later) == len(set(later))


def test_lead_cap_zero_keeps_leads_out():
    cfg = _cfg_with_teams()
    cfg["teams"][0]["max_leads"] = 0
    players = _analysed(_team_pool(), cfg)
    lead_id = next(p for p in players if p["player_name"] == "LeadT11")["player_id"]
    plan = battle.assign_teams(players, cfg, {lead_id: {"role": "lead"}})
    assert "LeadT11" not in [p["player_name"] for p in plan["teams"][0]["core"]]
    assert "LeadT11" in [p["player_name"] for p in plan["teams"][1]["core"]]


def test_lead_cap_one_lets_a_single_lead_in():
    """A garrison usually needs one lead of its own - Fen's case."""
    cfg = _cfg_with_teams()
    cfg["teams"][0]["max_leads"] = 1
    rows = _team_pool() + [
        _row("SecondLead", "T11", "T11", "T11", [1, 1, 1, 1, 1]),
    ]
    players = _analysed(rows, cfg)
    ids = {p["player_name"]: p["player_id"] for p in players}
    plan = battle.assign_teams(
        players,
        cfg,
        {ids["LeadT11"]: {"role": "lead"}, ids["SecondLead"]: {"role": "lead"}},
    )
    garrison_leads = [p["player_name"] for p in plan["teams"][0]["leads"]]
    assert len(garrison_leads) == 1
    # the other one stays free for the counter rallies
    assert len(plan["teams"][1]["leads"]) == 1


def test_no_lead_cap_lets_every_lead_in():
    cfg = _cfg_with_teams()
    cfg["teams"][0]["max_leads"] = None
    players = _analysed(_team_pool(), cfg)
    lead_id = next(p for p in players if p["player_name"] == "LeadT11")["player_id"]
    plan = battle.assign_teams(players, cfg, {lead_id: {"role": "lead"}})
    assert "LeadT11" in [p["player_name"] for p in plan["teams"][0]["core"]]


def test_pinned_lead_gets_in_even_at_cap_zero():
    """Leadership pinned them, so the cap does not veto that."""
    cfg = _cfg_with_teams()
    cfg["teams"][0]["max_leads"] = 0
    players = _analysed(_team_pool(), cfg)
    lead_id = next(p for p in players if p["player_name"] == "LeadT11")["player_id"]
    plan = battle.assign_teams(players, cfg, {lead_id: {"role": "lead", "team": "A"}})
    assert "LeadT11" in [p["player_name"] for p in plan["teams"][0]["core"]]


def test_old_exclude_leads_flag_becomes_a_cap():
    """Events created before the cap existed must keep behaving the same."""
    old_team = {
        "key": "A", "name": "Garrison", "alliance": "ICE", "min_tier": "TG5",
        "required_hours": [0, 1, 2], "size": 10, "exclude_leads": True,
    }
    cfg = battle.parse_config(json.dumps({"teams": [old_team]}))
    assert cfg["teams"][0]["max_leads"] == 0
    assert "exclude_leads" not in cfg["teams"][0]

    old_team["exclude_leads"] = False
    cfg = battle.parse_config(json.dumps({"teams": [old_team]}))
    assert cfg["teams"][0]["max_leads"] is None


def test_manual_pin_survives_and_overrides_the_rules():
    """Late5 misses the opening hours but leadership wants him in anyway."""
    cfg = _cfg_with_teams()
    players = _analysed(_team_pool(), cfg)
    late = next(p for p in players if p["player_name"] == "Late5")
    plan = battle.assign_teams(players, cfg, {late["player_id"]: {"team": "A"}})
    assert "Late5" in [p["player_name"] for p in plan["teams"][0]["core"]]


def test_excluded_player_disappears_entirely():
    cfg = _cfg_with_teams()
    players = _analysed(_team_pool(), cfg)
    victim = players[0]
    plan = battle.assign_teams(players, cfg, {victim["player_id"]: {"excluded": True}})
    everyone = [
        p["player_name"]
        for t in plan["teams"]
        for p in t["core"] + t["reserve"]
    ] + [p["player_name"] for p in plan["unassigned"]]
    assert victim["player_name"] not in everyone


def test_core_size_pushes_the_rest_into_reserve():
    cfg = _cfg_with_teams()
    cfg["teams"][0]["size"] = 2
    plan = battle.assign_teams(_analysed(_team_pool(), cfg), cfg)
    assert len(plan["teams"][0]["core"]) == 2
    assert len(plan["teams"][0]["reserve"]) == 1


def test_more_hours_beats_more_tier_for_a_garrison():
    """A TG5 who stays all five hours outranks a T11 who leaves after three."""
    cfg = _cfg_with_teams()
    cfg["teams"][0]["size"] = 1
    rows = [
        _row("AllDayTG5", "TG5", "TG5", "TG5", [1, 1, 1, 1, 1]),
        _row("ShortT11", "T11", "T11", "T11", [1, 1, 1, 0, 0]),
    ]
    plan = battle.assign_teams(_analysed(rows, cfg), cfg)
    assert plan["teams"][0]["core"][0]["player_name"] == "AllDayTG5"


def test_hedged_comment_pushes_a_player_down_the_order():
    cfg = _cfg_with_teams()
    cfg["teams"][0]["size"] = 1
    rows = [
        _row("Hedger", "TG5", "TG5", "TG5", [1, 1, 1, 1, 1], notes="I'll be on and off"),
        _row("Clean", "TG5", "TG5", "TG5", [1, 1, 1, 1, 1]),
    ]
    plan = battle.assign_teams(_analysed(rows, cfg), cfg)
    assert plan["teams"][0]["core"][0]["player_name"] == "Clean"


def test_swaps_flag_players_from_another_alliance():
    cfg = _cfg_with_teams()
    rows = [_row("Outsider", "TG5", "TG5", "TG5", [1, 1, 1, 1, 1], alliance="Meo")]
    plan = battle.assign_teams(_analysed(rows, cfg), cfg)
    assert [p["player_name"] for p in plan["teams"][0]["swaps"]] == ["Outsider"]


# --------------------------------------------------------------------------
# Rewards
# --------------------------------------------------------------------------


def _reward_pool(n, tier="TG5"):
    return [
        _row("P%02d" % i, tier, tier, tier, [1, 1, 1, 1, 1], pid="20000000%d" % i)
        for i in range(n)
    ]


def test_reward_gives_one_each_then_seconds_down_the_list():
    cfg = battle.default_config()
    players = _analysed(_reward_pool(8), cfg)
    rule = {"key": "x", "name": "X", "total": 12, "max_per": 2,
            "min_tier": "TG5", "required_hours": []}
    out = battle.allocate_reward(players, rule, cfg)
    amounts = [g["amount"] for g in out["grants"]]
    assert sum(amounts) == 12
    assert amounts.count(2) == 4 and amounts.count(1) == 4
    assert out["left"] == 0


def test_reward_never_hands_out_more_than_exists():
    cfg = battle.default_config()
    players = _analysed(_reward_pool(10), cfg)
    rule = {"key": "x", "name": "X", "total": 3, "max_per": 2,
            "min_tier": "TG5", "required_hours": []}
    out = battle.allocate_reward(players, rule, cfg)
    assert sum(g["amount"] for g in out["grants"]) == 3
    assert len(out["short"]) == 7


def test_reward_reports_spare_pieces():
    cfg = battle.default_config()
    players = _analysed(_reward_pool(3), cfg)
    rule = {"key": "x", "name": "X", "total": 20, "max_per": 2,
            "min_tier": "TG5", "required_hours": []}
    out = battle.allocate_reward(players, rule, cfg)
    assert sum(g["amount"] for g in out["grants"]) == 6
    assert out["left"] == 14


def test_reward_respects_what_was_already_handed_out():
    """Re-running must not give the same player a second share."""
    cfg = battle.default_config()
    players = _analysed(_reward_pool(4), cfg)
    rule = {"key": "x", "name": "X", "total": 8, "max_per": 2,
            "min_tier": "TG5", "required_hours": []}
    first = players[0]["player_id"]
    out = battle.allocate_reward(players, rule, cfg, already={first: 2})
    by_id = {g["player"]["player_id"]: g for g in out["grants"]}
    assert by_id[first]["amount"] == 2
    assert by_id[first]["outstanding"] == 0
    assert sum(g["outstanding"] for g in out["grants"]) == 6


def test_reward_tier_floor_filters_the_pool():
    cfg = battle.default_config()
    players = _analysed(_reward_pool(3, "TG3") + _reward_pool(2, "TG5"), cfg)
    rule = {"key": "x", "name": "X", "total": 10, "max_per": 2,
            "min_tier": "TG5", "required_hours": []}
    out = battle.allocate_reward(players, rule, cfg)
    assert out["eligible"] == 2


def test_reward_hour_requirement_filters_the_pool():
    cfg = battle.default_config()
    rows = [
        _row("Early", "TG5", "TG5", "TG5", [1, 1, 0, 0, 0], pid="300000001"),
        _row("Late", "TG5", "TG5", "TG5", [0, 0, 1, 1, 1], pid="300000002"),
    ]
    rule = {"key": "x", "name": "X", "total": 10, "max_per": 2,
            "min_tier": "TG5", "required_hours": [0, 1]}
    out = battle.allocate_reward(_analysed(rows, cfg), rule, cfg)
    assert [g["player"]["player_name"] for g in out["grants"]] == ["Early"]


# --------------------------------------------------------------------------
# Team and reward routes
# --------------------------------------------------------------------------


def _secret_for(client, uid):
    from app import database

    with client.application.app_context():
        return database.get_db().execute(
            "SELECT admin_secret FROM events WHERE uid = ?", (uid,)
        ).fetchone()[0]


def test_dashboard_shows_teams_and_rewards(client):
    uid = _create_battle_event(client)
    client.post(f"/event/{uid}/submit", data=_valid_form(), follow_redirects=True)
    body = client.get(f"/admin/{uid}?secret={_secret_for(client, uid)}").get_data(as_text=True)
    assert "Castle" in body
    assert "Turret" in body
    assert "Counter Rally" in body
    assert "Sanctuary" in body
    assert "Deployment" in body


def test_setting_a_rally_lead_persists(client):
    uid = _create_battle_event(client)
    client.post(f"/event/{uid}/submit", data=_valid_form(), follow_redirects=True)
    secret = _secret_for(client, uid)

    resp = client.post(
        f"/admin/{uid}/battle/player",
        data={"secret": secret, "player_id": "188755088", "field": "role", "value": "lead"},
    )
    assert resp.status_code == 302

    from app import database

    with client.application.app_context():
        row = database.get_db().execute(
            "SELECT role, is_lead FROM battle_player_admin "
            "WHERE event_uid = ? AND player_id = ?",
            (uid, "188755088"),
        ).fetchone()
    assert row[0] == "lead"
    assert row[1] == 1


def test_recording_a_delivered_reward_persists(client):
    uid = _create_battle_event(client)
    client.post(f"/event/{uid}/submit", data=_valid_form(), follow_redirects=True)
    secret = _secret_for(client, uid)

    client.post(
        f"/admin/{uid}/battle/reward",
        data={"secret": secret, "reward_key": "deployment",
              "player_id": "188755088", "delivered": "2"},
    )
    from app import database

    with client.application.app_context():
        row = database.get_db().execute(
            "SELECT delivered FROM battle_reward_grants "
            "WHERE event_uid = ? AND reward_key = ? AND player_id = ?",
            (uid, "deployment", "188755088"),
        ).fetchone()
    assert row[0] == 2


def test_admin_actions_need_the_secret(client):
    uid = _create_battle_event(client)
    assert client.post(
        f"/admin/{uid}/battle/player",
        data={"player_id": "188755088", "field": "role", "value": "lead"},
    ).status_code == 403
    assert client.post(
        f"/admin/{uid}/battle/reward",
        data={"reward_key": "deployment", "player_id": "1", "delivered": "1"},
    ).status_code == 403


def test_reward_route_rejects_unknown_key(client):
    uid = _create_battle_event(client)
    secret = _secret_for(client, uid)
    assert client.post(
        f"/admin/{uid}/battle/reward",
        data={"secret": secret, "reward_key": "nope",
              "player_id": "188755088", "delivered": "1"},
    ).status_code == 400


def test_flag_note_handles_other_languages():
    """Real submissions written in French slipped through an English-only screen."""
    cases = [
        # Ashura: "present the first two hours, then only from time to time"
        "Je serais présent les deux première heure et ensuite ça sera par moment",
        # Ben: "may be available in the other slots"
        "Peut être disponible sur les autres horaires",
        "Tal vez no pueda estar todo el tiempo",
        "Talvez eu não esteja o tempo todo",
        "Mungkin saya tidak online terus",
        "Vielleicht bin ich nicht die ganze Zeit da",
    ]
    for note in cases:
        assert battle.flag_note(note, [1, 1, 1, 1, 1]) is not None, note


# --------------------------------------------------------------------------
# Rules stay editable for the whole life of the event
# --------------------------------------------------------------------------


class _FakeForm(dict):
    """Mimics a Flask MultiDict for the fields we read."""

    def __init__(self, data, lists=None):
        super().__init__(data)
        self._lists = lists or {}

    def getlist(self, key):
        return self._lists.get(key, [])


def test_update_rules_changes_reward_amounts():
    cfg = battle.default_config(["ICE", "FYR"])
    form = _FakeForm({
        "reward_deployment_present": "1",
        "reward_hero_gear_present": "1",
        "reward_deployment_total": "23",
        "reward_deployment_max_per": "1",
        "reward_deployment_min_tier": "TG3",
        "reward_hero_gear_total": "365",
        "reward_hero_gear_max_per": "15",
    })
    out = battle.update_rules(cfg, form)
    dep = next(r for r in out["rewards"] if r["key"] == "deployment")
    hero = next(r for r in out["rewards"] if r["key"] == "hero_gear")
    assert dep["total"] == 23 and dep["max_per"] == 1 and dep["min_tier"] == "TG3"
    assert hero["total"] == 365 and hero["max_per"] == 15


def test_update_rules_changes_team_conditions():
    cfg = battle.default_config(["ICE", "FYR"])
    form = _FakeForm(
        {
            "team_A_present": "1",
            "team_A_name": "Castle Hold",
            "team_A_alliance": "ICE",
            "team_A_min_tier": "TG4",
            "team_A_size": "14",
            "team_A_max_leads": "1",
        },
        {"team_A_hours": ["0", "1"]},
    )
    out = battle.update_rules(cfg, form)
    team = out["teams"][0]
    assert team["name"] == "Castle Hold"
    assert team["min_tier"] == "TG4"
    assert team["size"] == 14
    assert team["required_hours"] == [0, 1]
    assert team["max_leads"] == 1


def test_update_rules_empty_size_means_no_limit():
    cfg = battle.default_config()
    out = battle.update_rules(cfg, _FakeForm({"team_A_present": "1", "team_A_size": ""}))
    assert out["teams"][0]["size"] is None


def test_update_rules_ignores_nonsense():
    """Garbage input leaves the previous value in place instead of breaking it."""
    cfg = battle.default_config()
    form = _FakeForm({
        "reward_deployment_present": "1",
        "reward_deployment_total": "not a number",
        "reward_deployment_max_per": "-5",
        "reward_deployment_min_tier": "TG99",
    })
    out = battle.update_rules(cfg, form)
    dep = out["rewards"][0]
    assert dep["total"] == 60
    assert dep["max_per"] == 1
    assert dep["min_tier"] == "TG4"


def test_update_rules_drops_hours_outside_the_battle():
    cfg = battle.default_config()  # 5 hours, indexes 0-4
    out = battle.update_rules(cfg, _FakeForm({"team_A_present": "1"}, {"team_A_hours": ["0", "9", "abc", "4"]}))
    assert out["teams"][0]["required_hours"] == [0, 4]


def test_update_rules_does_not_touch_the_original():
    cfg = battle.default_config()
    battle.update_rules(cfg, _FakeForm({"reward_deployment_present": "1", "reward_deployment_total": "999"}))
    assert cfg["rewards"][0]["total"] == 60


def test_settings_route_persists_and_reallocates(client):
    """Change the amount late in the event and the split follows."""
    uid = _create_battle_event(client)
    client.post(f"/event/{uid}/submit", data=_valid_form(), follow_redirects=True)
    client.post(
        f"/event/{uid}/submit",
        data=_valid_form(player_id="190314875", player_name="Giraffe"),
        follow_redirects=True,
    )
    secret = _secret_for(client, uid)

    resp = client.post(
        f"/admin/{uid}/battle/settings",
        data={"secret": secret, "reward_deployment_present": "1",
              "reward_deployment_total": "3", "reward_deployment_max_per": "2"},
    )
    assert resp.status_code == 302

    body = client.get(f"/admin/{uid}?secret={secret}").get_data(as_text=True)
    assert "3 pieces" in body


def test_settings_route_needs_the_secret(client):
    uid = _create_battle_event(client)
    assert client.post(
        f"/admin/{uid}/battle/settings", data={"reward_deployment_total": "5"}
    ).status_code == 403


def test_partial_update_leaves_other_blocks_alone():
    """Editing one reward must not wipe the settings of another."""
    cfg = battle.default_config()
    cfg["rewards"][1]["required_hours"] = [0, 1, 2]
    out = battle.update_rules(
        cfg, _FakeForm({"reward_deployment_present": "1", "reward_deployment_total": "12"})
    )
    assert out["rewards"][0]["total"] == 12
    assert out["rewards"][1]["required_hours"] == [0, 1, 2]
    assert out["teams"][0]["required_hours"] == cfg["teams"][0]["required_hours"]


def test_unchecking_every_hour_is_honoured():
    """An empty submitted block genuinely means 'no required hours'."""
    cfg = battle.default_config()
    out = battle.update_rules(cfg, _FakeForm({"team_A_present": "1"}))
    assert out["teams"][0]["required_hours"] == []


# --------------------------------------------------------------------------
# Players added by leadership
#
# Rally leads and senior officers routinely never answer the form, but still
# have to appear in the teams and the reward lists.
# --------------------------------------------------------------------------


def _manual_form(**overrides):
    form = {
        "player_name": "Fen",
        "player_id": "191000001",
        "alliance_name": "Meo",
        "tier_INF": "TG5",
        "tier_CAV": "TG5",
        "tier_ARC": "TG5",
        "hour_0": "yes",
        "hour_1": "yes",
        "hour_2": "yes",
        "hour_3": "yes",
        "hour_4": "yes",
        "notes": "never filled in the form",
    }
    form.update(overrides)
    return form


def test_manual_player_appears_everywhere(client):
    uid = _create_battle_event(client)
    secret = _secret_for(client, uid)
    client.post(f"/event/{uid}/submit", data=_valid_form(), follow_redirects=True)

    resp = client.post(
        f"/admin/{uid}/battle/manual",
        data=dict(_manual_form(), secret=secret),
    )
    assert resp.status_code == 302

    body = client.get(f"/admin/{uid}?secret={secret}").get_data(as_text=True)
    assert "Fen" in body
    assert "Added by leadership" in body


def test_manual_player_counts_towards_teams_and_rewards(client, app):
    uid = _create_battle_event(client)
    secret = _secret_for(client, uid)
    client.post(f"/admin/{uid}/battle/manual", data=dict(_manual_form(), secret=secret))

    from app import database

    with app.app_context():
        rows = database.get_db().execute(
            "SELECT source FROM battle_submissions WHERE event_uid = ?", (uid,)
        ).fetchall()
    assert [r[0] for r in rows] == ["admin"]

    cfg = battle.default_config(["ICE", "FYR", "Meo"])
    player = battle.enrich(
        {
            "player_id": "191000001",
            "player_name": "Fen",
            "alliance_name": "Meo",
            "tiers": {"INF": "TG5", "CAV": "TG5", "ARC": "TG5"},
            "availability": [1, 1, 1, 1, 1],
            "notes": "",
        },
        cfg,
    )
    plan = battle.assign_teams([player], cfg)
    assert "Fen" in [p["player_name"] for p in plan["teams"][0]["core"]]


def test_manual_entry_updates_instead_of_duplicating(client, app):
    uid = _create_battle_event(client)
    secret = _secret_for(client, uid)
    client.post(f"/admin/{uid}/battle/manual", data=dict(_manual_form(), secret=secret))
    client.post(
        f"/admin/{uid}/battle/manual",
        data=dict(_manual_form(player_name="Fen (R4)"), secret=secret),
    )

    from app import database

    with app.app_context():
        rows = database.get_db().execute(
            "SELECT player_name FROM battle_submissions WHERE event_uid = ?", (uid,)
        ).fetchall()
    assert [r[0] for r in rows] == ["Fen (R4)"]


def test_unticked_hours_become_no(client, app):
    """Unchecked boxes are not submitted at all - the hidden field covers them."""
    uid = _create_battle_event(client)
    secret = _secret_for(client, uid)
    form = _manual_form()
    # Simulate the browser: ticked hours send "yes" first, then the hidden "no";
    # unticked hours send only "no".
    del form["hour_3"], form["hour_4"]
    form["hour_3"] = "no"
    form["hour_4"] = "no"
    client.post(f"/admin/{uid}/battle/manual", data=dict(form, secret=secret))

    from app import database

    with app.app_context():
        row = database.get_db().execute(
            "SELECT availability FROM battle_submissions WHERE event_uid = ?", (uid,)
        ).fetchone()
    assert json.loads(row[0]) == [1, 1, 1, 0, 0]


def test_manual_entry_validates_the_gamer_id(client, app):
    uid = _create_battle_event(client)
    secret = _secret_for(client, uid)
    resp = client.post(
        f"/admin/{uid}/battle/manual",
        data=dict(_manual_form(player_id="Fen"), secret=secret),
    )
    assert resp.status_code == 302  # back to the dashboard with a message

    from app import database

    with app.app_context():
        rows = database.get_db().execute(
            "SELECT 1 FROM battle_submissions WHERE event_uid = ?", (uid,)
        ).fetchall()
    assert rows == []


def test_manual_player_can_be_removed(client, app):
    uid = _create_battle_event(client)
    secret = _secret_for(client, uid)
    client.post(f"/admin/{uid}/battle/manual", data=dict(_manual_form(), secret=secret))
    client.post(
        f"/admin/{uid}/battle/manual",
        data={"secret": secret, "action": "delete", "player_id": "191000001"},
    )

    from app import database

    with app.app_context():
        rows = database.get_db().execute(
            "SELECT 1 FROM battle_submissions WHERE event_uid = ?", (uid,)
        ).fetchall()
    assert rows == []


def test_removing_does_not_touch_a_real_players_answer(client, app):
    """A player's own submission is theirs to change, not ours to delete."""
    uid = _create_battle_event(client)
    secret = _secret_for(client, uid)
    client.post(f"/event/{uid}/submit", data=_valid_form(), follow_redirects=True)
    client.post(
        f"/admin/{uid}/battle/manual",
        data={"secret": secret, "action": "delete", "player_id": "188755088"},
    )

    from app import database

    with app.app_context():
        rows = database.get_db().execute(
            "SELECT player_name FROM battle_submissions WHERE event_uid = ?", (uid,)
        ).fetchall()
    assert [r[0] for r in rows] == ["Yasenova"]


def test_manual_route_needs_the_secret(client):
    uid = _create_battle_event(client)
    assert client.post(
        f"/admin/{uid}/battle/manual", data=_manual_form()
    ).status_code == 403


# --------------------------------------------------------------------------
# Roles: rally lead vs joiner
# --------------------------------------------------------------------------


def test_joiner_role_does_not_exclude_from_garrison():
    """Only rally leads are held back; joiners are ordinary members."""
    cfg = _cfg_with_teams()
    players = _analysed(_team_pool(), cfg)
    p = next(x for x in players if x["player_name"] == "Strong5h")
    plan = battle.assign_teams(players, cfg, {p["player_id"]: {"role": "joiner"}})
    assert "Strong5h" in [x["player_name"] for x in plan["teams"][0]["core"]]


def test_role_is_reported_per_team():
    cfg = _cfg_with_teams()
    cfg["teams"][0]["max_leads"] = 0        # keep the lead out so it lands in B
    players = _analysed(_team_pool(), cfg)
    by_name = {p["player_name"]: p["player_id"] for p in players}
    plan = battle.assign_teams(
        players,
        cfg,
        {
            by_name["LeadT11"]: {"role": "lead"},
            by_name["Strong5h"]: {"role": "joiner"},
        },
    )
    garrison = plan["teams"][0]
    assert [p["player_name"] for p in garrison["joiners"]] == ["Strong5h"]
    leads_elsewhere = []
    for team in plan["teams"][1:]:
        leads_elsewhere += [p["player_name"] for p in team["leads"]]
    assert leads_elsewhere == ["LeadT11"]


def test_setting_joiner_persists_and_clears_lead_flag(client):
    uid = _create_battle_event(client)
    client.post(f"/event/{uid}/submit", data=_valid_form(), follow_redirects=True)
    secret = _secret_for(client, uid)

    from app import database

    client.post(
        f"/admin/{uid}/battle/player",
        data={"secret": secret, "player_id": "188755088", "field": "role", "value": "lead"},
    )
    client.post(
        f"/admin/{uid}/battle/player",
        data={"secret": secret, "player_id": "188755088", "field": "role", "value": "joiner"},
    )
    with client.application.app_context():
        row = database.get_db().execute(
            "SELECT role, is_lead FROM battle_player_admin "
            "WHERE event_uid = ? AND player_id = ?",
            (uid, "188755088"),
        ).fetchone()
    assert row[0] == "joiner"
    assert row[1] == 0


def test_role_route_rejects_unknown_value(client):
    uid = _create_battle_event(client)
    secret = _secret_for(client, uid)
    assert client.post(
        f"/admin/{uid}/battle/player",
        data={"secret": secret, "player_id": "188755088",
              "field": "role", "value": "general"},
    ).status_code == 400


def test_player_form_shows_availability_and_timezone_toggle(client):
    uid = _create_battle_event(client)
    body = client.get(f"/event/{uid}").get_data(as_text=True)
    assert "Your information" in body
    assert "Troop tier" in body
    assert "Availability" in body
    assert 'id="tz-local"' in body   # UTC / local switch
    # The demand legend is gone, but the colouring it described is not:
    # a quiet hour and a crowded one still look different.
    assert "d-low" in body
