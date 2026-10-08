#!/usr/bin/env python3
"""
Digital Blood Donor Directory - web server.

Run:  python server.py            (opens http://localhost:8000)
      python server.py 9000       (custom port)

Pure Python standard library: no packages to install.
"""
import json
import mimetypes
import os
import re
import secrets
import sys
import time
from datetime import datetime, timedelta, timezone
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import parse_qs, urlparse

import database as db

STATIC_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), "static")
COOKIE = "bdd_session"
SESSION_DAYS = 7
EMAIL_RE = re.compile(r"^[^@\s]+@[^@\s]+\.[^@\s]+$")
PHONE_RE = re.compile(r"^[6-9]\d{9}$")

FAILED_LOGINS = {}              # (ip, email) -> (count, first_failure_time)
LOCK_AFTER, LOCK_SECONDS = 5, 300


class ApiError(Exception):
    def __init__(self, status, message, field=None):
        super().__init__(message)
        self.status, self.message, self.field = status, message, field


class Ctx:
    def __init__(self, handler, conn, user, match, query, body):
        self.handler, self.conn, self.user = handler, conn, user
        self.m, self.q, self.body = match, query, body
        self.out_headers = []

    def require(self, *roles):
        if not self.user:
            raise ApiError(401, "Please sign in to continue.")
        if roles and self.user["role"] not in roles:
            raise ApiError(403, "Your account type can't do that.")
        return self.user


# ------------------------------------------------------------- validation
def text(body, key, label, lo=1, hi=100, required=True):
    v = body.get(key)
    v = "" if v is None else str(v).strip()
    if not v:
        if required:
            raise ApiError(400, f"{label} is required.", key)
        return None
    if not lo <= len(v) <= hi:
        raise ApiError(400, f"{label} must be {lo}-{hi} characters.", key)
    return v


def integer(body, key, label, lo, hi, required=True):
    v = body.get(key)
    if v in (None, ""):
        if required:
            raise ApiError(400, f"{label} is required.", key)
        return None
    try:
        n = int(v)
    except (TypeError, ValueError):
        raise ApiError(400, f"{label} must be a number.", key)
    if not lo <= n <= hi:
        raise ApiError(400, f"{label} must be between {lo} and {hi}.", key)
    return n


def parse_date(body, key, label, required=True, allow_future=False):
    v = body.get(key)
    if v in (None, ""):
        if required:
            raise ApiError(400, f"{label} is required.", key)
        return None
    try:
        d = db.date.fromisoformat(str(v))
    except ValueError:
        raise ApiError(400, f"{label} must be a valid date.", key)
    if d > db.today() and not allow_future:
        raise ApiError(400, f"{label} can't be in the future.", key)
    if d.year < 1990:
        raise ApiError(400, f"{label} looks incorrect.", key)
    return d.isoformat()


def phone(body, key="contact_no", label="Mobile number"):
    v = re.sub(r"[\s-]", "", str(body.get(key) or ""))
    v = v[3:] if v.startswith("+91") else v
    if not PHONE_RE.match(v):
        raise ApiError(400, f"{label} must be a 10-digit Indian mobile number.", key)
    return v


def group_of(body, key="blood_group"):
    g = str(body.get(key) or "").strip().upper()
    if g not in db.GROUP_ID:
        raise ApiError(400, "Choose a valid blood group.", key)
    return g


def sector_of(conn, body, key="sector_id"):
    sid = integer(body, key, "Sector", 1, 10**9)
    if not conn.execute("SELECT 1 FROM sectors WHERE sector_id=?", (sid,)).fetchone():
        raise ApiError(400, "Choose a sector from the list.", key)
    return sid


# --------------------------------------------------------------- sessions
def start_session(ctx, user_id):
    token = secrets.token_urlsafe(32)
    expires = (datetime.now(timezone.utc).replace(tzinfo=None) + timedelta(days=SESSION_DAYS)).strftime("%Y-%m-%d %H:%M:%S")
    ctx.conn.execute("DELETE FROM sessions WHERE expires_at < datetime('now')")
    ctx.conn.execute("INSERT INTO sessions (token,user_id,expires_at) VALUES (?,?,?)",
                     (token, user_id, expires))
    ctx.out_headers.append(("Set-Cookie",
        f"{COOKIE}={token}; Path=/; HttpOnly; SameSite=Lax; Max-Age={SESSION_DAYS * 86400}"))


