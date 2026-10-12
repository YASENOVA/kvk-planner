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


def test_only_two_roles_exist():
    """
    Rally lead and joiner. A third role for hand-over pairs sounded useful
    and turned out to be something leadership solves by moving people
    between teams instead.
    """
    cfg = battle.default_config()
    players = [battle.enrich(r, cfg) for r in _rows()]
    fieldable = [p for p in players if p["fieldable"]]
    plan = battle.assign_teams(fieldable, cfg, {"191069150": {"role": "relay"}})
    # an unknown role never smuggles someone past the required hours
    assert "Zeqo" not in [p["player_name"] for p in plan["teams"][0]["core"]]


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


# --------------------------------------------------------------------------
# TG6 and T11
# --------------------------------------------------------------------------


def test_tier_list_covers_tg6_and_t11_on_top():
    """
    The level 8 town centre brings TG6, and from TG5 up a player can field
    T11 as well. Asking for one tier per troop type would lose half of that.
    """
    cfg = battle.default_config()
    assert cfg["tiers"] == [
        "T11 TG8", "TG8", "T11 TG7", "TG7",
        "T11 TG6", "TG6", "T11 TG5", "TG5",
        "TG4", "TG3", "TG2", "TG1",
    ]
    score = lambda t: battle.tier_score(t, cfg)
    ladder = ["T11 TG8", "TG8", "T11 TG7", "TG7", "T11 TG6", "TG6",
              "T11 TG5", "TG5", "TG4", "TG3", "TG2", "TG1"]
    assert all(score(a) > score(b) for a, b in zip(ladder, ladder[1:]))


def test_old_events_are_lifted_to_the_new_tier_list():
    for old in (battle.LEGACY_TIERS, battle.TG6_TIERS):
        stored = json.dumps({"tiers": list(old)})
        assert battle.parse_config(stored)["tiers"] == battle.DEFAULT_TIERS


def test_old_answers_still_rank():
    """A stored "T11" predates TG6 and must not fall to the bottom."""
    cfg = battle.default_config()
    assert battle.tier_score("T11", cfg) == battle.tier_score("T11 TG5", cfg)


def test_legacy_tier_values_are_still_accepted_and_kept_as_sent():
    cfg = battle.default_config()
    parsed = battle.parse_submission({
        "player_name": "Old Link", "player_id": "188755088", "alliance_name": "ICE",
        "tier_INF": "T11", "tier_CAV": "TG5", "tier_ARC": "TG5",
        "hour_0": "yes", "hour_1": "no", "hour_2": "no", "hour_3": "no", "hour_4": "no",
    }, cfg)
    assert parsed["tiers"]["INF"] == "T11"


def test_tg6_counts_as_full_tg5():
    cfg = battle.default_config()
    assert battle.full_tier({"INF": "TG6", "CAV": "T11 TG6", "ARC": "TG5"}, cfg, "TG5")
    assert not battle.full_tier({"INF": "T11 TG6", "CAV": "TG4", "ARC": "TG6"}, cfg, "TG5")


# --------------------------------------------------------------------------
# Fixing bad records
# --------------------------------------------------------------------------


def _submit(client, uid, name, pid, **extra):
    data = {
        "_mode": "battle", "player_name": name, "player_id": pid,
        "alliance_name": "ICE", "tier_INF": "TG5", "tier_CAV": "TG5", "tier_ARC": "TG5",
        "hour_0": "yes", "hour_1": "yes", "hour_2": "yes", "hour_3": "no", "hour_4": "no",
        "notes": "",
    }
    data.update(extra)
    return client.post(f"/event/{uid}/submit", data=data, follow_redirects=True)


def test_role_dropdown_only_offers_lead_and_joiner(client):
    uid, secret = _create_combined(client)
    _submit(client, uid, "YASENOVA", "188755088")
    body = client.get(f"/admin/{uid}/battle?secret={secret}").get_data(as_text=True)
    assert 'value="lead"' in body and 'value="joiner"' in body
    assert 'value="relay"' not in body
    ok = client.post(f"/admin/{uid}/battle/player", data={
        "secret": secret, "player_id": "188755088", "field": "role", "value": "lead"})
    assert ok.status_code == 302


def test_a_player_can_be_deleted(client):
    uid, secret = _create_combined(client)
    _submit(client, uid, "Typo", "999999999")
    client.post(f"/admin/{uid}/battle/remove",
                data={"secret": secret, "player_id": "999999999"})
    from app import database
    with client.application.app_context():
        db = database.get_db()
        assert db.execute(
            "SELECT COUNT(*) FROM battle_submissions WHERE event_uid = ?", (uid,)
        ).fetchone()[0] == 0


