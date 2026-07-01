"""
Real Google backend for the voice receptionist (service account).
Appointments -> Google Calendar (no overlaps allowed). Customers + Call Log -> Google Sheet. Confirmations/transfer -> SMTP.

Multi-tenant: every Calendar/Sheet call takes the company's own sheet_id / calendar_id, so one service
account drives an unlimited number of companies. run_tool(tool, args, tenant) where
tenant = {"company": {...}, "sheet_id": "...", "calendar_id": "..."}.

Secrets come from config.py (.env).
"""
import json, os, re, ssl, smtplib, urllib.request, urllib.error, urllib.parse, datetime
from email.mime.text import MIMEText
from email.utils import formataddr
from zoneinfo import ZoneInfo
import certifi
from google.oauth2 import service_account
import google.auth.transport.requests as gtr
import config

CTX = ssl.create_default_context(cafile=certifi.where())
TZ = config.TZ
ZONE = ZoneInfo(TZ)
SMTP_USER, SMTP_PW, OWNER_EMAIL = config.SMTP_USER, config.SMTP_PW.replace(" ", ""), config.OWNER_EMAIL
MAPS_KEY = config.MAPS_KEY
CITY_TYPES = {"locality", "postal_town", "sublocality", "sublocality_level_1", "administrative_area_level_3"}

HEADERS = ["phone", "name", "email", "address", "last_service", "notes", "status", "event_id", "appt_time"]
LOG_HEADERS = ["timestamp", "caller", "phone", "intent", "outcome", "summary"]

# New Sheets are created by the owner's Apps Script web app (a personal-account service account has zero
# Drive storage, so it can't own files). At runtime the SA only reads/writes calendars + shared sheets.
SHEET_CREATOR_URL = config.SHEET_CREATOR_URL
_creds = service_account.Credentials.from_service_account_file(
    config.SA_KEY_PATH, scopes=["https://www.googleapis.com/auth/calendar",
                                "https://www.googleapis.com/auth/spreadsheets"])

def _token():
    if not _creds.valid:
        _creds.refresh(gtr.Request())
    return _creds.token

def _req(method, url, body=None):
    data = json.dumps(body).encode() if body is not None else None
    req = urllib.request.Request(url, data=data, method=method,
        headers={"Authorization": f"Bearer {_token()}", "Content-Type": "application/json"})
    try:
        r = urllib.request.urlopen(req, timeout=30, context=CTX); raw = r.read()
        return json.loads(raw) if raw else {}
    except urllib.error.HTTPError as e:
        return {"_err": e.code, "_body": e.read().decode("utf-8", "ignore")[:300]}

# ---------- Calendar (per-tenant calendar_id) ----------
def _cal(cid, p): return f"https://www.googleapis.com/calendar/v3/calendars/{urllib.parse.quote(cid)}/events{p}"
def cal_create(cid, ev):     return _req("POST", _cal(cid, ""), ev)
def cal_patch(cid, eid, ev): return _req("PATCH", _cal(cid, f"/{eid}"), ev)
def cal_delete(cid, eid):    return _req("DELETE", _cal(cid, f"/{eid}"))
def cal_list(cid, tmin, tmax):
    q = urllib.parse.urlencode({"timeMin": tmin, "timeMax": tmax, "singleEvents": "true", "orderBy": "startTime"})
    return _req("GET", _cal(cid, f"?{q}")).get("items", [])

# ---------- Sheets (per-tenant sheet_id) ----------
def _sheet_get(sid, rng):
    return _req("GET", f"https://sheets.googleapis.com/v4/spreadsheets/{sid}/values/{urllib.parse.quote(rng)}").get("values", [])
def _sheet_update(sid, rng, values):
    return _req("PUT", f"https://sheets.googleapis.com/v4/spreadsheets/{sid}/values/{urllib.parse.quote(rng)}?valueInputOption=USER_ENTERED", {"values": values})
def _sheet_append(sid, rng, values):
    return _req("POST", f"https://sheets.googleapis.com/v4/spreadsheets/{sid}/values/{urllib.parse.quote(rng)}:append?valueInputOption=USER_ENTERED&insertDataOption=INSERT_ROWS", {"values": values})

def _digits(p):
    """Last 10 digits, so '(408) 555-0199', '408-555-0199', '+1 408 555 0199' all match."""
    return re.sub(r"\D", "", p or "")[-10:]

def _valid_phone(p):
    """Real US/NANP number: 10 digits, area & exchange codes start 2-9 (rejects '6767', '0000000000')."""
    d = re.sub(r"\D", "", p or "")
    if len(d) == 11 and d[0] == "1":
        d = d[1:]
    return len(d) == 10 and d[0] in "23456789" and d[3] in "23456789"