def user_json(conn, u):
    out = {"user_id": u["user_id"], "full_name": u["full_name"], "email": u["email"], "role": u["role"]}
    if u["role"] == "donor":
        d = conn.execute(db.DONOR_SELECT + " WHERE d.user_id=?", (u["user_id"],)).fetchone()
        if d:
            out["donor_id"] = d["donor_id"]
            out["blood_group"] = d["group_name"]
            out["can_donate"] = d["status"] == "Available" and db.eligibility(d["last_donation_date"])["eligible"]
    return out


# ----------------------------------------------------------------- routes
ROUTES = []


def route(method, pattern):
    def deco(fn):
        ROUTES.append((method, re.compile(pattern), fn))
        return fn
    return deco


@route("GET", r"/api/meta")
def meta(c):
    cities = []
    for city in c.conn.execute("SELECT * FROM cities ORDER BY city_name"):
        sectors = c.conn.execute(
            "SELECT sector_id, sector_name FROM sectors WHERE city_id=? ORDER BY sector_name",
            (city["city_id"],)).fetchall()
        cities.append({"city_id": city["city_id"], "city_name": city["city_name"],
                       "sectors": [dict(s) for s in sectors]})
    totals = c.conn.execute(
        f"SELECT COUNT(*) n, SUM(CASE WHEN d.status='Available' AND (d.last_donation_date IS NULL "
        f"OR d.last_donation_date <= ?) THEN 1 ELSE 0 END) a FROM donors d", (db.cutoff_date(),)).fetchone()
    return {"cities": cities, "blood_groups": db.BLOOD_GROUPS, "compat": db.DONOR_COMPAT, "eligibility_days": db.ELIGIBILITY_DAYS,
            "totals": {"donors": totals["n"], "available": totals["a"] or 0}}


@route("GET", r"/api/sector-stats")
def sector_stats(c):
    city_id = int(c.q.get("city_id") or 0)
    group = c.q.get("blood_group")
    params, join = [db.cutoff_date()], ""
    if group in db.GROUP_ID:
        groups = db.DONOR_COMPAT[group] if c.q.get("compatible") == "1" else [group]
        ids = [db.GROUP_ID[g] for g in groups]
        join = f" AND d.blood_group_id IN ({','.join('?' * len(ids))})"
        params += ids
    params.append(city_id)
    rows = c.conn.execute(
        f"""SELECT s.sector_id, s.sector_name, COUNT(d.donor_id) AS total,
                   COALESCE(SUM(CASE WHEN d.status='Available' AND
                        (d.last_donation_date IS NULL OR d.last_donation_date <= ?) THEN 1 ELSE 0 END),0) AS available
              FROM sectors s LEFT JOIN donors d ON d.sector_id = s.sector_id {join}
             WHERE s.city_id = ? GROUP BY s.sector_id ORDER BY s.sector_name""", params).fetchall()
    return {"sectors": [dict(r) for r in rows]}


@route("GET", r"/api/search")
def search(c):
    group = group_of(c.q, "blood_group")
    sector_id, city_id = c.q.get("sector_id"), c.q.get("city_id")
    if sector_id:
        ids = [int(sector_id)]
    elif city_id:
        ids = [r[0] for r in c.conn.execute("SELECT sector_id FROM sectors WHERE city_id=?", (int(city_id),))]
    else:
        raise ApiError(400, "Choose a city or sector to search.")
    if not ids:
        return {"donors": [], "hidden": 0}
    donors, hidden = db.search_donors(c.conn, group, ids, c.q.get("compatible") == "1")
    return {"donors": donors, "hidden": hidden, "eligibility_days": db.ELIGIBILITY_DAYS}