def test_a_mistyped_id_can_be_corrected_without_losing_the_flags(client):
    uid, secret = _create_combined(client)
    _submit(client, uid, "Yasenovaa", "188755080")
    client.post(f"/admin/{uid}/battle/player", data={
        "secret": secret, "player_id": "188755080", "field": "role", "value": "lead"})
    client.post(f"/admin/{uid}/battle/rename", data={
        "secret": secret, "player_id": "188755080",
        "new_name": "YASENOVA", "new_player_id": "188755088"})
    from app import database
    with client.application.app_context():
        db = database.get_db()
        row = db.execute(
            "SELECT player_name, player_id FROM battle_submissions WHERE event_uid = ?",
            (uid,)).fetchone()
        role = db.execute(
            "SELECT role FROM battle_player_admin WHERE event_uid = ? AND player_id = ?",
            (uid, "188755088")).fetchone()
    assert row[0] == "YASENOVA" and row[1] == "188755088"
    assert role is not None and role[0] == "lead", "the rally lead mark was lost"


def test_rename_refuses_to_merge_two_records(client):
    uid, secret = _create_combined(client)
    _submit(client, uid, "One", "111111111")
    _submit(client, uid, "Two", "222222222")
    client.post(f"/admin/{uid}/battle/rename", data={
        "secret": secret, "player_id": "111111111", "new_player_id": "222222222"})
    from app import database
    with client.application.app_context():
        db = database.get_db()
        assert db.execute(
            "SELECT COUNT(*) FROM battle_submissions WHERE event_uid = ?", (uid,)
        ).fetchone()[0] == 2


def test_reward_amount_can_be_set_by_hand(client):
    """
    A rally lead who cannot make the opening hour still earns their share.
    No rule phrased in hours will ever work that out, so leadership types it.
    """
    uid, secret = _create_combined(client)
    # Online only in the last two hours, so the rules give them nothing.
    _submit(client, uid, "LateLead", "188755088",
            hour_0="no", hour_1="no", hour_2="no", hour_3="yes", hour_4="yes")
    body = client.get(f"/admin/{uid}/battle?secret={secret}").get_data(as_text=True)
    assert "LateLead" in body

    client.post(f"/admin/{uid}/battle/force", data={
        "secret": secret, "player_id": "188755088",
        "reward_key": "deployment", "forced": "2"})

    from app import database
    with client.application.app_context():
        db = database.get_db()
        cfg = battle.parse_config(db.execute(
            "SELECT battle_config FROM events WHERE uid = ?", (uid,)).fetchone()[0])
        rows = db.execute(
            "SELECT * FROM battle_submissions WHERE event_uid = ?", (uid,)).fetchall()
    players = [battle.enrich({
        "player_id": r[2], "player_name": r[3], "alliance_name": r[4],
        "tiers": json.loads(r[5]), "availability": json.loads(r[6]), "notes": r[7],
    }, cfg) for r in rows]
    out = battle.allocate_reward(players, cfg["rewards"][0], cfg, None,
                                 {"188755088": 2})
    mine = [g for g in out["grants"] if g["player"]["player_id"] == "188755088"]
    assert mine and mine[0]["amount"] == 2 and mine[0]["forced"] is True


def test_clearing_a_forced_amount_returns_to_automatic():
    cfg = battle.default_config()
    players = [battle.enrich({
        "player_id": "1", "player_name": "A", "alliance_name": "ICE",
        "tiers": {"INF": "TG5", "CAV": "TG5", "ARC": "TG5"},
        "availability": [1, 1, 1, 1, 1], "notes": "",
    }, cfg)]
    rule = cfg["rewards"][0]
    forced = battle.allocate_reward(players, rule, cfg, None, {"1": 7})
    assert forced["grants"][0]["amount"] == 7
    auto = battle.allocate_reward(players, rule, cfg, None, {})
    assert auto["grants"][0]["amount"] == rule["max_per"]


# --------------------------------------------------------------------------
# Look and feel
# --------------------------------------------------------------------------


