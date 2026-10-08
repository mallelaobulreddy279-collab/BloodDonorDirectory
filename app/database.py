"""
Digital Blood Donor Directory - database layer (SQLite).

Contains the schema (3NF), seed data, password hashing, the 90-day
eligibility rule, blood-group compatibility and the sector-based search.
Uses only the Python standard library.
"""
import hashlib
import hmac
import os
import random
import secrets
import sqlite3
from datetime import date, datetime, timedelta, timezone

BASE_DIR = os.path.dirname(os.path.abspath(__file__))
DB_PATH = os.environ.get("BDD_DB", os.path.join(BASE_DIR, "data", "blood_donor_directory.db"))

ELIGIBILITY_DAYS = 90          # 90-Day Automated Eligibility Rule (SRS F3)
PBKDF2_ROUNDS = 200_000

BLOOD_GROUPS = ["A+", "A-", "B+", "B-", "AB+", "AB-", "O+", "O-"]
GROUP_ID = {g: i + 1 for i, g in enumerate(BLOOD_GROUPS)}

# recipient group -> donor groups that can safely give red cells
DONOR_COMPAT = {
    "A+": ["A+", "A-", "O+", "O-"],
    "A-": ["A-", "O-"],
    "B+": ["B+", "B-", "O+", "O-"],
    "B-": ["B-", "O-"],
    "AB+": ["AB+", "AB-", "A+", "A-", "B+", "B-", "O+", "O-"],
    "AB-": ["AB-", "A-", "B-", "O-"],
    "O+": ["O+", "O-"],
    "O-": ["O-"],
}

