import os, re, csv, io, sys, time, unicodedata
from urllib.parse import unquote
from flask import Flask, request, jsonify, send_from_directory, render_template
from flask_sqlalchemy import SQLAlchemy
import requests
from bs4 import BeautifulSoup
from werkzeug.utils import secure_filename

app = Flask(__name__, static_folder='static', template_folder='templates')

# ================== DATABASE FIX (HEROKU SAFE) ==================
DATABASE_URL = os.getenv("DATABASE_URL")

if DATABASE_URL:
    # Fix for SQLAlchemy 3 + Heroku
    if DATABASE_URL.startswith("postgres://"):
        DATABASE_URL = DATABASE_URL.replace("postgres://", "postgresql://", 1)
else:
    DATABASE_URL = "sqlite:///local.db"

app.config["SQLALCHEMY_DATABASE_URI"] = DATABASE_URL
app.config["SQLALCHEMY_TRACK_MODIFICATIONS"] = False

# Required for Heroku Postgres (SSL)
if DATABASE_URL.startswith("postgresql://"):
    app.config["SQLALCHEMY_ENGINE_OPTIONS"] = {
        "pool_pre_ping": True,
        "connect_args": {"sslmode": "require"}
    }

db = SQLAlchemy(app)
# ================================================================

# ---------------- ENV CONFIG ----------------
UPSTREAM_BASE = os.getenv("UPSTREAM_BASE", "http://mysmsportal.com")
LOGIN_FORM_RAW = os.getenv("LOGIN_FORM_RAW", "")
PHPSESSID_OVERRIDE = os.getenv("PHPSESSID_OVERRIDE", "")
USER_AGENT = "Mozilla/5.0 (Windows NT 10.0; Win64; x64; rv:144.0) Gecko/20100101 Firefox/144.0"

# ---------------- MODELS ----------------
class Allocation(db.Model):
    id = db.Column(db.Integer, primary_key=True)
    client_external_id = db.Column(db.String, nullable=False)
    range_code = db.Column(db.String, nullable=False)
    quantity = db.Column(db.Integer, nullable=False)
    status = db.Column(db.String, nullable=False)
    response = db.Column(db.Text)
    created_at = db.Column(db.DateTime, server_default=db.func.now())

with app.app_context():
    db.create_all()

# ---------------- HELPERS ----------------
BASE = UPSTREAM_BASE
LOGIN_PATH = "/index.php?login=1"
ALL_PATH   = "/index.php?opt=shw_all_v2"
TODAY_PATH = "/index.php?opt=shw_sts_today"

HEADERS = {
    "User-Agent": USER_AGENT,
    "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
    "Content-Type": "application/x-www-form-urlencoded",
    "Origin": BASE,
    "Referer": BASE + "/index.php?opt=shw_all_v2",
}

def parse_form_encoded(raw):
    parts = [p for p in raw.split("&") if "=" in p]
    return {k: unquote(v) for k, v in (p.split("=", 1) for p in parts)}

def do_login(sess):
    if not LOGIN_FORM_RAW:
        return None
    data = parse_form_encoded(LOGIN_FORM_RAW)
    hdr = dict(HEADERS)
    hdr["Referer"] = BASE + "/index.php?opt=shw_allo"
    return sess.post(BASE + LOGIN_PATH, data=data, headers=hdr, allow_redirects=True, timeout=15)

def attach_session(sess):
    if PHPSESSID_OVERRIDE:
        domain = BASE.replace("http://","").replace("https://","").split("/")[0]
        sess.cookies.set("PHPSESSID", PHPSESSID_OVERRIDE, domain=domain)
    else:
        do_login(sess)

# ---------------- ROUTES ----------------
@app.route("/")
def index():
    return render_template("index.html")

@app.route("/api/clients")
def api_clients():
    sess = requests.Session()
    attach_session(sess)
    r = sess.get(BASE + ALL_PATH, headers=HEADERS, timeout=15)
    soup = BeautifulSoup(r.text, "lxml")
    out = []
    for opt in soup.select("select[name=selidd] option"):
        val = (opt.get("value") or "").strip()
        if val:
            out.append({"name": opt.get_text(strip=True), "external_id": val})
    return jsonify(out)

@app.route("/api/allocate", methods=["POST"])
def api_allocate():
    data = request.json or {}
    selidd = data.get("selidd")
    selrng = data.get("selrng")
    qty = int(data.get("quantity", 0))

    if not selidd or not selrng or qty <= 0:
        return jsonify({"error": "invalid params"}), 400

    sess = requests.Session()
    attach_session(sess)

    resp = sess.post(BASE + ALL_PATH, data={
        "quantity": qty,
        "selidd": selidd,
        "selrng": selrng,
        "allocate": "1"
    }, headers=HEADERS, timeout=20)

    status = "success" if resp.status_code == 200 else "error"
    a = Allocation(
        client_external_id=selidd,
        range_code=selrng,
        quantity=qty,
        status=status,
        response=resp.text[:200]
    )
    db.session.add(a)
    db.session.commit()
    return jsonify({"status": status, "id": a.id})

@app.route("/api/history")
def api_history():
    rows = Allocation.query.order_by(Allocation.created_at.desc()).limit(200).all()
    return jsonify([
        {
            "id": r.id,
            "client_external_id": r.client_external_id,
            "range_code": r.range_code,
            "quantity": r.quantity,
            "status": r.status,
            "created_at": r.created_at.isoformat()
        } for r in rows
    ])

@app.route("/static/<path:p>")
def static_files(p):
    return send_from_directory("static", p)

if __name__ == "__main__":
    app.run(host="0.0.0.0", port=int(os.environ.get("PORT", 5000)))