@route("POST", r"/api/register")
def register(c):
    b = c.body
    role = b.get("role")
    if role not in ("donor", "recipient"):
        raise ApiError(400, "Choose whether you're registering as a donor or a recipient.", "role")
    name = text(b, "full_name", "Full name", 2, 80)
    email = text(b, "email", "Email", 5, 120).lower()
    if not EMAIL_RE.match(email):
        raise ApiError(400, "Enter a valid email address.", "email")
    pw = str(b.get("password") or "")
    if len(pw) < 8 or not re.search(r"[A-Za-z]", pw) or not re.search(r"\d", pw):
        raise ApiError(400, "Password needs at least 8 characters with a letter and a number.", "password")
    if c.conn.execute("SELECT 1 FROM users WHERE email=?", (email,)).fetchone():
        raise ApiError(409, "An account with this email already exists. Try signing in.", "email")
    donor = None
    if role == "donor":
        age = integer(b, "age", "Age", 1, 100)
        if age < 18:
            raise ApiError(400, "Donors must be 18 or older.", "age")
        weight = integer(b, "weight_kg", "Weight", 1, 250)
        if weight < 45:
            raise ApiError(400, "Donors must weigh at least 45 kg.", "weight_kg")
        donor = dict(group=group_of(b), sector=sector_of(c.conn, b), phone=phone(b), age=age, weight=weight,
                     last=parse_date(b, "last_donation_date", "Last donation date", required=False))
        if c.conn.execute("SELECT 1 FROM donors WHERE contact_no=?", (donor["phone"],)).fetchone():
            raise ApiError(409, "This mobile number is already registered.", "contact_no")
    uid = c.conn.execute(
        "INSERT INTO users (full_name,email,password_hash,role) VALUES (?,?,?,?)",
        (name, email, db.hash_password(pw), role)).lastrowid
    if donor:
        c.conn.execute(
            """INSERT INTO donors (user_id,blood_group_id,sector_id,contact_no,age,weight_kg,last_donation_date)
               VALUES (?,?,?,?,?,?,?)""",
            (uid, db.GROUP_ID[donor["group"]], donor["sector"], donor["phone"], donor["age"],
             donor["weight"], donor["last"]))
    start_session(c, uid)
    user = c.conn.execute("SELECT * FROM users WHERE user_id=?", (uid,)).fetchone()
    return 201, {"user": user_json(c.conn, user)}


@route("POST", r"/api/login")
def login(c):
    email = str(c.body.get("email") or "").strip().lower()
    pw = str(c.body.get("password") or "")
    key = (c.handler.client_address[0], email)
    count, first = FAILED_LOGINS.get(key, (0, 0))
    if count >= LOCK_AFTER and time.time() - first < LOCK_SECONDS:
        raise ApiError(429, "Too many failed attempts. Please wait 5 minutes and try again.")
    user = c.conn.execute("SELECT * FROM users WHERE email=?", (email,)).fetchone()
    if not user or not db.verify_password(pw, user["password_hash"]):
        if time.time() - first >= LOCK_SECONDS:
            count, first = 0, time.time()
        FAILED_LOGINS[key] = (count + 1, first or time.time())
        raise ApiError(401, "Email or password is incorrect.")
    FAILED_LOGINS.pop(key, None)
    start_session(c, user["user_id"])
    return {"user": user_json(c.conn, user)}


@route("POST", r"/api/logout")
def logout(c):
    cookie = c.handler.headers.get("Cookie", "")
    m = re.search(rf"{COOKIE}=([\w-]+)", cookie)
    if m:
        c.conn.execute("DELETE FROM sessions WHERE token=?", (m.group(1),))
    c.out_headers.append(("Set-Cookie", f"{COOKIE}=; Path=/; HttpOnly; SameSite=Lax; Max-Age=0"))
    return {"ok": True}


@route("GET", r"/api/me")
def me(c):
    return {"user": user_json(c.conn, c.user) if c.user else None}


