"""
The whole-KvK event: one player link, two planning views, two public pages.

Every case here comes from something that actually went wrong while planning
a real castle battle by hand.
"""

import json

import pytest

from app import battle


def _rows():
    T = lambda i, c, a: {"INF": i, "CAV": c, "ARC": a}
    return [
        {"player_id": "190844226", "player_name": "Pugs", "alliance_name": "ICE",
         "tiers": T("TG5", "TG5", "T11"), "availability": [1, 1, 1, 1, 1], "notes": ""},
        {"player_id": "696969", "player_name": "KingOfClubs", "alliance_name": "ICE",
         "tiers": T("TG5", "TG5", "T11"), "availability": [1, 1, 1, 0, 0], "notes": ""},
        {"player_id": "191171494", "player_name": "Lord Schultz", "alliance_name": "ICE",
         "tiers": T("TG5", "TG4", "TG5"), "availability": [1, 1, 1, 0, 0], "notes": ""},
        {"player_id": "191069150", "player_name": "Zeqo", "alliance_name": "ICE",
         "tiers": T("TG5", "TG5", "TG5"), "availability": [0, 0, 0, 1, 1], "notes": ""},
        {"player_id": "192461365", "player_name": "N18!", "alliance_name": "ICE",
         "tiers": T("TG5", "TG5", "TG5"), "availability": [1, 1, 1, 1, 1], "notes": ""},
    ]


# --------------------------------------------------------------------------
# Tier depth
# --------------------------------------------------------------------------


def test_full_tier_needs_every_troop_type():
    """
    Lord Schultz reads as TG5 because his best type is TG5, but his cavalry
    is a tier short. Planning a castle slot off the best type sends a march
    that is weaker than the sheet promises.
    """
    cfg = battle.default_config()
    strong = {"INF": "TG5", "CAV": "TG5", "ARC": "TG5"}
    uneven = {"INF": "TG5", "CAV": "TG4", "ARC": "TG5"}
    assert battle.best_tier(uneven, cfg) == "TG5"
    assert battle.full_tier(strong, cfg, "TG5") is True
    assert battle.full_tier(uneven, cfg, "TG5") is False


def test_enrich_names_the_weak_troop_type():
    cfg = battle.default_config()
    row = {"player_id": "1", "player_name": "x", "tiers": {"INF": "TG5", "CAV": "TG4", "ARC": "TG5"},
           "availability": [1, 1, 1, 0, 0]}
    out = battle.enrich(row, cfg)
    assert out["full_tg5"] is False
    assert "Cavalry TG4" in out["full_tier_note"]


def test_team_can_demand_full_tier():
    cfg = battle.default_config()
    cfg["teams"] = [{"key": "A", "name": "Castle", "alliance": "", "min_tier": "TG5",
                     "required_hours": [0], "size": None, "max_leads": None,
                     "full_tier": True}]
    players = [battle.enrich(r, cfg) for r in _rows()]
    plan = battle.assign_teams([p for p in players if p["fieldable"]], cfg)
    names = [p["player_name"] for p in plan["teams"][0]["core"]]
    assert "Lord Schultz" not in names
    assert "N18!" in names


# --------------------------------------------------------------------------
# Aliases and bad data
# --------------------------------------------------------------------------


def test_alias_folds_two_names_into_one_player():
    cfg = battle.default_config()
    cfg["aliases"] = {"KingOfClubs": "Pugs"}
    data = battle.analyse(_rows(), cfg)
    names = [p["player_name"] for p in data["players"]]
    assert names.count("Pugs") == 1
    assert "KingOfClubs" not in names
    # The record that covers more hours is the one we keep.
    pugs = next(p for p in data["players"] if p["player_name"] == "Pugs")
    assert pugs["hours"] == 5


def test_alias_merge_still_reports_the_duplicate():
    """Merging must not hide the reason the merge was needed."""
    cfg = battle.default_config()
    cfg["aliases"] = {"KingOfClubs": "Pugs"}
    data = battle.analyse(_rows(), cfg)
    levels = {w["level"] for w in data["warnings"]}
    assert "duplicate" in levels
    assert "fake_id" in levels


