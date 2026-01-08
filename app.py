import os, re, csv, io, unicodedata
from urllib.parse import unquote
from flask import Flask, request, jsonify, send_from_directory, render_template
from flask_sqlalchemy import SQLAlchemy
import requests
from bs4 import BeautifulSoup
from werkzeug.utils import secure_filename

# =====================================================
# APP
# =====================================================
app = Flask(__name__, static_folder="static", template_folder="templates")

# =====================================================
# DATABASE (HEROKU + SQLITE SAFE)
# =====================================================
DATABASE_URL = os.getenv("DATABASE_URL")

if DATABASE_URL and DATABASE_URL.startswith("postgres://"):
    DATABASE_URL = DATABASE_URL.replace("postgres://", "postgresql://", 1)

if not DATABASE_URL:
    DATABASE_URL = "sqlite:///local.db"

app.config["SQLALCHEMY_DATABASE_URI"] = DATABASE_URL
app.config["SQLALCHEMY_TRACK_MODIFICATIONS"] = False

if DATABASE_URL.startswith("postgresql://"):
    app.config["SQLALCHEMY_ENGINE_OPTIONS"] = {
        "pool_pre_ping": True,
        "connect_args": {"sslmode": "require"},
    }

db = SQLAlchemy(app)

# =====================================================
# 🔐 HARD-CODED UPSTREAM CONFIG (AS REQUESTED)
# =====================================================
UPSTREAM_BASE = "http://mysmsportal.com"

PORTAL_USERNAME = "7944"
PORTAL_PASSWORD = "10-16-2025@Swi"

LOGIN_PATH = "/index.php?login=1"
ALL_PATH   = "/index.php?opt=shw_all_v2"
TODAY_PATH = "/index.php?opt=shw_sts_today"

USER_AGENT = "Mozilla/5.0 (Windows NT 10.0; Win64; x64; rv:144.0) Gecko/20100101 Firefox/144.0"

HEADERS = {
    "User-Agent": USER_AGENT,
    "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
    "Content-Type": "application/x-www-form-urlencoded",
    "Origin": UPSTREAM_BASE,
    "Referer": UPSTREAM_BASE + "/index.php?opt=shw_all_v2",
}

# =====================================================
# MODELS
# =====================================================
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

# =====================================================
# LOGIN (HARD-FIXED)
# =====================================================
def do_login(sess: requests.Session):
    payload = {
        "username": PORTAL_USERNAME,
        "password": PORTAL_PASSWORD,
        "login": "1",
    }
    r = sess.post(
        UPSTREAM_BASE + LOGIN_PATH,
        data=payload,
        headers=HEADERS,
        allow_redirects=True,
        timeout=15,
    )
    if r.status_code != 200:
        raise Exception("Upstream login failed")
    return r


def get_session():
    sess = requests.Session()
    do_login(sess)
    return sess

# =====================================================
# HELPERS (ORIGINAL LOGIC)
# =====================================================
def num_from_text(txt: str) -> int:
    s = (txt or "").strip().replace(",", "")
    m = re.search(r"\d+", s)
    return int(m.group(0)) if m else 0


def parse_all_ranges_with_stats_and_value(html):
    soup = BeautifulSoup(html, "lxml")
    rows = []
    for tr in soup.select("table tr"):
        tds = tr.find_all("td")
        if len(tds) < 4:
            continue
        rng_text = tds[0].get_text(" ", strip=True)
        if not rng_text:
            continue
        up = rng_text.strip().upper()
        if up in ("RANGE", "S/N"):
            continue
        rows.append({
            "text": rng_text,
            "all": num_from_text(tds[1].text),
            "free": num_from_text(tds[2].text),
            "allocated": num_from_text(tds[3].text),
        })
    return rows

# =====================================================
# ROUTES
# =====================================================
@app.route("/")
def index():
    return render_template("index.html")


@app.route("/api/clients")
def api_clients():
    sess = get_session()
    r = sess.get(UPSTREAM_BASE + ALL_PATH, headers=HEADERS, timeout=15)
    soup = BeautifulSoup(r.text, "lxml")

    out = []
    for opt in soup.select("select[name=selidd] option"):
        val = (opt.get("value") or "").strip()
        if val:
            out.append({"name": opt.text.strip(), "external_id": val})

    return jsonify(out)


@app.route("/api/allocate", methods=["POST"])
def api_allocate():
    data = request.json or {}
    selidd = data.get("selidd")
    selrng = data.get("selrng")
    qty = int(data.get("quantity", 0))

    if not selidd or not selrng or qty <= 0:
        return jsonify({"error": "missing params"}), 400

    sess = get_session()
    resp = sess.post(
        UPSTREAM_BASE + ALL_PATH,
        data={
            "quantity": qty,
            "selidd": selidd,
            "selrng": selrng,
            "allocate": "1",
        },
        headers=HEADERS,
        timeout=20,
    )

    status = "success" if resp.status_code == 200 else "error"
    a = Allocation(
        client_external_id=selidd,
        range_code=selrng,
        quantity=qty,
        status=status,
        response=resp.text[:200],
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
            "created_at": r.created_at.isoformat(),
        }
        for r in rows
    ])


@app.route("/static/<path:p>")
def static_files(p):
    return send_from_directory("static", p)


if __name__ == "__main__":
    app.run(host="0.0.0.0", port=int(os.environ.get("PORT", 5000)))
