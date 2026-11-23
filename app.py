"""MGOC50 Lecture 9 bakery workbench. Run on localhost with Python 3."""
import csv
import re
from datetime import date, timedelta
from html.parser import HTMLParser
from pathlib import Path
import argparse
import errno
import hashlib
import json
import os
import socket
import sqlite3
import sys
import threading
import webbrowser
from contextlib import closing
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.error import URLError
from urllib.request import urlopen


BASE_DIR = Path(__file__).resolve().parent
FIRST_DAY = date(2026, 11, 23)
LAST_PICKUP = date(2026, 12, 1)
ONE_DAY = timedelta(days=1)
WEEKDAYS = ("Monday", "Tuesday", "Wednesday", "Thursday", "Friday", "Saturday", "Sunday")
DM_FILES = {
    (FIRST_DAY + timedelta(days=i)).isoformat(): f"InstagramOrders_{weekday}.html"
    for i, weekday in enumerate(WEEKDAYS)
}
# Per dozen for cupcakes, per individual cake for cakes.
PRODUCTS = {"cupcakes": (30, 40), "cake": (120, 120)}
BASE_MINUTES = 8 * 60
HELPER_MINUTES = 4 * 60
HELPER_COST = 100
NOTICE_DAYS = 2
NO_MATCH = "This customer has no confirmed order for that pickup date."
MULTIPLE_MATCHES = ("This customer has more than one confirmed order for that pickup date. "
                    "The message does not identify which order to update.")
BAKER_DECLINED = "Declined by the baker."

# Text from earlier app versions can still be present in a saved request history.
LEGACY_REASONS = {
    "\u8be5\u987e\u5ba2\u5728\u53d6\u8d27\u65e5\u6ca1\u6709\u5df2\u63a5\u53d7\u7684\u8ba2\u5355\u3002": NO_MATCH,
    "\u8be5\u987e\u5ba2\u5728\u53d6\u8d27\u65e5\u6709\u591a\u5f20\u8ba2\u5355\uff0c\u65e0\u6cd5\u786e\u5b9a\u8981\u5904\u7406\u54ea\u4e00\u5f20\u3002": MULTIPLE_MATCHES,
    "\u5e97\u4e3b\u51b3\u5b9a\u62d2\u7edd\u3002": BAKER_DECLINED,
}
LEGACY_NOTICE = re.compile(r"\u8ddd\u53d6\u8d27\u65e5\u53ea\u6709 (-?[0-9]+) \u5929\uff0c\u81f3\u5c11\u9700\u8981\u63d0\u524d ([0-9]+) \u5929\u3002")
LEGACY_CAPACITY = re.compile(r"\u53d6\u8d27\u65e5\u6700\u591a\u8fd8\u53ef\u5206\u914d (-?[0-9]+) \u5206\u949f\uff0c\u672c\u5355\u9700\u8981 ([0-9]+) \u5206\u949f\u3002")


class BakeryMessageParser(HTMLParser):
    """Read only the source page's message paragraphs, in arrival order."""

    def __init__(self):
        super().__init__(convert_charrefs=True)
        self.messages = []
        self.parts = None
        self.count_parts = None
        self.count_labels = []
        self.unclosed_message = False

    def handle_starttag(self, tag, attrs):
        classes = (dict(attrs).get("class") or "").split()
        if tag == "p" and "message" in classes:
            if self.parts is not None:
                self.unclosed_message = True
            self.parts = []
        elif tag == "br" and self.parts is not None:
            self.parts.append("\n")
        elif tag == "div" and "count" in classes:
            self.count_parts = []

    def handle_endtag(self, tag):
        if tag == "p" and self.parts is not None:
            self.messages.append("".join(self.parts))
            self.parts = None
        elif tag == "div" and self.count_parts is not None:
            self.count_labels.append("".join(self.count_parts).strip())
            self.count_parts = None

    def handle_data(self, data):
        if self.parts is not None:
            self.parts.append(data)
        if self.count_parts is not None:
            self.count_parts.append(data)

    def check_complete(self, filename):
        count = None
        if len(self.count_labels) == 1:
            count = re.fullmatch(r"(\d+)\s+new\s+messages?\s+today", self.count_labels[0], re.I)
        if count is None:
            raise ValueError(f"The message count in {filename} is missing or unreadable.")
        expected = int(count[1])
        found = len(self.messages)
        if self.parts is not None or self.unclosed_message or found != expected:
            raise ValueError(f"Incomplete message file: {filename}. "
                             f"It lists {expected} messages and contains {found} complete messages.")


def _pickup_date(value):
    # English month and weekday names in the course files are independent of OS locale.
    parts = value.split()
    if len(parts) != 3:
        raise ValueError(f"Unrecognized pickup date in the course data: {value}")
    weekday, middle, last = parts
    month, number = (last, middle) if middle.isdigit() else (middle, last)
    month_number = {"Nov": 11, "Dec": 12}.get(month)
    if month_number is None or not number.isdigit():
        raise ValueError(f"Unrecognized pickup date in the course data: {value}")
    pickup = date(FIRST_DAY.year, month_number, int(number))
    if not FIRST_DAY <= pickup <= LAST_PICKUP or WEEKDAYS[pickup.weekday()][:3] != weekday:
        raise ValueError(f"The pickup date is outside the schedule or does not match the weekday: {value}")
    return pickup.isoformat()


def _read_product(value):
    match = re.fullmatch(r"(\d+)\s+(dozen cupcakes?|cakes?)", value.strip(), re.IGNORECASE)
    if not match or int(match[1]) < 1:
        raise ValueError(f"Unrecognized product or quantity in the course data: {value}")
    return ("cupcakes" if match[2].lower().startswith("dozen") else "cake", int(match[1]))


def _parse_message(raw):
    fields = {}
    for line in raw.splitlines():
        key, sep, value = line.partition(":")
        if sep:
            fields[key.strip()] = value.strip()
    if not all(fields.get(k) for k in ("Name", "Order", "Pickup Date")):
        raise ValueError("A course message is missing the customer name, order request, or pickup date.")
    content = fields["Order"]
    kind = "new"
    product, qty = None, None
    if content.lower() == "cancel":
        kind = "cancel"
    else:
        if content.lower().startswith("change to "):
            kind = "change"
            content = content[len("change to "):]
        product, qty = _read_product(content)
    return fields["Name"], kind, _pickup_date(fields["Pickup Date"]), product, qty


def _read_starting_orders():
    filename = "existing_orders.csv"
    recovery = f"Restore the original {filename} beside app.py and reopen the order desk."
    required = ("order", "customer", "item", "pickup_day")
    rows = []
    seen = set()
    location = "header"
    try:
        with (BASE_DIR / filename).open(encoding="utf-8-sig", newline="") as source:
            reader = csv.DictReader(source, strict=True)
            header = reader.fieldnames or []
            if any(header.count(column) != 1 for column in required):
                raise ValueError("Expected one column each for order, customer, item, and pickup_day.")
            for row in reader:
                location = f"line {reader.line_num}"
                if None in row or any(not (row[column] or "").strip() for column in required):
                    raise ValueError("An order row has a missing value or extra unlabelled field.")
                order_text = row["order"].strip()
                match = re.fullmatch(r"#?([0-9]+)", order_text)
                if not match or not 0 < int(match[1]) <= 2**63 - 1:
                    raise ValueError(f"Invalid order number: {order_text!r}.")
                order_id = int(match[1])
                if order_id in seen:
                    raise ValueError(f"Duplicate order number: {order_text!r}.")
                seen.add(order_id)
                product, qty = _read_product(row["item"])
                if qty > 2**63 - 1:
                    raise ValueError("The order quantity is too large.")
                rows.append((order_id, row["customer"], product, qty, _pickup_date(row["pickup_day"])))
        if not rows:
            raise ValueError("The starting order list contains no orders.")
    except FileNotFoundError as exc:
        raise ValueError(f"The starting order file {filename} is missing. {recovery}") from exc
    except (ValueError, csv.Error, OSError) as exc:
        raise ValueError(f"Could not read {filename} ({location}). {exc} {recovery}") from exc
    return rows