def test_every_page_carries_the_same_palette(client):
    """
    One accent colour, defined in one place. A page that hardcodes the old
    greys or the old gold has drifted away from the rest of the tool.
    """
    uid, secret = _create_combined(client)
    pages = [
        "/", f"/event/{uid}", f"/event/{uid}/lineup",
        f"/success/{uid}?secret={secret}",
        f"/admin/{uid}?secret={secret}",
        f"/admin/{uid}/prep?secret={secret}",
        f"/admin/{uid}/battle?secret={secret}",
        f"/event/{uid}/finalized",
    ]
    for url in pages:
        resp = client.get(url)
        assert resp.status_code == 200, url
        body = resp.get_data(as_text=True)
        for stale in ("#FBBF24", "#1F2937", "#111827", "#374151"):
            assert stale not in body, "%s still carries %s" % (url, stale)
        assert "#00c8ff" in body, "%s has no accent colour" % url


def test_the_tier_grid_is_one_object_not_three_long_lists(client):
    uid, _ = _create_combined(client)
    page = client.get(f"/event/{uid}").get_data(as_text=True)
    assert page.count('class="tg-row"') == 3
    # eight tempered gold levels plus "none" per troop type
    assert page.count('name="tier_INF"') == 9
    assert page.count('name="t11_INF"') == 1
    # and the T11 box knows which tiers it is allowed on
    assert '["TG5", "TG6", "TG7", "TG8"]' in page


def test_tier_badges_survive_a_space_in_the_tier_name(client):
    """
    "T11 TG8" in a class attribute became two class names and half of them
    matched nothing, so the dashboard showed the wrong colours.
    """
    uid, secret = _create_combined(client)
    _submit(client, uid, "YASENOVA", "188755088",
            tier_INF="TG8", t11_INF="yes", tier_CAV="TG8", t11_CAV="yes",
            tier_ARC="TG8", t11_ARC="yes")
    body = client.get(f"/admin/{uid}/battle?secret={secret}").get_data(as_text=True)
    assert "tier-T11-TG8" in body
    assert 'tier-T11 TG8' not in body
    # and the ladder actually defines every rung it can render
    for tier in battle.DEFAULT_TIERS:
        assert ".tier-%s " % tier.replace(" ", "-") in body or \
               ".tier-%s{" % tier.replace(" ", "-") in body, tier


def test_no_explanation_lines_are_left_under_the_headings(client):
    """
    Either every heading carries a sentence or none does. None does.
    """
    uid, secret = _create_combined(client)
    gone = [
        "Send it once",
        "It counts for both halves below",
        "times you could take a King's Buff",
        "whether you also field T11",
        "can only be ticked",
        "be at your device and act",
        "updates an existing entry",
        'count as "no"',
        "alternate name first",
        "blank = automatic",
    ]
    for url in (f"/event/{uid}", f"/admin/{uid}/battle?secret={secret}"):
        body = client.get(url).get_data(as_text=True)
        for phrase in gone:
            assert phrase not in body, "%s still says: %s" % (url, phrase)
    assert "Select every hour you can play." in client.get(
        f"/event/{uid}").get_data(as_text=True)


def test_the_player_form_markup_stays_balanced(client):
    """
    Cutting an explanation line out of a nested block is how a stray </div>
    goes missing and the page quietly collapses below the fold.
    """
    uid, _ = _create_combined(client)
    page = client.get(f"/event/{uid}").get_data(as_text=True)
    assert page.count("<div") == page.count("</div>")
    assert page.count("<section") == page.count("</section>")
    assert page.count("<form") == page.count("</form>") == 1


def test_section_headings_are_consistent(client):
    """
    Either every heading carries one line under it or none does. It is one
    line under every one of them, and they all look the same.
    """
    uid, _ = _create_combined(client)
    page = client.get(f"/event/{uid}").get_data(as_text=True)
    assert page.count('class="sect"') == 2          # prep week, battle day
    assert page.count('class="sect-sub"') == 2      # each with its line
    import re
    subs = re.findall(r'class="subh[^"]*">([^<]+)<', page)
    titles = [t for t in subs if not t.endswith(".")]
    assert "Your information" in titles
    assert "Troop tier" in titles
    assert "Availability" in titles
    assert "Notes" in titles
    assert "Day 1: Construction" in titles
    # every sub-heading is followed by its own line of explanation
    assert page.count('class="subh-sub') >= len(titles) - 3


def test_the_form_head_says_what_this_is(client):
    uid, _ = _create_combined(client)
    page = client.get(f"/event/{uid}").get_data(as_text=True)
    assert 'class="page-title"' in page
    assert "KvK prep week" in page
    # the appointments are castle appointments, not the buff itself
    assert "King's Buff" not in page
    assert "castle appointment" in page


def test_sub_headings_carry_no_decoration(client):
    """The little vertical ticks were one marker too many on the page."""
    uid, _ = _create_combined(client)
    page = client.get(f"/event/{uid}").get_data(as_text=True)
    assert ".subh::before" not in page