# ---- contact reveal ------------------------------------------------------
@route("POST", r"/api/donors/(\d+)/contact")
def reveal_contact(c):
    u = c.require("recipient", "admin")
    did = int(c.m.group(1))
    row = c.conn.execute(db.DONOR_SELECT + " WHERE d.donor_id=?", (did,)).fetchone()
    if not row:
        raise ApiError(404, "Donor not found.")
    allowed = u["role"] == "admin"
    if not allowed:
        visible = c.conn.execute(f"SELECT 1 FROM donors d WHERE d.donor_id=? AND {db.VISIBLE_SQL}",
                                 (did, db.cutoff_date())).fetchone()
        responded = c.conn.execute(
            """SELECT 1 FROM request_responses rr JOIN emergency_requests er ON er.request_id=rr.request_id
                WHERE rr.donor_id=? AND er.requester_id=?""", (did, u["user_id"])).fetchone()
        allowed = bool(visible or responded)
    if not allowed:
        raise ApiError(409, "This donor is no longer available.")
    c.conn.execute("INSERT INTO contact_log (viewer_id, donor_id) VALUES (?,?)", (u["user_id"], did))
    return {"donor_id": did, "full_name": row["full_name"], "contact_no": row["contact_no"]}


# ---- donor self-service --------------------------------------------------
def donor_row(c):
    u = c.require("donor")
    row = c.conn.execute(db.DONOR_SELECT + " WHERE d.user_id=?", (u["user_id"],)).fetchone()
    if not row:
        raise ApiError(404, "Donor profile not found.")
    return row


def donor_profile(c):
    r = donor_row(c)
    extra = c.conn.execute("SELECT age, weight_kg, sector_id FROM donors WHERE donor_id=?",
                           (r["donor_id"],)).fetchone()
    city = c.conn.execute("SELECT city_id FROM sectors WHERE sector_id=?", (extra["sector_id"],)).fetchone()
    elig = db.eligibility(r["last_donation_date"])
    history = c.conn.execute(
        "SELECT donation_date, volume_ml, location, status FROM donation_history "
        "WHERE donor_id=? ORDER BY donation_date DESC LIMIT 20", (r["donor_id"],)).fetchall()
    stats = c.conn.execute(
        "SELECT COUNT(*) n, COALESCE(SUM(volume_ml),0) ml FROM donation_history WHERE donor_id=? AND status='Completed'",
        (r["donor_id"],)).fetchone()
    return {
        "profile": {"donor_id": r["donor_id"], "full_name": r["full_name"], "blood_group": r["group_name"],
                    "sector": r["sector_name"], "city": r["city_name"], "sector_id": extra["sector_id"],
                    "city_id": city["city_id"], "contact_no": r["contact_no"], "age": extra["age"],
                    "weight_kg": extra["weight_kg"], "status": r["status"]},
        "eligibility": elig,
        "visible_in_search": elig["eligible"] and r["status"] == "Available",
        "history": [dict(h) for h in history],
        "stats": {"donations": stats["n"], "total_ml": stats["ml"]},
        "eligibility_days": db.ELIGIBILITY_DAYS,
    }


@route("GET", r"/api/donor/me")
def donor_me(c):
    return donor_profile(c)


@route("PATCH", r"/api/donor/status")
def donor_status(c):
    r = donor_row(c)
    s = c.body.get("status")
    if s not in ("Available", "Busy", "Traveling"):
        raise ApiError(400, "Choose Available, Busy or Traveling.", "status")
    c.conn.execute("UPDATE donors SET status=? WHERE donor_id=?", (s, r["donor_id"]))
    return donor_profile(c)


@route("PUT", r"/api/donor/profile")
def donor_update(c):
    r = donor_row(c)
    b = c.body
    sid = sector_of(c.conn, b)
    ph = phone(b)
    weight = integer(b, "weight_kg", "Weight", 1, 250)
    if weight < 45:
        raise ApiError(400, "Donors must weigh at least 45 kg.", "weight_kg")
    if c.conn.execute("SELECT 1 FROM donors WHERE contact_no=? AND donor_id<>?", (ph, r["donor_id"])).fetchone():
        raise ApiError(409, "This mobile number is already registered.", "contact_no")
    c.conn.execute("UPDATE donors SET sector_id=?, contact_no=?, weight_kg=? WHERE donor_id=?",
                   (sid, ph, weight, r["donor_id"]))
    return donor_profile(c)