def init_db(db):
    """Create and seed on first open. A failed import leaves no partial starting state."""
    db.execute("SAVEPOINT bakery_initialization")
    try:
        db.execute("""CREATE TABLE IF NOT EXISTS bakery_run (
            id INTEGER PRIMARY KEY CHECK(id = 1), today TEXT NOT NULL,
            version INTEGER NOT NULL DEFAULT 0, complete INTEGER NOT NULL DEFAULT 0
        )""")
        if not db.execute("SELECT 1 FROM bakery_run WHERE id=1").fetchone():
            db.execute("""CREATE TABLE bakery_days (
                day TEXT PRIMARY KEY, fetched INTEGER NOT NULL DEFAULT 0,
                helper INTEGER NOT NULL DEFAULT 0
            )""")
            db.execute("""CREATE TABLE bakery_orders (
                id INTEGER PRIMARY KEY, customer TEXT NOT NULL, product TEXT NOT NULL,
                qty INTEGER NOT NULL CHECK(qty>0), pickup TEXT NOT NULL REFERENCES bakery_days(day),
                status TEXT NOT NULL CHECK(status IN ('booked','picked_up','cancelled'))
            )""")
            db.execute("""CREATE TABLE bakery_requests (
                id INTEGER PRIMARY KEY, day TEXT NOT NULL REFERENCES bakery_days(day),
                sequence INTEGER NOT NULL, raw TEXT NOT NULL, customer TEXT NOT NULL,
                kind TEXT NOT NULL CHECK(kind IN ('new','change','cancel')),
                pickup TEXT NOT NULL REFERENCES bakery_days(day), product TEXT, qty INTEGER,
                status TEXT NOT NULL CHECK(status IN ('pending','accepted','rejected')),
                reason TEXT NOT NULL DEFAULT '', order_id INTEGER REFERENCES bakery_orders(id),
                UNIQUE(day, sequence)
            )""")
            db.executemany("INSERT INTO bakery_days(day) VALUES (?)", (
                ((FIRST_DAY + timedelta(days=i)).isoformat(),)
                for i in range((LAST_PICKUP - FIRST_DAY).days + 1)
            ))
            db.executemany("""INSERT INTO bakery_orders(id,customer,product,qty,pickup,status)
                VALUES (?,?,?,?,?,'booked')""", _read_starting_orders())
            db.execute("INSERT INTO bakery_run(id,today) VALUES(1,?)", (FIRST_DAY.isoformat(),))
    except BaseException:
        db.execute("ROLLBACK TO bakery_initialization")
        db.execute("RELEASE bakery_initialization")
        raise
    db.execute("RELEASE bakery_initialization")


def _work_minutes(product, qty):
    return PRODUCTS[product][0] * qty


def _day_capacity(helper):
    return BASE_MINUTES + HELPER_MINUTES * int(helper)


def _short_notice_reason(notice, required):
    if notice < 0:
        return "The pickup date has already passed."
    unit = "day" if notice == 1 else "days"
    required_text = "1 day is required" if required == 1 else f"{required} days are required"
    return f"{notice} {unit} of notice. At least {required_text}."


def _notice_reason(today, pickup):
    notice = (date.fromisoformat(pickup) - date.fromisoformat(today)).days
    return _short_notice_reason(notice, NOTICE_DAYS) if notice < NOTICE_DAYS else ""


def _oven_time_reason(available, needed):
    return (f"At the time of review, {available} min of oven time remained on the pickup day. "
            f"This order needed {needed} min.")


def _display_reason(reason):
    if not reason:
        return reason
    if reason.startswith("Available oven time on the pickup day:"):
        old = re.fullmatch(r"Available oven time on the pickup day: (-?\d+) min\. "
                           r"This order needs (\d+) min\.", reason)
        if old:
            return _oven_time_reason(int(old[1]), int(old[2]))
    if reason.isascii():
        return reason
    if reason in LEGACY_REASONS:
        return LEGACY_REASONS[reason]
    notice = LEGACY_NOTICE.fullmatch(reason)
    if notice:
        return _short_notice_reason(int(notice[1]), int(notice[2]))
    capacity = LEGACY_CAPACITY.fullmatch(reason)
    if capacity:
        return _oven_time_reason(int(capacity[1]), int(capacity[2]))
    return reason


def _capacity_reason(db, pickup, product, qty, previous_minutes=0):
    day = db.execute("SELECT helper FROM bakery_days WHERE day=?", (pickup,)).fetchone()
    committed = db.execute(
        "SELECT product,qty FROM bakery_orders WHERE pickup=? AND status!='cancelled'", (pickup,)
    )
    used = sum(_work_minutes(row["product"], row["qty"]) for row in committed)
    available = _day_capacity(day["helper"]) - used + previous_minutes
    needed = _work_minutes(product, qty)
    if needed > available:
        return _oven_time_reason(available, needed)
    return ""


def _record_decision(db, request_id, status, reason="", order_id=None):
    db.execute("UPDATE bakery_requests SET status=?,reason=?,order_id=? WHERE id=?",
               (status, reason, order_id, request_id))


def _handle_incoming(db, request_id, today, customer, kind, pickup, product, qty):
    reason = _notice_reason(today, pickup)
    if reason:
        _record_decision(db, request_id, "rejected", reason)
        return
    if kind == "new":
        reason = _capacity_reason(db, pickup, product, qty)
        if reason:
            _record_decision(db, request_id, "rejected", reason)
        return

    matches = db.execute("""SELECT id,product,qty FROM bakery_orders
        WHERE customer=? COLLATE NOCASE AND pickup=? AND status='booked' ORDER BY id""",
                         (customer, pickup)).fetchall()
    if len(matches) != 1:
        reason = NO_MATCH if not matches else MULTIPLE_MATCHES
        _record_decision(db, request_id, "rejected", reason)
        return
    original = matches[0]
    order_id = original["id"]
    if kind == "cancel":
        db.execute("UPDATE bakery_orders SET status='cancelled' WHERE id=?", (order_id,))
    else:
        old_minutes = _work_minutes(original["product"], original["qty"])
        reason = _capacity_reason(db, pickup, product, qty, old_minutes)
        if reason:
            _record_decision(db, request_id, "rejected", reason, order_id)
            return
        db.execute("UPDATE bakery_orders SET product=?,qty=? WHERE id=?", (product, qty, order_id))
    _record_decision(db, request_id, "accepted", order_id=order_id)


def _fetch_messages(db, today):
    filename = DM_FILES.get(today)
    if not filename:
        raise ValueError("There is no message file for this day.")
    if db.execute("SELECT fetched FROM bakery_days WHERE day=?", (today,)).fetchone()["fetched"]:
        raise ValueError("Today's messages have already been loaded.")
    parser = BakeryMessageParser()
    try:
        parser.feed((BASE_DIR / filename).read_text(encoding="utf-8-sig"))
        parser.close()
        parser.check_complete(filename)
        messages = [(raw, _parse_message(raw)) for raw in parser.messages]
    except ValueError as exc:
        raise ValueError(f"{exc} Restore the original course file and load today's messages again.") from exc
    for sequence, (raw, fields) in enumerate(messages, 1):
        customer, kind, pickup, product, qty = fields
        request_id = db.execute("""INSERT INTO bakery_requests
            (day,sequence,raw,customer,kind,pickup,product,qty,status)
            VALUES (?,?,?,?,?,?,?,?,'pending')""",
            (today, sequence, raw, customer, kind, pickup, product, qty)).lastrowid
        _handle_incoming(db, request_id, today, customer, kind, pickup, product, qty)
    db.execute("UPDATE bakery_days SET fetched=1 WHERE day=?", (today,))
    count = len(parser.messages)
    return f"Loaded {count} {'message' if count == 1 else 'messages'} for today."


def _accept_new_order(db, request):
    reason = _capacity_reason(db, request["pickup"], request["product"], request["qty"])
    if reason:
        _record_decision(db, request["id"], "rejected", reason)
        return False
    order_id = db.execute("""INSERT INTO bakery_orders(customer,product,qty,pickup,status)
        VALUES (?,?,?,?,'booked')""", (request["customer"], request["product"],
        request["qty"], request["pickup"])).lastrowid
    _record_decision(db, request["id"], "accepted", order_id=order_id)
    return True