def test_both_timezone_toggles_are_the_same_control(client):
    """
    Two toggles doing the same job looked like two different controls. They
    share one set of classes now, so they cannot drift apart again.
    """
    uid, _ = _create_combined(client)
    page = client.get(f"/event/{uid}").get_data(as_text=True)
    assert page.count('class="tzbar"') == 2
    assert page.count('class="tzlabel"') == 2
    assert page.count('class="tzgroup"') == 2
    assert page.count('class="tzb on"') == 2      # both start on UTC
    assert "tzbtn" not in page                     # the old one-off style is gone


def test_the_head_warns_that_a_resend_replaces_everything(client):
    """
    Saying you can "change your answers" invites a half-filled resubmit, and
    a resubmit replaces the lot.
    """
    uid, _ = _create_combined(client)
    page = client.get(f"/event/{uid}").get_data(as_text=True)
    assert "replaces your last answer" in page
    assert "change your answers" not in page


def test_the_stylesheets_parse(client):
    """
    A half-deleted rule leaves a dangling line, and every rule after it is
    thrown away by the browser. That is how the screen-reader labels became
    visible: the rule that hides them sat below the damage.
    """
    import glob, os, re
    here = os.path.join(os.path.dirname(__file__), "..", "app", "templates")
    for path in glob.glob(os.path.join(here, "_*styles.html")) + \
                [os.path.join(here, "_theme.html")]:
        css = open(path).read()
        assert css.count("{") == css.count("}"), os.path.basename(path)

    uid, _ = _create_combined(client)
    page = client.get(f"/event/{uid}").get_data(as_text=True)
    style = page[page.index("<style"):page.rindex("</style>")]
    assert style.count("{") == style.count("}")
    # and the rule that hides the labels really is in there
    assert ".sr {" in page or ".sr{" in page


# --------------------------------------------------------------------------
# Editing a whole record, and the filter that has to keep working
# --------------------------------------------------------------------------


def _edit(client, uid, secret, old_id, **over):
    data = {
        "secret": secret, "old_player_id": old_id,
        "player_name": "YASENOVA", "player_id": old_id, "alliance_name": "ICE",
        "tier_INF": "TG5", "tier_CAV": "TG5", "tier_ARC": "TG5",
        "hour_0": "yes", "hour_1": "yes", "hour_2": "yes",
        "hour_3": "no", "hour_4": "no", "notes": "",
    }
    data.update(over)
    return client.post(f"/admin/{uid}/battle/edit", data=data)


def _stored(client, uid, player_id=None):
    from app import database
    with client.application.app_context():
        db = database.get_db()
        if player_id:
            return db.execute(
                "SELECT player_name, player_id, tiers, availability, notes, source "
                "FROM battle_submissions WHERE event_uid = ? AND player_id = ?",
                (uid, player_id)).fetchone()
        return db.execute(
            "SELECT player_name, player_id, tiers, availability, notes, source "
            "FROM battle_submissions WHERE event_uid = ?", (uid,)).fetchone()


def test_the_players_table_has_an_id_of_its_own(client):
    """
    The section and the table both answered to "players" once. getElementById
    then returned the section, whose .tBodies is undefined, and the script
    threw on load - which killed the alliance filter and the column sorting
    without a single visible symptom.
    """
    uid, secret = _create_combined(client)
    _submit(client, uid, "YASENOVA", "188755088")
    body = client.get(f"/admin/{uid}/battle?secret={secret}").get_data(as_text=True)
    assert body.count('id="players"') == 1, "two elements share the id again"
    assert 'id="players-table"' in body
    assert "getElementById('players-table')" in body


def test_filter_controls_are_rendered_for_every_alliance(client):
    uid, secret = _create_combined(client)
    _submit(client, uid, "YASENOVA", "188755088", alliance_name="ICE")
    _submit(client, uid, "Captain77", "186601440", alliance_name="Meo")
    body = client.get(f"/admin/{uid}/battle?secret={secret}").get_data(as_text=True)
    assert '<option value="ICE">ICE</option>' in body
    assert '<option value="Meo">Meo</option>' in body


def test_troop_tier_can_be_corrected(client):
    uid, secret = _create_combined(client)
    _submit(client, uid, "YASENOVA", "188755088")
    _edit(client, uid, secret, "188755088", tier_INF="T11 TG6", tier_ARC="TG6")
    tiers = json.loads(_stored(client, uid)[2])
    assert tiers["INF"] == "T11 TG6"
    assert tiers["ARC"] == "TG6"
    assert tiers["CAV"] == "TG5", "the untouched troop type changed too"