@route("POST", r"/api/donor/donations")
def donor_record_donation(c):
    r = donor_row(c)
    b = c.body
    when = parse_date(b, "donation_date", "Donation date")
    vol = integer(b, "volume_ml", "Volume", 200, 500)
    place = text(b, "location", "Place of donation", 2, 100)
    d = db.date.fromisoformat(when)
    for h in c.conn.execute("SELECT donation_date FROM donation_history WHERE donor_id=? AND status='Completed'",
                            (r["donor_id"],)):
        gap = abs((d - db.date.fromisoformat(h["donation_date"])).days)
        if gap < db.ELIGIBILITY_DAYS:
            raise ApiError(400, f"Donations must be at least {db.ELIGIBILITY_DAYS} days apart "
                                f"(you have one on {h['donation_date']}).", "donation_date")
    c.conn.execute("INSERT INTO donation_history (donor_id,donation_date,volume_ml,location,status) "
                   "VALUES (?,?,?,?,'Completed')", (r["donor_id"], when, vol, place))
    return 201, donor_profile(c)


# ---- notifications -------------------------------------------------------
@route("GET", r"/api/notifications")
def notifications(c):
    u = c.require()
    rows = c.conn.execute(
        "SELECT notification_id, request_id, message, is_read, created_at FROM notifications "
        "WHERE user_id=? ORDER BY notification_id DESC LIMIT 30", (u["user_id"],)).fetchall()
    unread = c.conn.execute("SELECT COUNT(*) FROM notifications WHERE user_id=? AND is_read=0",
                            (u["user_id"],)).fetchone()[0]
    return {"notifications": [dict(r) for r in rows], "unread": unread}


@route("POST", r"/api/notifications/read")
def notifications_read(c):
    u = c.require()
    c.conn.execute("UPDATE notifications SET is_read=1 WHERE user_id=?", (u["user_id"],))
    return {"ok": True}


# ---- emergency requests --------------------------------------------------
REQUEST_SELECT = """
    SELECT er.request_id, er.patient_name, er.hospital, er.units, er.urgency_level, er.status,
           er.created_at, er.requester_id, er.sector_id, bg.group_name, s.sector_name, c.city_name,
           (SELECT COUNT(*) FROM request_responses rr WHERE rr.request_id = er.request_id) AS responders
      FROM emergency_requests er
      JOIN blood_groups bg ON bg.blood_group_id = er.blood_group_id
      JOIN sectors s ON s.sector_id = er.sector_id
      JOIN cities c ON c.city_id = s.city_id
"""


def request_json(c, r, private=False):
    out = {"request_id": r["request_id"], "blood_group": r["group_name"], "units": r["units"],
           "urgency": r["urgency_level"], "hospital": r["hospital"], "sector": r["sector_name"],
           "city": r["city_name"], "created_at": r["created_at"], "status": r["status"],
           "verified": r["status"] == "Verified", "responders": r["responders"]}
    if private:
        out["patient_name"] = r["patient_name"]
    if c.user and c.user["role"] == "donor":
        d = c.conn.execute("SELECT donor_id FROM donors WHERE user_id=?", (c.user["user_id"],)).fetchone()
        out["responded"] = bool(d and c.conn.execute(
            "SELECT 1 FROM request_responses WHERE request_id=? AND donor_id=?",
            (r["request_id"], d["donor_id"])).fetchone())
    return out


@route("GET", r"/api/requests/open")
def open_requests(c):
    sql = REQUEST_SELECT + (" WHERE er.status IN ('Pending','Verified') "
                            "AND er.created_at >= datetime('now','-72 hours')")
    params = []
    if c.q.get("city_id"):
        sql += " AND s.city_id = ?"
        params.append(int(c.q["city_id"]))
    sql += (" ORDER BY CASE er.urgency_level WHEN 'Critical' THEN 0 WHEN 'Urgent' THEN 1 ELSE 2 END,"
            " er.created_at DESC LIMIT 50")
    return {"requests": [request_json(c, r) for r in c.conn.execute(sql, params)]}


@route("GET", r"/api/requests/mine")
def my_requests(c):
    u = c.require("recipient", "admin")
    rows = c.conn.execute(REQUEST_SELECT + " WHERE er.requester_id=? ORDER BY er.created_at DESC",
                          (u["user_id"],)).fetchall()
    return {"requests": [request_json(c, r, private=True) for r in rows]}