def test_parse_aliases_reads_the_textarea():
    out = battle.parse_aliases("KingOfClubs = Pugs\nJ0KER -> TINA\n# comment\n\nbad line")
    assert out == {"KingOfClubs": "Pugs", "J0KER": "TINA"}


def test_alias_text_round_trips():
    cfg = battle.default_config()
    cfg["aliases"] = {"J0KER": "TINA"}
    assert battle.parse_aliases(battle.aliases_text(cfg)) == {"J0KER": "TINA"}


def test_joke_id_is_flagged():
    cfg = battle.default_config()
    warnings = battle.analyse(_rows(), cfg)["warnings"]
    assert any(w["level"] == "fake_id" and "696969" in w["text"] for w in warnings)


def test_one_id_two_names_is_flagged():
    cfg = battle.default_config()
    rows = _rows()
    rows[1]["player_id"] = rows[0]["player_id"]
    warnings = battle.analyse(rows, cfg)["warnings"]
    assert any(w["level"] == "shared_id" for w in warnings)


# --------------------------------------------------------------------------
# Hours
# --------------------------------------------------------------------------


def test_late_only_players_get_their_own_bucket():
    cfg = battle.default_config()
    data = battle.analyse(_rows(), cfg)
    assert [p["player_name"] for p in data["late_only"]] == ["Zeqo"]
    assert "Zeqo" not in [p["player_name"] for p in data["deployable"]]


def test_relay_waives_the_required_hours():
    """
    Two players each cover part of the window and hand over. Leadership sets
    the flag; the hour rule is theirs to waive.
    """
    cfg = battle.default_config()
    players = [battle.enrich(r, cfg) for r in _rows()]
    fieldable = [p for p in players if p["fieldable"]]
    plain = battle.assign_teams(fieldable, cfg)
    assert "Zeqo" not in [p["player_name"] for p in plain["teams"][0]["core"]]
    with_relay = battle.assign_teams(fieldable, cfg, {"191069150": {"role": "relay"}})
    assert "Zeqo" in [p["player_name"] for p in with_relay["teams"][0]["core"]]


def test_lead_gap_is_reported():
    """The turret lost its rally lead at 15:00 and nothing said a word."""
    cfg = battle.default_config()
    cfg["teams"] = [{"key": "A", "name": "Castle", "alliance": "", "min_tier": "TG3",
                     "required_hours": [], "size": None, "max_leads": None}]
    rows = [
        {"player_id": "1", "player_name": "Lead", "alliance_name": "ICE",
         "tiers": {"INF": "TG5", "CAV": "TG5", "ARC": "TG5"},
         "availability": [1, 1, 1, 0, 0], "notes": ""},
        {"player_id": "2", "player_name": "Joiner", "alliance_name": "ICE",
         "tiers": {"INF": "TG5", "CAV": "TG5", "ARC": "TG5"},
         "availability": [1, 1, 1, 1, 1], "notes": ""},
    ]
    players = [battle.enrich(r, cfg) for r in rows]
    plan = battle.assign_teams(players, cfg, {"1": {"role": "lead"}})
    gaps = plan["teams"][0]["lead_gaps"]
    assert "15:00-16:00" in gaps and "16:00-17:00" in gaps
    assert "12:00-13:00" not in gaps


def test_three_default_teams():
    cfg = battle.default_config()
    assert [t["name"] for t in cfg["teams"]] == ["Castle", "Turret", "Counter Rally"]


# --------------------------------------------------------------------------
# Routes
# --------------------------------------------------------------------------


def _create_combined(client):
    resp = client.post("/create", data={
        "event_name": "Whole KvK",
        "event_type": "both",
        "battle_start_hour": "12",
        "battle_hour_count": "5",
        "battle_alliances": "ICE, FYR, Meo",
        "research_day": "5",
        "slot_count": "49",
    }, follow_redirects=False)
    assert resp.status_code == 302
    location = resp.headers["Location"]
    uid = location.split("/success/")[1].split("?")[0]
    secret = location.split("secret=")[1]
    return uid, secret


