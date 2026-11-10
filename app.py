import base64
import calendar
import csv
import io
import json
import math
import os
import secrets
import shutil
import smtplib
import ssl
import threading
import time
from email.message import EmailMessage
from datetime import date, datetime, timedelta
from functools import wraps
from urllib.parse import urlsplit

from flask import (Flask, Response, abort, flash, jsonify, redirect,
                   render_template, request, send_from_directory, session,
                   url_for)
from flask_login import (LoginManager, current_user, login_required,
                         login_user, logout_user)
from sqlalchemy import inspect, text
from sqlalchemy.exc import IntegrityError

from config import DEFAULT_SETTINGS, Config
from models import (Attendance, Concern, FaceSample, Holiday, LeaveRequest,
                    Setting, User, db)

app = Flask(__name__)
app.config.from_object(Config)
db.init_app(app)

login_manager = LoginManager(app)
login_manager.login_view = "login"
login_manager.login_message = "Please log in first."

ROLES = ("admin", "employee")
WEEKDAYS = ["Monday", "Tuesday", "Wednesday", "Thursday", "Friday", "Saturday", "Sunday"]


@login_manager.user_loader
def load_user(user_id):
    # A deactivated account is logged out right away, even if it still has a session cookie
    user = db.session.get(User, int(user_id))
    return user if user and user.active else None


def admin_required(view):
    @wraps(view)
    @login_required
    def wrapper(*args, **kwargs):
        if not current_user.is_admin:
            abort(403)
        return view(*args, **kwargs)
    return wrapper


# ---------------------------------------------------------------- CSRF protection
# Every POST must carry the token stored in the user's session, so other websites
# cannot submit forms on behalf of a logged-in user. Pages add it automatically (base.html).
# The kiosk API is excluded: it does not use the login cookie, it uses the kiosk key.

def csrf_token():
    if "csrf" not in session:
        session["csrf"] = secrets.token_hex(16)
    return session["csrf"]


@app.before_request
def csrf_protect():
    if request.method != "POST" or request.endpoint == "kiosk_scan":
        return None
    sent = request.form.get("csrf_token") or request.headers.get("X-CSRF-Token") or ""
    token = session.get("csrf")
    if token and secrets.compare_digest(token, sent):
        return None
    message = "Security check failed. Please refresh the page and try again."
    if request.is_json:
        return jsonify(ok=False, error=message), 400
    flash(message, "error")
    return redirect(back_url("index"))


def back_url(default, **values):
    """The page the user came from, but only if it belongs to this site."""
    ref = request.referrer
    if ref and urlsplit(ref).netloc == request.host:
        return ref
    return url_for(default, **values)


@app.after_request
def security_headers(resp):
    resp.headers.setdefault("X-Content-Type-Options", "nosniff")
    resp.headers.setdefault("X-Frame-Options", "SAMEORIGIN")
    # The kiosk link has a secret key in it; never send it to other sites
    resp.headers.setdefault("Referrer-Policy", "same-origin")
    return resp


@app.errorhandler(403)
@app.errorhandler(404)
def error_page(e):
    if request.path.startswith("/api/"):
        return jsonify(ok=False, error=e.name), e.code
    return render_template("error.html", code=e.code, name=e.name), e.code


@app.errorhandler(413)
def upload_too_large(e):
    flash(f"That file is too large. The limit is {Config.MAX_CONTENT_LENGTH // (1024 * 1024)} MB.", "error")
    return redirect(back_url("index"))


@app.context_processor
def inject_globals():
    open_concerns = 0
    if current_user.is_authenticated and current_user.is_admin:
        open_concerns = Concern.query.filter_by(status="open").count()
        pending_leaves = LeaveRequest.query.filter_by(status="pending").count()
    else:
        pending_leaves = 0
    return {"now": datetime.now(), "open_concerns": open_concerns, "pending_leaves": pending_leaves,
            "csrf_token": csrf_token, "leave_types": LEAVE_TYPES}


@app.template_filter("t")
def fmt_time(value):
    return value.strftime("%I:%M %p") if value else "—"


FLAG_LABELS = {"early": "Early leave", "half_day": "Half day", "no_checkout": "No check-out",
               "half_leave": "Half-day leave"}


STATUS_LABELS = {"present": "On time", "late": "Late", "absent": "Absent",
                 "off": "Weekly off", "holiday": "Holiday", "leave": "On leave"}

LEAVE_TYPES = {"casual": "Casual leave", "sick": "Sick leave", "other": "Other leave"}


@app.template_filter("status_label")
def status_label(status):
    return STATUS_LABELS.get(status, status)


@app.template_filter("flag_label")
def flag_label(flag):
    return FLAG_LABELS.get(flag, flag)


# ---------------------------------------------------------------- settings

_settings_cache = None


def S():
    """Current office settings (defaults merged with what admin saved)."""
    global _settings_cache
    if _settings_cache is None:
        s = dict(DEFAULT_SETTINGS)
        for row in Setting.query.all():
            if row.key in s:
                s[row.key] = json.loads(row.value)
        _settings_cache = s
    return _settings_cache


def save_settings(values):
    global _settings_cache
    for k, v in values.items():
        row = db.session.get(Setting, k) or Setting(key=k)
        row.value = json.dumps(v)
        db.session.add(row)
    db.session.commit()
    _settings_cache = None
    clear_face_cache()


def hm(value):
    return datetime.strptime(value, "%H:%M").time()


def late_cutoff():
    start = datetime.combine(date.today(), hm(S()["office_start"]))
    return (start + timedelta(minutes=int(S()["late_grace_minutes"]))).time()


def checkin_status(check_in):
    return "late" if check_in.time() > late_cutoff() else "present"


# ---------------------------------------------------------------- helpers

def parse_month(value):
    """'2026-11' -> (2026, 11). Falls back to current month."""
    try:
        y, m = (int(part) for part in value.split("-"))
        if 2000 <= y <= 2100 and 1 <= m <= 12:
            return y, m
    except (AttributeError, ValueError):
        pass
    today = date.today()
    return today.year, today.month


def parse_date(value):
    """'2026-11-05' -> date, or None when it is missing or not a real date."""
    try:
        return datetime.strptime(value or "", "%Y-%m-%d").date()
    except ValueError:
        return None


def holidays_between(start, end):
    return {h.date: h.name for h in Holiday.query.filter(Holiday.date.between(start, end))}


