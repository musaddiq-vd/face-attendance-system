import os
import secrets
from datetime import timedelta

BASE_DIR = os.path.abspath(os.path.dirname(__file__))


def _load_env_file():
    """Reads KEY=VALUE lines from a local .env file (listed in .gitignore), so settings do not
    have to be typed in the terminal every time. Real environment variables win over the file."""
    path = os.path.join(BASE_DIR, ".env")
    if not os.path.exists(path):
        return
    with open(path, encoding="utf-8") as f:
        for line in f:
            line = line.strip()
            if line and not line.startswith("#") and "=" in line:
                key, value = line.split("=", 1)
                value = value.split(" #", 1)[0]  # allow a comment after the value
                os.environ.setdefault(key.strip(), value.strip().strip("\"'"))


_load_env_file()

# Where private data is kept: database, photos and generated keys. Default: the project folder.
DATA_DIR = os.path.abspath(os.environ.get("DATA_DIR") or BASE_DIR)
os.makedirs(DATA_DIR, exist_ok=True)


def _local_secret(env_name, file_name, nbytes):
    """Use the environment variable if set. Otherwise create a random value once and keep it
    in a local file (listed in .gitignore), so nothing secret is ever written in the code."""
    if os.environ.get(env_name):
        return os.environ[env_name]
    path = os.path.join(DATA_DIR, file_name)
    if not os.path.exists(path):
        with open(path, "w") as f:
            f.write(secrets.token_urlsafe(nbytes))
    with open(path) as f:
        return f.read().strip()


class Config:
    SECRET_KEY = _local_secret("SECRET_KEY", ".secret_key", 32)
    SQLALCHEMY_DATABASE_URI = os.environ.get(
        "DATABASE_URL", "sqlite:///" + os.path.join(DATA_DIR, "attendance.db"))
    SQLALCHEMY_TRACK_MODIFICATIONS = False

    # Session cookie safety
    SESSION_COOKIE_HTTPONLY = True
    SESSION_COOKIE_SAMESITE = "Lax"
    # Set SESSION_COOKIE_SECURE=1 when the app is served over HTTPS
    SESSION_COOKIE_SECURE = os.environ.get("SESSION_COOKIE_SECURE") == "1"
    # People are logged out after this long without having to close the browser
    PERMANENT_SESSION_LIFETIME = timedelta(hours=12)

    # Kiosk page is opened as /kiosk?key=<KIOSK_KEY>. The admin dashboard shows the full link.
    KIOSK_KEY = _local_secret("KIOSK_KEY", ".kiosk_key", 12)

    # Check-in / check-out photos are saved here
    SNAPSHOT_DIR = os.environ.get("SNAPSHOT_DIR", os.path.join(DATA_DIR, "snapshots"))

    # Optional employee profile photos
    PROFILE_DIR = os.environ.get("PROFILE_DIR", os.path.join(DATA_DIR, "profile_photos"))
    MAX_CONTENT_LENGTH = 3 * 1024 * 1024  # largest upload allowed (3 MB)

    # Email notifications (optional). Leave SMTP_HOST empty to turn email off.
    SMTP_HOST = os.environ.get("SMTP_HOST", "")
    SMTP_PORT = int(os.environ.get("SMTP_PORT", "587"))
    SMTP_USER = os.environ.get("SMTP_USER", "")
    SMTP_PASSWORD = os.environ.get("SMTP_PASSWORD", "")
    SMTP_FROM = os.environ.get("SMTP_FROM", "") or os.environ.get("SMTP_USER", "")
    SMTP_USE_SSL = os.environ.get("SMTP_USE_SSL") == "1"   # port 465 style; otherwise STARTTLS
    EMAIL_ASYNC = True  # send in the background so pages stay fast

    # Login protection: block an IP after this many wrong passwords, for this many seconds
    MAX_FAILED_LOGINS = 5
    LOGIN_BLOCK_SECONDS = 600


# Office rules. These are only the starting values — admin changes them
# from the Settings page and they are saved in the database.
DEFAULT_SETTINGS = {
    "office_start": "09:00",      # HH:MM
    "office_end": "17:00",        # HH:MM
    "late_grace_minutes": 15,     # check-in after start + grace = Late
    "half_day_hours": 4.0,        # worked less than this = Half day
    "weekly_off": [6],            # Monday=0 ... Sunday=6
    "scan_cooldown_seconds": 120, # same person ignored for this long after a scan
    "match_threshold": 0.5,       # lower = stricter face match
    "save_photos": True,          # store a small photo at check-in / check-out
    "photo_keep_days": 90,        # photos older than this are deleted automatically
}