SCHEMA = """
PRAGMA foreign_keys = ON;

CREATE TABLE IF NOT EXISTS cities (
    city_id     INTEGER PRIMARY KEY AUTOINCREMENT,
    city_name   TEXT NOT NULL UNIQUE
);

CREATE TABLE IF NOT EXISTS sectors (
    sector_id   INTEGER PRIMARY KEY AUTOINCREMENT,
    city_id     INTEGER NOT NULL REFERENCES cities(city_id) ON DELETE CASCADE,
    sector_name TEXT NOT NULL,
    UNIQUE (city_id, sector_name)
);

CREATE TABLE IF NOT EXISTS blood_groups (
    blood_group_id INTEGER PRIMARY KEY,
    group_name     TEXT NOT NULL UNIQUE
);

CREATE TABLE IF NOT EXISTS users (
    user_id       INTEGER PRIMARY KEY AUTOINCREMENT,
    full_name     TEXT NOT NULL,
    email         TEXT NOT NULL UNIQUE COLLATE NOCASE,
    password_hash TEXT NOT NULL,
    role          TEXT NOT NULL CHECK (role IN ('donor','recipient','admin')),
    created_at    TEXT NOT NULL DEFAULT (datetime('now'))
);

CREATE TABLE IF NOT EXISTS donors (
    donor_id           INTEGER PRIMARY KEY AUTOINCREMENT,
    user_id            INTEGER NOT NULL UNIQUE REFERENCES users(user_id) ON DELETE CASCADE,
    blood_group_id     INTEGER NOT NULL REFERENCES blood_groups(blood_group_id),
    sector_id          INTEGER NOT NULL REFERENCES sectors(sector_id),
    contact_no         TEXT NOT NULL UNIQUE,
    age                INTEGER NOT NULL CHECK (age >= 18),
    weight_kg          INTEGER NOT NULL CHECK (weight_kg >= 45),
    last_donation_date TEXT,
    status             TEXT NOT NULL DEFAULT 'Available'
                       CHECK (status IN ('Available','Busy','Traveling')),
    registered_on      TEXT NOT NULL DEFAULT (datetime('now'))
);
CREATE INDEX IF NOT EXISTS idx_donors_group_sector ON donors (blood_group_id, sector_id);

CREATE TABLE IF NOT EXISTS donation_history (
    donation_id   INTEGER PRIMARY KEY AUTOINCREMENT,
    donor_id      INTEGER NOT NULL REFERENCES donors(donor_id) ON DELETE CASCADE,
    donation_date TEXT NOT NULL,
    volume_ml     INTEGER NOT NULL CHECK (volume_ml BETWEEN 200 AND 500),
    location      TEXT NOT NULL,
    status        TEXT NOT NULL DEFAULT 'Completed'
                  CHECK (status IN ('Completed','Screened','Pending'))
);

-- Real-time update: a completed donation refreshes the donor's last_donation_date
CREATE TRIGGER IF NOT EXISTS trg_donation_updates_last
AFTER INSERT ON donation_history
WHEN NEW.status = 'Completed'
BEGIN
    UPDATE donors
       SET last_donation_date = (SELECT MAX(donation_date) FROM donation_history
                                  WHERE donor_id = NEW.donor_id AND status = 'Completed')
     WHERE donor_id = NEW.donor_id;
END;

CREATE TABLE IF NOT EXISTS emergency_requests (
    request_id    INTEGER PRIMARY KEY AUTOINCREMENT,
    requester_id  INTEGER NOT NULL REFERENCES users(user_id) ON DELETE CASCADE,
    patient_name  TEXT NOT NULL,
    blood_group_id INTEGER NOT NULL REFERENCES blood_groups(blood_group_id),
    sector_id     INTEGER NOT NULL REFERENCES sectors(sector_id),
    hospital      TEXT NOT NULL,
    units         INTEGER NOT NULL CHECK (units BETWEEN 1 AND 10),
    urgency_level TEXT NOT NULL CHECK (urgency_level IN ('Critical','Urgent','Planned')),
    contact_no    TEXT NOT NULL,
    note          TEXT,
    status        TEXT NOT NULL DEFAULT 'Pending'
                  CHECK (status IN ('Pending','Verified','Fulfilled','Closed')),
    created_at    TEXT NOT NULL DEFAULT (datetime('now'))
);
CREATE INDEX IF NOT EXISTS idx_requests_sector ON emergency_requests (sector_id, status);

CREATE TABLE IF NOT EXISTS request_responses (
    response_id  INTEGER PRIMARY KEY AUTOINCREMENT,
    request_id   INTEGER NOT NULL REFERENCES emergency_requests(request_id) ON DELETE CASCADE,
    donor_id     INTEGER NOT NULL REFERENCES donors(donor_id) ON DELETE CASCADE,
    responded_at TEXT NOT NULL DEFAULT (datetime('now')),
    UNIQUE (request_id, donor_id)
);

CREATE TABLE IF NOT EXISTS notifications (
    notification_id INTEGER PRIMARY KEY AUTOINCREMENT,
    user_id    INTEGER NOT NULL REFERENCES users(user_id) ON DELETE CASCADE,
    request_id INTEGER REFERENCES emergency_requests(request_id) ON DELETE CASCADE,
    message    TEXT NOT NULL,
    is_read    INTEGER NOT NULL DEFAULT 0,
    created_at TEXT NOT NULL DEFAULT (datetime('now'))
);
CREATE INDEX IF NOT EXISTS idx_notifications_user ON notifications (user_id, is_read);

-- Audit trail: who revealed which donor's phone number
CREATE TABLE IF NOT EXISTS contact_log (
    log_id    INTEGER PRIMARY KEY AUTOINCREMENT,
    viewer_id INTEGER NOT NULL REFERENCES users(user_id) ON DELETE CASCADE,
    donor_id  INTEGER NOT NULL REFERENCES donors(donor_id) ON DELETE CASCADE,
    viewed_at TEXT NOT NULL DEFAULT (datetime('now'))
);

CREATE TABLE IF NOT EXISTS sessions (
    token      TEXT PRIMARY KEY,
    user_id    INTEGER NOT NULL REFERENCES users(user_id) ON DELETE CASCADE,
    expires_at TEXT NOT NULL
);
"""


# ----------------------------------------------------------------- helpers
def connect():
    os.makedirs(os.path.dirname(DB_PATH), exist_ok=True)
    conn = sqlite3.connect(DB_PATH, timeout=10)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys = ON")
    conn.execute("PRAGMA journal_mode = WAL")
    return conn


def hash_password(password, rounds=PBKDF2_ROUNDS):
    salt = secrets.token_hex(16)
    digest = hashlib.pbkdf2_hmac("sha256", password.encode(), bytes.fromhex(salt), rounds)
    return f"pbkdf2${rounds}${salt}${digest.hex()}"