def test_combined_event_offers_five_links(client):
    uid, secret = _create_combined(client)
    body = client.get(f"/success/{uid}?secret={secret}").get_data(as_text=True)
    for label in ("Player link", "Prep planning", "Prep schedule",
                  "Battle planning", "Battle line-up"):
        assert label in body


def test_player_link_is_one_form_with_one_button(client):
    """
    Two cards to click would be two links wearing a hat. The member fills in
    one page and presses send once.
    """
    uid, _ = _create_combined(client)
    page = client.get(f"/event/{uid}").get_data(as_text=True)
    assert page.count("<form") == 1
    assert page.count('type="submit"') == 1
    assert "Prep week" in page and "Battle day" in page
    # both halves are really on the page, not just their headings
    assert 'name="slots-construction"' in page or "slots-input-construction" in page
    assert 'name="hour_0"' in page
    assert 'name="tier_INF"' in page
    # identity is asked once
    assert page.count('name="player_id"') == 1
    assert page.count('name="player_name"') == 1


def test_one_post_stores_both_halves(client):
    uid, secret = _create_combined(client)
    resp = client.post(f"/event/{uid}/submit", data={
        "_mode": "both",
        "player_name": "Yasenova", "player_id": "190314875", "alliance_name": "ICE",
        # battle half
        "tier_INF": "T11", "tier_CAV": "TG5", "tier_ARC": "T11",
        "hour_0": "yes", "hour_1": "yes", "hour_2": "yes",
        "hour_3": "no", "hour_4": "no", "notes": "",
        # prep half
        "speedups-construction": "500", "truegold": "10", "tempered_truegold": "2",
        "slots-construction": "[4, 5]",
    }, follow_redirects=False)
    assert resp.status_code == 302

    from app import database
    with client.application.app_context():
        db = database.get_db()
        battle_rows = db.execute(
            "SELECT * FROM battle_submissions WHERE event_uid = ?", (uid,)
        ).fetchall()
        prep_rows = db.execute(
            "SELECT day_type FROM submissions WHERE event_uid = ?", (uid,)
        ).fetchall()
    assert len(battle_rows) == 1
    assert len(prep_rows) == 1
    assert prep_rows[0][0] == "construction"


def test_direct_halves_still_reachable(client):
    uid, _ = _create_combined(client)
    assert client.get(f"/event/{uid}/prep").status_code == 200
    assert client.get(f"/event/{uid}/battle").status_code == 200


def test_admin_link_leads_to_both_views(client):
    uid, secret = _create_combined(client)
    home = client.get(f"/admin/{uid}?secret={secret}").get_data(as_text=True)
    assert "Prep week planning" in home
    assert "Battle day planning" in home
    assert client.get(f"/admin/{uid}/prep?secret={secret}").status_code == 200
    assert client.get(f"/admin/{uid}/battle?secret={secret}").status_code == 200


def test_admin_views_need_the_secret(client):
    uid, _ = _create_combined(client)
    assert client.get(f"/admin/{uid}/prep").status_code == 403
    assert client.get(f"/admin/{uid}/battle").status_code == 403
    assert client.get(f"/admin/{uid}/battle?secret=wrong").status_code == 403


def test_lineup_is_closed_until_published(client):
    uid, secret = _create_combined(client)
    body = client.get(f"/event/{uid}/lineup").get_data(as_text=True)
    assert "not published yet" in body
    client.post(f"/admin/{uid}/battle/publish",
                data={"secret": secret, "published": "yes"}, follow_redirects=True)
    body = client.get(f"/event/{uid}/lineup").get_data(as_text=True)
    assert "not published yet" not in body