@route("POST", r"/api/requests")
def post_request(c):
    u = c.require("recipient", "admin")
    b = c.body
    patient = text(b, "patient_name", "Patient name", 2, 80)
    group = group_of(b)
    sid = sector_of(c.conn, b)
    hospital = text(b, "hospital", "Hospital", 3, 100)
    units = integer(b, "units", "Units required", 1, 10)
    urgency = b.get("urgency_level")
    if urgency not in ("Critical", "Urgent", "Planned"):
        raise ApiError(400, "Choose how urgent the need is.", "urgency_level")
    ph = phone(b, "contact_no", "Contact number")
    note = text(b, "note", "Note", 1, 300, required=False)
    rid, notified = db.create_request(c.conn, u["user_id"], patient, group, sid, hospital, units, urgency, ph, note)
    return 201, {"request_id": rid, "notified": notified}


def load_request(c, rid):
    r = c.conn.execute(REQUEST_SELECT + " WHERE er.request_id=?", (rid,)).fetchone()
    if not r:
        raise ApiError(404, "Request not found.")
    return r


@route("POST", r"/api/requests/(\d+)/respond")
def respond(c):
    u = c.require("donor")
    r = load_request(c, int(c.m.group(1)))
    if r["status"] not in ("Pending", "Verified"):
        raise ApiError(409, "This request is closed.")
    d = c.conn.execute(db.DONOR_SELECT + " WHERE d.user_id=?", (u["user_id"],)).fetchone()
    if d["group_name"] not in db.DONOR_COMPAT[r["group_name"]]:
        raise ApiError(409, f"{d['group_name']} blood can't be given to a {r['group_name']} patient.")
    if not db.eligibility(d["last_donation_date"])["eligible"]:
        raise ApiError(409, f"You're inside the {db.ELIGIBILITY_DAYS}-day window after your last donation.")
    if d["status"] != "Available":
        raise ApiError(409, "Set your status to Available before responding.")
    try:
        c.conn.execute("INSERT INTO request_responses (request_id, donor_id) VALUES (?,?)",
                       (r["request_id"], d["donor_id"]))
    except Exception:
        raise ApiError(409, "You've already responded to this request.")
    c.conn.execute("INSERT INTO notifications (user_id, request_id, message) VALUES (?,?,?)",
                   (r["requester_id"], r["request_id"],
                    f"{db.short_name(d['full_name'])} ({d['group_name']}, {d['sector_name']}) can donate for "
                    f"{r['hospital']}. Open My account to view contact details."))
    return {"ok": True}


@route("GET", r"/api/requests/(\d+)/responses")
def request_responses(c):
    u = c.require("recipient", "admin")
    r = load_request(c, int(c.m.group(1)))
    if u["role"] != "admin" and r["requester_id"] != u["user_id"]:
        raise ApiError(403, "That request belongs to another account.")
    rows = c.conn.execute(
        db.DONOR_SELECT + " JOIN request_responses rr ON rr.donor_id = d.donor_id "
        "WHERE rr.request_id=? ORDER BY rr.responded_at", (r["request_id"],)).fetchall()
    return {"donors": [db.public_donor(x) for x in rows]}


@route("POST", r"/api/requests/(\d+)/status")
def request_status(c):
    u = c.require("recipient", "admin")
    r = load_request(c, int(c.m.group(1)))
    new = c.body.get("status")
    allowed = ("Verified", "Fulfilled", "Closed") if u["role"] == "admin" else ("Fulfilled", "Closed")
    if new not in allowed:
        raise ApiError(403, "You can't set that status.")
    if u["role"] != "admin" and r["requester_id"] != u["user_id"]:
        raise ApiError(403, "That request belongs to another account.")
    c.conn.execute("UPDATE emergency_requests SET status=? WHERE request_id=?", (new, r["request_id"]))
    if new == "Verified":
        c.conn.execute("INSERT INTO notifications (user_id, request_id, message) VALUES (?,?,?)",
                       (r["requester_id"], r["request_id"],
                        f"Your request for {r['group_name']} at {r['hospital']} was verified by an administrator."))
    return {"ok": True, "status": new}