def approved_leaves(start, end, user_id=None):
    """{(user_id, date): (leave type label, is_half_day)} for approved leave days between start and end."""
    q = LeaveRequest.query.filter(LeaveRequest.status == "approved",
                                  LeaveRequest.start_date <= end, LeaveRequest.end_date >= start)
    if user_id:
        q = q.filter_by(user_id=user_id)
    days = {}
    for lv in q:
        d = max(lv.start_date, start)
        while d <= min(lv.end_date, end):
            days[(lv.user_id, d)] = (LEAVE_TYPES.get(lv.leave_type, "Leave"), bool(lv.half_day))
            d += timedelta(days=1)
    return days


def working_days(start, end):
    """Days between start and end that are not weekly off or holidays."""
    holidays = holidays_between(start, end)
    off = S()["weekly_off"]
    return sum(1 for i in range((end - start).days + 1)
               if (d := start + timedelta(days=i)) not in holidays and d.weekday() not in off)


def day_info(user, day, rec, holidays, leaves=None):
    """Returns (status, flags, label) for one employee on one day.
    label is the holiday name or the leave type."""
    s = S()
    now = datetime.now()
    leave = leaves.get((user.id, day)) if leaves else None
    if rec:
        flags = []
        day_over = day < now.date() or (day == now.date() and now.time() >= hm(s["office_end"]))
        if leave and leave[1]:
            # Approved half-day leave: leaving early or working fewer hours is expected
            flags.append("half_leave")
        elif day_over:
            if not rec.check_out:
                flags.append("no_checkout")
            else:
                if rec.check_out.time() < hm(s["office_end"]):
                    flags.append("early")
                if (rec.hours or 0) < float(s["half_day_hours"]):
                    flags.append("half_day")
        return rec.status, flags, None
    if day in holidays:
        return "holiday", [], holidays[day]
    if day.weekday() in s["weekly_off"]:
        return "off", [], None
    if leave:
        return "leave", [], leave[0] + (" (half day)" if leave[1] else "")
    if day > now.date() or (user.created_at and day < user.created_at.date()):
        return "", [], None  # future, or before the employee joined
    return "absent", [], None


def month_report(user, year, month):
    """Day-by-day rows for one employee + summary counts."""
    return month_reports([user], year, month)[user.id]


def month_reports(users, year, month):
    """{user.id: (rows, summary)} for many employees. Uses three queries in total,
    not three per employee, so reports and exports stay fast."""
    days_in_month = calendar.monthrange(year, month)[1]
    start, end = date(year, month, 1), date(year, month, days_in_month)
    q = Attendance.query.filter(Attendance.date.between(start, end))
    if len(users) == 1:
        q = q.filter(Attendance.user_id == users[0].id)
    records = {(a.user_id, a.date): a for a in q}
    holidays = holidays_between(start, end)
    leaves = approved_leaves(start, end, users[0].id if len(users) == 1 else None)
    return {u.id: _month_rows(u, year, month, days_in_month, records, holidays, leaves) for u in users}


def _month_rows(user, year, month, days_in_month, records, holidays, leaves):
    rows = []
    summary = {"present": 0, "late": 0, "absent": 0, "off": 0, "holiday": 0, "leave": 0,
               "half_day": 0, "early": 0, "corrected": 0, "hours": 0.0}
    for d in range(1, days_in_month + 1):
        day = date(year, month, d)
        rec = records.get((user.id, day))
        status, flags, holiday = day_info(user, day, rec, holidays, leaves)
        if status:
            half_leave = status == "leave" and holiday.endswith("(half day)")
            summary[status] += 0.5 if half_leave else 1
        for f in flags:
            if f in summary:
                summary[f] += 1
        if rec:
            summary["hours"] += rec.hours or 0
            if rec.edited_at:
                summary["corrected"] += 1
        rows.append({"date": day, "record": rec, "status": status, "flags": flags, "holiday": holiday})
    summary["hours"] = round(summary["hours"], 1)
    # Share of working days (so far) on which the employee came in
    came = summary["present"] + summary["late"]
    expected = came + summary["absent"]
    summary["percent"] = round(came * 100 / expected) if expected else None
    return rows, summary


# Face descriptors are cached in memory so the kiosk does not hit the DB each frame.
_face_cache = None


def face_index():
    global _face_cache
    if _face_cache is None:
        _face_cache = [(s.user_id, s.vector()) for s in
                       FaceSample.query.join(User).filter(User.active.is_(True))]
    return _face_cache


def clear_face_cache():
    global _face_cache
    _face_cache = None


def best_match(descriptor):
    best_user, best_dist = None, float("inf")
    for user_id, vec in face_index():
        dist = math.dist(descriptor, vec)
        if dist < best_dist:
            best_user, best_dist = user_id, dist
    return best_user, best_dist


def valid_descriptor(d):
    return (isinstance(d, list) and len(d) == 128
            and all(isinstance(x, (int, float)) and not isinstance(x, bool) and math.isfinite(x) for x in d))


def save_faces(user, descriptors):
    if not isinstance(descriptors, list) or not (3 <= len(descriptors) <= 10):
        return "Need 3 to 10 face samples."
    if not all(valid_descriptor(d) for d in descriptors):
        return "Invalid face data."
    # Stop one person registering on someone else's account
    for d in descriptors:
        other, dist = best_match(d)
        if other and other != user.id and dist < float(S()["match_threshold"]):
            name = db.session.get(User, other).name
            return f"This face already looks registered to {name}. Contact admin."
    FaceSample.query.filter_by(user_id=user.id).delete()
    for d in descriptors:
        db.session.add(FaceSample(user_id=user.id, descriptor=json.dumps(d)))
    db.session.commit()
    clear_face_cache()
    return None


_last_photo_cleanup = None


def cleanup_old_photos():
    """Deletes photo folders older than 'photo_keep_days'. Runs at most once a day."""
    global _last_photo_cleanup
    today = date.today()
    if _last_photo_cleanup == today:
        return
    _last_photo_cleanup = today
    cutoff = today - timedelta(days=int(S()["photo_keep_days"]))
    if os.path.isdir(Config.SNAPSHOT_DIR):
        for name in os.listdir(Config.SNAPSHOT_DIR):
            try:
                folder_day = datetime.strptime(name, "%Y-%m-%d").date()
            except ValueError:
                continue  # not a photo folder
            if folder_day < cutoff:
                shutil.rmtree(os.path.join(Config.SNAPSHOT_DIR, name), ignore_errors=True)
    Attendance.query.filter(Attendance.date < cutoff).update(
        {Attendance.check_in_photo: None, Attendance.check_out_photo: None})
    db.session.commit()


