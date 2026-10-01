import csv
import hmac
import io
import json
import logging
import mimetypes
import os
import secrets
import sqlite3
import time
from logging.handlers import RotatingFileHandler

import markdown
from flask import (
    Flask,
    Response,
    flash,
    redirect,
    render_template,
    request,
    send_from_directory,
    session,
    url_for,
)
from flask_wtf.csrf import CSRFProtect
from werkzeug.utils import secure_filename

from config import Config

from . import battle, database, logic
from .logic import (
    compute_event_insights,
    format_minutes,
    generate_short_uid,
    get_ordered_active_days,
    get_superadmin_metrics,
    validate_custom_slug,
)

# Ensure .js files are served with the correct MIME type
mimetypes.add_type("application/javascript", ".js")


def validate_safe_url(url: str | None) -> str | None:
    """Ensure URL uses only safe HTTP(S) or static relative schemes."""
    if not url or not isinstance(url, str):
        return None
    cleaned = url.strip()
    if cleaned.startswith(("http://", "https://", "/static/")):
        return cleaned
    return None


def generate_slot_labels(slot_count=49):
    labels = []
    for i in range(slot_count):
        if slot_count == 48:
            start_total_minutes = i * 30
        else:
            start_total_minutes = (i * 30) - 15

        if start_total_minutes < 0:
            start_total_minutes += 24 * 60

        start_hour = start_total_minutes // 60
        start_min = start_total_minutes % 60

        end_total_minutes = start_total_minutes + 30
        end_hour = (end_total_minutes // 60) % 24
        end_min = end_total_minutes % 60

        labels.append(
            f"{start_hour:02d}:{start_min:02d}-\u200b{end_hour:02d}:{end_min:02d}"
        )
    return labels


def create_app():
    app = Flask(__name__)
    app.config.from_object(Config)
    CSRFProtect(app)
    database.init_app(app)

    # Setup Audit Logging
    log_dir = os.path.join(app.root_path, "..", "logs")
    os.makedirs(log_dir, exist_ok=True)

    audit_handler = RotatingFileHandler(
        os.path.join(log_dir, "audit.log"), maxBytes=1000000, backupCount=5
    )
    audit_handler.setFormatter(
        logging.Formatter("%(asctime)s - %(levelname)s - %(message)s")
    )
    audit_logger = logging.getLogger("audit")
    audit_logger.setLevel(logging.INFO)
    audit_logger.addHandler(audit_handler)
    app.audit_logger = audit_logger

    # Make the label generator available to all templates
    @app.context_processor
    def inject_global_config():
        slot_count = 49
        try:
            event_uid = (
                request.view_args.get("event_uid") if request.view_args else None
            )
            if event_uid:
                db = database.get_db()
                row = db.execute(
                    "SELECT slot_count FROM events WHERE uid = ?", (event_uid,)
                ).fetchone()
                if row and row[0] is not None:
                    slot_count = row[0]
        except (RuntimeError, Exception):  # noqa: BLE001, S110
            pass

        return {
            "slot_labels": generate_slot_labels(slot_count),
            "enable_screenshot_upload": Config.ENABLE_SCREENSHOT_UPLOAD,
            "ga_measurement_id": Config.GA_MEASUREMENT_ID,
        }

    @app.after_request
    def add_security_headers(response):
        response.headers["X-Frame-Options"] = "DENY"
        response.headers["X-Content-Type-Options"] = "nosniff"
        response.headers["Referrer-Policy"] = "no-referrer"
        response.headers["Strict-Transport-Security"] = (
            "max-age=31536000; includeSubDomains"
        )
        csp = (
            "default-src 'self'; "
            "script-src 'self' 'unsafe-inline' https://www.googletagmanager.com; "
            "style-src 'self' 'unsafe-inline'; "
            "img-src 'self' data: https:; "
            "connect-src 'self' https://www.google-analytics.com;"
        )
        response.headers["Content-Security-Policy"] = csp
        return response

    @app.route("/")
    def index():
        return render_template("index.html")

    @app.route("/guide")
    def guide():
        try:
            with open("README.md", "r", encoding="utf-8") as f:
                lines = f.readlines()

            # Filter out technical badges for the in-app guide
            filtered_lines = [
                line for line in lines if not line.strip().startswith("[![")
            ]
            content = "".join(filtered_lines)

            # Replace local file paths with web-accessible static paths for the in-app guide
            content = content.replace("app/static/images/", "/static/images/")
            html_content = markdown.markdown(
                content, extensions=["extra", "toc", "fenced_code"]
            )
            return render_template("guide.html", content=html_content)
        except FileNotFoundError:
            return "Guide not found", 404

    @app.route("/favicon.ico")
    def favicon():
        return send_from_directory(
            os.path.join(app.root_path, "static"),
            "favicon.svg",
            mimetype="image/svg+xml",
        )

    @app.route("/create", methods=["POST"])
    def create_event():
        event_name = request.form.get("event_name", "").strip()
        if not event_name:
            event_name = "Untitled Event"

        research_day = request.form.get("research_day", "5")
        try:
            slot_count = int(request.form.get("slot_count", "49"))
            if slot_count not in [48, 49]:
                slot_count = 49
        except ValueError:
            slot_count = 49

        db = database.get_db()

        # Handle custom slug or auto-generated short UID
        custom_slug = request.form.get("custom_slug", "").strip()
        if custom_slug:
            is_valid, err_msg = validate_custom_slug(custom_slug, db)
            if not is_valid:
                return err_msg, 400
            uid = custom_slug
        else:
            # Generate unique short UID with collision check
            while True:
                candidate_uid = generate_short_uid(8)
                exists = db.execute(
                    "SELECT 1 FROM events WHERE uid = ?", (candidate_uid,)
                ).fetchone()
                if not exists:
                    uid = candidate_uid
                    break

        admin_secret = secrets.token_urlsafe(16)

        # Battle day events collect availability and troop tiers instead of
        # King's Buff appointments. Everything below stays as-is so a prep
        # event behaves exactly like it did before.
        # An absent value means an old caller or a script, not the create page -
        # the form always sends one. Defaulting those to prep keeps every
        # existing event and integration behaving exactly as before.
        event_type = request.form.get("event_type", "prep").strip().lower()
        if event_type not in ("prep", "battle", "both"):
            event_type = "prep"
        battle_config = (
            json.dumps(battle.build_config(request.form))
            if event_type in ("battle", "both")
            else None
        )

        active_days = {
            "construction": True,
            "training": True,
            "research": True,
            "research_day": int(research_day),
        }

        raw_server_id = request.form.get("server_id", "").strip()
        server_id = None
        if raw_server_id:
            try:
                parsed_id = int(raw_server_id)
                if parsed_id > 0:
                    server_id = parsed_id
            except ValueError:
                server_id = None

        db.execute(
            "INSERT INTO events (uid, name, active_days, admin_secret, slot_count, "
            "server_id, event_type, battle_config) VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
            (
                uid,
                event_name,
                json.dumps(active_days),
                admin_secret,
                slot_count,
                server_id,
                event_type,
                battle_config,
            ),
        )
        db.commit()

        return redirect(url_for("success", event_uid=uid, secret=admin_secret))

    @app.route("/success/<event_uid>")
    def success(event_uid):
        secret = request.args.get("secret")
        event = _event_row(event_uid)
        kind = _event_type(event) if event is not None else "prep"

        def link(endpoint, **kw):
            return url_for(endpoint, event_uid=event_uid, _external=True, **kw)

        links = []
        if kind == "both":
            links = [
                ("Player link",
                 "Give this to everyone. Prep week and battle day sit behind it.",
                 link("player_form"), False),
                ("Prep planning",
                 "Where you distribute the King's Buff appointments.",
                 link("admin_prep", secret=secret), True),
                ("Prep schedule",
                 "The confirmed appointments, readable without the key.",
                 link("locked_appointments"), False),
                ("Battle planning",
                 "Teams, rewards and hour coverage for the castle battle.",
                 link("admin_battle", secret=secret), True),
                ("Battle line-up",
                 "The published teams, readable without the key. Closed until you publish it.",
                 link("battle_lineup"), False),
            ]
        elif kind == "battle":
            links = [
                ("Player link", "Give this to everyone.", link("player_form"), False),
                ("Battle planning", "Teams, rewards and hour coverage.",
                 link("admin_dashboard", secret=secret), True),
                ("Battle line-up", "Published teams, readable without the key.",
                 link("battle_lineup"), False),
            ]
        else:
            links = [
                ("Player link", "Give this to everyone.", link("player_form"), False),
                ("Prep schedule", "The confirmed appointments.",
                 link("locked_appointments"), False),
                ("Prep planning", "Where you distribute the appointments.",
                 link("admin_dashboard", secret=secret), True),
            ]

        return render_template("success.html", links=links, kind=kind)

    @app.route("/event/<event_uid>/finalized")
    def locked_appointments(event_uid):
        db = database.get_db()
        db.row_factory = sqlite3.Row
        event = db.execute(
            "SELECT * FROM events WHERE uid = ?", (event_uid,)
        ).fetchone()

        if event is None:
            return "Event not found", 404

        active_days_config = json.loads(event["active_days"])
        active_days = get_ordered_active_days(active_days_config)

        # Create a dictionary from the database row for the template
        event_dict = {
            "uid": event["uid"],
            "name": event["name"],
            "active_days": active_days_config,
            "server_id": event["server_id"],
        }

        # Fetch all assignments
        assignments_raw = db.execute(
            "SELECT * FROM assignments WHERE event_uid = ?",
            (event_uid,),
        ).fetchall()

        # Fetch submissions to get player/alliance names
        submissions_raw = db.execute(
            "SELECT * FROM submissions WHERE event_uid = ?", (event_uid,)
        ).fetchall()
        submissions_map = {
            (sub["day_type"], sub["player_id"]): sub for sub in submissions_raw
        }

        # Group rich assignments by day_type
        all_assignments = {day: {} for day in active_days}
        for a in assignments_raw:
            day_type = a["day_type"]
            player_id = a["player_id"]
            if day_type in all_assignments:
                submission = submissions_map.get((day_type, player_id))
                if submission:
                    all_assignments[day_type][a["slot_index"]] = {
                        "player_id": player_id,
                        "player_name": submission["player_name"],
                        "alliance_name": submission["alliance_name"],
                        "avatar_url": submission["avatar_url"],
                        "is_locked": bool(a["is_locked"]),
                    }

        return render_template(
            "locked_appointments.html",
            event=event_dict,
            active_days=active_days,
            assignments=all_assignments,
        )

    def _event_row(event_uid):
        """Fetch an event as a dict-like row, or None."""
        db = database.get_db()
        db.row_factory = sqlite3.Row
        return db.execute(
            "SELECT * FROM events WHERE uid = ?", (event_uid,)
        ).fetchone()

    def _event_type(event):
        """Older rows predate the column, so treat a missing value as prep."""
        try:
            value = (event["event_type"] or "prep").strip().lower()
        except (IndexError, KeyError):
            return "prep"
        return value if value in ("prep", "battle", "both") else "prep"

    def _has_prep(event):
        return _event_type(event) in ("prep", "both")

    def _has_battle(event):
        return _event_type(event) in ("battle", "both")

    def _is_battle(event):
        """True only when the whole event is battle-day; 'both' is not."""
        return _event_type(event) == "battle"

    def _battle_cfg(event):
        try:
            raw = event["battle_config"]
        except (IndexError, KeyError):
            raw = None
        return battle.parse_config(raw)

    def _battle_event_dict(event, cfg):
        return {
            "uid": event["uid"],
            "name": event["name"],
            "server_id": event["server_id"],
            "cfg": cfg,
            "slots": battle.hour_slots(cfg),
        }

    def _battle_rows(db, event_uid):
        """Stored submissions with their JSON columns decoded."""
        rows = []
        for row in db.execute(
            "SELECT * FROM battle_submissions WHERE event_uid = ?", (event_uid,)
        ).fetchall():
            item = dict(row)
            for field, fallback in (("tiers", {}), ("availability", [])):
                try:
                    item[field] = json.loads(row[field])
                except (ValueError, TypeError):
                    item[field] = fallback
            rows.append(item)
        return rows

    def _battle_overrides(db, event_uid):
        out = {}
        for row in db.execute(
            "SELECT * FROM battle_player_admin WHERE event_uid = ?", (event_uid,)
        ).fetchall():
            try:
                role = row["role"] or ""
            except (IndexError, KeyError):
                role = "lead" if row["is_lead"] else ""
            out[row["player_id"]] = {
                "role": role,
                "is_lead": role == "lead",
                "team": row["team_key"] or "",
                "locked": bool(row["locked"]),
                "excluded": bool(row["excluded"]),
            }
        return out

    def _battle_grants(db, event_uid):
        out = {}
        for row in db.execute(
            "SELECT * FROM battle_reward_grants WHERE event_uid = ?", (event_uid,)
        ).fetchall():
            out.setdefault(row["reward_key"], {})[row["player_id"]] = row["delivered"]
        return out

    def _battle_forced(db, event_uid):
        """Amounts leadership typed in by hand, per reward and player."""
        out = {}
        for row in db.execute(
            "SELECT * FROM battle_reward_grants WHERE event_uid = ?", (event_uid,)
        ).fetchall():
            try:
                forced = row["forced"]
            except (IndexError, KeyError):
                forced = None
            if forced is not None:
                out.setdefault(row["reward_key"], {})[row["player_id"]] = forced
        return out

    def _battle_admin_guard(event_uid):
        """Returns (event, cfg, secret) or (None, None, None) when not allowed."""
        db = database.get_db()
        db.row_factory = sqlite3.Row
        secret = request.form.get("secret") or request.args.get("secret")
        event = db.execute(
            "SELECT * FROM events WHERE uid = ?", (event_uid,)
        ).fetchone()
        if event is None or not _has_battle(event):
            return None, None, None
        if not secret or not hmac.compare_digest(event["admin_secret"], secret):
            return None, None, None
        return event, _battle_cfg(event), secret

    @app.route("/admin/<event_uid>/battle/player", methods=["POST"])
    def battle_set_player(event_uid):
        """Mark a rally lead, pin someone to a team, or take them out."""
        event, _cfg, secret = _battle_admin_guard(event_uid)
        if event is None:
            return "Forbidden", 403

        player_id = battle.clean_gamer_id(request.form.get("player_id"))
        if not player_id:
            return "Bad request", 400

        field = request.form.get("field", "")
        if field not in ("role", "team_key", "locked", "excluded"):
            return "Bad request", 400

        raw = request.form.get("value", "").strip()
        if field == "role":
            if raw not in ("", "lead", "joiner"):
                return "Bad request", 400
            value = raw
        elif field == "team_key":
            value = raw
        else:
            value = 1 if raw == "1" else 0

        db = database.get_db()
        db.execute(
            "INSERT INTO battle_player_admin (event_uid, player_id) VALUES (?, ?) "
            "ON CONFLICT(event_uid, player_id) DO NOTHING",
            (event_uid, player_id),
        )
        db.execute(
            "UPDATE battle_player_admin SET %s = ? "
            "WHERE event_uid = ? AND player_id = ?" % field,
            (value, event_uid, player_id),
        )
        if field == "role":
            # is_lead still drives the "keep leads out of this team" rule.
            db.execute(
                "UPDATE battle_player_admin SET is_lead = ? "
                "WHERE event_uid = ? AND player_id = ?",
                (1 if value == "lead" else 0, event_uid, player_id),
            )
        db.commit()
        return redirect(url_for("admin_battle", event_uid=event_uid, secret=secret))

    @app.route("/admin/<event_uid>/battle/manual", methods=["POST"])
    def battle_manual_player(event_uid):
        """
        Add or edit a player by hand.

        Rally leads and senior officers routinely never fill the form, but
        they still have to show up in the teams and the reward lists.
        """
        event, cfg, secret = _battle_admin_guard(event_uid)
        if event is None:
            return "Forbidden", 403

        back = url_for("admin_battle", event_uid=event_uid, secret=secret)

        if request.form.get("action") == "delete":
            player_id = battle.clean_gamer_id(request.form.get("player_id"))
            if player_id:
                db = database.get_db()
                # Only rows leadership created themselves may be deleted here;
                # a real player's answer is theirs to change, not ours.
                db.execute(
                    "DELETE FROM battle_submissions "
                    "WHERE event_uid = ? AND player_id = ? AND source = 'admin'",
                    (event_uid, player_id),
                )
                db.commit()
            return redirect(back + "#manual")

        try:
            parsed = battle.parse_submission(request.form, cfg)
        except battle.SubmissionError as err:
            flash(str(err))
            return redirect(back + "#manual")

        db = database.get_db()
        db.execute(
            """
            INSERT INTO battle_submissions
                (id, event_uid, player_id, player_name, alliance_name,
                 tiers, availability, notes, source)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, 'admin')
            ON CONFLICT(event_uid, player_id) DO UPDATE SET
                player_name = excluded.player_name,
                alliance_name = excluded.alliance_name,
                tiers = excluded.tiers,
                availability = excluded.availability,
                notes = excluded.notes,
                timestamp = CURRENT_TIMESTAMP
            """,
            (
                secrets.token_urlsafe(12),
                event_uid,
                parsed["player_id"],
                parsed["player_name"],
                parsed["alliance_name"],
                json.dumps(parsed["tiers"]),
                json.dumps(parsed["availability"]),
                parsed["notes"],
            ),
        )
        db.commit()
        audit_logger.info(
            "BATTLE MANUAL ENTRY: %s (%s) for event %s",
            parsed["player_name"],
            parsed["player_id"],
            event_uid,
        )
        return redirect(back + "#manual")

    @app.route("/admin/<event_uid>/battle/remove", methods=["POST"])
    def battle_remove_player(event_uid):
        """
        Delete a submission outright.

        People mistype their Gamer ID often enough that leadership needs a
        way to throw a bad record away rather than work around it forever.
        """
        event, _cfg, secret = _battle_admin_guard(event_uid)
        if event is None:
            return "Forbidden", 403
        player_id = battle.clean_gamer_id(request.form.get("player_id"))
        if not player_id:
            return "Bad request", 400
        db = database.get_db()
        for table in ("battle_submissions", "battle_player_admin", "battle_reward_grants"):
            db.execute(
                "DELETE FROM %s WHERE event_uid = ? AND player_id = ?" % table,
                (event_uid, player_id),
            )
        db.commit()
        audit_logger.info(
            "BATTLE REMOVE: player %s from event %s", player_id, event_uid
        )
        return redirect(url_for("admin_battle", event_uid=event_uid, secret=secret))

    @app.route("/admin/<event_uid>/battle/rename", methods=["POST"])
    def battle_rename_player(event_uid):
        """
        Fix a name or a mistyped Gamer ID without losing the answers.

        Changing the ID has to carry the admin flags and reward grants with
        it, otherwise a corrected typo silently drops someone's rally-lead
        mark and their handed-out pieces.
        """
        event, _cfg, secret = _battle_admin_guard(event_uid)
        if event is None:
            return "Forbidden", 403
        old_id = battle.clean_gamer_id(request.form.get("player_id"))
        if not old_id:
            return "Bad request", 400

        db = database.get_db()
        db.row_factory = sqlite3.Row
        row = db.execute(
            "SELECT * FROM battle_submissions WHERE event_uid = ? AND player_id = ?",
            (event_uid, old_id),
        ).fetchone()
        if row is None:
            return "Bad request", 400

        back = url_for("admin_battle", event_uid=event_uid, secret=secret)
        new_name = (request.form.get("new_name") or "").strip()[: battle.MAX_NAME_LEN]
        if new_name:
            db.execute(
                "UPDATE battle_submissions SET player_name = ? "
                "WHERE event_uid = ? AND player_id = ?",
                (new_name, event_uid, old_id),
            )

        raw_new_id = (request.form.get("new_player_id") or "").strip()
        if raw_new_id and raw_new_id != old_id:
            new_id = battle.clean_gamer_id(raw_new_id)
            if not new_id:
                db.commit()
                return redirect(back + "#players")
            clash = db.execute(
                "SELECT 1 FROM battle_submissions WHERE event_uid = ? AND player_id = ?",
                (event_uid, new_id),
            ).fetchone()
            if clash:
                # Merging two records is a judgement call, not a rename.
                db.commit()
                return redirect(back + "#players")
            for table in ("battle_submissions", "battle_player_admin",
                          "battle_reward_grants"):
                db.execute(
                    "UPDATE %s SET player_id = ? WHERE event_uid = ? AND player_id = ?"
                    % table,
                    (new_id, event_uid, old_id),
                )
            audit_logger.info(
                "BATTLE RENAME: %s -> %s for event %s", old_id, new_id, event_uid
            )
        db.commit()
        return redirect(back + "#players")

    @app.route("/admin/<event_uid>/battle/force", methods=["POST"])
    def battle_force_reward(event_uid):
        """Set, or clear, a reward amount by hand for one player."""
        event, cfg, secret = _battle_admin_guard(event_uid)
        if event is None:
            return "Forbidden", 403
        player_id = battle.clean_gamer_id(request.form.get("player_id"))
        reward_key = (request.form.get("reward_key") or "").strip()
        keys = {r["key"] for r in (cfg.get("rewards") or [])}
        if not player_id or reward_key not in keys:
            return "Bad request", 400

        raw = (request.form.get("forced") or "").strip()
        if raw == "":
            forced = None          # back to the automatic proposal
        else:
            try:
                forced = max(0, min(100000, int(raw)))
            except ValueError:
                return "Bad request", 400

        db = database.get_db()
        db.execute(
            "INSERT INTO battle_reward_grants (event_uid, reward_key, player_id) "
            "VALUES (?, ?, ?) ON CONFLICT(event_uid, reward_key, player_id) DO NOTHING",
            (event_uid, reward_key, player_id),
        )
        db.execute(
            "UPDATE battle_reward_grants SET forced = ? "
            "WHERE event_uid = ? AND reward_key = ? AND player_id = ?",
            (forced, event_uid, reward_key, player_id),
        )
        db.commit()
        return redirect(
            url_for("admin_battle", event_uid=event_uid, secret=secret) + "#rewards"
        )

    @app.route("/admin/<event_uid>/battle/settings", methods=["POST"])
    def battle_settings(event_uid):
        """
        Change team and reward rules at any point in the event.

        Leadership decides the split at the end, once they know how many
        people actually answered - so nothing here may be locked in at
        creation time.
        """
        event, cfg, secret = _battle_admin_guard(event_uid)
        if event is None:
            return "Forbidden", 403

        updated = battle.update_rules(cfg, request.form)
        db = database.get_db()
        db.execute(
            "UPDATE events SET battle_config = ? WHERE uid = ?",
            (json.dumps(updated), event_uid),
        )
        db.commit()
        audit_logger.info("BATTLE SETTINGS updated for event %s", event_uid)
        return redirect(
            url_for("admin_battle", event_uid=event_uid, secret=secret) + "#settings"
        )

    @app.route("/admin/<event_uid>/battle/reward", methods=["POST"])
    def battle_set_reward(event_uid):
        """Record how many pieces a player has actually been handed in game."""
        event, cfg, secret = _battle_admin_guard(event_uid)
        if event is None:
            return "Forbidden", 403

        reward_key = request.form.get("reward_key", "")
        if reward_key not in {r["key"] for r in cfg.get("rewards", [])}:
            return "Bad request", 400

        player_id = battle.clean_gamer_id(request.form.get("player_id"))
        if not player_id:
            return "Bad request", 400

        try:
            delivered = max(0, int(request.form.get("delivered", "0")))
        except ValueError:
            return "Bad request", 400

        db = database.get_db()
        db.execute(
            """
            INSERT INTO battle_reward_grants (event_uid, reward_key, player_id, delivered)
            VALUES (?, ?, ?, ?)
            ON CONFLICT(event_uid, reward_key, player_id)
            DO UPDATE SET delivered = excluded.delivered
            """,
            (event_uid, reward_key, player_id, delivered),
        )
        db.commit()
        return redirect(
            url_for("admin_battle", event_uid=event_uid, secret=secret) + "#rewards"
        )

    @app.route("/event/<event_uid>")
    def player_form(event_uid):
        """
        One link for players. A combined event shows a chooser first, so a
        member fills in prep week and battle day from the same URL instead of
        being handed two links and losing one of them.
        """
        event = _event_row(event_uid)
        if event is None:
            return "Event not found", 404
        kind = _event_type(event)
        if kind == "battle":
            return _battle_player_page(event)
        if kind == "prep":
            return _prep_player_page(event)
        return _combined_player_page(event)

    @app.route("/event/<event_uid>/prep")
    def player_form_prep(event_uid):
        event = _event_row(event_uid)
        if event is None or not _has_prep(event):
            return "Event not found", 404
        return _prep_player_page(event)

    @app.route("/event/<event_uid>/battle")
    def player_form_battle(event_uid):
        event = _event_row(event_uid)
        if event is None or not _has_battle(event):
            return "Event not found", 404
        return _battle_player_page(event)

    def _combined_player_page(event, error=None):
        """
        Prep week and battle day on one page, in one form, with one button.

        A chooser with two cards is just two links wearing a hat: the member
        still clicks twice and submits twice, and half of them do one and
        forget the other.
        """
        db = database.get_db()
        db.row_factory = sqlite3.Row
        event_uid = event["uid"]
        cfg = _battle_cfg(event)
        slots = battle.hour_slots(cfg)

        density = [0] * len(slots)
        rows = db.execute(
            "SELECT availability FROM battle_submissions WHERE event_uid = ?",
            (event_uid,),
        ).fetchall()
        for row in rows:
            try:
                values = json.loads(row["availability"])
            except (ValueError, TypeError):
                continue
            for i, on in enumerate(values):
                if i < len(density) and on:
                    density[i] += 1

        active_days_config = json.loads(event["active_days"])
        active_days = get_ordered_active_days(active_days_config)
        slot_count = event["slot_count"] if event["slot_count"] is not None else 49
        slot_density = {day: [0] * slot_count for day in active_days}
        for sub in db.execute(
            "SELECT day_type, feasible_slots FROM submissions WHERE event_uid = ?",
            (event_uid,),
        ).fetchall():
            dt = sub["day_type"]
            if dt in slot_density and sub["feasible_slots"]:
                try:
                    for i in json.loads(sub["feasible_slots"]):
                        if 0 <= i < slot_count:
                            slot_density[dt][i] += 1
                except (json.JSONDecodeError, TypeError):
                    pass

        return render_template(
            "combined_form.html",
            event={
                "uid": event["uid"],
                "name": event["name"],
                "server_id": event["server_id"],
                "active_days": active_days_config,
                "slot_count": slot_count,
                "cfg": cfg,
                "slots": slots,
            },
            active_days=active_days,
            slot_density=slot_density,
            slots=slots,
            density=density,
            below_label=battle.BELOW_LABEL,
            base_tiers=battle.base_tiers(cfg),
            t11_tiers=[x for x in battle.base_tiers(cfg) if battle.t11_allowed(x, cfg)],
            t11_from=battle.T11_FROM,
            total_responses=len(rows),
            error=error,
            form=request.form if error else None,
        )

    def _battle_player_page(event):
        db = database.get_db()
        db.row_factory = sqlite3.Row
        event_uid = event["uid"]
        if True:
            cfg = _battle_cfg(event)
            slots = battle.hour_slots(cfg)
            # Show how many people already claimed each hour, so players can
            # see where the server is thin before they answer.
            density = [0] * len(slots)
            rows = db.execute(
                "SELECT availability FROM battle_submissions WHERE event_uid = ?",
                (event_uid,),
            ).fetchall()
            for row in rows:
                try:
                    values = json.loads(row["availability"])
                except (ValueError, TypeError):
                    continue
                for i, on in enumerate(values):
                    if i < len(density) and on:
                        density[i] += 1
            return render_template(
                "battle_form.html",
                event=_battle_event_dict(event, cfg),
                slots=slots,
                density=density,
                below_label=battle.BELOW_LABEL,
                base_tiers=battle.base_tiers(cfg),
                t11_tiers=[x for x in battle.base_tiers(cfg) if battle.t11_allowed(x, cfg)],
                t11_from=battle.T11_FROM,
                total_responses=len(rows),
            )

    def _prep_player_page(event):
        db = database.get_db()
        db.row_factory = sqlite3.Row
        event_uid = event["uid"]
        active_days_config = json.loads(event["active_days"])
        active_days = get_ordered_active_days(active_days_config)
        # Create a dictionary from the database row
        event_dict = {
            "uid": event["uid"],
            "name": event["name"],
            "active_days": active_days_config,
            "server_id": event["server_id"],
            "slot_count": event["slot_count"]
            if event["slot_count"] is not None
            else 49,
        }

        slot_count = event_dict["slot_count"]
        slot_density = {day: [0] * slot_count for day in active_days}
        submissions = db.execute(
            "SELECT day_type, feasible_slots FROM submissions WHERE event_uid = ?",
            (event_uid,),
        ).fetchall()
        for sub in submissions:
            dt = sub["day_type"]
            if dt in slot_density and sub["feasible_slots"]:
                try:
                    slots = json.loads(sub["feasible_slots"])
                    for s in slots:
                        if 0 <= s < slot_count:
                            slot_density[dt][s] += 1
                except (json.JSONDecodeError, TypeError):
                    pass

        return render_template(
            "player_form.html",
            event=event_dict,
            active_days=active_days,
            slot_density=slot_density,
        )

    @app.route("/event/<event_uid>/submit", methods=["POST"])
    def submit(event_uid):
        db = database.get_db()

        battle_event = _event_row(event_uid)
        mode = (request.form.get("_mode") or "").strip().lower()
        def _prep_half_touched():
            """Did they enter anything in the prep half at all?"""
            for key in ("speedups-construction", "speedups-training",
                        "speedups-research", "truegold", "tempered_truegold",
                        "truegold_dust"):
                try:
                    if int(request.form.get(key) or 0) > 0:
                        return True
                except (TypeError, ValueError):
                    continue
            for key in ("slots-construction", "slots-training", "slots-research"):
                raw = (request.form.get(key) or "").strip()
                if raw and raw not in ("[]", "null"):
                    return True
            return False

        def _battle_half_touched(cfg):
            """Did they start the battle half at all?

            On the combined form an untouched battle half is not an error -
            it just means they answered prep week. Rejecting the whole POST
            would throw away the half they did fill in.
            """
            for troop in cfg.get("troop_types") or battle.DEFAULT_TROOP_TYPES:
                if (request.form.get("tier_%s" % troop["key"]) or "").strip():
                    return True
            for i in range(len(battle.hour_slots(cfg))):
                if (request.form.get("hour_%d" % i) or "").strip().lower() == "yes":
                    return True
            return False

        if battle_event is not None and _has_battle(battle_event) and (
            mode in ("battle", "both") or (mode == "" and _is_battle(battle_event))
        ) and (mode != "both" or _battle_half_touched(_battle_cfg(battle_event))):
            cfg = _battle_cfg(battle_event)
            try:
                parsed = battle.parse_submission(request.form, cfg)
            except battle.SubmissionError as err:
                slots = battle.hour_slots(cfg)
                if mode == "both":
                    return _combined_player_page(battle_event, error=str(err)), 400
                return (
                    render_template(
                        "battle_form.html",
                        event=_battle_event_dict(battle_event, cfg),
                        slots=slots,
                        density=[0] * len(slots),
                        below_label=battle.BELOW_LABEL,
                        base_tiers=battle.base_tiers(cfg),
                        t11_tiers=[x for x in battle.base_tiers(cfg) if battle.t11_allowed(x, cfg)],
                        t11_from=battle.T11_FROM,
                        total_responses=0,
                        error=str(err),
                        form=request.form,
                    ),
                    400,
                )

            # UNIQUE(event_uid, player_id) turns a re-submission into an
            # update, so players can correct themselves without creating a
            # duplicate for leadership to clean up later.
            db.execute(
                """
                INSERT INTO battle_submissions
                    (id, event_uid, player_id, player_name, alliance_name,
                     tiers, availability, notes)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT(event_uid, player_id) DO UPDATE SET
                    player_name = excluded.player_name,
                    alliance_name = excluded.alliance_name,
                    tiers = excluded.tiers,
                    availability = excluded.availability,
                    notes = excluded.notes,
                    timestamp = CURRENT_TIMESTAMP
                """,
                (
                    secrets.token_urlsafe(12),
                    event_uid,
                    parsed["player_id"],
                    parsed["player_name"],
                    parsed["alliance_name"],
                    json.dumps(parsed["tiers"]),
                    json.dumps(parsed["availability"]),
                    parsed["notes"],
                ),
            )
            db.commit()
            audit_logger.info(
                "BATTLE SUBMISSION: %s (%s) for event %s",
                parsed["player_name"],
                parsed["player_id"],
                event_uid,
            )
            # A combined form carries both halves in one POST. Keep going and
            # let the prep block below store the other half from the same
            # fields - the player pressed send once, not twice.
            if mode != "both":
                return redirect(url_for("submission_success"))

        player_id = request.form.get("player_id", "").strip()
        player_name = request.form.get("player_name", "").strip()
        alliance_name = request.form.get("alliance_name", "").strip()

        # Server-side validation
        if not player_id.isdigit():
            return "Invalid Player ID: Must be numeric", 400

        if not player_name:
            return "Invalid Player Name: Cannot be empty", 400

        app.audit_logger.info(
            f"SUBMISSION: Player {player_name} ({player_id}) submitted resources for event {event_uid}"
        )

        # Handle backpack screenshot upload
        backpack_url = None
        if Config.ENABLE_SCREENSHOT_UPLOAD and "backpack_screenshot" in request.files:
            file = request.files["backpack_screenshot"]
            if file and file.filename:
                # Validate file extension
                allowed_extensions = {"png", "jpg", "jpeg", "gif"}
                extension = file.filename.rsplit(".", 1)[-1].lower()
                if extension not in allowed_extensions:
                    return "Invalid file type. Only images are allowed.", 400

                # Validate image header / magic bytes
                header = file.read(16)
                file.seek(0)
                is_png = header.startswith(b"\x89PNG\r\n\x1a\n")
                is_jpeg = header.startswith(b"\xff\xd8\xff")
                is_gif = header.startswith((b"GIF87a", b"GIF89a"))
                if not (is_png or is_jpeg or is_gif):
                    return (
                        "Invalid image content. Only PNG, JPEG, and GIF images are allowed.",
                        400,
                    )

                # Create upload directory if it doesn't exist
                upload_dir = os.path.join(app.static_folder, "uploads")
                os.makedirs(upload_dir, exist_ok=True)

                # Generate unique filename: event_uid + player_id + timestamp + original filename
                filename = secure_filename(
                    f"{event_uid}_{player_id}_{int(time.time())}_{file.filename}"
                )
                file.save(os.path.join(upload_dir, filename))
                backpack_url = validate_safe_url(
                    url_for("static", filename=f"uploads/{filename}")
                )

        # On the combined form an empty prep half means "I only came to change
        # my battle hours". The block below wipes and rewrites this player's
        # prep submissions AND their assignments, so running it on an empty
        # form would silently delete appointments they already have.
        if mode == "both" and not _prep_half_touched():
            return redirect(url_for("submission_success"))

        # First, delete all previous submissions and assignments for this player and event.
        db.execute(
            "DELETE FROM submissions WHERE event_uid = ? AND player_id = ?",
            (event_uid, player_id),
        )
        db.execute(
            "DELETE FROM assignments WHERE event_uid = ? AND player_id = ?",
            (event_uid, player_id),
        )

        # Then, insert the new submissions from the form.
        avatar_url = validate_safe_url(request.form.get("avatar_url"))

        # --- Process Construction Submission ---
        construction_speedups = int(request.form.get("speedups-construction") or 0)
        truegold = int(request.form.get("truegold") or 0)
        tempered_truegold = int(request.form.get("tempered_truegold") or 0)
        feasible_slots = request.form.get("slots-construction", "[]")
        if (
            construction_speedups > 0 or truegold > 0 or tempered_truegold > 0
        ) and feasible_slots != "[]":
            day_type = "construction"
            score = (
                (construction_speedups * 30)
                + (truegold * 2000)
                + (tempered_truegold * 30000)
            )
            raw_data = {
                "speedups": construction_speedups,
                "truegold": truegold,
                "tempered_truegold": tempered_truegold,
            }
            submission_id = f"{event_uid}_{player_id}_{day_type}"
            db.execute(
                "INSERT INTO submissions (id, event_uid, day_type, player_name, player_id, avatar_url, backpack_url, alliance_name, resources, raw_data, feasible_slots) VALUES (?,?,?,?,?,?,?,?,?,?,?)",
                (
                    submission_id,
                    event_uid,
                    day_type,
                    player_name,
                    player_id,
                    avatar_url,
                    backpack_url,
                    alliance_name,
                    score,
                    json.dumps(raw_data),
                    feasible_slots,
                ),
            )

        # --- Process Training Submission ---
        training_speedups = int(request.form.get("speedups-training") or 0)
        feasible_slots = request.form.get("slots-training", "[]")
        if training_speedups > 0 and feasible_slots != "[]":
            day_type = "training"
            score = training_speedups * 90
            raw_data = {"speedups": training_speedups}
            submission_id = f"{event_uid}_{player_id}_{day_type}"
            db.execute(
                "INSERT INTO submissions (id, event_uid, day_type, player_name, player_id, avatar_url, backpack_url, alliance_name, resources, raw_data, feasible_slots) VALUES (?,?,?,?,?,?,?,?,?,?,?)",
                (
                    submission_id,
                    event_uid,
                    day_type,
                    player_name,
                    player_id,
                    avatar_url,
                    backpack_url,
                    alliance_name,
                    score,
                    json.dumps(raw_data),
                    feasible_slots,
                ),
            )

        # --- Process Research Submission ---
        research_speedups = int(request.form.get("speedups-research") or 0)
        truegold_dust = int(request.form.get("truegold_dust") or 0)
        feasible_slots = request.form.get("slots-research", "[]")
        if (research_speedups > 0 or truegold_dust > 0) and feasible_slots != "[]":
            day_type = "research"
            score = (research_speedups * 30) + (truegold_dust * 1000)
            raw_data = {"speedups": research_speedups, "truegold_dust": truegold_dust}
            submission_id = f"{event_uid}_{player_id}_{day_type}"
            db.execute(
                "INSERT INTO submissions (id, event_uid, day_type, player_name, player_id, avatar_url, backpack_url, alliance_name, resources, raw_data, feasible_slots) VALUES (?,?,?,?,?,?,?,?,?,?,?)",
                (
                    submission_id,
                    event_uid,
                    day_type,
                    player_name,
                    player_id,
                    avatar_url,
                    backpack_url,
                    alliance_name,
                    score,
                    json.dumps(raw_data),
                    feasible_slots,
                ),
            )

        db.commit()

        return redirect(url_for("submission_success"))

    @app.route("/submission-success")
    def submission_success():
        return render_template("submission_success.html")

    @app.route("/admin/<event_uid>")
    def admin_dashboard(event_uid):
        """Entry point. A combined event lands on a chooser, not on one half."""
        db = database.get_db()
        db.row_factory = sqlite3.Row
        secret = request.args.get("secret")
        event = db.execute(
            "SELECT * FROM events WHERE uid = ?", (event_uid,)
        ).fetchone()
        if event is None:
            return "Event not found", 404
        if not secret or not hmac.compare_digest(event["admin_secret"], secret):
            return "Forbidden", 403
        kind = _event_type(event)
        if kind == "battle":
            return _battle_admin_page(event, secret)
        if kind == "prep":
            return _prep_admin_page(event, secret)
        cfg = _battle_cfg(event)
        return render_template(
            "admin_home.html",
            event={"uid": event["uid"], "name": event["name"],
                   "server_id": event["server_id"]},
            secret=secret,
            published=bool(cfg.get("published")),
        )

    @app.route("/admin/<event_uid>/prep")
    def admin_prep(event_uid):
        db = database.get_db()
        db.row_factory = sqlite3.Row
        secret = request.args.get("secret")
        event = db.execute(
            "SELECT * FROM events WHERE uid = ?", (event_uid,)
        ).fetchone()
        if event is None or not _has_prep(event):
            return "Event not found", 404
        if not secret or not hmac.compare_digest(event["admin_secret"], secret):
            return "Forbidden", 403
        return _prep_admin_page(event, secret)

    @app.route("/admin/<event_uid>/battle")
    def admin_battle(event_uid):
        db = database.get_db()
        db.row_factory = sqlite3.Row
        secret = request.args.get("secret")
        event = db.execute(
            "SELECT * FROM events WHERE uid = ?", (event_uid,)
        ).fetchone()
        if event is None or not _has_battle(event):
            return "Event not found", 404
        if not secret or not hmac.compare_digest(event["admin_secret"], secret):
            return "Forbidden", 403
        return _battle_admin_page(event, secret)

    def _battle_admin_page(event, secret):
        db = database.get_db()
        db.row_factory = sqlite3.Row
        event_uid = event["uid"]
        if True:
            cfg = _battle_cfg(event)
            data = battle.analyse(_battle_rows(db, event_uid), cfg)
            overrides = _battle_overrides(db, event_uid)
            fieldable = data["fieldable"]
            return render_template(
                "battle_admin.html",
                event=_battle_event_dict(event, cfg),
                secret=secret,
                data=data,
                overrides=overrides,
                below_label=battle.BELOW_LABEL,
                manual_players=[
                    p for p in data["players"] if p.get("source") == "admin"
                ],
                plan=battle.assign_teams(fieldable, cfg, overrides),
                alias_text=battle.aliases_text(cfg),
                rewards=battle.allocate_all_rewards(
                    fieldable, cfg,
                    _battle_grants(db, event_uid),
                    _battle_forced(db, event_uid),
                ),
            )

    def _prep_admin_page(event, secret):
        db = database.get_db()
        db.row_factory = sqlite3.Row
        event_uid = event["uid"]
        active_days_config = json.loads(event["active_days"])
        active_days = get_ordered_active_days(active_days_config)

        # Create a dictionary from the database row for the template
        event_dict = {
            "uid": event["uid"],
            "name": event["name"],
            "active_days": active_days_config,
            "server_id": event["server_id"],
        }

        # 1. Group submissions by day_type
        submissions_raw = db.execute(
            "SELECT * FROM submissions WHERE event_uid = ? ORDER BY resources DESC",
            (event_uid,),
        ).fetchall()
        submissions_by_day = {day: [] for day in active_days}
        for row in submissions_raw:
            if row["day_type"] in submissions_by_day:
                # Convert sqlite3.Row to a dictionary to allow item assignment
                sub_dict = dict(row)
                try:
                    sub_dict["raw_resources"] = json.loads(row["raw_data"])
                except (json.JSONDecodeError, TypeError):
                    sub_dict["raw_resources"] = {}
                submissions_by_day[row["day_type"]].append(sub_dict)

        # 2. Group assignments and related data by day_type
        assignments_raw = db.execute(
            "SELECT * FROM assignments WHERE event_uid = ?", (event_uid,)
        ).fetchall()
        rich_assignments = {day: {} for day in active_days}
        assignments_by_sub_id = {}
        submissions_map = {
            (sub["day_type"], sub["player_id"]): sub for sub in submissions_raw
        }

        for a in assignments_raw:
            day_type = a["day_type"]
            player_id = a["player_id"]
            if day_type in rich_assignments:
                submission = submissions_map.get((day_type, player_id))
                if submission:
                    rich_assignments[day_type][a["slot_index"]] = {
                        "player_id": player_id,
                        "player_name": submission["player_name"],
                        "alliance_name": submission["alliance_name"],
                        "avatar_url": submission["avatar_url"],
                        "is_locked": a["is_locked"],
                    }
            assignments_by_sub_id[(a["day_type"], a["player_id"])] = a

        # 3. Group everything else by day_type
        slot_count = event["slot_count"] if event["slot_count"] is not None else 49
        slot_density = {day: [0] * slot_count for day in active_days}
        slot_players = {day: {i: [] for i in range(slot_count)} for day in active_days}
        max_density = {day: 1 for day in active_days}
        available_slots = {day: [] for day in active_days}

        slot_labels = generate_slot_labels(slot_count)

        for day in active_days:
            # Heatmap & Requested Slots Text
            for sub in submissions_by_day[day]:
                if not sub["feasible_slots"]:
                    sub["requested_slots_text"] = "No slots selected"
                    sub["requested_slots_labels"] = []
                    continue
                try:
                    feasible_slots = json.loads(sub["feasible_slots"])
                    # Create human readable labels for hover text and shelf badges
                    requested_labels = [
                        slot_labels[i] for i in feasible_slots if 0 <= i < slot_count
                    ]
                    sub["requested_slots_labels"] = requested_labels
                    sub["requested_slots_text"] = (
                        ", ".join(requested_labels)
                        if requested_labels
                        else "No slots selected"
                    )

                    for slot_index in feasible_slots:
                        if 0 <= slot_index < slot_count:
                            slot_density[day][slot_index] += 1
                            slot_players[day][slot_index].append(
                                {
                                    "player_name": sub["player_name"],
                                    "alliance_name": sub["alliance_name"],
                                    "resources": sub["resources"],
                                    "submission_id": sub["id"],
                                }
                            )

                except (json.JSONDecodeError, TypeError, KeyError):
                    sub["requested_slots_text"] = "Error parsing slots"
                    sub["requested_slots_labels"] = []

                # Resources Hover Text
                try:
                    raw_resources = json.loads(sub["raw_data"])
                    parts = []
                    if day == "construction":
                        if raw_resources.get("speedups"):
                            parts.append(
                                f"Speedups: {format_minutes(raw_resources['speedups'])}"
                            )
                        if raw_resources.get("truegold"):
                            parts.append(f"Truegold: {raw_resources['truegold']}")
                        if raw_resources.get("tempered_truegold"):
                            parts.append(
                                f"Tempered Gold: {raw_resources['tempered_truegold']}"
                            )
                    elif day == "training":
                        if raw_resources.get("speedups"):
                            parts.append(
                                f"Speedups: {format_minutes(raw_resources['speedups'])}"
                            )
                    elif day == "research":
                        if raw_resources.get("speedups"):
                            parts.append(
                                f"Speedups: {format_minutes(raw_resources['speedups'])}"
                            )
                        if raw_resources.get("truegold_dust"):
                            parts.append(f"Dust: {raw_resources['truegold_dust']}")
                    sub["resources_text"] = (
                        " | ".join(parts) if parts else "No raw data"
                    )
                except (json.JSONDecodeError, TypeError):
                    sub["resources_text"] = "Error parsing resources"

            max_density[day] = max(slot_density[day]) if any(slot_density[day]) else 1

            # Available Slots
            assigned_slots_for_day = rich_assignments[day].keys()
            available_slots[day] = [
                i for i in range(slot_count) if i not in assigned_slots_for_day
            ]

        # Generate URLs for the admin dashboard links
        player_url = url_for("player_form", event_uid=event_uid, _external=True)
        finalized_url = url_for(
            "locked_appointments", event_uid=event_uid, _external=True
        )

        insights = compute_event_insights(event_uid, db)

        return render_template(
            "admin_dashboard.html",
            event=event_dict,
            active_days=active_days,
            submissions_by_day=submissions_by_day,
            assignments=rich_assignments,
            assignments_by_sub_id=assignments_by_sub_id,
            available_slots=available_slots,
            secret=secret,
            slot_density=slot_density,
            slot_players=slot_players,
            max_density=max_density,
            player_url=player_url,
            finalized_url=finalized_url,
            insights=insights,
        )

    @app.route("/event/<event_uid>/lineup")
    def battle_lineup(event_uid):
        """
        The battle line-up, readable without the admin key.

        It stays dark until leadership publishes it. Posting the full roster
        the moment an event is created would hand the other kingdom the plan,
        so the default is closed and opening it is a deliberate click.
        """
        db = database.get_db()
        db.row_factory = sqlite3.Row
        event = _event_row(event_uid)
        if event is None or not _has_battle(event):
            return "Event not found", 404
        cfg = _battle_cfg(event)
        published = bool(cfg.get("published"))
        plan = None
        data = None
        if published:
            data = battle.analyse(_battle_rows(db, event_uid), cfg)
            plan = battle.assign_teams(
                data["fieldable"], cfg, _battle_overrides(db, event_uid)
            )
        return render_template(
            "battle_lineup.html",
            event=_battle_event_dict(event, cfg),
            published=published,
            plan=plan,
            data=data,
        )

    @app.route("/admin/<event_uid>/battle/publish", methods=["POST"])
    def battle_publish(event_uid):
        event, cfg, secret = _battle_admin_guard(event_uid)
        if event is None:
            return "Forbidden", 403
        cfg["published"] = request.form.get("published") == "yes"
        db = database.get_db()
        db.execute(
            "UPDATE events SET battle_config = ? WHERE uid = ?",
            (json.dumps(cfg), event_uid),
        )
        db.commit()
        return redirect(url_for("admin_battle", event_uid=event_uid, secret=secret))

    @app.route("/admin/<event_uid>/battle/export.csv")
    def battle_export(event_uid):
        """Everything the dashboard shows, as one csv - no more hand-copying."""
        event, cfg, secret = _battle_admin_guard(event_uid)
        if event is None:
            return "Forbidden", 403
        db = database.get_db()
        db.row_factory = sqlite3.Row
        data = battle.analyse(_battle_rows(db, event_uid), cfg)
        overrides = _battle_overrides(db, event_uid)
        plan = battle.assign_teams(data["fieldable"], cfg, overrides)
        team_of = {}
        role_of = {}
        for team in plan["teams"]:
            for p in team["core"]:
                team_of[p["player_id"]] = team["cfg"]["name"]
                role_of[p["player_id"]] = p.get("role") or ""
            for p in team["reserve"]:
                team_of[p["player_id"]] = team["cfg"]["name"] + " (reserve)"
        late_ids = {p["player_id"] for p in data["late_only"]}
        slots = battle.hour_slots(cfg)
        types = cfg.get("troop_types") or battle.DEFAULT_TROOP_TYPES
        buf = io.StringIO()
        writer = csv.writer(buf)
        writer.writerow(
            ["Player", "Gamer ID", "Alliance", "Team", "Role", "Best tier", "Full TG5",
             "Weak types"]
            + [t["name"] for t in types]
            + [s["label"] for s in slots]
            + ["Hours", "Note"]
        )
        for p in data["players"]:
            assignment = team_of.get(p["player_id"], "")
            if not assignment and p["player_id"] in late_ids:
                assignment = "late only"
            writer.writerow(
                [p["player_name"], p["player_id"], p.get("alliance_name") or "",
                 assignment, role_of.get(p["player_id"], ""), p.get("best_tier") or "",
                 "yes" if p.get("full_tg5") else "no", p.get("full_tier_note") or ""]
                + [(p.get("tiers") or {}).get(t["key"], "") for t in types]
                + ["x" if i < len(p["availability"]) and p["availability"][i] else ""
                   for i in range(len(slots))]
                + [p.get("hours"), (p.get("notes") or "").replace("\n", " ")]
            )
        payload = buf.getvalue()
        return Response(
            payload,
            mimetype="text/csv",
            headers={
                "Content-Disposition": 'attachment; filename="%s-battle.csv"' % event_uid
            },
        )

    @app.route("/event/<event_uid>/schedule")
    def public_schedule(event_uid):
        db = database.get_db()
        db.row_factory = sqlite3.Row
        event = db.execute(
            "SELECT * FROM events WHERE uid = ?", (event_uid,)
        ).fetchone()

        if event is None:
            return "Event not found", 404

        active_days_config = json.loads(event["active_days"])
        active_days = get_ordered_active_days(active_days_config)

        # Create a dictionary from the database row for the template
        event_dict = {
            "uid": event["uid"],
            "name": event["name"],
            "active_days": active_days_config,
            "server_id": event["server_id"],
        }

        assignments_raw = db.execute(
            "SELECT * FROM assignments WHERE event_uid = ?", (event_uid,)
        ).fetchall()

        # Group assignments by day_type
        assignments = {day: {} for day in active_days}
        for a in assignments_raw:
            if a["day_type"] in assignments:
                assignments[a["day_type"]][a["slot_index"]] = a

        return render_template(
            "public_schedule.html",
            event=event_dict,
            active_days=active_days,
            assignments=assignments,
        )

    @app.route("/admin/<event_uid>/manual_assign", methods=["POST"])
    def manual_assign(event_uid):
        secret = request.form.get("secret")
        db = database.get_db()
        db.row_factory = sqlite3.Row
        event = db.execute(
            "SELECT * FROM events WHERE uid = ?", (event_uid,)
        ).fetchone()
        if event is None:
            return "Event not found", 404
        if not secret or not hmac.compare_digest(event["admin_secret"], secret):
            return "Forbidden", 403

        submission_id = request.form.get("submission_id")
        slot_index = request.form.get("slot_index")

        if not slot_index:  # Don't do anything if the slot is empty
            return redirect(
                url_for("admin_dashboard", event_uid=event_uid, secret=secret)
            )

        try:
            slot_idx_val = int(slot_index)
            slot_count = event["slot_count"] if event["slot_count"] is not None else 49
            if not (0 <= slot_idx_val < slot_count):
                return "Invalid slot index range", 400
        except (ValueError, TypeError):
            return "Invalid slot index format", 400

        if not submission_id:
            return "Missing submission_id", 400
        try:
            _, player_id, day_type = submission_id.split("_", 2)
        except (ValueError, AttributeError):
            return "Invalid submission_id format", 400

        sub = db.execute(
            "SELECT 1 FROM submissions WHERE id = ? AND event_uid = ?",
            (submission_id, event_uid),
        ).fetchone()
        if not sub:
            return "Submission not found", 404

        app.audit_logger.info(
            f"ADMIN: Manual assign - Player {player_id} to slot {slot_idx_val} for day {day_type} in event {event_uid}"
        )

        # Check if there is an existing assignment in this slot that will be overridden
        existing_assignment = db.execute(
            "SELECT player_id FROM assignments WHERE event_uid = ? AND day_type = ? AND slot_index = ?",
            (event_uid, day_type, slot_idx_val),
        ).fetchone()
        if existing_assignment and existing_assignment["player_id"] != player_id:
            db.execute(
                "UPDATE submissions SET status = 'Pending' WHERE event_uid = ? AND player_id = ? AND day_type = ?",
                (event_uid, existing_assignment["player_id"], day_type),
            )

        # Delete any pre-existing assignment for this player on this day
        db.execute(
            "DELETE FROM assignments WHERE event_uid = ? AND player_id = ? AND day_type = ?",
            (event_uid, player_id, day_type),
        )

        # Overwrite whatever was in the target slot and lock it
        db.execute(
            "REPLACE INTO assignments (event_uid, day_type, slot_index, player_id, is_locked) VALUES (?, ?, ?, ?, ?)",
            (event_uid, day_type, slot_idx_val, player_id, 1),
        )

        # Update submission status to 'Locked'
        db.execute(
            "UPDATE submissions SET status = 'Locked' WHERE event_uid = ? AND player_id = ? AND day_type = ?",
            (event_uid, player_id, day_type),
        )

        db.commit()

        return redirect(url_for("admin_dashboard", event_uid=event_uid, secret=secret))

    @app.route("/admin/<event_uid>/distribute", methods=["POST"])
    def distribute(event_uid):
        secret = request.form.get("secret")
        db = database.get_db()
        db.row_factory = sqlite3.Row
        event = db.execute(
            "SELECT * FROM events WHERE uid = ?", (event_uid,)
        ).fetchone()
        if event is None:
            return "Event not found", 404
        if not secret or not hmac.compare_digest(event["admin_secret"], secret):
            return "Forbidden", 403

        day_type = request.form.get("day_type")
        app.audit_logger.info(
            f"ADMIN: Automatic distribution triggered for event {event_uid}, day {day_type or 'all'}"
        )
        logic.run_distribution_algorithm(event_uid, day_type)

        return redirect(url_for("admin_dashboard", event_uid=event_uid, secret=secret))

    @app.route("/admin/<event_uid>/export/<day_type>", methods=["GET"])
    def export_csv(event_uid, day_type):
        secret = request.args.get("secret")
        db = database.get_db()
        db.row_factory = sqlite3.Row
        event = db.execute(
            "SELECT * FROM events WHERE uid = ?", (event_uid,)
        ).fetchone()
        if event is None:
            return "Event not found", 404
        if not secret or not hmac.compare_digest(event["admin_secret"], secret):
            return "Forbidden", 403

        # Fetch locked assignments joined with submissions to get player name
        assignments = db.execute(
            """
            SELECT a.day_type, a.player_id, s.player_name, a.slot_index
            FROM assignments a
            JOIN submissions s ON a.event_uid = s.event_uid AND a.day_type = s.day_type AND a.player_id = s.player_id
            WHERE a.event_uid = ? AND a.day_type = ? AND a.is_locked = 1
            ORDER BY a.slot_index ASC
            """,
            (event_uid, day_type),
        ).fetchall()

        slot_count = event["slot_count"] if event["slot_count"] is not None else 49
        slot_labels = generate_slot_labels(slot_count)

        output = io.StringIO()
        writer = csv.writer(output)
        writer.writerow(["Event Type", "Player ID", "Player Name", "Appointment Slot"])

        for a in assignments:
            writer.writerow(
                [
                    a["day_type"],
                    a["player_id"],
                    a["player_name"],
                    slot_labels[a["slot_index"]],
                ]
            )

        output.seek(0)
        return Response(
            output.getvalue(),
            mimetype="text/csv",
            headers={
                "Content-Disposition": f"attachment; filename=schedule_{event['uid']}_{day_type}.csv"
            },
        )

    @app.route("/admin/<event_uid>/export_submissions", methods=["GET"])
    def export_submissions(event_uid):
        secret = request.args.get("secret")
        db = database.get_db()
        db.row_factory = sqlite3.Row
        event = db.execute(
            "SELECT * FROM events WHERE uid = ?", (event_uid,)
        ).fetchone()
        if event is None:
            return "Event not found", 404
        if not secret or not hmac.compare_digest(event["admin_secret"], secret):
            return "Forbidden", 403

        submissions = db.execute(
            """
            SELECT day_type, player_name, player_id, avatar_url, backpack_url, 
                   alliance_name, resources, raw_data, feasible_slots, status 
            FROM submissions 
            WHERE event_uid = ?
            """,
            (event_uid,),
        ).fetchall()

        # Build array of dictionaries
        sub_list = []
        for s in submissions:
            sub_list.append(
                {
                    "day_type": s["day_type"],
                    "player_name": s["player_name"],
                    "player_id": s["player_id"],
                    "avatar_url": s["avatar_url"],
                    "backpack_url": s["backpack_url"],
                    "alliance_name": s["alliance_name"],
                    "resources": s["resources"],
                    "raw_data": s["raw_data"],
                    "feasible_slots": s["feasible_slots"],
                    "status": s["status"],
                }
            )

        import datetime

        timestamp = datetime.datetime.now(datetime.timezone.utc).strftime(
            "%Y%m%d_%H%M%S"
        )
        filename = f"submissions_{event_uid}_{timestamp}.json"

        return Response(
            json.dumps(sub_list, indent=2),
            mimetype="application/json",
            headers={"Content-Disposition": f"attachment; filename={filename}"},
        )

    @app.route("/admin/<event_uid>/import_submissions", methods=["POST"])
    def import_submissions(event_uid):
        secret = request.form.get("secret")
        db = database.get_db()
        db.row_factory = sqlite3.Row
        event = db.execute(
            "SELECT * FROM events WHERE uid = ?", (event_uid,)
        ).fetchone()
        if event is None:
            return "Event not found", 404
        if not secret or not hmac.compare_digest(event["admin_secret"], secret):
            return "Forbidden", 403

        file = request.files.get("submissions_file")
        if not file or file.filename == "":
            flash("No file selected.", "error")
            return redirect(
                url_for("admin_dashboard", event_uid=event_uid, secret=secret)
            )

        try:
            data = json.load(file)
        except Exception:  # noqa: BLE001
            flash("Invalid file format. Please upload a valid JSON file.", "error")
            return redirect(
                url_for("admin_dashboard", event_uid=event_uid, secret=secret)
            )

        if not isinstance(data, list):
            flash(
                "Invalid JSON schema. Submissions must be formatted as an array.",
                "error",
            )
            return redirect(
                url_for("admin_dashboard", event_uid=event_uid, secret=secret)
            )

        required_fields = [
            "day_type",
            "player_name",
            "player_id",
            "resources",
            "raw_data",
            "feasible_slots",
        ]
        for idx, item in enumerate(data):
            if not isinstance(item, dict):
                flash(f"Item at index {idx} is not a valid submission object.", "error")
                return redirect(
                    url_for("admin_dashboard", event_uid=event_uid, secret=secret)
                )
            for field in required_fields:
                if field not in item:
                    flash(
                        f"Missing required field '{field}' at submission index {idx}.",
                        "error",
                    )
                    return redirect(
                        url_for("admin_dashboard", event_uid=event_uid, secret=secret)
                    )

            # Validate resources can be parsed to float, convert and save as float
            try:
                item["resources"] = float(item["resources"])
            except (ValueError, TypeError):
                flash("Must be a number.", "error")
                return redirect(
                    url_for("admin_dashboard", event_uid=event_uid, secret=secret)
                )

            # Validate and normalize feasible_slots to JSON string of list of integers
            fs_val = item["feasible_slots"]
            if isinstance(fs_val, str):
                try:
                    fs_val = json.loads(fs_val)
                except Exception:  # noqa: BLE001
                    flash("feasible_slots must be a list.", "error")
                    return redirect(
                        url_for("admin_dashboard", event_uid=event_uid, secret=secret)
                    )
            if not isinstance(fs_val, list):
                flash("feasible_slots must be a list.", "error")
                return redirect(
                    url_for("admin_dashboard", event_uid=event_uid, secret=secret)
                )
            try:
                fs_val = [int(x) for x in fs_val]
            except (ValueError, TypeError):
                flash("feasible_slots must be a list of integers.", "error")
                return redirect(
                    url_for("admin_dashboard", event_uid=event_uid, secret=secret)
                )
            item["feasible_slots"] = json.dumps(fs_val)

            # Validate and normalize raw_data to JSON string of a dictionary/object
            rd_val = item["raw_data"]
            if isinstance(rd_val, str):
                try:
                    rd_val = json.loads(rd_val)
                except Exception:  # noqa: BLE001
                    flash("raw_data must be a JSON object.", "error")
                    return redirect(
                        url_for("admin_dashboard", event_uid=event_uid, secret=secret)
                    )
            if not isinstance(rd_val, dict):
                flash("raw_data must be a JSON object.", "error")
                return redirect(
                    url_for("admin_dashboard", event_uid=event_uid, secret=secret)
                )
            item["raw_data"] = json.dumps(rd_val)

        # Process upserts inside transaction
        unique_players = list({item["player_id"] for item in data})

        # Delete existing matching records (for the specific player_id and day_type)
        for item in data:
            db.execute(
                "DELETE FROM submissions WHERE event_uid = ? AND player_id = ? AND day_type = ?",
                (event_uid, item["player_id"], item["day_type"]),
            )
            db.execute(
                "DELETE FROM assignments WHERE event_uid = ? AND player_id = ? AND day_type = ?",
                (event_uid, item["player_id"], item["day_type"]),
            )

        # Insert the imported submissions
        for item in data:
            sub_id = f"{event_uid}_{item['player_id']}_{item['day_type']}"
            # Ensure values are safely parsed (re-encode json strings if they were parsed as dicts/lists)
            raw_data_str = (
                item["raw_data"]
                if isinstance(item["raw_data"], str)
                else json.dumps(item["raw_data"])
            )
            feasible_slots_str = (
                item["feasible_slots"]
                if isinstance(item["feasible_slots"], str)
                else json.dumps(item["feasible_slots"])
            )

            db.execute(
                """
                INSERT INTO submissions (
                    id, event_uid, day_type, player_name, player_id, 
                    avatar_url, backpack_url, alliance_name, resources, 
                    raw_data, feasible_slots, status
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    sub_id,
                    event_uid,
                    item["day_type"],
                    item["player_name"],
                    item["player_id"],
                    validate_safe_url(item.get("avatar_url")),
                    validate_safe_url(item.get("backpack_url")),
                    item.get("alliance_name"),
                    item["resources"],
                    raw_data_str,
                    feasible_slots_str,
                    item.get("status", "Pending"),
                ),
            )

        db.commit()
        app.audit_logger.info(
            f"ADMIN: Imported {len(data)} submissions for {len(unique_players)} players in event {event_uid}"
        )
        flash(
            f"Successfully imported {len(data)} submissions for {len(unique_players)} players.",
            "success",
        )

        return redirect(url_for("admin_dashboard", event_uid=event_uid, secret=secret))

    @app.route("/admin/<event_uid>/confirm", methods=["POST"])
    def confirm(event_uid):
        secret = request.form.get("secret")
        db = database.get_db()
        db.row_factory = sqlite3.Row
        event = db.execute(
            "SELECT * FROM events WHERE uid = ?", (event_uid,)
        ).fetchone()
        if event is None:
            return "Event not found", 404
        if not secret or not hmac.compare_digest(event["admin_secret"], secret):
            return "Forbidden", 403

        slot_index = request.form.get("slot_index")
        day_type = request.form.get("day_type")

        # Get the player_id for this assignment
        assignment = db.execute(
            "SELECT player_id FROM assignments WHERE event_uid = ? AND day_type = ? AND slot_index = ?",
            (event_uid, day_type, slot_index),
        ).fetchone()

        db.execute(
            "UPDATE assignments SET is_locked = 1 WHERE event_uid = ? AND day_type = ? AND slot_index = ?",
            (event_uid, day_type, slot_index),
        )

        app.audit_logger.info(
            f"ADMIN: Lock - Slot {slot_index} for day {day_type} in event {event_uid}"
        )

        if assignment:
            db.execute(
                "UPDATE submissions SET status = 'Locked' WHERE event_uid = ? AND day_type = ? AND player_id = ?",
                (event_uid, day_type, assignment["player_id"]),
            )

        db.commit()

        return redirect(url_for("admin_dashboard", event_uid=event_uid, secret=secret))

    @app.route("/admin/<event_uid>/unlock", methods=["POST"])
    def unlock(event_uid):
        secret = request.form.get("secret")
        db = database.get_db()
        db.row_factory = sqlite3.Row
        event = db.execute(
            "SELECT * FROM events WHERE uid = ?", (event_uid,)
        ).fetchone()
        if event is None:
            return "Event not found", 404
        if not secret or not hmac.compare_digest(event["admin_secret"], secret):
            return "Forbidden", 403

        slot_index = request.form.get("slot_index")
        day_type = request.form.get("day_type")

        # Get the player_id for this assignment
        assignment = db.execute(
            "SELECT player_id FROM assignments WHERE event_uid = ? AND day_type = ? AND slot_index = ?",
            (event_uid, day_type, slot_index),
        ).fetchone()

        db.execute(
            "UPDATE assignments SET is_locked = 0 WHERE event_uid = ? AND day_type = ? AND slot_index = ?",
            (event_uid, day_type, slot_index),
        )

        app.audit_logger.info(
            f"ADMIN: Unlock - Slot {slot_index} for day {day_type} in event {event_uid}"
        )

        if assignment:
            db.execute(
                "UPDATE submissions SET status = 'Confirmed' WHERE event_uid = ? AND day_type = ? AND player_id = ?",
                (event_uid, day_type, assignment["player_id"]),
            )

        db.commit()

        return redirect(url_for("admin_dashboard", event_uid=event_uid, secret=secret))

    @app.route("/admin/<event_uid>/delete", methods=["POST"])
    def delete(event_uid):
        secret = request.form.get("secret")
        db = database.get_db()
        db.row_factory = sqlite3.Row
        event = db.execute(
            "SELECT * FROM events WHERE uid = ?", (event_uid,)
        ).fetchone()
        if event is None:
            return "Event not found", 404
        if not secret or not hmac.compare_digest(event["admin_secret"], secret):
            return "Forbidden", 403

        submission_id = request.form.get("submission_id")

        app.audit_logger.info(
            f"ADMIN: Delete submission {submission_id} in event {event_uid}"
        )

        # Find player_id and day_type from submission_id
        _, player_id, day_type = submission_id.split("_", 2)

        # First, clear the specific assignment for this player and day
        db.execute(
            "DELETE FROM assignments WHERE event_uid = ? AND player_id = ? AND day_type = ?",
            (event_uid, player_id, day_type),
        )

        # Then, delete the submission
        db.execute("DELETE FROM submissions WHERE id = ?", (submission_id,))

        db.commit()

        return redirect(url_for("admin_dashboard", event_uid=event_uid, secret=secret))

    @app.route("/admin/<event_uid>/update_alliance", methods=["POST"])
    def update_alliance(event_uid):
        secret = request.form.get("secret")
        db = database.get_db()
        db.row_factory = sqlite3.Row
        event = db.execute(
            "SELECT * FROM events WHERE uid = ?", (event_uid,)
        ).fetchone()
        if event is None:
            return "Event not found", 404
        if not secret or not hmac.compare_digest(event["admin_secret"], secret):
            return "Forbidden", 403

        submission_id = request.form.get("submission_id")
        new_alliance_name = request.form.get("alliance_name").strip()

        app.audit_logger.info(
            f"ADMIN: Update alliance for submission {submission_id} to {new_alliance_name} in event {event_uid}"
        )

        db.execute(
            "UPDATE submissions SET alliance_name = ? WHERE id = ? AND event_uid = ?",
            (new_alliance_name, submission_id, event_uid),
        )
        db.commit()

        return redirect(url_for("admin_dashboard", event_uid=event_uid, secret=secret))

    @app.route("/admin/<event_uid>/override_resources", methods=["POST"])
    def override_resources(event_uid):
        secret = request.form.get("secret")
        db = database.get_db()
        db.row_factory = sqlite3.Row
        event = db.execute(
            "SELECT * FROM events WHERE uid = ?", (event_uid,)
        ).fetchone()
        if event is None:
            return "Event not found", 404
        if not secret or not hmac.compare_digest(event["admin_secret"], secret):
            return "Forbidden", 403

        submission_id = request.form.get("submission_id")
        submission = db.execute(
            "SELECT * FROM submissions WHERE id = ? AND event_uid = ?",
            (submission_id, event_uid),
        ).fetchone()

        if submission is None:
            return "Submission not found", 404

        day_type = submission["day_type"]

        try:
            if day_type == "construction":
                speedups = int(request.form.get("speedups") or 0)
                truegold = int(request.form.get("truegold") or 0)
                tempered_truegold = int(request.form.get("tempered_truegold") or 0)
                score = (
                    (speedups * 30) + (truegold * 2000) + (tempered_truegold * 30000)
                )
                raw_data = {
                    "speedups": speedups,
                    "truegold": truegold,
                    "tempered_truegold": tempered_truegold,
                }
            elif day_type == "training":
                speedups = int(request.form.get("speedups") or 0)
                score = speedups * 90
                raw_data = {"speedups": speedups}
            elif day_type == "research":
                speedups = int(request.form.get("speedups") or 0)
                truegold_dust = int(request.form.get("truegold_dust") or 0)
                score = (speedups * 30) + (truegold_dust * 1000)
                raw_data = {"speedups": speedups, "truegold_dust": truegold_dust}
            else:
                return "Invalid day type", 400
        except ValueError:
            return "Invalid resource values", 400

        app.audit_logger.info(
            f"ADMIN: Override resources for submission {submission_id} (day_type={day_type}) - score={score}, raw_data={raw_data} in event {event_uid}"
        )

        db.execute(
            "UPDATE submissions SET resources = ?, raw_data = ? WHERE id = ? AND event_uid = ?",
            (score, json.dumps(raw_data), submission_id, event_uid),
        )
        db.commit()

        return redirect(url_for("admin_dashboard", event_uid=event_uid, secret=secret))

    @app.route("/admin/<event_uid>/unset", methods=["POST"])
    def unset_assignment(event_uid):
        secret = request.form.get("secret")
        db = database.get_db()
        db.row_factory = sqlite3.Row
        event = db.execute(
            "SELECT * FROM events WHERE uid = ?", (event_uid,)
        ).fetchone()
        if event is None:
            return "Event not found", 404
        if not secret or not hmac.compare_digest(event["admin_secret"], secret):
            return "Forbidden", 403

        submission_id = request.form.get("submission_id")
        _, player_id, day_type = submission_id.split("_", 2)

        app.audit_logger.info(
            f"ADMIN: Unset assignment for Player {player_id} on day {day_type} in event {event_uid}"
        )

        # Delete the assignment for this player on this day
        db.execute(
            "DELETE FROM assignments WHERE event_uid = ? AND player_id = ? AND day_type = ?",
            (event_uid, player_id, day_type),
        )

        # Update submission status back to 'Pending'
        db.execute(
            "UPDATE submissions SET status = 'Pending' WHERE event_uid = ? AND player_id = ? AND day_type = ?",
            (event_uid, player_id, day_type),
        )

        db.commit()

        return redirect(url_for("admin_dashboard", event_uid=event_uid, secret=secret))

    @app.route("/admin/<event_uid>/logs")
    def view_logs(event_uid):
        secret = request.args.get("secret")
        db = database.get_db()
        db.row_factory = sqlite3.Row
        event = db.execute(
            "SELECT * FROM events WHERE uid = ?", (event_uid,)
        ).fetchone()
        if event is None:
            return "Event not found", 404
        if not secret or not hmac.compare_digest(event["admin_secret"], secret):
            return "Forbidden", 403

        log_path = os.path.join(app.root_path, "..", "logs", "audit.log")
        if not os.path.exists(log_path):
            return "Log file not found", 404

        with open(log_path, "r", encoding="utf-8") as f:
            all_lines = f.readlines()
            # Filter lines specifically for this event_uid to enforce tenant isolation
            matching_lines = [line for line in all_lines if event_uid in line][-1000:]
            content = "".join(matching_lines)

        return Response(content, mimetype="text/plain")

    @app.route("/superadmin")
    def superadmin():
        secret = request.args.get("secret")
        if secret is not None:
            expected_secret = app.config.get("SUPERADMIN_SECRET", "")
            if hmac.compare_digest(secret, expected_secret):
                session["is_superadmin"] = True
                range_param = request.args.get("range", "all")
                return redirect(url_for("superadmin", range=range_param))
            return "Forbidden", 403

        if session.get("is_superadmin") is True:
            range_param = request.args.get("range", "all")
            valid_range = range_param if range_param in ("1w", "2w", "4w") else "all"
            db = database.get_db()
            metrics = get_superadmin_metrics(db, time_range=valid_range)
            slot_labels = generate_slot_labels(49)
            return render_template(
                "superadmin.html",
                metrics=metrics,
                current_range=valid_range,
                slot_labels=slot_labels,
            )

        return "Forbidden", 403

    @app.route("/superadmin/logout")
    def superadmin_logout():
        session.pop("is_superadmin", None)
        return redirect(url_for("index"))

    return app


app = create_app()