def _individual_decision(db, today, payload):
    request_id = payload.get("id")
    if type(request_id) is not int:
        raise ValueError("Choose a pending order request.")
    request = db.execute("""SELECT * FROM bakery_requests
        WHERE id=? AND day=? AND status='pending' AND kind='new'""", (request_id, today)).fetchone()
    if request is None:
        raise ValueError("This request has already been handled or belongs to another day.")
    if payload["action"] == "reject":
        _record_decision(db, request_id, "rejected", BAKER_DECLINED)
        return f"Declined the request from {request['customer']}."
    if _accept_new_order(db, request):
        return f"Accepted the order from {request['customer']}."
    return f"Declined the request from {request['customer']}. There is not enough oven time on the pickup day."


def _book_helper(db, today, pickup):
    if type(pickup) is not str:
        raise ValueError("Choose the day the helper will work.")
    day = db.execute("SELECT helper FROM bakery_days WHERE day=?", (pickup,)).fetchone()
    if day is None:
        raise ValueError("Choose a helper date within the bakery schedule.")
    if day["helper"]:
        raise ValueError("A helper is already booked for that day.")
    if _notice_reason(today, pickup):
        raise ValueError(f"Book a helper at least {NOTICE_DAYS} days in advance.")
    db.execute("UPDATE bakery_days SET helper=1 WHERE day=?", (pickup,))
    return f"Helper booked for {pickup}. Added 4 hours of oven time for ${HELPER_COST}."


def _close_day(db, today):
    if today in DM_FILES and not db.execute(
            "SELECT fetched FROM bakery_days WHERE day=?", (today,)).fetchone()["fetched"]:
        raise ValueError("Load today's messages before closing the day.")
    pending = db.execute("""SELECT * FROM bakery_requests WHERE day=? AND status='pending'
        ORDER BY sequence""", (today,)).fetchall()
    for request in pending:
        _accept_new_order(db, request)
    picked_up = db.execute("""UPDATE bakery_orders SET status='picked_up'
        WHERE pickup=? AND status='booked'""", (today,)).rowcount
    day = date.fromisoformat(today)
    if day == LAST_PICKUP:
        db.execute("UPDATE bakery_run SET complete=1 WHERE id=1")
    else:
        db.execute("UPDATE bakery_run SET today=? WHERE id=1", ((day + ONE_DAY).isoformat(),))
    if today in DM_FILES:
        decisions = dict(db.execute("""SELECT status, COUNT(*) FROM bakery_requests
            WHERE day=? GROUP BY status""", (today,)).fetchall())
        approved = decisions.get("accepted", 0)
        declined = decisions.get("rejected", 0)
        summary = (f"{approved} approved {'request' if approved == 1 else 'requests'} "
                   f"and {declined} declined {'request' if declined == 1 else 'requests'}.")
    else:
        summary = "No customer messages were scheduled."
    closed_date = f"{WEEKDAYS[day.weekday()]}, {day:%B} {day.day}"
    pickup_summary = (f"{picked_up} {'order was' if picked_up == 1 else 'orders were'} "
                      "picked up at 5 PM.")
    return f"{closed_date} closed. {summary} {pickup_summary}"


def apply_action(db, payload):
    run = db.execute("SELECT today,complete FROM bakery_run WHERE id=1").fetchone()
    if run["complete"]:
        raise ValueError("The final pickup day is closed.")
    today = run["today"]
    action = payload.get("action")
    if action == "fetch":
        message = _fetch_messages(db, today)
    elif action == "advance":
        message = _close_day(db, today)
    elif action == "helper":
        message = _book_helper(db, today, payload.get("day"))
    elif action in ("accept", "reject"):
        message = _individual_decision(db, today, payload)
    else:
        raise ValueError("Unknown action. Refresh the bakery dashboard and try again.")
    db.execute("UPDATE bakery_run SET version=version+1 WHERE id=1")
    return message


def state(db):
    run = db.execute("SELECT today,version,complete FROM bakery_run WHERE id=1").fetchone()
    today = run["today"]
    days = {}
    weeks = {}
    week_for_day = {}
    fetched = False
    helper_count = 0
    for row in db.execute("SELECT day,fetched,helper FROM bakery_days ORDER BY day"):
        helper = bool(row["helper"])
        helper_count += int(helper)
        pickup_day = date.fromisoformat(row["day"])
        week_start = (pickup_day - timedelta(days=pickup_day.weekday())).isoformat()
        if week_start not in weeks:
            weeks[week_start] = dict(start=week_start, end=row["day"], earned=0, booked=0,
                                     helper_cost=0, pickups=0)
        week = weeks[week_start]
        week["end"] = row["day"]
        week["helper_cost"] += HELPER_COST * int(helper)
        week_for_day[row["day"]] = week
        if row["day"] == today:
            fetched = bool(row["fetched"])
        days[row["day"]] = dict(day=row["day"], capacity=_day_capacity(helper), used=0,
                                helper=helper, orders=0, sales=0)
    totals = dict(earned=0, booked=0, helper_cost=HELPER_COST * helper_count)
    orders = []
    for row in db.execute("SELECT * FROM bakery_orders ORDER BY pickup,id"):
        order = dict(row)
        minutes, price = PRODUCTS[order["product"]]
        order["minutes"] = minutes * order["qty"]
        order["amount"] = price * order["qty"]
        orders.append(order)
        if order["status"] == "cancelled":
            continue
        day = days[order["pickup"]]
        day["used"] += order["minutes"]
        day["orders"] += 1
        day["sales"] += order["amount"]
        key = "earned" if order["status"] == "picked_up" else "booked"
        totals[key] += order["amount"]
        week = week_for_day[order["pickup"]]
        week[key] += order["amount"]
        if order["status"] == "picked_up":
            week["pickups"] += 1
    requests = [dict(row) for row in db.execute("""SELECT
        id,day,customer,raw,kind,pickup,status,reason,product,qty
        FROM bakery_requests ORDER BY day,sequence""")]
    for request in requests:
        request["reason"] = _display_reason(request["reason"])
    return dict(today=today, version=run["version"], complete=bool(run["complete"]),
                fetched=fetched, has_source=today in DM_FILES, days=list(days.values()), weeks=list(weeks.values()),
                requests=requests, orders=orders, totals=totals)