def save_snapshot(data_url, user, kind, when):
    """Saves a small JPEG from the kiosk. Returns relative path or None."""
    if not S()["save_photos"] or not isinstance(data_url, str):
        return None
    prefix = "data:image/jpeg;base64,"
    if not data_url.startswith(prefix) or len(data_url) > 400_000:
        return None
    try:
        raw = base64.b64decode(data_url[len(prefix):], validate=True)
    except ValueError:
        return None
    if not raw.startswith(b"\xff\xd8"):  # JPEG magic bytes
        return None
    rel = f"{when:%Y-%m-%d}/{user.emp_code}_{kind}_{when:%H%M%S}.jpg"
    path = os.path.join(Config.SNAPSHOT_DIR, rel)
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, "wb") as f:
        f.write(raw)
    return rel


# ---------------------------------------------------------------- auth

@app.route("/")
def index():
    if not current_user.is_authenticated:
        return redirect(url_for("login"))
    return redirect(url_for("admin_dashboard" if current_user.is_admin else "my_dashboard"))


@app.route("/login", methods=["GET", "POST"])
def login():
    if current_user.is_authenticated:
        return redirect(url_for("index"))
    role = request.values.get("role", "employee")
    if role not in ("admin", "employee"):
        role = "employee"
    code = ""
    if request.method == "POST":
        code = request.form.get("emp_code", "").strip()
        ip = request.remote_addr or "unknown"
        if login_blocked(ip):
            flash(f"Too many wrong attempts. Please try again in "
                  f"{Config.LOGIN_BLOCK_SECONDS // 60} minutes.", "error")
            return render_template("login.html", role=role, emp_code=code)
        user = User.query.filter_by(emp_code=code).first()
        if not user or not user.check_password(request.form.get("password", "")):
            _failed_logins.setdefault(ip, []).append(time.time())
            left = Config.MAX_FAILED_LOGINS - len(_failed_logins[ip])
            flash("Wrong ID or password." + (f" {left} attempt(s) left." if 0 < left <= 2 else ""), "error")
        elif not user.active:
            flash("Your account is deactivated. Contact admin.", "error")
        elif role == "admin" and not user.is_admin:
            flash("This is not an admin account. Use Employee login.", "error")
        elif role == "employee" and user.is_admin:
            flash("This is an admin account. Use Admin login.", "error")
        else:
            _failed_logins.pop(ip, None)
            login_user(user)
            return redirect(url_for("index"))
    return render_template("login.html", role=role, emp_code=code)


# Wrong-password times per IP address (kept in memory, cleared on restart)
_failed_logins = {}


def login_blocked(ip):
    now = time.time()
    recent = [t for t in _failed_logins.get(ip, []) if now - t < Config.LOGIN_BLOCK_SECONDS]
    if recent:
        _failed_logins[ip] = recent
    else:
        _failed_logins.pop(ip, None)  # do not keep empty entries forever
    return len(recent) >= Config.MAX_FAILED_LOGINS


@app.route("/logout")
def logout():
    logout_user()
    return redirect(url_for("login"))


@app.route("/change-password", methods=["GET", "POST"])
@login_required
def change_password():
    if request.method == "POST":
        if not current_user.check_password(request.form.get("old", "")):
            flash("Current password is wrong.", "error")
        elif len(request.form.get("new", "")) < 6:
            flash("New password must be at least 6 characters.", "error")
        else:
            current_user.set_password(request.form["new"])
            db.session.commit()
            flash("Password changed.", "success")
            return redirect(url_for("index"))
    return render_template("change_password.html")


# ---------------------------------------------------------------- employee

def office_rules_text():
    s = S()
    off = ", ".join(WEEKDAYS[d] for d in s["weekly_off"]) or "none"
    return (f"Office {hm(s['office_start']):%I:%M %p} – {hm(s['office_end']):%I:%M %p} · "
            f"Late after {late_cutoff():%I:%M %p} · Half day under {s['half_day_hours']} h · Weekly off: {off}")


@app.route("/me")
@login_required
def my_dashboard():
    year, month = parse_month(request.args.get("month"))
    rows, summary = month_report(current_user, year, month)
    return render_template("user_dashboard.html", rows=rows, summary=summary,
                           month_value=f"{year:04d}-{month:02d}",
                           month_label=date(year, month, 1).strftime("%B %Y"),
                           rules=office_rules_text())


@app.route("/me/concerns", methods=["GET", "POST"])
@login_required
def my_concerns():
    if request.method == "POST":
        f = request.form
        subject = f.get("subject", "").strip()[:150]
        message = f.get("message", "").strip()[:2000]
        try:
            day = datetime.strptime(f["date"], "%Y-%m-%d").date() if f.get("date") else None
            req_in = hm(f["requested_in"]).strftime("%H:%M") if f.get("requested_in") else None
            req_out = hm(f["requested_out"]).strftime("%H:%M") if f.get("requested_out") else None
        except ValueError:
            flash("Invalid date or time.", "error")
            return redirect(url_for("my_concerns"))
        if not subject or not message:
            flash("Subject and message are required.", "error")
        elif (req_in or req_out) and not day:
            flash("Please choose the date for the time correction.", "error")
        elif day and day > date.today():
            flash("Date cannot be in the future.", "error")
        elif req_in and req_out and req_out <= req_in:
            flash("Check-out time must be after check-in time.", "error")
        else:
            db.session.add(Concern(user_id=current_user.id, subject=subject, message=message,
                                   date=day, requested_in=req_in, requested_out=req_out))
            db.session.commit()
            flash("Concern sent to admin. You will see the reply here.", "success")
        return redirect(url_for("my_concerns"))
    concerns = Concern.query.filter_by(user_id=current_user.id).order_by(Concern.created_at.desc()).all()
    prefill_date = request.args.get("date", "")
    prefill_rec = None
    if prefill_date:
        try:
            prefill_rec = Attendance.query.filter_by(
                user_id=current_user.id, date=datetime.strptime(prefill_date, "%Y-%m-%d").date()).first()
        except ValueError:
            prefill_date = ""
    return render_template("user_concerns.html", concerns=concerns, records=concern_records(concerns),
                           prefill_date=prefill_date, prefill_rec=prefill_rec)


