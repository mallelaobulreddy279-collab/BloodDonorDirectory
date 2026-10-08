# Digital Blood Donor Directory

Sector-based blood donor search with automatic 90-day eligibility, masked contact
numbers, emergency broadcasts and an admin console. Mini project, Department of CSE,
Aditya College of Engineering, Madanapalle.

## Run it (2 steps, nothing to install)

You only need **Python 3.8 or newer**.

| System | Command |
|---|---|
| Windows | double-click `run.bat` |
| macOS / Linux | `./run.sh` |
| Any | `cd app` then `python server.py` (use `python3` on macOS/Linux) |

Then open **http://localhost:8000** (custom port: `python server.py 9000`).
The first start creates the database and fills it with demonstration data.
To reset the data, stop the server and delete `app/data/`.

## Demo accounts

| Role | Email | Password | What to try |
|---|---|---|---|
| Recipient | recipient@demo.in | Demo@123 | Reveal donor numbers, post an emergency request |
| Donor (inside 90 days) | donor1@demo.in | Donor@123 | See the eligibility countdown, try recording a donation too early |
| Donor (eligible) | donor2@demo.in | Donor@123 | Respond to a live request |
| Admin | admin@blooddonor.local | Admin@123 | Verify requests, add or remove sectors |

Every seeded donor is `donor1@demo.in` ... `donor52@demo.in` with password `Donor@123`.
All names and numbers are made up. Do not rely on this app for a real emergency.

## What is where

```
BloodDonorDirectory/
  app/
    server.py        HTTP server + JSON API (Python standard library only)
    database.py      Schema, seed data, 90-day rule, search, compatibility
    static/          index.html, css/style.css, js/app.js (no frameworks, works offline)
    data/            SQLite database (created on first run)
  database/
    schema_mysql.sql     Same 3NF design for MySQL 8, with trigger and search view
    sample_queries.sql   DDL/DML/DCL/DQL, aggregates, joins and sub-queries for the report
  protocol/
    Project_Protocol.docx   The project protocol document
  tests/test_api.py  22 automated tests (python tests/test_api.py -v)
  docs/screenshots/  Screens used in the protocol
```

## How it maps to the project report

| Report requirement | Where it lives |
|---|---|
| F1 registration, age >= 18, weight >= 45 kg, hashed passwords | `POST /api/register` (PBKDF2-SHA256, 200,000 rounds) |
| F2 city -> sector nested dropdown | `/api/meta` + `bindNested()` in `app.js` |
| F3 90-day eligibility, manual toggle can't override | `eligibility()` and `VISIBLE_SQL` in `database.py` |
| F4 sector search, masked numbers | `GET /api/search`, `POST /api/donors/{id}/contact` |
| F5 emergency broadcast | `POST /api/requests` -> `notify_donors_for_request()` |
| NF1 indexed search, under 2 s | `idx_donors_group_sector`; average 2 ms in test TC21 |
| NF3 privacy | masked numbers, reveal audit log, HttpOnly cookies, lockout after 5 bad logins |
| Master/slave tables + trigger | `donors`, `donation_history`, `trg_donation_updates_last` |

## Notes

* The app runs on SQLite so it needs no database server. `database/schema_mysql.sql`
  is the equivalent MySQL design; it has not been run against a live MySQL server here.
* Alerts are in-app notifications. SMS or WhatsApp delivery would need a gateway account.
* The 90-day interval is one constant (`ELIGIBILITY_DAYS` in `database.py`). Final
  eligibility is always decided by medical screening at the collection centre.