def _validate_address(addr):
    """Confirm a real street address via Places API (New). Requires the caller to have stated the street
    number AND city that Google resolves to, so vague input like 'my house' or 'Sunnyvale apartment' fails.
    Returns (ok, normalized_formatted_address)."""
    if not (addr or "").strip() or not MAPS_KEY:
        return False, None
    try:
        req = urllib.request.Request("https://places.googleapis.com/v1/places:searchText",
            data=json.dumps({"textQuery": addr}).encode(), method="POST",
            headers={"Content-Type": "application/json", "X-Goog-Api-Key": MAPS_KEY,
                     "X-Goog-FieldMask": "places.formattedAddress,places.addressComponents"})
        r = json.load(urllib.request.urlopen(req, timeout=20, context=CTX))
    except Exception:
        return False, None
    places = r.get("places", [])
    if not places:
        return False, None
    comps = places[0].get("addressComponents", [])
    snum = next((c["longText"] for c in comps if "street_number" in c.get("types", [])), None)
    city = next((c["longText"] for c in comps if CITY_TYPES & set(c.get("types", []))), None)
    inp = addr.lower()
    ok = bool(snum) and snum in addr and bool(city) and city.lower() in inp
    return (ok, places[0].get("formattedAddress")) if ok else (False, None)

def _name_match(a, b):
    a, b = (a or "").strip().lower(), (b or "").strip().lower()
    return bool(a) and bool(b) and (a == b or a in b or b in a)

def _find_customer(sid, phone):
    target = _digits(phone)
    if not target:
        return None, None
    for i, row in enumerate(_sheet_get(sid, "Customers!A2:I")):
        if row and _digits(row[0]) == target:
            return i + 2, {HEADERS[j]: (row[j] if j < len(row) else "") for j in range(len(HEADERS))}
    return None, None

def _upsert_customer(sid, d):
    row = [d.get(h, "") for h in HEADERS]
    idx, _ = _find_customer(sid, d.get("phone", ""))
    if idx:
        _sheet_update(sid, f"Customers!A{idx}:I{idx}", [row])
    else:
        _sheet_append(sid, "Customers!A:I", [row])

# ---------- time helpers ----------
def _to_dt(s):
    s = (s or "").strip().replace("Z", "+00:00")
    dt = datetime.datetime.fromisoformat(s)
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=ZONE)
    return dt.astimezone(ZONE)

def _pretty(dt): return dt.strftime("%a %b %-d at %-I:%M %p")
def _ampm(dt):   return dt.strftime("%-I:%M %p")

def _email(to, subject, body, from_name="Receptionist"):
    try:
        m = MIMEText(body, "plain"); m["Subject"] = subject
        m["From"] = formataddr((from_name, SMTP_USER)); m["To"] = to
        with smtplib.SMTP_SSL("smtp.gmail.com", 465, context=CTX, timeout=20) as s:
            s.login(SMTP_USER, SMTP_PW); s.sendmail(SMTP_USER, [to], m.as_string())
        return True
    except Exception as e:
        print("  (email failed:", str(e)[:120], ")"); return False

# ---------- scheduling / conflict logic ----------
def _conflicts(cid, start, end, exclude_id=None):
    """Return overlapping non-cancelled events in [start, end)."""
    out = []
    for e in cal_list(cid, start.isoformat(), end.isoformat()):
        if e.get("status") == "cancelled" or e.get("id") == exclude_id:
            continue
        if e.get("start", {}).get("dateTime"):  # skip all-day
            out.append(e)
    return out

def _free_slots(cid, day, duration_min, company):
    """Real open start-times on `day` within business hours that fit `duration_min` without overlap."""
    oh, ch = company.get("open_hour", 8), company.get("close_hour", 18)
    day_start = datetime.datetime.combine(day, datetime.time(0, 0), ZONE)
    day_end = datetime.datetime.combine(day, datetime.time(23, 59), ZONE)
    busy = []
    for e in cal_list(cid, day_start.isoformat(), day_end.isoformat()):
        if e.get("status") == "cancelled":
            continue
        st, en = e.get("start", {}).get("dateTime"), e.get("end", {}).get("dateTime")
        if st and en:
            busy.append((_to_dt(st), _to_dt(en)))
    slots = []
    h = oh
    while h * 60 + duration_min <= ch * 60:
        s = datetime.datetime.combine(day, datetime.time(h, 0), ZONE)
        e = s + datetime.timedelta(minutes=duration_min)
        if not any(bs < e and be > s for bs, be in busy):
            slots.append(s)
        h += 1
    return slots

def _alts(cid, day, duration_min, company):
    slots = _free_slots(cid, day, duration_min, company)
    if slots:
        return "We have openings at " + ", ".join(_ampm(s) for s in slots[:4]) + ". Which works?"
    return "We're fully booked that day. Would another day work?"