def concern_records(concerns):
    """{concern.id: Attendance or None} — the record for the day each concern is about."""
    dated = [c for c in concerns if c.date]
    found = {}
    if dated:
        # One query for the whole page instead of one per concern
        found = {(a.user_id, a.date): a for a in Attendance.query.filter(
            Attendance.user_id.in_({c.user_id for c in dated}),
            Attendance.date.in_({c.date for c in dated}))}
    return {c.id: found.get((c.user_id, c.date)) for c in concerns}


# ---------------------------------------------------------------- admin

@app.route("/admin")
@admin_required
def admin_dashboard():
    today = date.today()
    employees = User.query.filter_by(role="employee", active=True).order_by(User.name).all()
    records = {a.user_id: a for a in Attendance.query.filter_by(date=today)}
    leaves = approved_leaves(today, today)
    rows = []
    for u in employees:
        leave = leaves.get((u.id, today))
        rows.append({"user": u, "record": records.get(u.id),
                     "leave": (leave[0] + (" (half day)" if leave[1] else "")) if leave else None})
    stats = {
        "total": len(employees),
        "present": sum(1 for r in rows if r["record"]),
        "late": sum(1 for r in rows if r["record"] and r["record"].status == "late"),
        "on_leave": sum(1 for r in rows if r["leave"] and not r["record"]),
        "no_face": sum(1 for u in employees if not u.has_face),
    }
    stats["absent"] = stats["total"] - stats["present"] - stats["on_leave"]
    holiday = Holiday.query.filter_by(date=today).first()
    cleanup_old_photos()
    warnings = []
    if uses_old_default_password(current_user):
        # Accounts created by older versions of seed.py used this password
        warnings.append("You are still using the old default admin password. Change it from 'Password' (top right).")
    return render_template("admin_dashboard.html", rows=rows, stats=stats, today=today,
                           holiday=holiday, rules=office_rules_text(), warnings=warnings,
                           kiosk_url=url_for("kiosk", key=Config.KIOSK_KEY, _external=True))


_old_password_checks = {}


def uses_old_default_password(user):
    """Password hashing is slow on purpose, so the answer is remembered per password hash."""
    key = user.password_hash
    if key not in _old_password_checks:
        _old_password_checks.clear()
        _old_password_checks[key] = user.check_password("admin123")
    return _old_password_checks[key]


@app.route("/admin/settings", methods=["GET", "POST"])
@admin_required
def admin_settings():
    if request.method == "POST":
        action = request.form.get("action")
        if action == "add_holiday":
            day = parse_date(request.form.get("date"))
            name = request.form.get("name", "").strip()[:100] or "Holiday"
            if not day:
                flash("Pick a valid date.", "error")
                return redirect(url_for("admin_settings"))
            if Holiday.query.filter_by(date=day).first():
                flash("That date is already a holiday.", "error")
            else:
                db.session.add(Holiday(date=day, name=name))
                db.session.commit()
                flash(f"Holiday added: {name} ({day:%d %b %Y}).", "success")
        elif action == "delete_holiday":
            h = db.get_or_404(Holiday, request.form.get("id", type=int))
            db.session.delete(h)
            db.session.commit()
            flash("Holiday removed.", "success")
        else:
            f = request.form
            try:
                start, end = hm(f["office_start"]), hm(f["office_end"])
                values = {
                    "office_start": f["office_start"],
                    "office_end": f["office_end"],
                    "late_grace_minutes": max(0, int(f["late_grace_minutes"])),
                    "half_day_hours": max(0.0, float(f["half_day_hours"])),
                    "weekly_off": sorted(int(d) for d in f.getlist("weekly_off") if d.isdigit() and 0 <= int(d) <= 6),
                    "scan_cooldown_seconds": max(10, int(f["scan_cooldown_seconds"])),
                    "match_threshold": min(0.7, max(0.3, float(f["match_threshold"]))),
                    "save_photos": "save_photos" in f,
                    "photo_keep_days": min(3650, max(7, int(f["photo_keep_days"]))),
                }
            except (KeyError, ValueError):
                flash("Some values are invalid. Please check the form.", "error")
                return redirect(url_for("admin_settings"))
            if end <= start:
                flash("Office end time must be after start time.", "error")
            else:
                save_settings(values)
                flash("Settings saved. Refresh the kiosk page to apply camera changes.", "success")
        return redirect(url_for("admin_settings"))
    holidays = Holiday.query.order_by(Holiday.date).filter(Holiday.date >= date.today() - timedelta(days=60)).all()
    return render_template("admin_settings.html", s=S(), holidays=holidays,
                           email_on=email_enabled(), email_from=Config.SMTP_FROM,
                           no_email=User.query.filter(User.role == "employee",
                                                      (User.email.is_(None)) | (User.email == "")).count(),
                           weekdays=WEEKDAYS, late_at=late_cutoff())


@app.route("/admin/employees", methods=["GET", "POST"])
@admin_required
def admin_employees():
    if request.method == "POST":
        code = request.form.get("emp_code", "").strip()
        name = request.form.get("name", "").strip()
        password = request.form.get("password", "")
        role = request.form.get("role", "employee")
        if not code or not name or len(password) < 6:
            flash("Employee ID, name and a 6+ character password are required.", "error")
        elif len(code) > 30 or len(name) > 100 or role not in ROLES:
            flash("Employee ID can be at most 30 characters and name at most 100.", "error")
        elif User.query.filter_by(emp_code=code).first():
            flash(f"Employee ID {code} already exists.", "error")
        else:
            u = User(emp_code=code, name=name,
                     email=request.form.get("email", "").strip(),
                     department=request.form.get("department", "").strip(),
                     role=role)
            u.set_password(password)
            db.session.add(u)
            db.session.commit()
            flash(f"{name} added. Now register their face with the camera.", "success")
            return redirect(url_for("admin_employee_face", uid=u.id))
        return redirect(url_for("admin_employees"))
    users = User.query.order_by(User.role, User.name).all()
    return render_template("admin_employees.html", users=users)


