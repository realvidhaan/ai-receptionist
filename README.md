# AI Receptionist

A free, real-time **AI voice receptionist** for an HVAC business, built on **Google Gemini Live**
(no Vapi, no paid voice platform). A caller opens a web page, talks to the receptionist in
their browser, and it can answer questions, **book / reschedule / cancel appointments on Google
Calendar**, and keep a **customer database + call log in Google Sheets** — all driven by
function-calling.

## How it works

```
Browser (mic) ──realtime voice──► Gemini Live  (gemini-3.1-flash-live-preview)
      │  page + single-use token served by ►  server.py  (Python, no framework)
      └── tool calls ──► /tool ──► google_backend.py ──► Google Calendar + Sheets (service account)
```

- **server.py** — serves the page (`GET /`), mints a **constrained, single-use, 30-min ephemeral
  token** (`POST /token`) so the real API key never reaches the browser, and relays tool calls
  (`POST /tool`). Builds the receptionist's persona, grounded in verified company facts (with an
  optional Google-Search enrichment step via Gemini).
- **google_backend.py** — the tools: `check_availability`, `book_appointment`,
  `reschedule_appointment`, `cancel_appointment`, `lookup_customer`, `log_call`,
  `transfer_to_human`. Talks to Google Calendar + Sheets with a **service account**.
- **demo.html** — the browser client: captures mic at 16 kHz, streams to Gemini Live, plays the
  24 kHz reply, and relays function calls to the backend.
- **config.py** — loads secrets/settings from `.env` and holds the company profile.

## Safety & correctness hardening

- **Emergency triage** — gas/CO/smoke/sparking/dizziness → the agent tells the caller to get to
  safety and call 911 / the gas company / an ambulance **before** anything else, and never books
  first.
- **Anti-fabrication** — the agent may only state owner-verified facts; it never invents pricing,
  policy numbers, insurance/legal claims, or guarantees — it defers to the office instead.
- **No double-booking** — `book_appointment` and `reschedule` reject any time that overlaps an
  existing event and offer real open slots; availability is computed from the live calendar.
- **Real confirmation codes** — booking returns a verifiable code; the agent never makes one up.
- **Light identity check** — name is confirmed before a reschedule/cancel.

## Setup

1. **Python deps**: `pip install -r requirements.txt`
2. **Google Cloud** (one project): enable **Calendar API** + **Sheets API**; create a **service
   account** and download its JSON key.
3. **Share with the service account email**: a Google Sheet named with tabs `Customers`
   (`phone | name | email | address | last_service | notes | status | event_id | appt_time`) and
   `Call Log` (`timestamp | caller | phone | intent | outcome | summary`), and the Google Calendar
   you want appointments on (permission: *make changes to events*).
4. **Gemini key** from Google AI Studio.
5. Copy `.env.example` → `.env` and fill it in.

## Run

```bash
python3 server.py
# open http://localhost:8765 and click "Talk to the receptionist"
```

To let a real caller reach it, expose it with a free tunnel:

```bash
cloudflared tunnel --url http://localhost:8765
```

## Security

Secrets live only in `.env` (gitignored). The browser only ever receives a single-use, 30-minute
token locked to one model + persona + toolset — never the real API key. Never commit `.env` or the
service-account JSON.