# ---- admin ---------------------------------------------------------------
@route("GET", r"/api/admin/overview")
def admin_overview(c):
    c.require("admin")
    q = lambda sql, *p: c.conn.execute(sql, p).fetchone()[0]
    counts = {
        "donors": q("SELECT COUNT(*) FROM donors"),
        "available": q(f"SELECT COUNT(*) FROM donors d WHERE {db.VISIBLE_SQL}", db.cutoff_date()),
        "recipients": q("SELECT COUNT(*) FROM users WHERE role='recipient'"),
        "open_requests": q("SELECT COUNT(*) FROM emergency_requests WHERE status IN ('Pending','Verified')"),
        "pending": q("SELECT COUNT(*) FROM emergency_requests WHERE status='Pending'"),
        "contact_views": q("SELECT COUNT(*) FROM contact_log"),
    }
    reqs = c.conn.execute(REQUEST_SELECT + " ORDER BY er.created_at DESC LIMIT 30").fetchall()
    donors = c.conn.execute(db.DONOR_SELECT + " ORDER BY d.donor_id DESC LIMIT 40").fetchall()
    out_donors = []
    for r in donors:
        e = db.eligibility(r["last_donation_date"])
        out_donors.append({"donor_id": r["donor_id"], "full_name": r["full_name"], "blood_group": r["group_name"],
                           "sector": r["sector_name"], "city": r["city_name"], "status": r["status"],
                           "eligible": e["eligible"], "days_since": e["days_since"]})
    return {"counts": counts, "requests": [request_json(c, r, private=True) for r in reqs],
            "donors": out_donors}


@route("POST", r"/api/admin/sectors")
def admin_add_sector(c):
    c.require("admin")
    b = c.body
    sector = text(b, "sector_name", "Sector name", 2, 60)
    if b.get("city_id"):
        cid = integer(b, "city_id", "City", 1, 10**9)
        if not c.conn.execute("SELECT 1 FROM cities WHERE city_id=?", (cid,)).fetchone():
            raise ApiError(400, "City not found.", "city_id")
    else:
        name = text(b, "city_name", "City name", 2, 60)
        row = c.conn.execute("SELECT city_id FROM cities WHERE city_name=? COLLATE NOCASE", (name,)).fetchone()
        cid = row["city_id"] if row else c.conn.execute("INSERT INTO cities (city_name) VALUES (?)", (name,)).lastrowid
    try:
        c.conn.execute("INSERT INTO sectors (city_id, sector_name) VALUES (?,?)", (cid, sector))
    except Exception:
        raise ApiError(409, "That sector already exists in this city.", "sector_name")
    return 201, {"ok": True}


@route("DELETE", r"/api/admin/sectors/(\d+)")
def admin_delete_sector(c):
    c.require("admin")
    sid = int(c.m.group(1))
    used = c.conn.execute("SELECT (SELECT COUNT(*) FROM donors WHERE sector_id=?) + "
                          "(SELECT COUNT(*) FROM emergency_requests WHERE sector_id=?)", (sid, sid)).fetchone()[0]
    if used:
        raise ApiError(409, "This sector still has donors or requests, so it can't be removed.")
    c.conn.execute("DELETE FROM sectors WHERE sector_id=?", (sid,))
    return {"ok": True}


# ---------------------------------------------------------------- handler
SECURITY_HEADERS = [
    ("X-Content-Type-Options", "nosniff"),
    ("X-Frame-Options", "DENY"),
    ("Referrer-Policy", "same-origin"),
    ("Content-Security-Policy",
     "default-src 'self'; img-src 'self' data:; style-src 'self' 'unsafe-inline'; script-src 'self'; frame-ancestors 'none'"),
]