@app.route("/admin/employees/<int:uid>", methods=["GET", "POST"])
@admin_required
def admin_employee_edit(uid):
    u = db.get_or_404(User, uid)
    if request.method == "POST":
        action = request.form.get("action")
        if action == "toggle":
            if u.id == current_user.id:
                flash("You cannot deactivate yourself.", "error")
            else:
                u.active = not u.active
                flash(f"{u.name} {'activated' if u.active else 'deactivated'}.", "success")
        elif action == "clear_face":
            FaceSample.query.filter_by(user_id=u.id).delete()
            flash("Face data removed. Employee must register again.", "success")
        elif action == "upload_photo":
            error = save_profile_photo(u, request.files.get("photo"))
            flash(error or "Profile photo saved.", "error" if error else "success")
        elif action == "remove_photo":
            delete_profile_photo(u)
            flash("Profile photo removed.", "success")
        else:
            name = request.form.get("name", "").strip()
            role = request.form.get("role", u.role)
            if not name or len(name) > 100 or role not in ROLES:
                flash("Name is required (at most 100 characters).", "error")
                return redirect(url_for("admin_employee_edit", uid=uid))
            u.name = name
            u.email = request.form.get("email", "").strip()
            u.department = request.form.get("department", "").strip()
            if u.id != current_user.id:
                u.role = role
            if request.form.get("password"):
                if len(request.form["password"]) < 6:
                    flash("Password must be at least 6 characters.", "error")
                    return redirect(url_for("admin_employee_edit", uid=uid))
                u.set_password(request.form["password"])
            flash("Saved.", "success")
        db.session.commit()
        clear_face_cache()
        return redirect(url_for("admin_employee_edit", uid=uid))
    return render_template("admin_employee_edit.html", u=u)


# Profile photos: optional, uploaded by admin. JPEG / PNG / WebP, checked by file content.
PHOTO_TYPES = {b"\xff\xd8\xff": "jpg", b"\x89PNG": "png", b"RIFF": "webp"}


def save_profile_photo(user, file):
    if not file or not file.filename:
        return "Please choose a photo."
    data = file.read()
    ext = next((e for magic, e in PHOTO_TYPES.items() if data.startswith(magic)), None)
    if ext == "webp" and data[8:12] != b"WEBP":
        ext = None
    if not ext:
        return "Only JPG, PNG or WebP images are allowed."
    os.makedirs(Config.PROFILE_DIR, exist_ok=True)
    delete_profile_photo(user)
    name = f"{user.id}_{secrets.token_hex(4)}.{ext}"  # new name each time so browsers show the new photo
    with open(os.path.join(Config.PROFILE_DIR, name), "wb") as f:
        f.write(data)
    user.photo = name
    return None


def delete_profile_photo(user):
    if user.photo:
        try:
            os.remove(os.path.join(Config.PROFILE_DIR, user.photo))
        except OSError:
            pass
        user.photo = None


@app.route("/photos/<int:uid>")
@login_required
def profile_photo(uid):
    if not current_user.is_admin and current_user.id != uid:
        abort(403)
    u = db.get_or_404(User, uid)
    if not u.photo:
        abort(404)
    return send_from_directory(Config.PROFILE_DIR, u.photo, max_age=86400)


@app.route("/admin/employees/<int:uid>/face", methods=["GET", "POST"])
@admin_required
def admin_employee_face(uid):
    u = db.get_or_404(User, uid)
    if request.method == "POST":
        error = save_faces(u, (request.get_json(silent=True) or {}).get("descriptors"))
        if error:
            return jsonify(ok=False, error=error), 400
        return jsonify(ok=True)
    return render_template("register_face.html", target=u,
                           post_url=url_for("admin_employee_face", uid=uid),
                           back_url=url_for("admin_employee_edit", uid=uid))


@app.route("/admin/attendance")
@admin_required
def admin_attendance():
    employees = User.query.filter_by(role="employee").order_by(User.name).all()
    emp_id = request.args.get("employee", type=int)
    if emp_id:
        # Monthly view for one employee
        u = db.get_or_404(User, emp_id)
        year, month = parse_month(request.args.get("month"))
        rows, summary = month_report(u, year, month)
        return render_template("admin_attendance.html", mode="month", employees=employees,
                               selected=u, rows=rows, summary=summary,
                               month_value=f"{year:04d}-{month:02d}",
                               late_at=late_cutoff().strftime("%H:%M"))
    # Daily view for everyone
    day = parse_date(request.args.get("date")) or date.today()
    records = {a.user_id: a for a in Attendance.query.filter_by(date=day)}
    holidays = holidays_between(day, day)
    leaves = approved_leaves(day, day)
    rows = []
    for u in employees:
        rec = records.get(u.id)
        status, flags, holiday = day_info(u, day, rec, holidays, leaves)
        rows.append({"user": u, "record": rec, "status": status, "flags": flags, "holiday": holiday})
    return render_template("admin_attendance.html", mode="day", employees=employees,
                           rows=rows, day=day, selected=None,
                           month_value=day.strftime("%Y-%m"),
                           late_at=late_cutoff().strftime("%H:%M"))


def parse_hhmm(day, value):
    if not value:
        return None
    return datetime.combine(day, datetime.strptime(value, "%H:%M").time())


def apply_attendance(u, day, check_in_str, check_out_str, status=None, note=None):
    """Admin correction. Empty check-in = absent. Returns (ok, message). Does not commit."""
    try:
        check_in = parse_hhmm(day, check_in_str)
        check_out = parse_hhmm(day, check_out_str)
    except ValueError:
        return False, "Invalid time."
    rec = Attendance.query.filter_by(user_id=u.id, date=day).first()
    if not check_in:
        if rec:
            db.session.delete(rec)
        return True, f"{u.name} marked absent on {day:%d %b}."
    if check_out and check_out < check_in:
        return False, "Check-out cannot be before check-in."
    if not rec:
        rec = Attendance(user_id=u.id, date=day, was_absent=True)
        db.session.add(rec)
    elif not rec.edited_at:
        # First correction: keep the camera times so the employee can see what changed
        rec.original_in, rec.original_out = rec.check_in, rec.check_out
    rec.edited_at = datetime.now().replace(microsecond=0)
    rec.check_in, rec.check_out = check_in, check_out
    rec.status = status if status in ("present", "late") else checkin_status(check_in)
    rec.note = note or f"Edited by {current_user.name}"
    return True, f"Attendance updated for {u.name} on {day:%d %b}."


@app.route("/admin/attendance/edit", methods=["POST"])
@admin_required
def admin_attendance_edit():
    u = db.get_or_404(User, request.form.get("user_id", type=int))
    day = parse_date(request.form.get("date"))
    if not day or day > date.today():
        ok, msg = False, "Pick a valid date that is not in the future."
    else:
        ok, msg = apply_attendance(u, day, request.form.get("check_in"),
                                   request.form.get("check_out"), request.form.get("status"))
    if ok:
        db.session.commit()
    flash(msg, "success" if ok else "error")
    return redirect(back_url("admin_attendance"))