# ---------- provisioning: create a fresh Sheet + Calendar for a new company ----------
def create_calendar(summary, tz):
    """Create a secondary calendar owned by the service account. Returns its id (or {_err}})."""
    return _req("POST", "https://www.googleapis.com/calendar/v3/calendars", {"summary": summary, "timeZone": tz})

def share_calendar(cid, email, role="writer"):
    """Grant a human owner access to the calendar via its ACL (uses the Calendar scope)."""
    return _req("POST", f"https://www.googleapis.com/calendar/v3/calendars/{urllib.parse.quote(cid)}/acl",
                {"role": role, "scope": {"type": "user", "value": email}})

def create_sheet(title):
    """Create a Customers+Call Log spreadsheet OWNED BY THE OWNER via their Apps Script web app, which also
    shares it with this service account for runtime read/write. Returns {spreadsheet_id, url} or {_err}.
    (Personal-account service accounts have zero Drive storage, so they can't create the file themselves.)"""
    if not SHEET_CREATOR_URL:
        return {"_err": "no_creator", "_body": "SHEET_CREATOR_URL not set - deploy the sheet-creator Apps Script."}
    try:
        body = json.dumps({"secret": config.PROVISION_SECRET, "title": title,
                           "share_with": _creds.service_account_email,
                           "customers_headers": HEADERS, "log_headers": LOG_HEADERS}).encode()
        req = urllib.request.Request(SHEET_CREATOR_URL, data=body, method="POST",
                                     headers={"Content-Type": "application/json"})
        r = json.load(urllib.request.urlopen(req, timeout=60, context=CTX))
    except Exception as e:
        return {"_err": "creator", "_body": str(e)[:200]}
    if r.get("spreadsheet_id"):
        return {"spreadsheet_id": r["spreadsheet_id"], "url": r.get("url")}
    return {"_err": "creator", "_body": str(r)[:200]}

def provision_company(company, owner_email):
    """Create this company's own Sheet + Calendar (both usable by the owner and this SA). Returns
    {sheet_id, calendar_id, sheet_url} on success, or {error, detail}. Sheet is created first so a
    sheet failure never leaves an orphaned calendar behind."""
    biz = company.get("business", "HVAC Company")
    tz = company.get("tz", TZ)
    sh = create_sheet(f"{biz} - Receptionist DB")
    if "_err" in sh:
        return {"error": "could not create sheet", "detail": sh}
    sid = sh["spreadsheet_id"]
    cal = create_calendar(f"{biz} - Appointments", tz)
    if "_err" in cal or not cal.get("id"):
        return {"error": "could not create calendar", "detail": cal, "sheet_id": sid, "sheet_url": sh.get("url")}
    cid = cal["id"]
    share_calendar(cid, owner_email, "writer")
    return {"calendar_id": cid, "sheet_id": sid, "sheet_url": sh.get("url")}

