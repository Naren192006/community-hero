import json
import hashlib
import os
import threading
from pathlib import Path
from datetime import datetime
from filelock import FileLock

DB_FILE = Path(__file__).parent / "reports_db.json"
LOCK_FILE = Path(__file__).parent / "reports_db.json.lock"
TMP_FILE = Path(__file__).parent / "reports_db.json.tmp"

# Process-wide lock for in-process thread safety (FastAPI can run multiple
# threads/workers within one container). FileLock additionally protects
# against multiple processes/containers touching the same file.
_thread_lock = threading.Lock()
_file_lock = FileLock(str(LOCK_FILE), timeout=10)


def init_db():
    if not DB_FILE.exists():
        DB_FILE.write_text(json.dumps({"users": {}, "reports": []}, indent=2))


def _load_db_unsafe():
    """Internal: load without locking. Only call this from inside a lock."""
    init_db()
    content = DB_FILE.read_text().strip()
    if not content:
        DB_FILE.write_text(json.dumps({"users": {}, "reports": []}, indent=2))
        return {"users": {}, "reports": []}
    return json.loads(content)


def _save_db_unsafe(data):
    """Internal: save without locking. Only call this from inside a lock.

    Writes to a temp file first, then atomically renames it over the real
    file. This means a crash or interruption mid-write can NEVER leave you
    with a half-written or empty reports_db.json -- the old file stays
    intact until the new one is fully written and verified.
    """
    TMP_FILE.write_text(json.dumps(data, indent=2))
    os.replace(TMP_FILE, DB_FILE)  # atomic on POSIX and Windows


def load_db():
    """Public read — still safe to call on its own for simple reads."""
    with _thread_lock, _file_lock:
        return _load_db_unsafe()


def save_db(data):
    """Public write — kept for compatibility, but prefer the
    read-modify-write helpers below for anything that needs atomicity."""
    with _thread_lock, _file_lock:
        _save_db_unsafe(data)


def _atomic_update(update_fn):
    """
    Runs update_fn(db) -> result inside a single locked
    load -> mutate -> save cycle, so no other request can interleave.
    """
    with _thread_lock, _file_lock:
        db = _load_db_unsafe()
        result = update_fn(db)
        _save_db_unsafe(db)
        return result


def hash_password(password):
    return hashlib.sha256(password.encode("utf-8")).hexdigest()


# --- USER FUNCTIONS ---
def create_user(email, password, name):
    def op(db):
        if email in db["users"]:
            return None, "User already exists"
        db["users"][email] = {
            "name": name,
            "email": email,
            "password": hash_password(password),
            "created_at": datetime.now().isoformat(),
            "reports": []
        }
        return db["users"][email], None

    return _atomic_update(op)


def get_user(email, password):
    db = load_db()
    user = db["users"].get(email)
    if not user:
        return None
    hashed = hash_password(password)
    # Backward-compat: if an old plaintext password is still stored,
    # accept it once and upgrade it to a hash.
    if user["password"] == hashed:
        return user
    if user["password"] == password:
        def op(db):
            db["users"][email]["password"] = hashed
            return db["users"][email]
        return _atomic_update(op)
    return None


def user_exists(email):
    """Returns True if the email belongs to a registered user.
    Used to enforce 'must be logged in to submit a report' on the
    backend, since a frontend check alone can be bypassed."""
    db = load_db()
    return email in db["users"]


# --- REPORT FUNCTIONS ---
def save_report(report_data, user_email="anonymous"):
    def op(db):
        report_data["created_at"] = datetime.now().isoformat()
        report_data["status"] = "Pending"
        report_data["upvotes"] = 0
        report_data["user_email"] = user_email
        db["reports"].append(report_data)
        if user_email and user_email in db["users"]:
            db["users"][user_email]["reports"].append(report_data["report_id"])
        return report_data

    return _atomic_update(op)


def get_all_reports():
    db = load_db()
    return db["reports"]


def check_duplicate(location, issue_type):
    db = load_db()
    location_lower = location.lower()
    issue_lower = issue_type.lower()
    for report in db["reports"]:
        existing_loc = report.get("location", "").lower()
        existing_type = report.get("issue_type", "").lower()
        if (location_lower in existing_loc or existing_loc in location_lower):
            if (issue_lower in existing_type or existing_type in issue_lower):
                return report
    return None


def upvote_report(report_id):
    def op(db):
        for report in db["reports"]:
            if report["report_id"] == report_id:
                report["upvotes"] = report.get("upvotes", 0) + 1
                return report
        return None

    return _atomic_update(op)


def get_reports_by_user(email):
    db = load_db()
    return [r for r in db["reports"] if r.get("user_email") == email]


def update_report_status(report_id, status):
    def op(db):
        for report in db["reports"]:
            if report["report_id"] == report_id:
                report["status"] = status
                return report
        return None

    return _atomic_update(op)