def test_csv_export_needs_the_secret_and_lists_players(client):
    uid, secret = _create_combined(client)
    client.post(f"/event/{uid}/submit", data={
        "_mode": "battle",
        "player_name": "Lord Schultz", "player_id": "191171494", "alliance_name": "ICE",
        "tier_INF": "TG5", "tier_CAV": "TG4", "tier_ARC": "TG5",
        "hour_0": "yes", "hour_1": "yes", "hour_2": "yes",
        "hour_3": "no", "hour_4": "no", "notes": "",
    }, follow_redirects=True)
    assert client.get(f"/admin/{uid}/battle/export.csv").status_code == 403
    resp = client.get(f"/admin/{uid}/battle/export.csv?secret={secret}")
    assert resp.status_code == 200
    text = resp.get_data(as_text=True)
    assert "Lord Schultz" in text
    # the full-tier answer travels with the export, not just the best tier
    assert "Full TG5" in text.splitlines()[0]
    assert ",no," in text


def test_prep_only_event_has_no_battle_pages(client):
    resp = client.post("/create", data={
        "event_name": "Prep only", "event_type": "prep",
        "research_day": "5", "slot_count": "49",
    }, follow_redirects=False)
    uid = resp.headers["Location"].split("/success/")[1].split("?")[0]
    assert client.get(f"/event/{uid}/battle").status_code == 404
    assert client.get(f"/event/{uid}/lineup").status_code == 404


def test_battle_only_answer_still_goes_through(client):
    """
    Someone who skips the prep half must not lose their battle answer.
    """
    uid, _ = _create_combined(client)
    resp = client.post(f"/event/{uid}/submit", data={
        "_mode": "both",
        "player_name": "Otter", "player_id": "189987755", "alliance_name": "ICE",
        "tier_INF": "TG3", "tier_CAV": "TG3", "tier_ARC": "TG3",
        "hour_0": "yes", "hour_1": "yes", "hour_2": "yes",
        "hour_3": "no", "hour_4": "no", "notes": "",
    }, follow_redirects=False)
    assert resp.status_code == 302
    from app import database
    with client.application.app_context():
        db = database.get_db()
        assert db.execute(
            "SELECT COUNT(*) FROM battle_submissions WHERE event_uid = ?", (uid,)
        ).fetchone()[0] == 1
        assert db.execute(
            "SELECT COUNT(*) FROM submissions WHERE event_uid = ?", (uid,)
        ).fetchone()[0] == 0


def test_prep_only_answer_still_goes_through(client):
    """And the other way round: no tiers, but the prep half is real."""
    uid, _ = _create_combined(client)
    resp = client.post(f"/event/{uid}/submit", data={
        "_mode": "both",
        "player_name": "Lera", "player_id": "192597026", "alliance_name": "ICE",
        "speedups-construction": "900", "truegold": "5", "tempered_truegold": "0",
        "slots-construction": "[10]",
    }, follow_redirects=False)
    from app import database
    with client.application.app_context():
        db = database.get_db()
        prep = db.execute(
            "SELECT COUNT(*) FROM submissions WHERE event_uid = ?", (uid,)
        ).fetchone()[0]
    assert resp.status_code == 302
    assert prep == 1


def test_no_weakest_hour_when_every_hour_is_equal():
    """
    With one response every hour holds one player. Calling the first one
    "weakest" is a warning about nothing.
    """
    flat = [{"slot": {"index": i, "label": "x"}, "count": 3} for i in range(5)]
    assert battle.weakest_hour(flat) is None
    dip = list(flat)
    dip[3] = {"slot": {"index": 3, "label": "15:00-16:00"}, "count": 1}
    assert battle.weakest_hour(dip)["slot"]["index"] == 3