def verify_password(password, stored):
    try:
        _, rounds, salt, digest = stored.split("$")
        test = hashlib.pbkdf2_hmac("sha256", password.encode(), bytes.fromhex(salt), int(rounds))
        return hmac.compare_digest(test.hex(), digest)
    except (ValueError, AttributeError):
        return False


def today():
    return date.today()


def cutoff_date():
    """Donors whose last donation is on/before this date are eligible again."""
    return (today() - timedelta(days=ELIGIBILITY_DAYS)).isoformat()


def eligibility(last_donation_date):
    """The 90-day rule (SRS F3). Returns a plain dict used by API + UI."""
    if not last_donation_date:
        return {"eligible": True, "days_since": None, "days_left": 0,
                "eligible_on": None, "last_donation_date": None}
    last = date.fromisoformat(last_donation_date)
    days_since = (today() - last).days
    eligible_on = last + timedelta(days=ELIGIBILITY_DAYS)
    return {
        "eligible": days_since >= ELIGIBILITY_DAYS,
        "days_since": days_since,
        "days_left": max(0, ELIGIBILITY_DAYS - days_since),
        "eligible_on": eligible_on.isoformat(),
        "last_donation_date": last_donation_date,
    }


def mask_phone(number):
    return f"{number[:2]}••• ••{number[-3:]}"


def short_name(full_name):
    parts = full_name.split()
    if len(parts) == 1:
        return parts[0]
    return f"{parts[0]} {parts[-1][0]}."


def public_donor(row, requested_group=None):
    """Donor as shown to the public: first name + initial, masked phone."""
    elig = eligibility(row["last_donation_date"])
    return {
        "donor_id": row["donor_id"],
        "display_name": short_name(row["full_name"]),
        "blood_group": row["group_name"],
        "sector": row["sector_name"],
        "city": row["city_name"],
        "status": row["status"],
        "last_donation_date": row["last_donation_date"],
        "days_since": elig["days_since"],
        "masked_contact": mask_phone(row["contact_no"]),
        "exact_match": requested_group is None or row["group_name"] == requested_group,
    }


DONOR_SELECT = """
    SELECT d.donor_id, u.full_name, bg.group_name, s.sector_name, c.city_name,
           d.last_donation_date, d.status, d.contact_no
      FROM donors d
      JOIN users u ON u.user_id = d.user_id
      JOIN blood_groups bg ON bg.blood_group_id = d.blood_group_id
      JOIN sectors s ON s.sector_id = d.sector_id
      JOIN cities c ON c.city_id = s.city_id
"""
VISIBLE_SQL = "d.status = 'Available' AND (d.last_donation_date IS NULL OR d.last_donation_date <= ?)"


def search_donors(conn, group, sector_ids, compatible=False):
    """Sector-based search. Hidden donors (busy / inside 90 days) are counted, not listed."""
    groups = DONOR_COMPAT[group] if compatible else [group]
    gids = [GROUP_ID[g] for g in groups]
    gq = ",".join("?" * len(gids))
    sq = ",".join("?" * len(sector_ids))
    base = f" WHERE d.blood_group_id IN ({gq}) AND d.sector_id IN ({sq})"
    rows = conn.execute(
        DONOR_SELECT + base + f" AND {VISIBLE_SQL} ORDER BY (bg.group_name = ?) DESC, "
        "d.last_donation_date IS NOT NULL, d.last_donation_date, u.full_name",
        [*gids, *sector_ids, cutoff_date(), group],
    ).fetchall()
    total = conn.execute(
        "SELECT COUNT(*) FROM donors d" + base, [*gids, *sector_ids]
    ).fetchone()[0]
    donors = [public_donor(r, group) for r in rows]
    return donors, total - len(donors)