PAGE_HTML = r'''<!doctype html>
<html lang="en-CA">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<meta name="color-scheme" content="light">
<title>Bakery Order Desk</title>
<link rel="icon" href="data:,">
<style>
:root {
  --paper:#f7f6f0;
  --white:#fffef9;
  --ink:#202d25;
  --muted:#536458;
  --green:#244d37;
  --pale:#e8efe4;
  --line:#d4d9cd;
  --brown:#97432e;
  --pink:#f8e9e1;
  --gold:#8d501b;
  --cream:#f5eedb;
  --radius:16px;
  font-family:"DM Sans",system-ui,"Microsoft YaHei",sans-serif;
  color:var(--ink);
  background:var(--paper);
  font-size:15px;
}
* {
  box-sizing:border-box;
}
body {
  margin:0;
}
button,select {
  font:inherit;
}
button,select,a {
  touch-action:manipulation;
}
button {
  cursor:pointer;
}
button:disabled {
  cursor:default;
}
button,a,select,summary {
  outline-offset:4px;
}
button:focus-visible,a:focus-visible,select:focus-visible,summary:focus-visible {
  outline:3px solid #b96638;
}
a {
  color:inherit;
}
.wrap {
  max-width:1450px;
  margin:0 auto;
  padding:0 clamp(16px,4.5vw,76px);
}
header {
  border-bottom:1px solid var(--line);
  background:var(--white);
}
.nav {
  display:flex;
  align-items:center;
  justify-content:space-between;
  min-height:68px;
  gap:16px;
}
.brand {
  display:flex;
  align-items:center;
  gap:11px;
  font-size:19px;
  font-weight:780;
  text-decoration:none;
  letter-spacing:.03em;
}
.mark {
  height:35px;
  width:35px;
  display:grid;
  place-items:center;
  background:var(--green);
  border-radius:11px;
  transform:rotate(-4deg);
}
.loaf {
  height:15px;
  width:23px;
  border:1.6px solid #fff7dc;
  border-radius:65% 56% 35% 35%;
  position:relative;
  transform:rotate(-12deg);
}
.loaf::before,.loaf::after {
  content:"";
  position:absolute;
  top:1px;
  width:2px;
  height:6px;
  border-radius:80%;
  background:#fff7dc;
  transform:rotate(23deg);
}
.loaf::before {
  left:7px;
}
.loaf::after {
  left:13px;
}
nav {
  display:flex;
  align-items:center;
  gap:clamp(12px,2.5vw,34px);
  font-size:14px;
}
nav a {
  text-decoration:none;
  color:var(--muted);
}
nav a:hover,a.jump:hover {
  text-decoration:underline;
  text-underline-offset:4px;
}
nav a:last-child {
  padding-left:clamp(10px,2vw,24px);
  border-left:1px solid var(--line);
}
main {
  padding-bottom:60px;
}
section {
  scroll-margin-top:16px;
}
.hero {
  display:flex;
  align-items:end;
  justify-content:space-between;
  gap:24px;
  padding:34px 0 24px;
}
.day-line {
  display:flex;
  gap:12px;
  align-items:center;
  flex-wrap:wrap;
}
.day-line small,.day-line span {
  font-size:13px;
  color:var(--muted);
}
.day-line span {
  display:inline-flex;
  align-items:center;
  gap:6px;
}
.day-line span::before {
  content:"";
  width:7px;
  height:7px;
  background:#659265;
  border-radius:50%;
}
h1,h2,h3,p {
  margin:0;
}
h1 {
  font-size:clamp(28px,4.1vw,52px);
  font-weight:690;
  letter-spacing:-.045em;
  line-height:1.23;
  margin:7px 0 0;
}
h1 small {
  font:500 clamp(16px,1.9vw,24px)/1.25 system-ui,"Microsoft YaHei",sans-serif;
  margin-left:14px;
  color:var(--muted);
  letter-spacing:0;
}
.actions {
  display:flex;
  gap:8px;
  flex-wrap:wrap;
  align-items:center;
}
.btn {
  display:inline-flex;
  align-items:center;
  justify-content:center;
  gap:6px;
  min-height:40px;
  padding:8px 16px;
  border-radius:8px;
  border:1px solid #aebaaa;
  color:var(--green);
  background:var(--white);
  font-weight:650;
  font-size:14px;
  white-space:nowrap;
  text-decoration:none;
}
.btn:hover:not(:disabled) {
  background:#edf1e7;
  border-color:#879d85;
}
.btn.primary {
  background:var(--green);
  color:#fff;
  border-color:var(--green);
}
.btn.primary:hover:not(:disabled) {
  background:#173c28;
}
.btn:disabled {
  color:#6b756a;
  background:#f1f2eb;
  border-color:var(--line);
}
.btn.small {
  min-height:33px;
  padding:5px 12px;
  font-size:13px;
}
.statusbar {
  color:var(--muted);
  font-size:13px;
  line-height:1.5;
  margin:0 0 15px;
  display:flex;
  align-items:center;
  gap:9px;
  flex-wrap:wrap;
}
.statusbar:empty {
  display:none;
}
.statusbar.error {
  color:#90361f;
}
.stats {
  display:grid;
  grid-template-columns:repeat(3,1fr);
  background:var(--white);
  border:1px solid var(--line);
  border-radius:var(--radius);
  overflow:hidden;
  margin-bottom:22px;
}
.stat {
  padding:16px clamp(16px,2.4vw,34px);
  position:relative;
}
.stat+.stat::before {
  content:"";
  position:absolute;
  left:0;
  top:20px;
  bottom:20px;
  width:1px;
  background:var(--line);
}
.stat>span {
  font-size:13px;
  font-weight:600;
  color:var(--muted);
}
.stat strong {
  display:block;
  font-family:"Castoro",Georgia,"Times New Roman",serif;
  font-size:clamp(29px,3.1vw,42px);
  font-weight:500;
  letter-spacing:-.035em;
  line-height:1.18;
  margin:5px 0 1px;
  font-variant-numeric:lining-nums tabular-nums;
}
.stat:first-child strong {
  color:var(--green);
}
.layout {
  display:grid;
  grid-template-columns:minmax(0,1.63fr) minmax(345px,1fr);
  gap:20px;
  align-items:start;
}
.stack {
  display:grid;
  gap:20px;
  min-width:0;
}
.card {
  background:var(--white);
  border:1px solid var(--line);
  border-radius:var(--radius);
  overflow:hidden;
}
.card-head {
  display:flex;
  align-items:center;
  justify-content:space-between;
  gap:12px;
  padding:20px 22px 15px;
}
.card-head h2 {
  font-size:20px;
  letter-spacing:-.04em;
}
.card-head p,.fine {
  font-size:13px;
  color:var(--muted);
  line-height:1.5;
}
.card-head p {
  margin-top:4px;
}
.card-head select {
  font-size:13px;
  max-width:155px;
}
.selector {
  padding:7px 23px 7px 8px;
  border-radius:7px;
  background:var(--white);
  border:1px solid var(--line);
  color:var(--ink);
}
.pad {
  padding:0 22px 20px;
}
.empty {
  padding:22px;
  border:1px dashed #c7cfbd;
  background:#f7f8f1;
  border-radius:10px;
  line-height:1.6;
  color:var(--muted);
  font-size:14px;
}
.empty strong {
  display:block;
  color:var(--ink);
  font-size:16px;
  font-weight:660;
  margin-bottom:3px;
}
.empty .btn {
  margin-top:12px;
}
.messages {
  display:grid;
  gap:10px;
}
.msg {
  border:1px solid var(--line);
  border-radius:11px;
  padding:15px 16px;
  background:#fffefa;
}
.msg.pending {
  border-color:#bdcbb7;
}
.mhead,.mfoot,.cal-top,.cal-bottom {
  display:flex;
  align-items:center;
  justify-content:space-between;
  gap:10px;
}
.mhead h3 {
  font-size:17px;
  font-weight:720;
  overflow-wrap:anywhere;
}
.mhead h3 small {
  font-size:12px;
  font-weight:550;
  color:var(--muted);
  margin-left:9px;
}
.badge {
  display:inline-flex;
  align-items:center;
  white-space:nowrap;
  font-size:12px;
  font-weight:650;
  padding:4px 9px;
  border-radius:999px;
  background:var(--pale);
  color:var(--green);
}
.badge.rejected,.badge.cancelled {
  background:var(--pink);
  color:var(--brown);
}
.badge.pending {
  background:var(--cream);
  color:var(--gold);
}
.badge.booked {
  color:#3e5669;
  background:#e8eef1;
}
.msg .description {
  font-size:14px;
  line-height:1.5;
  margin:8px 0;
}
.msg .capacity-fact,.msg .reason {
  font-size:13px;
  line-height:1.55;
  color:var(--muted);
  margin-top:3px;
}
.msg.rejected .reason {
  color:var(--brown);
}
.mfoot {
  margin-top:10px;
  align-items:flex-start;
}
.raw {
  min-width:0;
  flex:1;
}
.raw summary {
  width:max-content;
  max-width:100%;
  font-size:13px;
  color:var(--muted);
  cursor:pointer;
  line-height:30px;
}
.raw pre {
  font:13px/1.55 "Courier Prime",Consolas,monospace;
  background:#f1f3ec;
  padding:9px 11px;
  border-radius:6px;
  margin:4px 0 0;
  white-space:pre-wrap;
  overflow-wrap:anywhere;
  color:var(--ink);
}
.cal-head {
  padding-bottom:13px;
}
.cal-list {
  border-top:1px solid var(--line);
}
.cal-day {
  padding:12px 21px;
  border-bottom:1px solid #e1e4da;
}
.cal-day:last-child {
  border-bottom:0;
}
.cal-day.today {
  background:#f1f5eb;
  box-shadow:inset 3px 0 var(--green);
}
.cal-top strong {
  font-size:14px;
}
.cal-top strong small {
  font-size:12px;
  margin-left:6px;
  color:var(--muted);
  font-weight:550;
}
.cal-top .free {
  color:var(--green);
  font-size:13px;
  font-weight:630;
  font-variant-numeric:tabular-nums;
}
.cal-bottom {
  justify-content:flex-start;
  gap:7px;
  align-items:center;
  margin-top:7px;
}
.meter {
  width:65px;
  height:5px;
  background:#dfe5d8;
  border-radius:10px;
  overflow:hidden;
  flex:none;
}
.meter>i {
  height:100%;
  display:block;
  background:#709473;
  border-radius:10px;
}
.cal-bottom .fine {
  font-size:12px;
}
.helper {
  margin-left:auto;
  background:transparent;
  color:var(--green);
  border:0;
  padding:3px 0 3px 4px;
  text-decoration:underline;
  text-underline-offset:3px;
  font-size:12px;
  line-height:1.4;
}
.helper:hover {
  color:#141f17;
}
.helper-on {
  margin-left:auto;
  white-space:nowrap;
  font-size:12px;
  color:var(--green);
}
.order-wrap {
  overflow:auto;
}
.order-table {
  width:100%;
  border:0;
  border-collapse:collapse;
  text-align:left;
  font-size:13px;
}
.order-table th {
  background:#f1f3eb;
  color:var(--muted);
  font-weight:600;
  white-space:nowrap;
}
.order-table th,.order-table td {
  padding:10px 8px;
  border-top:1px solid #e1e4da;
}
.order-table th:first-child,.order-table td:first-child {
  padding-left:22px;
}
.order-table th:last-child,.order-table td:last-child {
  padding-right:20px;
}
.order-table td strong {
  display:block;
  font-size:14px;
}
.order-table td small {
  display:block;
  color:var(--muted);
  margin-top:2px;
  font-size:12px;
}
.order-table .money {
  font-variant-numeric:tabular-nums;
  white-space:nowrap;
}
.order-table .badge {
  font-size:11px;
  padding:3px 7px;
}
.none {
  padding:4px 22px 22px;
  font-size:14px;
  color:var(--muted);
}
.sr {
  position:absolute;
  width:1px;
  height:1px;
  padding:0;
  margin:-1px;
  overflow:hidden;
  clip:rect(0,0,0,0);
  white-space:nowrap;
  border:0;
}
dialog {
  border:1px solid var(--line);
  border-radius:16px;
  background:var(--white);
  color:var(--ink);
  padding:24px;
  width:min(440px,calc(100% - 32px));
  box-shadow:0 24px 80px #152b1b33;
}
dialog::backdrop {
  background:#19251dcc;
}
dialog h2 {
  font-size:21px;
}
dialog p {
  margin:12px 0 22px;
  line-height:1.7;
  color:var(--muted);
  font-size:14px;
}
dialog .actions {
  justify-content:flex-end;
}
[hidden] {
  display:none!important;
}
@media(min-width:1450px) {
  .layout {
    grid-template-columns:minmax(0,1.8fr) minmax(365px,1fr);
  }
}
@media(max-width:1040px) {
  .wrap {
    padding-left:23px;
    padding-right:23px;
  }
  .layout {
    gap:14px;
    grid-template-columns:minmax(0,1.4fr) minmax(310px,1fr);
  }
  .card-head {
    padding:16px 15px 12px;
  }
  .pad {
    padding:0 15px 15px;
  }
  .cal-day {
    padding:12px 15px;
  }
  .order-table th:first-child,.order-table td:first-child {
    padding-left:15px;
  }
  .order-table th:last-child,.order-table td:last-child {
    padding-right:12px;
  }
  .order-table th,.order-table td {
    padding-left:5px;
    padding-right:5px;
  }
  .stack {
    gap:14px;
  }
}
@media(max-width:800px) {
  .layout {
    grid-template-columns:1fr;
  }
  .hero {
    align-items:flex-start;
    flex-wrap:wrap;
    gap:15px;
  }
  .layout>.stack {
    display:contents;
  }
  #messages {
    order:0;
  }
  #calendar {
    order:1;
  }
  #orders {
    order:2;
  }
  .stats {
    margin-bottom:15px;
  }
  .cal-list {
    display:grid;
    grid-template-columns:repeat(2,minmax(0,1fr));
  }
  .cal-day {
    border-top:1px solid #e1e4da;
    border-bottom:0;
  }
  .cal-day:nth-child(odd) {
    border-right:1px solid #e1e4da;
  }
  .cal-list {
    border-top:0;
  }
}
@media(max-width:580px) {
  .wrap {
    padding-left:14px;
    padding-right:14px;
  }
  .nav {
    min-height:57px;
  }
  .brand {
    font-size:16px;
    gap:7px;
  }
  .mark {
    width:29px;
    height:29px;
  }
  .loaf {
    transform:rotate(-12deg) scale(.87);
  }
  nav {
    font-size:12px;
    gap:10px;
  }
  nav a:first-child {
    display:none;
  }
  nav a:last-child {
    padding-left:10px;
  }
  .hero {
    padding:22px 0 15px;
  }
  .hero .actions {
    width:100%;
  }
  .hero .btn {
    flex:1;
    font-size:13px;
    min-height:41px;
    padding:8px;
  }
  .stat {
    padding:10px 8px;
  }
  .stat>span {
    font-size:11px;
  }
  .stat strong {
    font-size:26px;
    margin-top:3px;
  }
  .stat+.stat::before {
    top:13px;
    bottom:13px;
  }
  .stats,.card {
    border-radius:12px;
  }
  .card-head h2 {
    font-size:18px;
  }
  .cal-list {
    display:block;
    border-top:1px solid var(--line);
  }
  .cal-day,.cal-day:nth-child(odd) {
    border:0;
    border-bottom:1px solid #e1e4da;
  }
  .mfoot:has(.raw[open]) {
    flex-wrap:wrap;
  }
  .mfoot:has(.raw[open]) .raw {
    flex-basis:100%;
  }
  .msg {
    padding:13px;
  }
  .order-table th,.order-table td {
    padding:8px 3px;
    font-size:12px;
  }
  .order-table th:first-child,.order-table td:first-child {
    padding-left:10px;
  }
  .order-table th:last-child,.order-table td:last-child {
    padding-right:8px;
  }
  .order-table td strong {
    font-size:12px;
  }
  .order-table td small {
    font-size:10px;
  }
  .order-table .badge {
    padding:2px 4px;
  }
}
@media(max-width:580px) {
  .nav {flex-wrap:wrap;gap:4px 12px;padding-top:9px;padding-bottom:9px}
  .hero .btn {flex:1 1 140px;white-space:normal;line-height:1.35}
  .stat>span {display:block;min-height:2.7em;line-height:1.35}
  .cal-bottom {flex-wrap:wrap}
  .order-table .badge {white-space:normal;text-align:center}
}
@media(max-width:380px) {
  .nav nav {flex-basis:100%}
  .nav nav a:first-child {display:block}
  .card-head {flex-wrap:wrap}
  .mfoot {flex-wrap:wrap}
}
.fetch-guidance {margin:-7px 0 15px;color:var(--muted);font-size:14px;line-height:1.55}
.fetch-guidance a {color:var(--green);font-weight:650;text-underline-offset:3px}
.stats {margin-bottom:0;border-bottom-left-radius:0;border-bottom-right-radius:0}
.weekly {background:var(--white);border:1px solid var(--line);border-top:0;border-radius:0 0 var(--radius) var(--radius);margin-bottom:20px}
.weekly>summary {width:max-content;max-width:100%;cursor:pointer;padding:9px 20px;color:var(--green);font-size:14px;font-weight:650}
.week-grid {display:grid;grid-template-columns:repeat(auto-fit,minmax(min(250px,100%),1fr));gap:10px;padding:4px 16px 16px}
.week-block {background:#f4f5ee;border-radius:9px;padding:12px 14px}
.week-block h3 {font-size:15px;margin:0 0 8px}
.week-block dl {display:grid;grid-template-columns:minmax(0,1fr) auto;gap:5px 12px;margin:0;font-size:13px}
.week-block dt {color:var(--muted)}
.week-block dd {margin:0;text-align:right;font-weight:600;font-variant-numeric:tabular-nums}
.week-block p {font-size:12px;margin-top:7px;color:var(--muted)}
.local-feedback {color:var(--green);background:var(--pale);border-radius:6px;font-size:13px;line-height:1.45;margin:10px 0 0;padding:7px 9px}
.local-feedback.error {color:var(--brown);background:var(--pink)}
#messages>.local-feedback {margin:0 16px 12px}
.local-feedback:focus-visible,#notice:focus-visible {outline:2px solid #8b582b;outline-offset:2px}
</style></head>
<body>
<header><div class="wrap nav"><a class="brand" href="#top"><span class="mark" aria-hidden="true"><span class="loaf"></span></span>Bakery Order Desk</a><nav aria-label="Main navigation"><a href="#messages">Inbox</a><a href="#orders">Orders</a><a href="#calendar">Capacity</a><a href="./README.html">User guide</a></nav></div></header>
<main class="wrap" id="top">
<div class="hero"><div><div class="day-line"><small id="year">Simulated business day</small><span id="phase">Loading</span></div><h1><span id="date">Order desk</span><small id="weekday"></small></h1></div><div class="actions" id="mainActions"></div></div>
<p id="previewNote" class="statusbar" hidden>This browser saves your progress. A fresh preview starts with 13 booked orders.</p>
<p id="fetchGuidance" class="fetch-guidance" hidden>Loading messages applies valid changes and cancellations. Requests with too little notice or oven time are rejected immediately. Book any extra oven time first. <a href="#calendar">Review oven capacity</a></p>
<div id="notice" class="statusbar" role="status" aria-live="polite" tabindex="-1"></div>
<section class="stats" aria-label="Financial summary"><div class="stat"><span>Revenue from pickups</span><strong id="earned">—</strong></div><div class="stat"><span>Value awaiting pickup</span><strong id="booked">—</strong></div><div class="stat"><span>Helper costs</span><strong id="cost">—</strong></div></section>
<details id="weeklyTotals" class="weekly"><summary>Weekly totals</summary><div id="weekBody" class="week-grid"></div></details>
<div class="layout"><div class="stack">
<section class="card" id="messages"><div class="card-head"><div><h2>Customer inbox</h2><p id="msgCount"></p></div><label><span class="sr">Filter messages by date received</span><select id="msgFilter" class="selector" hidden></select></label></div><div class="pad" id="messageBody" aria-live="off"></div></section>
<section class="card" id="orders"><div class="card-head"><div><h2>Pickup orders</h2><p id="orderCount">Browse by pickup date</p></div><label><span class="sr">Filter orders by pickup date</span><select id="orderFilter" class="selector" hidden></select></label></div><div class="order-wrap" id="orderBody"></div></section>
</div><section class="card" id="calendar" tabindex="-1"><div class="card-head cal-head"><div><h2>Oven capacity</h2><p>The oven runs 8 hours a day. Book a helper at least 2 days ahead to add 4 hours for $100.</p></div></div><div class="cal-list" id="calBody"></div></section></div>
</main>
<dialog id="approval" aria-labelledby="dialogTitle" aria-describedby="dialogText"><h2 id="dialogTitle"></h2><p id="dialogText"></p><form method="dialog" class="actions"><button class="btn" value="cancel" autofocus>Go back</button><button id="dialogYes" class="btn primary" value="confirm">Confirm</button></form></dialog>
<noscript>Enable JavaScript in your browser to load your local orders.</noscript>
<script>
'use strict';
const byId=id=>document.getElementById(id);
const html=value=>String(value??'').replace(/[&<>"']/g,ch=>({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;',"'":'&#39;'}[ch]));
const money=n=>'$'+Number(n).toLocaleString('en-CA');
const dayDate=day=>new Date(day+'T12:00:00Z');
const dateFormat=options=>new Intl.DateTimeFormat('en-US',{...options,timeZone:'UTC'});
const weekdayFormat=dateFormat({weekday:'long'}),longDate=dateFormat({month:'long',day:'numeric'}),shortDate=dateFormat({month:'short',day:'numeric'});
const dayName=day=>weekdayFormat.format(dayDate(day));
const dateName=day=>longDate.format(dayDate(day));
const dateShort=day=>shortDate.format(dayDate(day));
const chooseDay=day=>dayName(day).slice(0,3)+', '+dateShort(day);
const count=(n,label)=>n+' '+label+(n===1?'':'s');
const hours=minutes=>count(Number((minutes/60).toFixed(2)),'hour');
const item=(product,n)=>product==='cake'?count(n,'cake'):n+' dozen cupcakes';
const kinds={new:'New order',change:'Change request',cancel:'Cancellation'};
const statuses={booked:'Booked',picked_up:'Picked up',cancelled:'Cancelled'};
let state=null, working=false, messageFilter=null, orderFilter=null, confirmAction=null;
const findDay=key=>state.days.find(row=>row.day===key);
function setNotice(message,isError=false){const n=byId('notice');n.classList.toggle('error',isError);n.textContent=message}
function button(label,action,values='',primary=false,small=false,disabled=false){
 return '<button type="button" class="btn'+(primary?' primary':'')+(small?' small':'')+'" data-action="'+action+'" '+values+(disabled?' disabled':'')+'>'+html(label)+'</button>';
}
function setState(next){
 const changedDay=!state||state.today!==next.today||state.complete!==next.complete;
 state=next;
 if(changedDay){messageFilter=next.has_source&&!next.complete?next.today:(next.requests.at(-1)?.day??next.today);orderFilter=next.complete?'all':next.today}
 render();
}
function render(){
 const s=state;
 byId('year').textContent=s.today.slice(0,4)+' · Simulated business day';
 byId('date').textContent=dateName(s.today);byId('weekday').textContent=dayName(s.today);
 byId('phase').textContent=s.complete?'All days closed':!s.has_source?'Pickups only':s.fetched?'Inbox up to date':'Messages ready to load';
 let controls='';
 if(!s.complete){
  if(s.has_source) controls+=button(s.fetched?'Messages loaded':'Get today’s messages','fetch',s.fetched?'':'aria-describedby="fetchGuidance"',!s.fetched,false,s.fetched);
  const finalDay=s.today===s.days.at(-1).day;
  const label=finalDay?'Close final day':!s.has_source?'Close day & continue':'Accept remaining & close day';
  controls+=button(label,'advance','',s.fetched||!s.has_source,false,s.has_source&&!s.fetched);
 }
 byId('mainActions').innerHTML=controls;
 byId('fetchGuidance').hidden=s.complete||s.fetched||!s.has_source;
 byId('earned').textContent=money(s.totals.earned);byId('booked').textContent=money(s.totals.booked);byId('cost').textContent=money(s.totals.helper_cost);
 byId('weekBody').innerHTML=(s.weeks??[]).map(w=>{
  const scheduledThrough=s.days.at(-1).day;
  const label=dateShort(w.start)+' – '+dateShort(w.end)+', '+w.end.slice(0,4);
  return '<section class="week-block" data-week="'+html(w.start)+'"><h3>'+html(label)+'</h3><dl><dt>Revenue from pickups</dt><dd>'+money(w.earned)+'</dd><dt>Value awaiting pickup</dt><dd>'+money(w.booked)+'</dd><dt>Booked helper costs</dt><dd>'+money(w.helper_cost)+'</dd><dt>Orders picked up</dt><dd>'+Number(w.pickups)+'</dd></dl>'+(w.end>scheduledThrough?'<p>Course schedule ends '+dateName(scheduledThrough)+'.</p>':'')+'</section>';
 }).join('');
 renderMessages();renderCalendar();renderOrders();setControls();
}
function fillSelect(id,choices,selected){
 const e=byId(id);e.innerHTML=choices.map(([v,t])=>'<option value="'+html(v)+'">'+html(t)+'</option>').join('');e.value=selected;e.hidden=choices.length<2;
}
function renderMessages(){
 const expanded=new Set([...byId('messageBody').querySelectorAll('.raw[open]')].map(el=>el.closest('[data-request-id]').dataset.requestId));
 const choices=[...new Set(state.requests.map(r=>r.day).concat(state.has_source&&!state.complete?[state.today]:[]))].sort();
 if(!choices.includes(messageFilter)) messageFilter=choices.at(-1)??state.today;
 fillSelect('msgFilter',choices.map(d=>[d,chooseDay(d)]),messageFilter);
 const rows=state.requests.filter(r=>r.day===messageFilter),n=rows.filter(r=>r.status==='pending').length;
 const current=messageFilter===state.today&&!state.complete;
 byId('msgCount').textContent=rows.length?chooseDay(messageFilter)+' · '+count(rows.length,'message')+' · '+n+' pending':(current?'Today’s messages':'Loaded messages');
 if(!rows.length){byId('messageBody').innerHTML='<div class="empty"><strong>'+((current&&state.has_source&&!state.fetched)?'Today’s inbox is ready':'No messages')+'</strong>'+((current&&state.has_source&&!state.fetched)?'Select “Get today’s messages” to review new orders, changes and cancellations.':'There are no messages to show for this date.')+'</div>';return}
 byId('messageBody').innerHTML='<div class="messages">'+rows.map(r=>{
  const d=findDay(r.pickup),change=r.kind==='change',cancel=r.kind==='cancel';
  const label=r.status==='pending'?'Pending':r.status==='rejected'?'Rejected':change?'Updated':cancel?'Cancelled':'Accepted';
  const desc=(cancel?'Cancel order':(change?'Change to ':'')+item(r.product,r.qty))+' · Pickup '+chooseDay(r.pickup);
  let facts='',fits=true;
  if(r.status==='pending'&&d){
   const need=r.qty*(r.product==='cake'?120:30),remaining=d.capacity-d.used;
   fits=need<=remaining;
   facts='<p class="capacity-fact">Needs '+hours(need)+' · '+hours(remaining)+' available on pickup day'+(fits?'':'. There isn’t enough oven time to accept this order.')+'</p>';
  }
  const actions=r.status==='pending'&&current?'<div class="actions">'+button('Accept','accept','data-id="'+r.id+'"',true,true,!fits)+button('Reject','reject','data-id="'+r.id+'"',false,true)+'</div>':'';
  return '<article class="msg '+html(r.status)+'" data-request-id="'+r.id+'"><div class="mhead"><h3>'+html(r.customer)+'<small>'+html(kinds[r.kind])+'</small></h3><span class="badge '+html(r.status)+'">'+label+'</span></div><p class="description">'+html(desc)+'</p>'+facts+(r.reason?'<p class="reason">'+html(r.reason)+'</p>':'')+'<div class="mfoot"><details class="raw"'+(expanded.has(String(r.id))?' open':'')+'><summary>Original message</summary><pre lang="en">'+html(r.raw)+'</pre></details>'+actions+'</div></article>';
 }).join('')+'</div>';
}
function renderCalendar(){
 byId('calBody').innerHTML=state.days.map(d=>{
  const canBook=!state.complete&&!d.helper&&(dayDate(d.day)-dayDate(state.today))/86400000>=2;
  const helper=d.helper?'<span class="helper-on">Helper booked</span>':canBook?'<button type="button" class="helper" data-action="helper" data-day="'+html(d.day)+'" aria-label="Book helper for '+dateName(d.day)+'">Book helper</button>':'';
  const pct=Math.min(100,Math.max(0,100*d.used/d.capacity));
  return '<div class="cal-day'+(d.day===state.today?' today':'')+'" data-capacity-day="'+html(d.day)+'"><div class="cal-top"><strong>'+dateShort(d.day)+'<small>'+dayName(d.day).slice(0,3)+(d.day===state.today?' · Today':'')+'</small></strong><span class="free">'+hours(d.capacity-d.used)+' left</span></div><div class="cal-bottom"><span class="meter" aria-hidden="true"><i style="width:'+pct+'%"></i></span><span class="fine">'+(d.used/60)+' of '+hours(d.capacity)+' used</span>'+helper+'</div></div>';
 }).join('');
}
function renderOrders(){
 fillSelect('orderFilter',[['all','All pickup dates'],...state.days.map(d=>[d.day,chooseDay(d.day)])],orderFilter);
 const rows=state.orders.filter(r=>orderFilter==='all'||r.pickup===orderFilter).slice().sort((a,b)=>a.pickup.localeCompare(b.pickup)||a.id-b.id);
 const open=rows.filter(r=>r.status==='booked').length;
 byId('orderCount').textContent=count(rows.length,'order')+' · '+open+' awaiting pickup'+(orderFilter==='all'?'':' · '+money(findDay(orderFilter).sales)+' in orders · Pickup at 5 PM');
 if(!rows.length){byId('orderBody').innerHTML='<p class="none">No orders are scheduled for this date.</p>';return}
 byId('orderBody').innerHTML='<table class="order-table"><caption class="sr">Bakery orders by pickup date</caption><thead><tr><th scope="col">Customer</th><th scope="col">Product</th><th scope="col">Pickup</th><th scope="col">Amount</th><th scope="col">Status</th></tr></thead><tbody>'+rows.map(r=>'<tr><td><strong>'+html(r.customer)+'</strong><small>#'+html(r.id)+'</small></td><td>'+html(item(r.product,r.qty))+'<small>'+hours(r.minutes)+'</small></td><td>'+dateShort(r.pickup)+'</td><td class="money">'+money(r.amount)+'</td><td><span class="badge '+html(r.status)+'">'+html(statuses[r.status])+'</span></td></tr>').join('')+'</tbody></table>';
}
function setControls(){document.querySelectorAll('[data-action]').forEach(b=>{if(working){b.disabled=true}})}
const isPreview=Boolean(window.bakeryPreview||document.querySelector('script[src="./client.js"]'));
async function localRequest(url,options){
 const response=await fetch(url,options);
 const data=await response.json().catch(()=>{throw Error('The order desk returned an unreadable response. Refresh the page to try again.')});
 if(!response.ok)throw Error(data.error||'The action could not be completed.');
 return data;
}
const transport=isPreview?window.bakeryPreview:{
 state:()=>localRequest('./api/state',{cache:'no-store'}),
 apply:payload=>localRequest('./api/action',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify(payload)})
};
async function readState(){
 if(!transport)throw Error('The browser preview could not start. Refresh the page to load it again.');
 setState(await transport.state());
}
function focusResult(payload,message,isError){
 document.querySelector('.local-feedback')?.remove();
 let target;
 if(payload.action==='helper')target=document.querySelector('[data-capacity-day="'+payload.day+'"]');
 if(payload.id!==undefined)target=document.querySelector('[data-request-id="'+payload.id+'"]');
 if(payload.action==='fetch')target=byId('messages');
 if(!target){byId('notice').focus();return}
 const result=document.createElement('p');result.className='local-feedback'+(isError?' error':'');
 result.textContent=message;result.tabIndex=-1;
 if(payload.action==='fetch')byId('messageBody').before(result);else target.append(result);
 result.focus({preventScroll:true});result.scrollIntoView({block:'nearest'});
}
async function send(payload){
 if(working||!state)return;
 const version=state.version;working=true;setControls();setNotice(payload.action==='fetch'?'Loading today’s messages…':'Saving…');
 let message='',failed=false;
 try{
  const result=await transport.apply({...payload,version});
  if(payload.action==='fetch')messageFilter=result.state.today;
  setState(result.state);message=result.message;
 }catch(e){
  try{await readState()}catch(_){}
  failed=true;message=e.message||'Couldn’t reach the order desk. Refresh the page to try again.';
 }finally{
  working=false;if(state)render();
  setNotice(message,failed);focusResult(payload,message,failed);
 }
}
function confirmWith(title,detail,label,payload){
 confirmAction=payload;byId('dialogTitle').textContent=title;byId('dialogText').textContent=detail;byId('dialogYes').textContent=label;
 const el=byId('approval');el.returnValue='';el.showModal();
}
document.addEventListener('click',event=>{
 const b=event.target.closest('[data-action]');if(!b||b.disabled||working||!state)return;
 const action=b.dataset.action;
 if(action==='accept'){
  const id=Number(b.dataset.id),r=state.requests.find(x=>x.id===id);
  const minutes=r.qty*(r.product==='cake'?120:30);
  confirmWith('Accept the order from '+r.customer+'?',item(r.product,r.qty)+' for pickup on '+dateName(r.pickup)+'. This reserves '+hours(minutes)+' of oven time.','Accept order',{action,id});return;
 }
 if(action==='helper'){const d=b.dataset.day;confirmWith('Book a helper for '+dateName(d)+'?','The helper costs $100 and increases oven time from 8 to 12 hours that day.','Book helper',{action,day:d});return}
 if(action==='advance'){
  const pending=state.requests.filter(r=>r.day===state.today&&r.status==='pending').length;
  const pickups=state.orders.filter(o=>o.pickup===state.today&&o.status==='booked').length;
  const finalDay=state.today===state.days.at(-1).day;
  const pre=pending?'The desk will check '+count(pending,'remaining request')+' in the order received. It will accept each order that fits and reject any that exceed the remaining oven time.':'There are no pending requests.';
  const collection=pickups?' At 5 PM, '+count(pickups,'order')+' will be marked as picked up.':' No pickups are scheduled for today.';
  confirmWith('Close '+dateName(state.today)+'?',pre+collection+(finalDay?' This is the final business day.':' The desk will then move to the next day.'),finalDay?'Close final day':'Close day & continue',{action});return;
 }
 if(action==='reject'){const id=Number(b.dataset.id),r=state.requests.find(x=>x.id===id);confirmWith('Reject the order from '+r.customer+'?',item(r.product,r.qty)+' for pickup on '+dateName(r.pickup)+'. Rejecting this request closes it.','Reject order',{action,id});return}
 send({action});
});
byId('approval').addEventListener('close',()=>{const payload=confirmAction;confirmAction=null;if(byId('approval').returnValue==='confirm'&&payload)send(payload)});
byId('msgFilter').addEventListener('change',e=>{messageFilter=e.target.value;renderMessages();setControls()});
byId('orderFilter').addEventListener('change',e=>{orderFilter=e.target.value;renderOrders()});
if(isPreview){
 byId('previewNote').hidden=false;
 setNotice('Opening your saved orders in this browser…');
 window.addEventListener('bakery-preview-progress',event=>{if(!state)setNotice(event.detail)});
}
readState().then(()=>{if(isPreview)setNotice('')}).catch(error=>{
 setNotice(error.message||'Couldn’t load your orders. Refresh the page to try again.',true);
 byId('phase').textContent='Unable to load';
});
</script></body></html>'''