@app.route("/admin/attendance/export")
@admin_required
def admin_export():
    year, month = parse_month(request.args.get("month"))
    emp_id = request.args.get("employee", type=int)
    users = [db.get_or_404(User, emp_id)] if emp_id else \
        User.query.filter_by(role="employee").order_by(User.name).all()
    out = io.StringIO()
    w = csv.writer(out)
    w.writerow(["Employee ID", "Name", "Department", "Date", "Day", "Status", "Remarks",
                "Check-in", "Check-out", "Hours"])
    reports = month_reports(users, year, month) if users else {}
    for u in users:
        for r in reports[u.id][0]:
            rec = r["record"]
            notes = [FLAG_LABELS[f] for f in r["flags"]]
            if rec and rec.edited_at:
                notes.append("Corrected by admin")
            remarks = ", ".join(notes) or (r["holiday"] or "")
            w.writerow([csv_safe(u.emp_code), csv_safe(u.name), csv_safe(u.department or ""),
                        r["date"].isoformat(),
                        r["date"].strftime("%a"), status_label(r["status"]), remarks,
                        rec.check_in.strftime("%H:%M") if rec and rec.check_in else "",
                        rec.check_out.strftime("%H:%M") if rec and rec.check_out else "",
                        rec.hours if rec and rec.hours is not None else ""])
    name = f"attendance_{year}-{month:02d}" + (f"_{file_safe(users[0].emp_code)}" if emp_id else "") + ".csv"
    return csv_response(out, name)


def csv_safe(value):
    """Spreadsheet apps run cells that start with = + - @ as formulas. A leading ' stops that."""
    value = str(value)
    return "'" + value if value[:1] in ("=", "+", "-", "@") else value


def file_safe(value):
    return "".join(ch if ch.isalnum() or ch in "-_" else "_" for ch in value)


def csv_response(out, name):
    return Response(out.getvalue(), mimetype="text/csv",
                    headers={"Content-Disposition": f'attachment; filename="{name}"'})


def report_users(year, month):
    """Employees for the monthly summary: active ones, plus inactive ones who have records that month."""
    days = calendar.monthrange(year, month)[1]
    seen = {uid for (uid,) in db.session.query(Attendance.user_id).filter(
        Attendance.date.between(date(year, month, 1), date(year, month, days))).distinct()}
    return [u for u in User.query.filter_by(role="employee").order_by(User.name)
            if u.active or u.id in seen]


@app.route("/admin/reports")
@admin_required
def admin_reports():
    """One line per employee for a month: the numbers needed for payroll."""
    year, month = parse_month(request.args.get("month"))
    users = report_users(year, month)
    reports = month_reports(users, year, month) if users else {}
    rows = [{"user": u, "s": reports[u.id][1]} for u in users]
    return render_template("admin_reports.html", rows=rows,
                           month_value=f"{year:04d}-{month:02d}",
                           month_label=date(year, month, 1).strftime("%B %Y"))


@app.route("/admin/reports/export")
@admin_required
def admin_reports_export():
    year, month = parse_month(request.args.get("month"))
    users = report_users(year, month)
    reports = month_reports(users, year, month) if users else {}
    out = io.StringIO()
    w = csv.writer(out)
    w.writerow(["Employee ID", "Name", "Department", "On time", "Late", "Absent", "On leave",
                "Half days", "Early leaves", "Total hours", "Attendance %"])
    for u in users:
        s = reports[u.id][1]
        w.writerow([csv_safe(u.emp_code), csv_safe(u.name), csv_safe(u.department or ""),
                    s["present"], s["late"], s["absent"], s["leave"], s["half_day"], s["early"],
                    s["hours"], "" if s["percent"] is None else s["percent"]])
    return csv_response(out, f"summary_{year}-{month:02d}.csv")


@app.route("/admin/snapshots/<path:rel>")
@admin_required
def admin_snapshot(rel):
    return send_from_directory(Config.SNAPSHOT_DIR, rel)


@app.route("/admin/concerns")
@admin_required
def admin_concerns():
    status = request.args.get("status", "open")
    q = Concern.query
    if status in ("open", "resolved"):
        q = q.filter_by(status=status)
    concerns = q.order_by(Concern.created_at.desc()).all()
    return render_template("admin_concerns.html", status=status, concerns=concerns,
                           records=concern_records(concerns))


@app.route("/admin/concerns/<int:cid>/fix", methods=["POST"])
@admin_required
def admin_concern_fix(cid):
    """Correct the attendance for the concern's day, reply, and resolve — in one step."""
    c = db.get_or_404(Concern, cid)
    if not c.date:
        flash("This concern has no date.", "error")
        return redirect(url_for("admin_concerns"))
    ok, msg = apply_attendance(c.user, c.date, request.form.get("check_in"), request.form.get("check_out"),
                               note=f"Corrected by {current_user.name} (concern #{c.id})")
    if not ok:
        flash(msg, "error")
        return redirect(url_for("admin_concerns", status=request.args.get("status", "open")))
    rec = Attendance.query.filter_by(user_id=c.user_id, date=c.date).first()
    default_reply = (f"Corrected: check-in {fmt_time(rec.check_in)}, check-out {fmt_time(rec.check_out)}."
                     if rec else "Marked absent for this day.")
    c.admin_reply = request.form.get("reply", "").strip() or default_reply
    c.status = "resolved"
    db.session.commit()
    notify(c.user, "Your attendance concern was resolved",
           f"Your concern \"{c.subject}\" for {c.date:%d %b %Y} was resolved.\n\nAdmin's reply: {c.admin_reply}")
    flash(f"{msg} Concern resolved.", "success")
    return redirect(url_for("admin_concerns", status=request.args.get("status", "open")))


@app.route("/admin/concerns/<int:cid>", methods=["POST"])
@admin_required
def admin_concern_reply(cid):
    c = db.get_or_404(Concern, cid)
    new_reply = request.form.get("reply", "").strip()
    changed = bool(new_reply) and new_reply != (c.admin_reply or "")
    c.admin_reply = new_reply or c.admin_reply
    old_status = c.status
    c.status = "resolved" if request.form.get("action") == "resolve" else "open"
    db.session.commit()
    if changed or (c.status == "resolved" and old_status != "resolved"):
        state = "was resolved" if c.status == "resolved" else "has a new reply"
        notify(c.user, f"Your attendance concern {state}",
               f"Your concern \"{c.subject}\" {state}."
               + (f"\n\nAdmin's reply: {c.admin_reply}" if c.admin_reply else ""))
    flash("Concern updated.", "success")
    return redirect(back_url("admin_concerns"))


