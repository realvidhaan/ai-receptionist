"""
Central config + secrets loader. Reads a local .env (gitignored) so no secrets live in code.
Both server.py and google_backend.py import from here.
"""
import os

def _load_env(path):
    if os.path.exists(path):
        for line in open(path):
            line = line.strip()
            if line and not line.startswith("#") and "=" in line:
                k, v = line.split("=", 1)
                os.environ.setdefault(k.strip(), v.strip())

_load_env(os.path.join(os.path.dirname(os.path.abspath(__file__)), ".env"))

# --- secrets / environment (set these in .env) ---
GEMINI_KEY   = os.environ.get("GEMINI_KEY", "")
MAPS_KEY     = os.environ.get("MAPS_KEY", "")   # Google Maps Platform key (Places API New) for address validation
SA_KEY_PATH  = os.environ.get("SA_KEY_PATH", "")
SA_KEY_JSON  = os.environ.get("SA_KEY_JSON", "")  # raw service-account JSON (cloud hosts: set this instead of a file)
SHEET_ID     = os.environ.get("SHEET_ID", "")
CALENDAR_ID  = os.environ.get("CALENDAR_ID", "")
SMTP_USER    = os.environ.get("SMTP_USER", "")
SMTP_PW      = os.environ.get("SMTP_PW", "")
OWNER_EMAIL  = os.environ.get("OWNER_EMAIL", "")
# shared secret the /provision endpoint requires (set the same value in the n8n call); "" = no check
PROVISION_SECRET = os.environ.get("PROVISION_SECRET", "")
# public base URL (e.g. your cloudflared tunnel) used to build demo links; "" = use the request Host header
PUBLIC_BASE      = os.environ.get("PUBLIC_BASE", "").rstrip("/")
# Apps Script web-app URL that creates owner-owned Sheets (personal SAs can't own files); "" = can't provision sheets
SHEET_CREATOR_URL = os.environ.get("SHEET_CREATOR_URL", "")

# --- non-secret settings (override via .env if desired) ---
TZ    = os.environ.get("TZ", "America/Los_Angeles")
MODEL = os.environ.get("MODEL", "models/gemini-3.1-flash-live-preview")  # half-cascade live: reliable tools + natural voice
VOICE = os.environ.get("VOICE", "Aoede")                                  # pinned so it never changes
PORT  = int(os.environ.get("PORT", "8765"))

# --- default/fallback company (a NEUTRAL generic HVAC template). Real prospects each get their own
#     provisioned tenant with a web-scraped profile, so this is only the demo shown at "/" with no ?c=. ---
COMPANY = {
    "business": os.environ.get("BUSINESS", "Demo HVAC Co"),
    "city": os.environ.get("CITY", ""),
    "tz": TZ,
    "hours": "Monday to Saturday, 8am to 6pm",
    "service_area": "the local area",
    "services": "heating and cooling repair, installation, and maintenance",
    "emergency": "Call us for urgent no-heat or no-cooling situations",
    "features": "licensed and insured, free estimates, upfront quotes",
    "default_duration_min": 60,
    # business hours window for scheduling (24h clock, local time)
    "open_hour": 8,
    "close_hour": 18,
}