class BakeryHTTPServer(ThreadingHTTPServer):
    daemon_threads = True
    allow_reuse_address = os.name != "nt"
    allow_reuse_port = False

    def server_bind(self):
        if os.name == "nt":
            self.socket.setsockopt(socket.SOL_SOCKET, socket.SO_EXCLUSIVEADDRUSE, 1)
        super().server_bind()

    def __init__(self, port, database):
        self.database = database
        self.instance = instance_key(database)
        super().__init__(("127.0.0.1", port), BakeryHandler)

    def connect(self):
        db = sqlite3.connect(self.database, timeout=8, isolation_level=None)
        db.row_factory = sqlite3.Row
        db.execute("PRAGMA foreign_keys=ON")
        return db


class BakeryHandler(BaseHTTPRequestHandler):
    def log_message(self, fmt, *args):
        if sys.stderr is not None and sys.stderr.isatty():
            super().log_message(fmt, *args)

    def send_content(self, status, body, content_type):
        data = body.encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", content_type + "; charset=utf-8")
        self.send_header("Content-Length", str(len(data)))
        self.send_header("Cache-Control", "no-store")
        self.send_header("X-Content-Type-Options", "nosniff")
        self.send_header("Referrer-Policy", "no-referrer")
        self.end_headers()
        try:
            self.wfile.write(data)
        except (BrokenPipeError, ConnectionResetError, ConnectionAbortedError):
            pass

    def send_json(self, status, value):
        self.send_content(status, json.dumps(value, ensure_ascii=False), "application/json")

    def do_GET(self):
        path = self.path.partition("?")[0]
        if path == "/":
            self.send_content(200, PAGE_HTML, "text/html")
        elif path == "/README.html":
            try:
                guide = (BASE_DIR / "README.html").read_text(encoding="utf-8")
            except OSError:
                self.send_json(404, {"error": "The user guide is missing. Place README.html in the same folder as the app."})
            else:
                self.send_content(200, guide, "text/html")
        elif path == "/healthz":
            self.send_json(200, {"app": "mgoc50-lecture9-bakery", "instance": self.server.instance})
        elif path == "/api/state":
            try:
                with closing(self.server.connect()) as db, db:
                    db.execute("BEGIN")
                    snapshot = state(db)
                self.send_json(200, snapshot)
            except sqlite3.Error:
                self.send_json(503, {"error": "Orders could not be loaded. Refresh the dashboard and try again."})
        else:
            self.send_json(404, {"error": "Page not found."})

    def do_POST(self):
        if self.path != "/api/action":
            return self.send_json(404, {"error": "Action not found."})
        port = self.server.server_port
        local_hosts = {f"127.0.0.1:{port}", f"localhost:{port}"}
        host = self.headers.get("Host", "")
        origin = self.headers.get("Origin")
        if host not in local_hosts or (origin and origin != f"http://{host}"):
            return self.send_json(403, {"error": "Open the bakery dashboard on this computer to make changes."})
        if self.headers.get_content_type() != "application/json":
            return self.send_json(415, {"error": "Use the buttons on the bakery dashboard to make changes."})
        try:
            size = int(self.headers.get("Content-Length", "0"))
            if not 0 < size <= 4096:
                raise ValueError
            payload = json.loads(self.rfile.read(size))
            if not isinstance(payload, dict) or type(payload.get("version")) is not int:
                raise ValueError
        except (ValueError, UnicodeError):
            return self.send_json(400, {"error": "This action could not be read. Refresh the dashboard and try again."})
        try:
            with closing(self.server.connect()) as db, db:
                db.execute("BEGIN IMMEDIATE")
                if payload["version"] != state(db)["version"]:
                    raise ValueError("The dashboard has changed. Review the latest orders before trying again.")
                message = apply_action(db, payload)
                snapshot = state(db)
            self.send_json(200, {"state": snapshot, "message": message})
        except ValueError as exc:
            self.send_json(409, {"error": str(exc)})
        except (OSError, UnicodeError):
            self.send_json(503, {"error": "The bakery data files could not be read. Check that the original message and order files are in the same folder as the app."})
        except sqlite3.Error:
            self.send_json(503, {"error": "Your change could not be saved. Refresh the dashboard and try again."})