# ---------------------------------------------------------------- email notifications

def email_enabled():
    return bool(Config.SMTP_HOST and Config.SMTP_FROM)


def _deliver(to, subject, body):
    """Sends one email over SMTP. Returns None on success or an error message."""
    msg = EmailMessage()
    msg["From"], msg["To"], msg["Subject"] = Config.SMTP_FROM, to, subject
    msg.set_content(body)
    try:
        context = ssl.create_default_context()
        if Config.SMTP_USE_SSL:
            server = smtplib.SMTP_SSL(Config.SMTP_HOST, Config.SMTP_PORT, context=context, timeout=20)
        else:
            server = smtplib.SMTP(Config.SMTP_HOST, Config.SMTP_PORT, timeout=20)
            server.starttls(context=context)
        with server:
            if Config.SMTP_USER:
                server.login(Config.SMTP_USER, Config.SMTP_PASSWORD)
            server.send_message(msg)
        return None
    except (OSError, smtplib.SMTPException) as e:
        app.logger.warning("Email to %s failed: %s", to, e)
        return str(e)


def notify(user, subject, body):
    """Emails an employee if email is set up and they have an address. Never blocks the page."""
    if not email_enabled() or not user.email:
        return
    body = f"Hi {user.name},\n\n{body}\n\nLog in to see the details: {request.host_url}\n\n— FaceAttend"
    if Config.EMAIL_ASYNC:
        threading.Thread(target=_deliver, args=(user.email, subject, body), daemon=True).start()
    else:
        _deliver(user.email, subject, body)


def leave_dates_text(lv):
    text = f"{lv.start_date:%d %b %Y}"
    if lv.end_date != lv.start_date:
        text += f" to {lv.end_date:%d %b %Y}"
    return text + (" (half day)" if lv.half_day else "")


@app.route("/admin/settings/test-email", methods=["POST"])
@admin_required
def admin_test_email():
    to = request.form.get("to", "").strip()
    if not email_enabled():
        flash("Email is not set up. Add the SMTP settings first (see README).", "error")
    elif not to:
        flash("Enter an email address.", "error")
    else:
        error = _deliver(to, "FaceAttend test email", "Email notifications are working.")
        flash(f"Could not send: {error}" if error else f"Test email sent to {to}.",
              "error" if error else "success")
    return redirect(url_for("admin_settings"))


# ---------------------------------------------------------------- leave requests

MAX_LEAVE_DAYS = 60
LEAVE_BACKDATE_DAYS = 30  # employee can ask for leave up to this many days in the past


@app.route("/me/leaves", methods=["GET", "POST"])
@login_required
def my_leaves():
    if request.method == "POST":
        f = request.form
        if f.get("action") == "cancel":
            lv = db.get_or_404(LeaveRequest, f.get("id", type=int))
            if lv.user_id != current_user.id or lv.status != "pending":
                abort(403)
            lv.status = "cancelled"
            db.session.commit()
            flash("Leave request cancelled.", "success")
            return redirect(url_for("my_leaves"))

        start = parse_date(f.get("start_date"))
        end = parse_date(f.get("end_date") or f.get("start_date"))
        if not start or not end:
            flash("Please choose valid dates.", "error")
            return redirect(url_for("my_leaves"))
        leave_type = f.get("leave_type")
        reason = f.get("reason", "").strip()[:1000]
        half_day = f.get("half_day") == "1"
        today = date.today()
        overlap = LeaveRequest.query.filter(
            LeaveRequest.user_id == current_user.id,
            LeaveRequest.status.in_(["pending", "approved"]),
            LeaveRequest.start_date <= end, LeaveRequest.end_date >= start).first()
        if leave_type not in LEAVE_TYPES or not reason:
            flash("Leave type and reason are required.", "error")
        elif end < start:
            flash("End date cannot be before start date.", "error")
        elif half_day and end != start:
            flash("Half-day leave can only be for one day. Use the same From and To date.", "error")
        elif start < today - timedelta(days=LEAVE_BACKDATE_DAYS):
            flash(f"You can only ask for leave up to {LEAVE_BACKDATE_DAYS} days in the past.", "error")
        elif (end - start).days + 1 > MAX_LEAVE_DAYS:
            flash(f"One request can be at most {MAX_LEAVE_DAYS} days.", "error")
        elif working_days(start, end) == 0:
            flash("These dates are all weekly off or holidays. No leave needed.", "error")
        elif overlap:
            flash(f"You already have a {overlap.status} leave from "
                  f"{overlap.start_date:%d %b} to {overlap.end_date:%d %b}.", "error")
        else:
            db.session.add(LeaveRequest(user_id=current_user.id, start_date=start, end_date=end,
                                        leave_type=leave_type, reason=reason, half_day=half_day))
            db.session.commit()
            flash("Leave request sent to admin.", "success")
        return redirect(url_for("my_leaves"))

    leaves = LeaveRequest.query.filter_by(user_id=current_user.id).order_by(LeaveRequest.created_at.desc()).all()
    # Only the part of each leave that falls inside this calendar year is counted
    year_start, year_end = date(date.today().year, 1, 1), date(date.today().year, 12, 31)
    taken = sum(0.5 if lv.half_day else working_days(max(lv.start_date, year_start), min(lv.end_date, year_end))
                for lv in leaves
                if lv.status == "approved" and lv.end_date >= year_start and lv.start_date <= year_end)
    return render_template("user_leaves.html", leaves=leaves, taken=taken,
                           working_days=working_days, today=date.today(),
                           min_date=date.today() - timedelta(days=LEAVE_BACKDATE_DAYS))


@app.route("/admin/leaves")
@admin_required
def admin_leaves():
    status = request.args.get("status", "pending")
    q = LeaveRequest.query
    if status in ("pending", "approved", "rejected", "cancelled"):
        q = q.filter_by(status=status)
    leaves = q.order_by(LeaveRequest.start_date.desc()).all()
    # Days of attendance already recorded inside each request (useful before approving)
    worked = {lv.id: 0 for lv in leaves}
    if leaves:
        # One query for the whole page instead of one per request
        scanned = {}
        for uid, day in db.session.query(Attendance.user_id, Attendance.date).filter(
                Attendance.user_id.in_({lv.user_id for lv in leaves}),
                Attendance.date.between(min(lv.start_date for lv in leaves),
                                        max(lv.end_date for lv in leaves))):
            scanned.setdefault(uid, []).append(day)
        for lv in leaves:
            worked[lv.id] = sum(1 for d in scanned.get(lv.user_id, ()) if lv.start_date <= d <= lv.end_date)
    return render_template("admin_leaves.html", leaves=leaves, status=status,
                           worked=worked, working_days=working_days)