class Handler(BaseHTTPRequestHandler):
    server_version = "BloodDonorDirectory/1.0"

    def log_message(self, fmt, *args):
        if "favicon" not in self.path:
            sys.stderr.write("  %s %s\n" % (self.command, self.path.split("?")[0]))

    # -- plumbing
    def send_json(self, status, payload, extra=()):
        data = json.dumps(payload).encode()
        self.send_response(status)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(data)))
        self.send_header("Cache-Control", "no-store")
        for k, v in (*SECURITY_HEADERS, *extra):
            self.send_header(k, v)
        self.end_headers()
        if self.command != "HEAD":
            self.wfile.write(data)

    def current_user(self, conn):
        m = re.search(rf"{COOKIE}=([\w-]+)", self.headers.get("Cookie", ""))
        if not m:
            return None
        return conn.execute(
            """SELECT u.* FROM sessions s JOIN users u ON u.user_id = s.user_id
                WHERE s.token=? AND s.expires_at > datetime('now')""", (m.group(1),)).fetchone()

    def dispatch(self):
        parsed = urlparse(self.path)
        if parsed.path.startswith("/api/"):
            return self.api(parsed)
        if self.command not in ("GET", "HEAD"):
            return self.send_json(405, {"error": "Method not allowed."})
        return self.static(parsed.path)

    do_GET = do_POST = do_PUT = do_PATCH = do_DELETE = do_HEAD = dispatch

    def api(self, parsed):
        match = fn = None
        path_known = False
        for method, rx, f in ROUTES:
            m = rx.fullmatch(parsed.path)
            if m:
                path_known = True
                if method == self.command:
                    match, fn = m, f
                    break
        if not fn:
            return self.send_json(405 if path_known else 404, {"error": "Not found."})
        body = {}
        if self.command in ("POST", "PUT", "PATCH", "DELETE"):
            length = int(self.headers.get("Content-Length") or 0)
            if length > 65536:
                return self.send_json(413, {"error": "Request too large."})
            if length:
                if "application/json" not in (self.headers.get("Content-Type") or ""):
                    return self.send_json(415, {"error": "Send JSON."})
                try:
                    body = json.loads(self.rfile.read(length))
                    assert isinstance(body, dict)
                except Exception:
                    return self.send_json(400, {"error": "Invalid request body."})
        query = {k: v[0] for k, v in parse_qs(parsed.query).items()}
        conn = db.connect()
        try:
            ctx = Ctx(self, conn, self.current_user(conn), match, query, body)
            result = fn(ctx)
            status, payload = result if isinstance(result, tuple) else (200, result)
            conn.commit()
            self.send_json(status, payload, ctx.out_headers)
        except ApiError as e:
            conn.rollback()
            self.send_json(e.status, {"error": e.message, "field": e.field})
        except Exception as e:                                   # pragma: no cover
            conn.rollback()
            sys.stderr.write(f"  ERROR {self.path}: {e!r}\n")
            self.send_json(500, {"error": "Something went wrong on our side. Please try again."})
        finally:
            conn.close()

    def static(self, path):
        path = "/index.html" if path in ("/", "") else path
        full = os.path.normpath(os.path.join(STATIC_DIR, path.lstrip("/")))
        if not full.startswith(STATIC_DIR) or not os.path.isfile(full):
            full = os.path.join(STATIC_DIR, "index.html")        # SPA fallback
        ctype = mimetypes.guess_type(full)[0] or "application/octet-stream"
        with open(full, "rb") as f:
            data = f.read()
        self.send_response(200)
        self.send_header("Content-Type", ctype + ("; charset=utf-8" if ctype.startswith("text/") or "javascript" in ctype else ""))
        self.send_header("Content-Length", str(len(data)))
        self.send_header("Cache-Control", "no-cache")
        for k, v in SECURITY_HEADERS:
            self.send_header(k, v)
        self.end_headers()
        if self.command != "HEAD":
            self.wfile.write(data)


def main():
    port = int(sys.argv[1]) if len(sys.argv) > 1 else int(os.environ.get("PORT", 8000))
    conn = db.connect()
    db.init_db(conn)
    conn.close()
    httpd = ThreadingHTTPServer(("0.0.0.0", port), Handler)
    print(f"\n  Digital Blood Donor Directory is running")
    print(f"  Open  http://localhost:{port}  in your browser   (Ctrl+C to stop)\n")
    try:
        httpd.serve_forever()
    except KeyboardInterrupt:
        print("\n  Server stopped.")


if __name__ == "__main__":
    main()