def test_availability_can_be_corrected(client):
    uid, secret = _create_combined(client)
    _submit(client, uid, "YASENOVA", "188755088")
    assert json.loads(_stored(client, uid)[3]) == [1, 1, 1, 0, 0]
    _edit(client, uid, secret, "188755088", hour_0="no", hour_4="yes")
    assert json.loads(_stored(client, uid)[3]) == [0, 1, 1, 0, 1]


def test_editing_keeps_the_answer_marked_as_the_players_own(client):
    """
    Leadership correcting a tier must not turn a real answer into an admin
    entry - the manual-entry card deletes those, and it would then be able
    to delete somebody's actual submission.
    """
    uid, secret = _create_combined(client)
    _submit(client, uid, "YASENOVA", "188755088")
    assert _stored(client, uid)[5] == "player"
    _edit(client, uid, secret, "188755088", tier_CAV="TG6")
    assert _stored(client, uid)[5] == "player"


def test_edit_carries_flags_and_rewards_to_a_corrected_id(client):
    uid, secret = _create_combined(client)
    _submit(client, uid, "Yasenovaa", "188755080")
    client.post(f"/admin/{uid}/battle/player", data={
        "secret": secret, "player_id": "188755080", "field": "role", "value": "lead"})
    _edit(client, uid, secret, "188755080",
          player_id="188755088", player_name="YASENOVA", tier_INF="TG6")
    from app import database
    with client.application.app_context():
        db = database.get_db()
        role = db.execute(
            "SELECT role FROM battle_player_admin WHERE event_uid = ? AND player_id = ?",
            (uid, "188755088")).fetchone()
    row = _stored(client, uid, "188755088")
    assert row is not None and row[0] == "YASENOVA"
    assert json.loads(row[2])["INF"] == "TG6"
    assert role is not None and role[0] == "lead", "the rally lead mark was lost"
    assert _stored(client, uid, "188755080") is None


def test_edit_refuses_to_merge_two_records(client):
    uid, secret = _create_combined(client)
    _submit(client, uid, "One", "111111111")
    _submit(client, uid, "Two", "222222222")
    _edit(client, uid, secret, "111111111", player_id="222222222", player_name="One")
    assert _stored(client, uid, "111111111")[0] == "One"
    assert _stored(client, uid, "222222222")[0] == "Two"


def test_edit_rejects_a_bad_tier_without_touching_the_record(client):
    uid, secret = _create_combined(client)
    _submit(client, uid, "YASENOVA", "188755088")
    _edit(client, uid, secret, "188755088", tier_INF="TG99")
    assert json.loads(_stored(client, uid)[2])["INF"] == "TG5"


def test_edit_needs_the_admin_secret(client):
    uid, secret = _create_combined(client)
    _submit(client, uid, "YASENOVA", "188755088")
    r = _edit(client, uid, "wrong-secret", "188755088", player_name="Hacked")
    assert r.status_code == 403
    assert _stored(client, uid)[0] == "YASENOVA"


def test_every_player_row_gets_an_editor_row(client):
    uid, secret = _create_combined(client)
    _submit(client, uid, "YASENOVA", "188755088")
    _submit(client, uid, "Captain77", "186601440", alliance_name="Meo")
    body = client.get(f"/admin/{uid}/battle?secret={secret}").get_data(as_text=True)
    assert body.count('class="editrow') == 2
    assert body.count('name="old_player_id"') == 2
    # The editor row is driven by inline display, same as the filter, so the
    # two cannot fight over whether a row is visible.
    assert 'class="editrow bg-kvk-gray-900" style="display:none"' in body


def test_the_upstream_links_are_gone(client):
    uid, secret = _create_combined(client)
    for url in ("/", f"/admin/{uid}/battle?secret={secret}"):
        body = client.get(url).get_data(as_text=True)
        assert "github.com/prashmohan" not in body
        assert "User Guide & Tutorial" not in body


def test_the_footer_links_to_this_repository_in_the_accent_colour(client):
    """
    The icon carries no fill of its own - .ghlink colours it, so it follows
    the palette instead of needing a second edit when the accent changes.
    """
    body = client.get("/").get_data(as_text=True)
    assert 'href="https://github.com/YASENOVA/kvk-planner"' in body
    assert 'class="ghlink"' in body
    assert "text-gray-500" not in body.split("<footer")[1]
    assert ".ghlink{color:var(--neon)" in body
    assert ".ghlink svg{fill:currentColor}" in body