def notify_donors_for_request(conn, request_id, requester_id):
    """Emergency broadcast (SRS F5): alert eligible, compatible donors in the sector."""
    r = conn.execute(
        """SELECT er.*, bg.group_name, s.sector_name, c.city_name
             FROM emergency_requests er
             JOIN blood_groups bg ON bg.blood_group_id = er.blood_group_id
             JOIN sectors s ON s.sector_id = er.sector_id
             JOIN cities c ON c.city_id = s.city_id
            WHERE er.request_id = ?""", (request_id,)).fetchone()
    gids = [GROUP_ID[g] for g in DONOR_COMPAT[r["group_name"]]]
    gq = ",".join("?" * len(gids))
    donors = conn.execute(
        f"""SELECT d.user_id FROM donors d
             WHERE d.sector_id = ? AND d.blood_group_id IN ({gq})
               AND d.status = 'Available'
               AND (d.last_donation_date IS NULL OR d.last_donation_date <= ?)
               AND d.user_id <> ?""",
        [r["sector_id"], *gids, cutoff_date(), requester_id]).fetchall()
    unit_word = "unit" if r["units"] == 1 else "units"
    msg = (f"{r['urgency_level']}: {r['units']} {unit_word} of {r['group_name']} needed at "
           f"{r['hospital']}, {r['sector_name']}, {r['city_name']}. Open Live requests to respond.")
    conn.executemany(
        "INSERT INTO notifications (user_id, request_id, message) VALUES (?,?,?)",
        [(d["user_id"], request_id, msg) for d in donors])
    return len(donors)


def create_request(conn, requester_id, patient_name, group, sector_id, hospital,
                   units, urgency, contact_no, note=None):
    cur = conn.execute(
        """INSERT INTO emergency_requests
           (requester_id, patient_name, blood_group_id, sector_id, hospital, units,
            urgency_level, contact_no, note)
           VALUES (?,?,?,?,?,?,?,?,?)""",
        (requester_id, patient_name, GROUP_ID[group], sector_id, hospital, units,
         urgency, contact_no, note))
    rid = cur.lastrowid
    return rid, notify_donors_for_request(conn, rid, requester_id)


# -------------------------------------------------------------------- seed
CITIES = {
    "Madanapalle": ["Downtown", "Industrial Area", "North Sector", "East Sector",
                    "South Sector", "Medical Quarter"],
    "Tirupati": ["Temple District", "Railway Colony", "University Sector",
                 "Airport Road", "Old Town", "Renigunta Belt"],
    "Chittoor": ["Collectorate Area", "Market Yard", "Gandhi Road",
                 "Bypass Industrial Estate", "Green Valley", "Hospital Road"],
}
SECTOR_WEIGHTS = {"Downtown": 9, "Industrial Area": 5, "North Sector": 5,
                  "East Sector": 4, "South Sector": 4, "Medical Quarter": 4}
FIRST = ["Ravi", "Sita", "Anil", "Priya", "Harika", "Suresh", "Divya", "Kiran", "Lakshmi",
         "Venkatesh", "Swathi", "Naveen", "Pooja", "Imran", "Fathima", "Charan", "Manasa",
         "Rahul", "Bhavana", "Sai", "Meena", "Arjun", "Keerthana", "Mohan", "Anusha",
         "Prasad", "Deepika", "Ramesh", "Sneha", "Vamsi", "Tejaswi", "Gopal", "Nandini"]
LAST = ["Kumar", "Devi", "Reddy", "Sharma", "Naidu", "Yadav", "Rao", "Prasad", "Goud",
        "Chowdary", "Singh", "Basha", "Begum", "Verma", "Naik", "Das", "Babu", "Raju"]
GROUP_WEIGHTS = {"O+": 35, "B+": 28, "A+": 20, "AB+": 6, "O-": 3, "B-": 3, "A-": 3, "AB-": 2}

DEMO_PASSWORDS = {"admin": "Admin@123", "recipient": "Demo@123", "donor": "Donor@123"}


def init_db(conn):
    conn.executescript(SCHEMA)
    conn.executemany("INSERT OR IGNORE INTO blood_groups (blood_group_id, group_name) VALUES (?,?)",
                     [(GROUP_ID[g], g) for g in BLOOD_GROUPS])
    conn.commit()
    seed(conn)