def instance_key(database):
    locations = [os.path.normcase(str(path.resolve())) for path in (BASE_DIR, database)]
    return hashlib.sha256("\n".join(locations).encode("utf-8")).hexdigest()


def existing_app(url, database):
    try:
        with urlopen(url + "healthz", timeout=2) as response:
            info = json.load(response)
            return (info.get("app") == "mgoc50-lecture9-bakery"
                    and info.get("instance") == instance_key(database))
    except (OSError, URLError, ValueError, AttributeError):
        return False


def start():
    parser = argparse.ArgumentParser(description="MGOC50 bakery order dashboard")
    parser.add_argument("--port", type=int, help="Local server port (default: 8000)")
    parser.add_argument("--database", type=Path, default=BASE_DIR / "bakery.db",
                        help="SQLite data file (default: bakery.db in the app folder)")
    parser.add_argument("--open", action="store_true", help="Open the dashboard in the browser")
    args = parser.parse_args()
    open_browser = args.open or len(sys.argv) == 1
    if args.port is not None:
        ports = [args.port]
    else:
        ports = range(8000, 8010) if open_browser else [8000]
    for port in ports:
        url = f"http://127.0.0.1:{port}/"
        try:
            server = BakeryHTTPServer(port, args.database)
        except OSError as exc:
            if exc.errno not in (errno.EADDRINUSE, 10048) and getattr(exc, "winerror", None) != 10013:
                raise
            if open_browser and existing_app(url, args.database):
                webbrowser.open(url)
                return
        else:
            break
    else:
        raise RuntimeError("The local server port is in use. Close the other service and reopen the bakery dashboard.")
    with server:
        with closing(server.connect()) as db, db:
            db.execute("BEGIN IMMEDIATE")
            init_db(db)
        if open_browser:
            address = f"http://127.0.0.1:{server.server_port}/"
            threading.Timer(0.25, webbrowser.open, args=(address,)).start()
        server.serve_forever()


if __name__ == "__main__":
    try:
        start()
    except KeyboardInterrupt:
        pass
    except (RuntimeError, ValueError, OSError, sqlite3.Error) as exc:
        if sys.stderr is None and sys.platform == "win32":
            import ctypes
            ctypes.windll.user32.MessageBoxW(None, str(exc), "Bakery Dashboard", 16)
        else:
            raise