# ---------- the tools ----------
def run_tool(tool, args, tenant):
    company = tenant["company"]
    sid, cid = tenant["sheet_id"], tenant["calendar_id"]
    biz = company.get("business", "the business")
    dur = int(args.get("duration_min") or company.get("default_duration_min", 60))

    if tool == "check_availability":
        date = (args.get("date") or "").strip()
        try:
            day = datetime.date.fromisoformat(date[:10])
        except Exception:
            return {"result": "What day would you like? I can check availability."}
        slots = _free_slots(cid, day, dur, company)
        if not slots:
            return {"result": f"We're fully booked on {day.strftime('%A, %b %-d')}. Want me to check another day?"}
        return {"result": f"On {day.strftime('%A, %b %-d')} we have openings at: {', '.join(_ampm(s) for s in slots[:4])}."}

    if tool == "book_appointment":
        name, phone, issue = args.get("caller_name", ""), args.get("phone", ""), args.get("issue", "service")
        if not _valid_phone(phone):
            return {"result": "That doesn't look like a complete phone number. What's the best 10-digit number to reach you?"}
        ok_addr, addr_fmt = _validate_address(args.get("address", ""))
        if not ok_addr:
            return {"result": "I couldn't find that address. Could you give me the full service address - street number, street name, and city?"}
        try:
            start = _to_dt(args["start_time"])
        except Exception:
            return {"result": "What time works for you? I can book it then."}
        end = start + datetime.timedelta(minutes=dur)
        if _conflicts(cid, start, end):  # hard double-booking / overlap guard
            return {"result": f"Sorry, {_pretty(start)} is already booked. {_alts(cid, start.date(), dur, company)}"}
        ev = cal_create(cid, {
            "summary": f"{issue} - {name}",
            "description": f"Booked by {biz} AI receptionist.\nName: {name}\nPhone: {phone}\nAddress: {addr_fmt}\nIssue: {issue}",
            "start": {"dateTime": start.isoformat(), "timeZone": TZ},
            "end": {"dateTime": end.isoformat(), "timeZone": TZ},
            "location": addr_fmt,
            "extendedProperties": {"private": {"phone": phone}},
        })
        if "_err" in ev:
            return {"result": "I had trouble saving that to the calendar, so let me take a message and have the office confirm."}
        _upsert_customer(sid, {"phone": phone, "name": name, "email": args.get("email", ""),
                               "address": addr_fmt, "last_service": issue, "notes": "",
                               "status": "booked", "event_id": ev["id"], "appt_time": start.isoformat()})
        if args.get("email"):
            _email(args["email"], f"Your appointment with {biz}",
                   f"Hi {name},\n\nYou're booked with {biz} for {_pretty(start)}.\nService: {issue}\nAddress: {addr_fmt}\n\nReply or call us to make changes.\n\n- {biz}",
                   from_name=biz)
        return {"result": f"Booked {name} for {_pretty(start)} ({issue})."
                          + (" A confirmation email is on the way." if args.get('email') else "")}

    if tool == "reschedule_appointment":
        idx, cust = _find_customer(sid, args.get("phone", ""))
        if not cust or not cust.get("event_id"):
            return {"result": "I couldn't find an active appointment under that number."}
        if args.get("caller_name") and not _name_match(args["caller_name"], cust.get("name", "")):
            return {"result": "Just to confirm I have the right appointment — what's the name it's booked under?"}
        try:
            start = _to_dt(args["new_time"])
        except Exception:
            return {"result": "What new time would you like?"}
        end = start + datetime.timedelta(minutes=dur)
        if _conflicts(cid, start, end, exclude_id=cust["event_id"]):
            return {"result": f"Sorry, {_pretty(start)} is already taken. {_alts(cid, start.date(), dur, company)}"}
        r = cal_patch(cid, cust["event_id"], {"start": {"dateTime": start.isoformat(), "timeZone": TZ},
                                              "end": {"dateTime": end.isoformat(), "timeZone": TZ}})
        if "_err" in r:
            return {"result": "I couldn't move that appointment - it may have been removed."}
        cust["appt_time"] = start.isoformat()
        _sheet_update(sid, f"Customers!A{idx}:I{idx}", [[cust.get(h, "") for h in HEADERS]])
        return {"result": f"Moved your appointment to {_pretty(start)}."}

    if tool == "cancel_appointment":
        idx, cust = _find_customer(sid, args.get("phone", ""))
        if not cust or not cust.get("event_id"):
            return {"result": "I couldn't find an active appointment under that number."}
        if args.get("caller_name") and not _name_match(args["caller_name"], cust.get("name", "")):
            return {"result": "Just to confirm I have the right appointment - what's the name it's booked under?"}
        cal_delete(cid, cust["event_id"])
        cust["status"] = "cancelled"; cust["event_id"] = ""
        _sheet_update(sid, f"Customers!A{idx}:I{idx}", [[cust.get(h, "") for h in HEADERS]])
        return {"result": "Your appointment is cancelled. Anything else?"}

    if tool == "lookup_customer":
        phone = args.get("phone", "")
        idx, cust = _find_customer(sid, phone) if phone else (None, None)
        if not cust and args.get("name"):
            nm = args["name"].lower()
            for row in _sheet_get(sid, "Customers!A2:I"):
                if len(row) > 1 and nm in row[1].lower():
                    cust = {HEADERS[j]: (row[j] if j < len(row) else "") for j in range(len(HEADERS))}; break
        if not cust:
            return {"result": "I don't see you in our system yet - happy to set you up."}
        appt = ""
        if cust.get("status") == "booked" and cust.get("appt_time"):
            try: appt = f" You have an appointment on {_pretty(_to_dt(cust['appt_time']))}."
            except Exception: pass
        return {"result": f"Welcome back, {cust.get('name','')}. Last service: {cust.get('last_service','n/a')}.{appt}"}

    if tool == "log_call":
        ts = datetime.datetime.now(ZONE).strftime("%Y-%m-%d %H:%M")
        _sheet_append(sid, "'Call Log'!A:F", [[ts, args.get("caller_name", ""), args.get("phone", ""),
                                               args.get("intent", ""), args.get("outcome", ""), args.get("summary", "")]])
        return {"result": "logged"}

    if tool == "transfer_to_human":
        _email(OWNER_EMAIL, f"[Receptionist] Callback request - {biz}",
               f"A caller asked for a human.\nReason: {args.get('reason','')}\nCaller: {args.get('caller_name','')}\nCallback: {args.get('callback_number','')}",
               from_name=biz)
        return {"result": "I've passed this to the team and someone will call you back shortly."}

    return {"result": f"(unknown tool {tool})"}