def seed(conn):
    if conn.execute("SELECT COUNT(*) FROM users").fetchone()[0]:
        return
    rnd = random.Random(2026)
    sector_ids = {}
    for city, sectors in CITIES.items():
        cid = conn.execute("INSERT INTO cities (city_name) VALUES (?)", (city,)).lastrowid
        for s in sectors:
            sector_ids[(city, s)] = conn.execute(
                "INSERT INTO sectors (city_id, sector_name) VALUES (?,?)", (cid, s)).lastrowid

    donor_hash = hash_password(DEMO_PASSWORDS["donor"])
    admin_id = conn.execute(
        "INSERT INTO users (full_name,email,password_hash,role) VALUES (?,?,?,?)",
        ("Directory Admin", "admin@blooddonor.local", hash_password(DEMO_PASSWORDS["admin"]), "admin")
    ).lastrowid
    recipient_id = conn.execute(
        "INSERT INTO users (full_name,email,password_hash,role) VALUES (?,?,?,?)",
        ("Rajesh Kumar", "recipient@demo.in", hash_password(DEMO_PASSWORDS["recipient"]), "recipient")
    ).lastrowid

    used_phones, used_names = set(), set()
    quotas = [("Madanapalle", 30), ("Tirupati", 14), ("Chittoor", 8)]
    plan = [c for c, n in quotas for _ in range(n)]
    for i, city in enumerate(plan, start=1):
        if i == 1:      # demo donor inside the 90-day window
            name, group, sector, days_ago = "Ravi Kumar", "O+", "Downtown", 34
        elif i == 2:    # demo donor who is eligible
            name, group, sector, days_ago = "Sita Devi", "A+", "Downtown", None
        else:
            while True:
                name = f"{rnd.choice(FIRST)} {rnd.choice(LAST)}"
                if name not in used_names:
                    break
            group = rnd.choices(list(GROUP_WEIGHTS), weights=GROUP_WEIGHTS.values())[0]
            if city == "Madanapalle":
                sector = rnd.choices(list(SECTOR_WEIGHTS), weights=SECTOR_WEIGHTS.values())[0]
            else:
                sector = rnd.choice(CITIES[city])
            days_ago = None if rnd.random() < 0.2 else rnd.randint(8, 420)
        used_names.add(name)
        while True:
            phone = f"90000{rnd.randint(10000, 99999)}"
            if phone not in used_phones:
                break
        used_phones.add(phone)
        uid = conn.execute(
            "INSERT INTO users (full_name,email,password_hash,role) VALUES (?,?,?,?)",
            (name, f"donor{i}@demo.in", donor_hash, "donor")).lastrowid
        status = rnd.choices(["Available", "Busy", "Traveling"], weights=[88, 7, 5])[0] if i > 2 else "Available"
        did = conn.execute(
            """INSERT INTO donors (user_id, blood_group_id, sector_id, contact_no, age, weight_kg, status)
               VALUES (?,?,?,?,?,?,?)""",
            (uid, GROUP_ID[group], sector_ids[(city, sector)], phone,
             rnd.randint(19, 52), rnd.randint(50, 88), status)).lastrowid
        if days_ago is not None:
            for k in range(rnd.randint(1, 3)):
                when = today() - timedelta(days=days_ago + k * rnd.randint(100, 160))
                place = (f"{city} Government Blood Bank" if rnd.random() < 0.6
                         else f"Voluntary Camp, {city}")
                conn.execute(
                    "INSERT INTO donation_history (donor_id,donation_date,volume_ml,location,status) "
                    "VALUES (?,?,?,?,'Completed')",
                    (did, when.isoformat(), rnd.choice([350, 450]), place))

    demo_requests = [
        ("Lakshmi Narayana", "B+", ("Madanapalle", "Downtown"), "Madanapalle Area Hospital", 2, "Critical", 5),
        ("Baby of Anitha", "O+", ("Madanapalle", "Medical Quarter"), "Sunrise Multispeciality Hospital", 1, "Urgent", 20),
        ("Mohammed Rafi", "A-", ("Tirupati", "University Sector"), "University Medical Centre", 3, "Urgent", 52),
    ]
    for n, (patient, group, loc, hospital, units, urgency, hours_ago) in enumerate(demo_requests):
        rid, _ = create_request(conn, recipient_id, patient, group, sector_ids[loc], hospital,
                                units, urgency, "9000099001", "Please call before arriving.")
        stamp = (datetime.now(timezone.utc).replace(tzinfo=None) - timedelta(hours=hours_ago)).strftime("%Y-%m-%d %H:%M:%S")
        conn.execute("UPDATE emergency_requests SET created_at=?, status=? WHERE request_id=?",
                     (stamp, "Verified" if n != 1 else "Pending", rid))
        conn.execute("UPDATE notifications SET created_at=? WHERE request_id=?", (stamp, rid))
    conn.commit()