def test_changing_only_the_battle_half_keeps_the_prep_answers(client):
    """
    The prep block wipes and rewrites a player's submissions and assignments.
    Someone who re-opens the link only to fix their hours must not lose the
    appointments they already have.
    """
    uid, _ = _create_combined(client)
    full = {
        "_mode": "both",
        "player_name": "YASENOVA", "player_id": "188755088", "alliance_name": "ICE",
        "tier_INF": "T11", "tier_CAV": "TG5", "tier_ARC": "T11",
        "hour_0": "yes", "hour_1": "yes", "hour_2": "yes", "hour_3": "no", "hour_4": "no",
        "notes": "",
        "speedups-construction": "500", "truegold": "10", "tempered_truegold": "2",
        "slots-construction": "[4, 5]",
    }
    client.post(f"/event/{uid}/submit", data=full, follow_redirects=False)

    from app import database
    with client.application.app_context():
        db = database.get_db()
        before = db.execute(
            "SELECT COUNT(*) FROM submissions WHERE event_uid = ?", (uid,)
        ).fetchone()[0]
    assert before == 1

    # Second visit: only the hours change, prep fields left empty.
    client.post(f"/event/{uid}/submit", data={
        "_mode": "both",
        "player_name": "YASENOVA", "player_id": "188755088", "alliance_name": "ICE",
        "tier_INF": "T11", "tier_CAV": "TG5", "tier_ARC": "T11",
        "hour_0": "no", "hour_1": "no", "hour_2": "yes", "hour_3": "yes", "hour_4": "yes",
        "notes": "",
    }, follow_redirects=False)

    with client.application.app_context():
        db = database.get_db()
        after = db.execute(
            "SELECT COUNT(*) FROM submissions WHERE event_uid = ?", (uid,)
        ).fetchone()[0]
        avail = db.execute(
            "SELECT availability FROM battle_submissions WHERE event_uid = ?", (uid,)
        ).fetchone()[0]
    assert after == 1, "prep answers were wiped by a battle-only resubmission"
    assert json.loads(avail) == [0, 0, 1, 1, 1], "battle hours were not updated"


def test_no_lead_gap_warning_when_no_lead_was_ever_marked():
    """
    Listing every hour as "no rally lead online" before anyone has been made
    a lead buries the real warning under noise.
    """
    cfg = battle.default_config()
    cfg["teams"] = [{"key": "A", "name": "Castle", "alliance": "", "min_tier": "TG3",
                     "required_hours": [], "size": None, "max_leads": None}]
    rows = [{"player_id": "1", "player_name": "Solo", "alliance_name": "ICE",
             "tiers": {"INF": "TG5", "CAV": "TG5", "ARC": "TG5"},
             "availability": [1, 1, 1, 1, 1], "notes": ""}]
    players = [battle.enrich(r, cfg) for r in rows]
    assert battle.assign_teams(players, cfg)["teams"][0]["lead_gaps"] == []
    with_lead = battle.assign_teams(players, cfg, {"1": {"role": "lead"}})
    assert with_lead["teams"][0]["lead_gaps"] == []


def test_no_below_target_warning_for_an_empty_team():
    cfg = battle.default_config()
    plan = battle.assign_teams([], cfg)
    for team in plan["teams"]:
        assert team["short_hours"] == []


def test_create_page_has_no_choices_that_are_not_choices(client):
    """
    The castle battle is always 12:00-17:00 UTC and the appointment grid is
    always 48 slots. Offering those as questions only invites a wrong answer.
    """
    page = client.get("/").get_data(as_text=True)
    assert 'value="prep"' not in page
    assert 'value="battle"' not in page
    assert 'name="event_type" value="both"' in page
    assert "Battle starts at" not in page
    assert "How many hours" not in page
    assert "Appointment Slots" not in page
    assert '49 Slots' not in page
    assert 'name="slot_count" value="48"' in page
    # the things that do vary are still asked
    assert 'name="battle_alliances"' in page
    assert 'name="research_day"' in page


def test_creating_from_the_page_gives_12_to_17_utc_and_48_slots(client):
    resp = client.post("/create", data={
        "event_name": "KvK Prep & Battle", "event_type": "both",
        "battle_start_hour": "12", "battle_hour_count": "5",
        "battle_alliances": "ICE, FYR, Meo",
        "research_day": "2", "slot_count": "48",
    }, follow_redirects=False)
    uid = resp.headers["Location"].split("/success/")[1].split("?")[0]
    from app import database
    with client.application.app_context():
        db = database.get_db()
        row = db.execute(
            "SELECT slot_count, battle_config FROM events WHERE uid = ?", (uid,)
        ).fetchone()
    assert row[0] == 48
    cfg = json.loads(row[1])
    assert cfg["start_hour"] == 12 and cfg["hour_count"] == 5
    assert cfg["alliances"] == ["ICE", "FYR", "Meo"]