@app.route("/admin/leaves/<int:lid>", methods=["POST"])
@admin_required
def admin_leave_decide(lid):
    lv = db.get_or_404(LeaveRequest, lid)
    action = request.form.get("action")
    if action not in ("approve", "reject"):
        abort(400)
    if lv.status == "cancelled":
        flash("The employee cancelled this request.", "error")
        return redirect(url_for("admin_leaves"))
    lv.status = "approved" if action == "approve" else "rejected"
    lv.admin_reply = request.form.get("reply", "").strip()[:500] or None
    lv.decided_by = current_user.name
    lv.decided_at = datetime.now().replace(microsecond=0)
    db.session.commit()
    notify(lv.user, f"Your leave request was {lv.status}",
           f"Your {LEAVE_TYPES.get(lv.leave_type, 'leave').lower()} for {leave_dates_text(lv)} "
           f"was {lv.status} by {lv.decided_by}."
           + (f"\n\nAdmin's reply: {lv.admin_reply}" if lv.admin_reply else ""))
    flash(f"Leave {lv.status} for {lv.user.name} "
          f"({lv.start_date:%d %b} – {lv.end_date:%d %b}).", "success")
    return redirect(url_for("admin_leaves", status=request.args.get("status", "pending")))


# ---------------------------------------------------------------- kiosk

def kiosk_key_ok(value):
    """Compares in constant time, so the key cannot be guessed from response timing."""
    return isinstance(value, str) and secrets.compare_digest(value.encode(), Config.KIOSK_KEY.encode())


@app.route("/kiosk")
def kiosk():
    if not kiosk_key_ok(request.args.get("key")):
        abort(403)
    return render_template("kiosk.html", key=Config.KIOSK_KEY, save_photos=bool(S()["save_photos"]))


@app.route("/api/kiosk/scan", methods=["POST"])
def kiosk_scan():
    data = request.get_json(silent=True)
    if not isinstance(data, dict) or not kiosk_key_ok(data.get("key")):
        abort(403)
    descriptor = data.get("descriptor")
    if not valid_descriptor(descriptor):
        return jsonify(ok=False, error="bad descriptor"), 400

    s = S()
    cleanup_old_photos()
    user_id, dist = best_match(descriptor)
    if user_id is None or dist > float(s["match_threshold"]):
        return jsonify(ok=True, matched=False)

    user = db.session.get(User, user_id)
    now = datetime.now().replace(microsecond=0)
    rec = Attendance.query.filter_by(user_id=user.id, date=now.date()).first()
    base = {"ok": True, "matched": True, "name": user.name, "emp_code": user.emp_code,
            "confidence": round(max(0.0, 1 - dist) * 100)}

    if rec and rec.last_scan and (now - rec.last_scan) < timedelta(seconds=int(s["scan_cooldown_seconds"])):
        return jsonify(**base, action="repeat",
                       time=(rec.check_out or rec.check_in).strftime("%I:%M %p"))

    if not rec:
        rec = Attendance(user_id=user.id, date=now.date(), check_in=now,
                         status=checkin_status(now), last_scan=now,
                         check_in_photo=save_snapshot(data.get("photo"), user, "in", now))
        db.session.add(rec)
        action = "check_in"
    elif rec.edited_at and rec.check_out:
        # Admin already set the final times for today; a later camera scan must not change them
        rec.last_scan = now
        db.session.commit()
        return jsonify(**base, action="locked", time=rec.check_out.strftime("%I:%M %p"))
    else:
        rec.check_out = now
        rec.last_scan = now
        rec.check_out_photo = save_snapshot(data.get("photo"), user, "out", now) or rec.check_out_photo
        action = "check_out"
    try:
        db.session.commit()
    except IntegrityError:
        # Two scans of the same person arrived together; the other one already saved the record
        db.session.rollback()
        return jsonify(**base, action="repeat", time=now.strftime("%I:%M %p"))
    early = action == "check_out" and now.time() < hm(s["office_end"])
    return jsonify(**base, action=action, status=rec.status, early=early,
                   time=now.strftime("%I:%M %p"))


# ---------------------------------------------------------------- setup

@app.cli.command("create-admin")
def create_admin_cmd():
    """flask create-admin  ->  creates default admin if none exists."""
    from seed import create_default_admin
    create_default_admin()


def upgrade_db():
    """Creates new tables and adds columns that older databases are missing."""
    db.create_all()
    new_columns = {
        "attendance": {"check_in_photo": "VARCHAR(200)", "check_out_photo": "VARCHAR(200)",
                       "edited_at": "DATETIME", "was_absent": "BOOLEAN DEFAULT 0",
                       "original_in": "DATETIME", "original_out": "DATETIME"},
        "concern": {"requested_in": "VARCHAR(5)", "requested_out": "VARCHAR(5)"},
        "user": {"photo": "VARCHAR(100)"},
        "leave_request": {"half_day": "BOOLEAN DEFAULT 0"},
    }
    with db.engine.begin() as conn:
        for table, columns in new_columns.items():
            existing = {c["name"] for c in inspect(conn).get_columns(table)}
            for col, sql_type in columns.items():
                if col not in existing:
                    conn.execute(text(f"ALTER TABLE {table} ADD COLUMN {col} {sql_type}"))
        # Indexes that databases made by older versions do not have yet
        for name, target in {"ix_attendance_date": "attendance (date)",
                             "ix_face_sample_user_id": "face_sample (user_id)",
                             "ix_leave_request_user_id": "leave_request (user_id)",
                             "ix_concern_user_id": "concern (user_id)"}.items():
            conn.execute(text(f"CREATE INDEX IF NOT EXISTS {name} ON {target}"))


with app.app_context():
    upgrade_db()


if __name__ == "__main__":
    # Defaults are safe for one computer. Set HOST=0.0.0.0 to allow other computers on the network,
    # and FLASK_DEBUG=1 only while developing.
    app.run(host=os.environ.get("HOST", "127.0.0.1"),
            port=int(os.environ.get("PORT", "5000")),
            debug=os.environ.get("FLASK_DEBUG") == "1")
