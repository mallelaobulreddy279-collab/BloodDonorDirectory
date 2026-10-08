"""
Automated test suite for the Digital Blood Donor Directory.

Run from the project root:   python tests/test_api.py -v
Each test name starts with its test-case ID (TC01...) from the Project Protocol.
Uses a throw-away database, so your real data is never touched.
"""
import http.cookiejar
import json
import os
import sqlite3
import sys
import tempfile
import threading
import time
import unittest
import urllib.error
import urllib.request
from datetime import date, timedelta
from http.server import ThreadingHTTPServer

TMP = tempfile.mkdtemp()
os.environ["BDD_DB"] = os.path.join(TMP, "test.db")
sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "app"))

import database as db      # noqa: E402
import server              # noqa: E402


class Client:
    def __init__(self, base):
        self.base = base
        self.jar = http.cookiejar.CookieJar()
        self.op = urllib.request.build_opener(urllib.request.HTTPCookieProcessor(self.jar))

    def call(self, method, path, body=None, raw=None, ctype="application/json"):
        data = raw if raw is not None else (json.dumps(body).encode() if body is not None else None)
        req = urllib.request.Request(self.base + path, data=data, method=method,
                                     headers={"Content-Type": ctype} if data is not None else {})
        try:
            with self.op.open(req) as r:
                payload = r.read()
                return r.status, (json.loads(payload) if r.headers.get_content_type() == "application/json" else payload), r.headers
        except urllib.error.HTTPError as e:
            payload = e.read()
            try:
                return e.code, json.loads(payload), e.headers
            except ValueError:
                return e.code, payload, e.headers

    def login(self, email, pw):
        s, d, _ = self.call("POST", "/api/login", {"email": email, "password": pw})
        assert s == 200, d
        return d


class DirectoryTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        conn = db.connect()
        db.init_db(conn)
        conn.close()
        cls.httpd = ThreadingHTTPServer(("127.0.0.1", 0), server.Handler)
        cls.base = f"http://127.0.0.1:{cls.httpd.server_address[1]}"
        threading.Thread(target=cls.httpd.serve_forever, daemon=True).start()
        server.Handler.log_message = lambda *a, **k: None

    @classmethod
    def tearDownClass(cls):
        cls.httpd.shutdown()

    # ---- helpers
    def client(self):
        return Client(self.base)

    def sql(self, query, *params):
        conn = db.connect()
        try:
            rows = conn.execute(query, params).fetchall()
            conn.commit()
            return rows
        finally:
            conn.close()

    def sector_id(self, city, sector):
        return self.sql("SELECT s.sector_id FROM sectors s JOIN cities c USING(city_id) "
                        "WHERE c.city_name=? AND s.sector_name=?", city, sector)[0][0]

    def new_donor_payload(self, **kw):
        n = int(time.time() * 1000) % 10**8
        base = dict(role="donor", full_name="Test Donor", email=f"t{n}@test.in", password="Passw0rd!",
                    blood_group="O+", sector_id=self.sector_id("Madanapalle", "Downtown"),
                    contact_no=f"98{n:08d}", age=25, weight_kg=60)
        base.update(kw)
        return base

    # ---- tests
    def test_TC01_city_sector_hierarchy(self):
        s, d, _ = self.client().call("GET", "/api/meta")
        self.assertEqual(s, 200)
        madanapalle = next(c for c in d["cities"] if c["city_name"] == "Madanapalle")
        self.assertIn("Downtown", [x["sector_name"] for x in madanapalle["sectors"]])
        other = next(c for c in d["cities"] if c["city_name"] == "Tirupati")
        self.assertNotIn("Downtown", [x["sector_name"] for x in other["sectors"]])

    def test_TC02_search_is_sector_specific(self):
        sid = self.sector_id("Madanapalle", "Downtown")
        s, d, _ = self.client().call("GET", f"/api/search?blood_group=B%2B&sector_id={sid}")
        self.assertEqual(s, 200)
        self.assertTrue(d["donors"])
        self.assertTrue(all(x["sector"] == "Downtown" and x["city"] == "Madanapalle" for x in d["donors"]))

    def test_TC03_donor_inside_90_days_is_hidden(self):
        sid = self.sector_id("Madanapalle", "Downtown")
        s, d, _ = self.client().call("GET", f"/api/search?blood_group=O%2B&sector_id={sid}")
        names = [x["display_name"] for x in d["donors"]]
        self.assertNotIn("Ravi K.", names)           # donor1 gave blood 34 days ago
        self.assertGreaterEqual(d["hidden"], 1)

    def test_TC04_boundary_89_vs_90_days(self):
        sid = self.sector_id("Chittoor", "Green Valley")
        client = self.client()
        for days, expect in ((89, False), (90, True)):
            payload = self.new_donor_payload(blood_group="AB-", sector_id=sid,
                                             last_donation_date=(date.today() - timedelta(days=days)).isoformat())
            c = self.client()
            s, d, _ = c.call("POST", "/api/register", payload)
            self.assertEqual(s, 201, d)
            s, res, _ = client.call("GET", f"/api/search?blood_group=AB-&sector_id={sid}")
            self.assertEqual(any(x["donor_id"] for x in res["donors"] if x["last_donation_date"] == payload["last_donation_date"]), expect,
                             f"{days} days since donation")

    def test_TC05_manual_available_cannot_override_rule(self):
        c = self.client()
        c.login("donor1@demo.in", "Donor@123")
        s, d, _ = c.call("PATCH", "/api/donor/status", {"status": "Available"})
        self.assertEqual(s, 200)
        self.assertFalse(d["eligibility"]["eligible"])
        self.assertFalse(d["visible_in_search"])

    def test_TC06_busy_status_hides_donor(self):
        c = self.client()
        c.login("donor2@demo.in", "Donor@123")       # Sita Devi, A+, Downtown, eligible
        sid = self.sector_id("Madanapalle", "Downtown")
        try:
            c.call("PATCH", "/api/donor/status", {"status": "Busy"})
            _, d, _ = self.client().call("GET", f"/api/search?blood_group=A%2B&sector_id={sid}")
            self.assertNotIn("Sita D.", [x["display_name"] for x in d["donors"]])
        finally:
            c.call("PATCH", "/api/donor/status", {"status": "Available"})
        _, d, _ = self.client().call("GET", f"/api/search?blood_group=A%2B&sector_id={sid}")
        self.assertIn("Sita D.", [x["display_name"] for x in d["donors"]])

    def test_TC07_guest_sees_masked_numbers_only(self):
        sid = self.sector_id("Madanapalle", "Downtown")
        s, d, _ = self.client().call("GET", f"/api/search?blood_group=B%2B&sector_id={sid}")
        blob = json.dumps(d)
        self.assertNotIn("9000", blob.replace("90•••", ""))
        self.assertTrue(all("•" in x["masked_contact"] for x in d["donors"]))
        self.assertTrue(all("contact_no" not in x for x in d["donors"]))

    def test_TC08_guest_cannot_reveal_contact(self):
        s, d, _ = self.client().call("POST", "/api/donors/3/contact", {})
        self.assertEqual(s, 401)

    def test_TC09_donor_cannot_reveal_contact(self):
        c = self.client()
        c.login("donor1@demo.in", "Donor@123")
        s, d, _ = c.call("POST", "/api/donors/3/contact", {})
        self.assertEqual(s, 403)

    def test_TC10_recipient_reveals_contact_and_is_logged(self):
        c = self.client()
        c.login("recipient@demo.in", "Demo@123")
        before = self.sql("SELECT COUNT(*) FROM contact_log")[0][0]
        sid = self.sector_id("Madanapalle", "Downtown")
        _, res, _ = c.call("GET", f"/api/search?blood_group=B%2B&sector_id={sid}")
        s, d, _ = c.call("POST", f"/api/donors/{res['donors'][0]['donor_id']}/contact", {})
        self.assertEqual(s, 200)
        self.assertRegex(d["contact_no"], r"^\d{10}$")
        self.assertEqual(self.sql("SELECT COUNT(*) FROM contact_log")[0][0], before + 1)

    def test_TC11_registration_validation(self):
        c = self.client()
        cases = [(dict(age=17), "age"), (dict(weight_kg=44), "weight_kg"), (dict(contact_no="12345"), "contact_no"),
                 (dict(password="short1"), "password"), (dict(email="not-an-email"), "email"),
                 (dict(blood_group="Z+"), "blood_group")]
        for change, field in cases:
            s, d, _ = c.call("POST", "/api/register", self.new_donor_payload(**change))
            self.assertEqual((s, d["field"]), (400, field), change)

    def test_TC12_password_hashed_and_duplicate_email_rejected(self):
        payload = self.new_donor_payload(password="Secret123")
        c = self.client()
        s, d, _ = c.call("POST", "/api/register", payload)
        self.assertEqual(s, 201, d)
        stored = self.sql("SELECT password_hash FROM users WHERE email=?", payload["email"])[0][0]
        self.assertTrue(stored.startswith("pbkdf2$"))
        self.assertNotIn("Secret123", stored)
        s, d, _ = self.client().call("POST", "/api/register", payload)
        self.assertEqual(s, 409)

    def test_TC13_login_failure_and_lockout(self):
        c = self.client()
        for _ in range(5):
            s, _, _ = c.call("POST", "/api/login", {"email": "lock@test.in", "password": "wrong"})
            self.assertEqual(s, 401)
        s, d, _ = c.call("POST", "/api/login", {"email": "lock@test.in", "password": "wrong"})
        self.assertEqual(s, 429)

    def test_TC14_recording_donation_triggers_eligibility_update(self):
        payload = self.new_donor_payload(blood_group="B-")
        c = self.client()
        c.call("POST", "/api/register", payload)
        sid = payload["sector_id"]
        _, d, _ = self.client().call("GET", f"/api/search?blood_group=B-&sector_id={sid}")
        self.assertIn("Test D.", [x["display_name"] for x in d["donors"]])
        s, me, _ = c.call("POST", "/api/donor/donations",
                          {"donation_date": date.today().isoformat(), "volume_ml": 450, "location": "Test Camp"})
        self.assertEqual(s, 201, me)
        self.assertEqual(me["eligibility"]["last_donation_date"], date.today().isoformat())
        self.assertFalse(me["visible_in_search"])

    def test_TC15_donations_must_be_90_days_apart(self):
        c = self.client()
        c.login("donor1@demo.in", "Donor@123")
        s, d, _ = c.call("POST", "/api/donor/donations",
                         {"donation_date": date.today().isoformat(), "volume_ml": 450, "location": "Camp"})
        self.assertEqual((s, d["field"]), (400, "donation_date"))

    def test_TC16_emergency_broadcast_targets_eligible_compatible_donors(self):
        sid = self.sector_id("Madanapalle", "Downtown")
        rec = self.client()
        rec.login("recipient@demo.in", "Demo@123")
        expected = {r[0] for r in self.sql(
            "SELECT d.user_id FROM donors d JOIN blood_groups g USING(blood_group_id) "
            "WHERE d.sector_id=? AND g.group_name IN ('A+','A-','O+','O-') AND d.status='Available' "
            "AND (d.last_donation_date IS NULL OR d.last_donation_date <= ?)", sid, db.cutoff_date())}
        before = {r[0] for r in self.sql("SELECT user_id FROM notifications WHERE request_id IS NOT NULL")}
        s, d, _ = rec.call("POST", "/api/requests", dict(
            patient_name="Test Patient", blood_group="A+", sector_id=sid, hospital="Test Hospital",
            units=2, urgency_level="Critical", contact_no="9000099001"))
        self.assertEqual(s, 201, d)
        got = {r[0] for r in self.sql("SELECT user_id FROM notifications WHERE request_id=?", d["request_id"])}
        self.assertEqual(got, expected)
        ravi = self.sql("SELECT user_id FROM users WHERE email='donor1@demo.in'")[0][0]
        self.assertNotIn(ravi, got)                  # inside 90 days: never alerted
        self.assertEqual(d["notified"], len(expected))

    def test_TC17_donor_response_rules(self):
        rec = self.client()
        rec.login("recipient@demo.in", "Demo@123")
        sid = self.sector_id("Madanapalle", "Downtown")
        _, r, _ = rec.call("POST", "/api/requests", dict(
            patient_name="Patient P", blood_group="B+", sector_id=sid, hospital="Hospital X", units=1,
            urgency_level="Urgent", contact_no="9000099001"))
        rid = r["request_id"]
        sita = self.client(); sita.login("donor2@demo.in", "Donor@123")                  # A+ cannot give to B+
        self.assertEqual(sita.call("POST", f"/api/requests/{rid}/respond", {})[0], 409)
        ravi = self.client(); ravi.login("donor1@demo.in", "Donor@123")                  # inside 90 days
        self.assertEqual(ravi.call("POST", f"/api/requests/{rid}/respond", {})[0], 409)
        match = self.sql("SELECT u.email FROM donors d JOIN users u USING(user_id) JOIN blood_groups g USING(blood_group_id) "
                         "WHERE g.group_name='B+' AND d.status='Available' AND (d.last_donation_date IS NULL "
                         "OR d.last_donation_date <= ?) LIMIT 1", db.cutoff_date())[0][0]
        ok = self.client(); ok.login(match, "Donor@123")
        self.assertEqual(ok.call("POST", f"/api/requests/{rid}/respond", {})[0], 200)
        self.assertEqual(ok.call("POST", f"/api/requests/{rid}/respond", {})[0], 409)    # duplicate
        _, resp, _ = rec.call("GET", f"/api/requests/{rid}/responses")
        self.assertEqual(len(resp["donors"]), 1)

    def test_TC18_only_admin_can_verify(self):
        rec = self.client(); rec.login("recipient@demo.in", "Demo@123")
        rid = self.sql("SELECT request_id FROM emergency_requests WHERE status='Pending' LIMIT 1")[0][0]
        self.assertEqual(rec.call("POST", f"/api/requests/{rid}/status", {"status": "Verified"})[0], 403)
        adm = self.client(); adm.login("admin@blooddonor.local", "Admin@123")
        self.assertEqual(adm.call("POST", f"/api/requests/{rid}/status", {"status": "Verified"})[0], 200)
        self.assertEqual(self.sql("SELECT status FROM emergency_requests WHERE request_id=?", rid)[0][0], "Verified")
        self.assertEqual(self.client().call("GET", "/api/admin/overview")[0], 401)

    def test_TC19_sector_with_donors_cannot_be_deleted(self):
        adm = self.client(); adm.login("admin@blooddonor.local", "Admin@123")
        s, d, _ = adm.call("DELETE", f"/api/admin/sectors/{self.sector_id('Madanapalle', 'Downtown')}", {})
        self.assertEqual(s, 409)
        s, _, _ = adm.call("POST", "/api/admin/sectors", {"city_name": "Kuppam", "sector_name": "Bus Stand Area"})
        self.assertEqual(s, 201)
        new = self.sector_id("Kuppam", "Bus Stand Area")
        self.assertEqual(adm.call("DELETE", f"/api/admin/sectors/{new}", {})[0], 200)

    def test_TC20_compatible_groups_search(self):
        sid = self.sector_id("Madanapalle", "Downtown")
        c = self.client()
        _, strict, _ = c.call("GET", f"/api/search?blood_group=AB%2B&sector_id={sid}")
        _, wide, _ = c.call("GET", f"/api/search?blood_group=AB%2B&sector_id={sid}&compatible=1")
        self.assertGreaterEqual(len(wide["donors"]), len(strict["donors"]))
        self.assertTrue(all(x["blood_group"] in db.DONOR_COMPAT["AB+"] for x in wide["donors"]))
        _, o_neg, _ = c.call("GET", f"/api/search?blood_group=O-&city_id=1&compatible=1")
        self.assertTrue(all(x["blood_group"] == "O-" for x in o_neg["donors"]))

    def test_TC21_search_response_time_under_2_seconds(self):
        c = self.client()
        start = time.perf_counter()
        for _ in range(25):
            s, _, _ = c.call("GET", "/api/search?blood_group=O%2B&city_id=1&compatible=1")
            self.assertEqual(s, 200)
        avg = (time.perf_counter() - start) / 25
        self.assertLess(avg, 2.0)
        print(f"\n      average search time: {avg * 1000:.1f} ms", end="")

    def test_TC22_transport_and_input_safety(self):
        c = self.client()
        s, d, h = c.call("GET", "/api/meta")
        self.assertEqual(h["X-Content-Type-Options"], "nosniff")
        self.assertIn("default-src 'self'", h["Content-Security-Policy"])
        self.assertEqual(c.call("POST", "/api/login", raw=b"email=a&password=b", ctype="application/x-www-form-urlencoded")[0], 415)
        s, body, _ = c.call("GET", "/../database.py")
        self.assertIn(b"<html", body)                 # path traversal falls back to index.html
        sid = self.sector_id("Madanapalle", "Downtown")
        s, d, _ = c.call("GET", "/api/search?blood_group=O%2B%27%20OR%201%3D1--&sector_id=" + str(sid))
        self.assertEqual(s, 400)                      # injection attempt rejected by validation
        s, d, h = c.call("POST", "/api/login", {"email": "recipient@demo.in", "password": "Demo@123"})
        self.assertIn("HttpOnly", h["Set-Cookie"])


if __name__ == "__main__":
    unittest.main(verbosity=2)
