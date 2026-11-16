import hmac
import json
import os
import re
import secrets
import sqlite3
import urllib.error
import urllib.request
import uuid
from contextlib import contextmanager
from urllib.parse import parse_qsl, urlencode, urlsplit, urlunsplit

from flask import Flask, abort, jsonify, redirect, render_template_string, request, send_from_directory, session
from werkzeug.security import check_password_hash, generate_password_hash

try:
    import psycopg2
    import psycopg2.extras
except ImportError:  # faqat lokal sinov (SQLite) uchun
    psycopg2 = None

BASE_DIR = os.path.dirname(os.path.abspath(__file__))

# ---- Sozlamalar (hammasi Vercel > Settings > Environment Variables ichida) ----
DATABASE_URL = os.environ.get("DATABASE_URL") or os.environ.get("POSTGRES_URL") or ""
SUPABASE_URL = (os.environ.get("SUPABASE_URL") or os.environ.get("NEXT_PUBLIC_SUPABASE_URL") or "").rstrip("/")
SUPABASE_KEY = os.environ.get("SUPABASE_SERVICE_KEY") or os.environ.get("SUPABASE_SERVICE_ROLE_KEY") or ""
BUCKET = os.environ.get("STORAGE_BUCKET", "rasmlar")
ADMIN_TOKEN = os.environ.get("ADMIN_TOKEN", "")
USE_PG = bool(DATABASE_URL)
DB_PATH = os.environ.get("DB_PATH", "/tmp/database.db")      # faqat DATABASE_URL yo'q bo'lsa (lokal)
UPLOAD_FOLDER = os.environ.get("UPLOAD_FOLDER", "/tmp/uploads")  # faqat Supabase Storage yo'q bo'lsa (lokal)

CATEGORIES = {"Sabzavotlar", "Mevalar", "Poliz", "Ko'katlar"}
REGIONS = {"Toshkent shahri", "Toshkent viloyati", "Andijon", "Buxoro", "Farg'ona", "Jizzax", "Namangan", "Navoiy", "Qashqadaryo", "Samarqand", "Sirdaryo", "Surxondaryo", "Xorazm", "Qoraqalpog'iston Respublikasi"}
PAGES = {"elon.html", "chat.html", "Eloncss.css", "favicon.png", "manifest.json", "sw.js", "icon-192.png", "icon-512.png", "apple-touch-icon.png"}

app = Flask(__name__, static_folder=None)
app.config["MAX_CONTENT_LENGTH"] = 6 * 1024 * 1024
_secret = os.environ.get("SECRET_KEY")
if not _secret:
    # SECRET_KEY berilmasa, har ishga tushishda tasodifiy kalit olinadi (xavfsiz, lekin login'lar tez-tez uziladi)
    _secret = secrets.token_hex(32) if os.environ.get("VERCEL") else "faqat-lokal-sinov"
    print("OGOHLANTIRISH: SECRET_KEY o'rnatilmagan")
app.secret_key = _secret
app.config.update(SESSION_COOKIE_HTTPONLY=True, SESSION_COOKIE_SAMESITE="Lax",
                  SESSION_COOKIE_SECURE=bool(os.environ.get("VERCEL")))

# ---------------------------------------------------------------- Baza
SCHEMA_PG = [
    """CREATE TABLE IF NOT EXISTS users (
        id BIGSERIAL PRIMARY KEY, name TEXT NOT NULL, phone TEXT NOT NULL UNIQUE,
        password_hash TEXT NOT NULL, role TEXT NOT NULL, telegram TEXT DEFAULT '',
        verified BOOLEAN NOT NULL DEFAULT FALSE, created_at TIMESTAMPTZ DEFAULT now())""",
    """CREATE TABLE IF NOT EXISTS products (
        id BIGSERIAL PRIMARY KEY, user_id BIGINT REFERENCES users(id) ON DELETE SET NULL,
        title TEXT NOT NULL, category TEXT NOT NULL, region TEXT NOT NULL,
        price DOUBLE PRECISION NOT NULL, quantity DOUBLE PRECISION NOT NULL, min_order DOUBLE PRECISION NOT NULL,
        phone TEXT NOT NULL, telegram TEXT DEFAULT '', description TEXT, image TEXT,
        created_at TIMESTAMPTZ DEFAULT now())""",
    # RLS yoqiladi: Supabase'ning ochiq API'si jadvallarni o'qiy olmasin (ilova to'g'ridan-to'g'ri ulanish orqali ishlaydi)
    "ALTER TABLE users ENABLE ROW LEVEL SECURITY",
    "ALTER TABLE products ENABLE ROW LEVEL SECURITY",
    """CREATE TABLE IF NOT EXISTS orders (
        id BIGSERIAL PRIMARY KEY, buyer_id BIGINT REFERENCES users(id) ON DELETE SET NULL,
        seller_id BIGINT REFERENCES users(id) ON DELETE SET NULL, items TEXT NOT NULL,
        subtotal DOUBLE PRECISION NOT NULL, logistics_type TEXT NOT NULL, logistics_cost DOUBLE PRECISION NOT NULL,
        total DOUBLE PRECISION NOT NULL, status TEXT NOT NULL DEFAULT 'new', created_at TIMESTAMPTZ DEFAULT now())""",
    "ALTER TABLE orders ENABLE ROW LEVEL SECURITY",
]
SCHEMA_SQLITE = [
    """CREATE TABLE IF NOT EXISTS users (
        id INTEGER PRIMARY KEY AUTOINCREMENT, name TEXT NOT NULL, phone TEXT NOT NULL UNIQUE,
        password_hash TEXT NOT NULL, role TEXT NOT NULL, telegram TEXT DEFAULT '',
        verified INTEGER NOT NULL DEFAULT 0, created_at TEXT DEFAULT CURRENT_TIMESTAMP)""",
    """CREATE TABLE IF NOT EXISTS products (
        id INTEGER PRIMARY KEY AUTOINCREMENT, user_id INTEGER,
        title TEXT NOT NULL, category TEXT NOT NULL, region TEXT NOT NULL,
        price REAL NOT NULL, quantity REAL NOT NULL, min_order REAL NOT NULL,
        phone TEXT NOT NULL, telegram TEXT DEFAULT '', description TEXT, image TEXT,
        created_at TEXT DEFAULT CURRENT_TIMESTAMP)""",
    """CREATE TABLE IF NOT EXISTS orders (
        id INTEGER PRIMARY KEY AUTOINCREMENT, buyer_id INTEGER, seller_id INTEGER, items TEXT NOT NULL,
        subtotal REAL NOT NULL, logistics_type TEXT NOT NULL, logistics_cost REAL NOT NULL,
        total REAL NOT NULL, status TEXT NOT NULL DEFAULT 'new', created_at TEXT DEFAULT CURRENT_TIMESTAMP)""",
]
INTEGRITY_ERRORS = (sqlite3.IntegrityError,) + ((psycopg2.IntegrityError,) if psycopg2 else ())
_schema_ready = False


def clean_dsn(url):
    # Vercel-Supabase integratsiyasi qo'shadigan "supa=..." parametri psycopg2 ni buzadi, olib tashlanadi
    p = urlsplit(url)
    q = [(k, v) for k, v in parse_qsl(p.query) if k != "supa"]
    return urlunsplit(p._replace(query=urlencode(q)))


def connect():
    if USE_PG:
        if psycopg2 is None:
            raise RuntimeError("psycopg2-binary o'rnatilmagan (requirements.txt ni tekshiring)")
        return psycopg2.connect(clean_dsn(DATABASE_URL), connect_timeout=10,
                                cursor_factory=psycopg2.extras.RealDictCursor)
    if os.environ.get("VERCEL"):
        # Vercelda vaqtinchalik SQLite'ga jim yozib ketmaslik uchun aniq xato beriladi
        raise RuntimeError("Baza ulanmagan: Vercel Environment Variables ga DATABASE_URL (yoki POSTGRES_URL) qo'shing")
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    return conn


def ensure_schema():
    global _schema_ready
    if _schema_ready:
        return
    conn = connect()
    try:
        cur = conn.cursor()
        for stmt in (SCHEMA_PG if USE_PG else SCHEMA_SQLITE):
            cur.execute(stmt)
        conn.commit()
    finally:
        conn.close()
    _schema_ready = True


@contextmanager
def db():
    ensure_schema()
    conn = connect()
    try:
        yield conn
        conn.commit()
    except Exception:
        conn.rollback()
        raise
    finally:
        conn.close()


def run(conn, sql, params=()):
    cur = conn.cursor()
    cur.execute(sql.replace("?", "%s") if USE_PG else sql, params)
    return cur


# ---------------------------------------------------------------- Yordamchilar
def norm_phone(v):
    d = re.sub(r"\D", "", v or "")
    if d.startswith("998"):
        d = d[3:]
    return d if len(d) == 9 else None


def norm_tg(v):
    t = re.sub(r"^(https?://)?(t\.me/|telegram\.me/)?@?", "", (v or "").strip()).split("?")[0].strip("/")
    return t if re.fullmatch(r"[A-Za-z][A-Za-z0-9_]{4,31}", t) else None


def current_user():
    uid = session.get("uid")
    if not uid:
        return None
    with db() as conn:
        return run(conn, "SELECT * FROM users WHERE id=?", (uid,)).fetchone()


def user_json(u):
    return {"id": u["id"], "name": u["name"], "phone": "+998" + u["phone"], "role": u["role"],
            "telegram": u["telegram"] or "", "verified": bool(u["verified"])}


def err(msg, code=400):
    return jsonify({"error": msg}), code


@app.errorhandler(RuntimeError)
def runtime_error(e):
    return jsonify({"error": str(e)}), 503


@app.route("/api/health")
def api_health():
    # Tekshiruv: /api/health  (hisoblar soni uchun: /api/health?token=ADMIN_TOKEN)
    info = {"db": "postgres" if USE_PG else "sqlite", "storage": bool(SUPABASE_URL and SUPABASE_KEY),
            "vercel": bool(os.environ.get("VERCEL"))}
    try:
        with db() as conn:
            info["ok"] = True
            if ADMIN_TOKEN and hmac.compare_digest(request.args.get("token", ""), ADMIN_TOKEN):
                info["products"] = run(conn, "SELECT COUNT(*) AS n FROM products").fetchone()["n"]
                info["users"] = run(conn, "SELECT COUNT(*) AS n FROM users").fetchone()["n"]
    except RuntimeError:
        raise
    except Exception as e:
        print("Baza xatosi:", e)
        info.update(ok=False, error="Bazaga ulanib bo'lmadi (parol yoki manzilni tekshiring)")
        return jsonify(info), 503
    return jsonify(info)


def save_image(data, ext):
    """Rasmni Supabase Storage'ga yuklaydi (sozlanmagan bo'lsa, lokal papkaga). Muvaffaqiyatsiz bo'lsa, bo'sh qaytaradi."""
    name = f"{uuid.uuid4().hex}.{ext}"
    if SUPABASE_URL and SUPABASE_KEY:
        req = urllib.request.Request(
            f"{SUPABASE_URL}/storage/v1/object/{BUCKET}/{name}", data=data, method="POST",
            headers={"Authorization": f"Bearer {SUPABASE_KEY}", "apikey": SUPABASE_KEY,
                     "Content-Type": "image/png" if ext == "png" else "image/jpeg"})
        try:
            urllib.request.urlopen(req, timeout=15).close()
            return f"{SUPABASE_URL}/storage/v1/object/public/{BUCKET}/{name}"
        except (urllib.error.URLError, OSError) as e:
            print("Storage xatosi:", e)
            return ""
    try:
        os.makedirs(UPLOAD_FOLDER, exist_ok=True)
        with open(os.path.join(UPLOAD_FOLDER, name), "wb") as f:
            f.write(data)
        return f"/uploads/{name}"
    except OSError:
        return ""


# ---------------------------------------------------------------- Sahifalar
def serve(name):
    for folder in (os.path.join(BASE_DIR, "templates"), BASE_DIR):
        if os.path.isfile(os.path.join(folder, name)):
            return send_from_directory(folder, name)
    abort(404)


@app.route("/")
def home():
    return serve("index.html")


@app.route("/<path:name>")
def pages(name):
    if name not in PAGES:
        abort(404)
    resp = serve(name)
    if name == "sw.js":
        resp.headers["Cache-Control"] = "no-cache"
    return resp


@app.route("/uploads/<path:name>")
def uploads(name):
    return send_from_directory(UPLOAD_FOLDER, name)


# ---------------------------------------------------------------- Kirish / ro'yxatdan o'tish
@app.route("/api/register", methods=["POST"])
def api_register():
    d = request.get_json(silent=True) or {}
    name = (d.get("name") or "").strip()
    phone, role = norm_phone(d.get("phone")), d.get("role")
    password = d.get("password") or ""
    tg = norm_tg(d.get("telegram")) if role == "seller" else ""
    if not (2 <= len(name) <= 40):
        return err("Ism 2 dan 40 gacha belgi bo'lsin")
    if not phone:
        return err("Telefon raqami 9 xonali bo'lishi kerak")
    if role not in ("buyer", "seller"):
        return err("Hisob turini tanlang")
    if len(password) < 6 or len(password) > 72:
        return err("Parol kamida 6 ta belgi bo'lsin")
    if role == "seller" and not tg:
        return err("Telegram username noto'g'ri (masalan: @ali_fermer)")
    try:
        with db() as conn:
            uid = run(conn, "INSERT INTO users (name, phone, password_hash, role, telegram) VALUES (?,?,?,?,?) RETURNING id",
                      (name, phone, generate_password_hash(password), role, tg)).fetchone()["id"]
    except INTEGRITY_ERRORS:
        return err("Bu raqam allaqachon ro'yxatdan o'tgan. Kirishni bosing", 409)
    session["uid"] = uid
    return jsonify({"user": user_json(current_user())})


@app.route("/api/login", methods=["POST"])
def api_login():
    d = request.get_json(silent=True) or {}
    phone = norm_phone(d.get("phone"))
    u = None
    if phone:
        with db() as conn:
            u = run(conn, "SELECT * FROM users WHERE phone=?", (phone,)).fetchone()
    if not u or not check_password_hash(u["password_hash"], d.get("password") or ""):
        return err("Telefon yoki parol noto'g'ri", 401)
    session["uid"] = u["id"]
    return jsonify({"user": user_json(u)})


@app.route("/api/logout", methods=["POST"])
def api_logout():
    session.clear()
    return jsonify({"ok": True})


@app.route("/api/me")
def api_me():
    u = current_user()
    return jsonify({"user": user_json(u) if u else None})


@app.route("/admin/verify/<phone>")
def admin_verify(phone):
    # Sotuvchini tasdiqlash: /admin/verify/901234567?token=ADMIN_TOKEN  (bekor qilish: &undo=1)
    if not ADMIN_TOKEN or not hmac.compare_digest(request.args.get("token", ""), ADMIN_TOKEN):
        abort(403)
    p = norm_phone(phone)
    value = not request.args.get("undo")
    with db() as conn:
        n = run(conn, "UPDATE users SET verified=? WHERE phone=? AND role='seller'", (value, p)).rowcount
    return ("Bajarildi" if n else "Bunday sotuvchi topilmadi"), (200 if n else 404)


# ---------------------------------------------------------------- E'lonlar
@app.route("/api/products/")
def api_products():
    with db() as conn:
        rows = run(conn, """SELECT p.*, u.name AS seller, u.verified AS v, u.telegram AS utg
                            FROM products p LEFT JOIN users u ON u.id = p.user_id
                            ORDER BY p.id DESC""").fetchall()
    return jsonify([{
        "id": r["id"], "seller_id": r["user_id"], "title": r["title"], "category": r["category"], "region": r["region"],
        "price_per_kg": r["price"], "available_qty": r["quantity"], "min_order": r["min_order"],
        "phone_number": r["phone"], "telegram": r["utg"] or r["telegram"] or "",
        "image_url": r["image"] or "", "seller_name": r["seller"] or "Fermer",
        "is_verified": bool(r["v"]), "harvest_date": "Yangi e'lon", "quality": "STANDARD", "availability": "READY",
    } for r in rows])


def bad(msg, code=400):
    return f"Xato: {msg}. Orqaga qaytib tuzating.", code


@app.route("/elon-yuborish", methods=["POST"])
def elon_yuborish():
    u = current_user()
    if not u:
        return bad("avval tizimga kiring", 401)
    if u["role"] != "seller":
        return bad("e'lon berish uchun sotuvchi hisobi kerak", 403)

    f = request.form
    title = (f.get("title") or "").strip()
    category, region = f.get("category"), f.get("region")
    try:
        price, quantity, min_order = (float(f.get(k)) for k in ("price", "quantity", "min_order"))
    except (TypeError, ValueError):
        return bad("narx, hajm va min. buyurtma raqam bo'lishi kerak")
    if not title or len(title) > 80:
        return bad("mahsulot nomi bo'sh yoki juda uzun")
    if category not in CATEGORIES or region not in REGIONS:
        return bad("kategoriya yoki viloyat noto'g'ri")
    if price <= 0 or quantity <= 0 or min_order <= 0 or min_order > quantity:
        return bad("narx, hajm yoki min. buyurtma noto'g'ri")

    image_url = ""
    file = request.files.get("file-upload")
    if file and file.filename:
        data = file.read(5 * 1024 * 1024 + 1)
        if len(data) > 5 * 1024 * 1024:
            return bad("rasm 5 MB dan katta")
        if data.startswith(b"\x89PNG\r\n\x1a\n"):
            ext = "png"
        elif data[:3] == b"\xff\xd8\xff":
            ext = "jpg"
        else:
            return bad("rasm faqat PNG yoki JPG bo'lishi mumkin")
        image_url = save_image(data, ext)

    with db() as conn:
        run(conn, "INSERT INTO products (title, category, region, price, quantity, min_order, phone, description, image, telegram, user_id) "
                  "VALUES (?,?,?,?,?,?,?,?,?,?,?)",
            (title, category, region, price, quantity, min_order, "+998" + u["phone"],
             (f.get("description") or "").strip()[:500], image_url, u["telegram"], u["id"]))
    return redirect("/?elon=1")


# ---------------------------------------------------------------- Buyurtmalar
LOGISTICS = {"REF_EXPRESS": ("Sovutgichli avto, ekspress", 180000),
             "STANDARD_TRUCK": ("Standart yuk mashinasi", 120000)}


def money(x):
    return f"{float(x):,.0f}"


def order_number(oid):
    return f"AE-{int(oid):06d}"


@app.route("/api/orders", methods=["POST"])
def api_order_create():
    u = current_user()
    if not u:
        return err("Buyurtma berish uchun avval ro'yxatdan o'ting", 401)
    d = request.get_json(silent=True) or {}
    lg = d.get("logistics")
    if lg not in LOGISTICS:
        return err("Logistika turini tanlang")
    raw = d.get("items")
    if not isinstance(raw, list) or not (1 <= len(raw) <= 30):
        return err("Savat bo'sh")
    wanted = {}
    for it in raw:
        try:
            pid, qty = int(it.get("id")), float(it.get("qty"))
        except (TypeError, ValueError, AttributeError):
            return err("Buyurtma ma'lumoti noto'g'ri")
        if qty <= 0:
            return err("Miqdor noto'g'ri")
        wanted[pid] = wanted.get(pid, 0) + qty

    lines, seller_id = [], None
    with db() as conn:
        for pid, qty in wanted.items():
            p = run(conn, "SELECT * FROM products WHERE id=?", (pid,)).fetchone()
            if not p:
                return err("E'lon topilmadi (o'chirilgan bo'lishi mumkin)", 404)
            if not p["user_id"]:
                return err("Bu e'lon egasi aniq emas")
            if seller_id is None:
                seller_id = p["user_id"]
            elif seller_id != p["user_id"]:
                return err("Bitta buyurtmada faqat bitta fermer mahsuloti bo'lsin")
            if p["user_id"] == u["id"]:
                return err("O'z e'loningizga buyurtma bera olmaysiz")
            if qty < p["min_order"]:
                return err(f"{p['title']}: minimal buyurtma {p['min_order']:g} kg")
            if qty > p["quantity"]:
                return err(f"{p['title']}: omborda atigi {p['quantity']:g} kg bor")
            # Narx har doim bazadan olinadi (brauzerdan kelgan narxga ishonilmaydi)
            lines.append({"id": pid, "title": p["title"], "price": p["price"], "qty": qty, "sum": p["price"] * qty})
        subtotal = sum(x["sum"] for x in lines)
        lname, lcost = LOGISTICS[lg]
        total = subtotal + lcost
        oid = run(conn, "INSERT INTO orders (buyer_id, seller_id, items, subtotal, logistics_type, logistics_cost, total) "
                        "VALUES (?,?,?,?,?,?,?) RETURNING id",
                  (u["id"], seller_id, json.dumps(lines, ensure_ascii=False), subtotal, lg, lcost, total)).fetchone()["id"]
        seller = run(conn, "SELECT name, telegram FROM users WHERE id=?", (seller_id,)).fetchone()
    return jsonify({"order": {
        "id": oid, "number": order_number(oid), "total": total, "url": f"/order/{oid}",
        "seller_name": seller["name"] if seller else "Fermer",
        "seller_telegram": (seller["telegram"] if seller else "") or ""}})


def load_order(conn, oid):
    return run(conn, """SELECT o.*, b.name AS bname, b.phone AS bphone, s.name AS sname, s.phone AS sphone, s.telegram AS stg
                        FROM orders o LEFT JOIN users b ON b.id = o.buyer_id LEFT JOIN users s ON s.id = o.seller_id
                        WHERE o.id=?""", (oid,)).fetchone()


SHEET_CSS = """
*{box-sizing:border-box}body{margin:0;background:#f1f5f9;font-family:system-ui,-apple-system,Segoe UI,Roboto,sans-serif;color:#0f172a;line-height:1.5}
.sheet{max-width:760px;margin:0 auto;background:#fff;padding:24px 20px 32px}
h1{margin:0;font-size:24px;color:#166534}.muted{color:#64748b;font-size:13px}
.top{display:flex;justify-content:space-between;gap:12px;flex-wrap:wrap;border-bottom:3px solid #16a34a;padding-bottom:12px}
.num{text-align:right;font-weight:800;font-size:20px}.num small{font-weight:400;color:#64748b;font-size:13px}
.parties{display:grid;grid-template-columns:1fr 1fr;gap:12px;margin:16px 0}
.parties div{background:#f8fafc;border:1px solid #e2e8f0;border-radius:12px;padding:12px;font-size:14px;word-break:break-word}
.parties h3{margin:0 0 4px;font-size:12px;color:#64748b;text-transform:uppercase;letter-spacing:.04em}
.tw{overflow-x:auto}table{width:100%;border-collapse:collapse;font-size:14px;min-width:420px}
th,td{padding:8px;border-bottom:1px solid #e2e8f0;text-align:left}th{background:#f8fafc;font-size:12px;color:#475569}
td.r,th.r{text-align:right;white-space:nowrap}.tot{margin:12px 0 0 auto;max-width:320px;font-size:14px}
.tot div{display:flex;justify-content:space-between;padding:4px 0}.tot .g{border-top:2px solid #0f172a;margin-top:4px;padding-top:8px;font-weight:800;font-size:17px}
.note{margin-top:20px;font-size:12px;color:#475569;background:#fffbeb;border:1px solid #fde68a;border-radius:10px;padding:10px}
.actions{display:flex;gap:8px;flex-wrap:wrap;margin-top:18px}
.btn{display:inline-block;border:0;border-radius:10px;padding:12px 18px;font-size:15px;font-weight:700;background:#16a34a;color:#fff;text-decoration:none;cursor:pointer}
.btn.alt{background:#e2e8f0;color:#0f172a}
@media(max-width:520px){.parties{grid-template-columns:1fr}}
@media print{body{background:#fff}.no-print{display:none!important}.sheet{padding:0;max-width:none}}
"""

SHEET_HTML = """<!doctype html><html lang="uz"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1"><meta name="robots" content="noindex">
<title>Buyurtma {{ number }} | AgriEko</title><style>""" + SHEET_CSS + """</style></head><body><main class="sheet">
<div class="top"><div><h1>AgriEko</h1><div class="muted">Buyurtma varaqasi</div></div>
<div class="num">{{ number }}<br><small>{{ date }}</small></div></div>
<div class="parties">
<div><h3>Xaridor</h3><b>{{ bname }}</b><br>{{ bphone }}</div>
<div><h3>Sotuvchi</h3><b>{{ sname }}</b><br>{{ sphone }}{% if stg %}<br>@{{ stg }}{% endif %}</div></div>
<div class="tw"><table><thead><tr><th>Mahsulot</th><th class="r">Miqdor</th><th class="r">Narx (1 kg)</th><th class="r">Summa</th></tr></thead><tbody>
{% for l in lines %}<tr><td>{{ l.title }}</td><td class="r">{{ l.qty }} kg</td><td class="r">{{ l.price }}</td><td class="r">{{ l.sum }}</td></tr>{% endfor %}
</tbody></table></div>
<div class="tot"><div><span>Mahsulotlar</span><span>{{ subtotal }} UZS</span></div>
<div><span>Logistika: {{ lname }}</span><span>{{ lcost }} UZS</span></div>
<div class="g"><span>Jami</span><span>{{ total }} UZS</span></div></div>
<p class="note">Bu hujjat buyurtma qabul qilinganini va yuqoridagi tarkibni tasdiqlaydi. To'lov AgriEko saytida amalga oshirilmaydi:
to'lov va yetkazib berish shartlari xaridor va sotuvchi o'rtasida kelishiladi. Hujjat rasmiy soliq cheki yoki hisob-faktura emas.</p>
<div class="actions no-print"><button class="btn" onclick="window.print()">PDF qilib saqlash / chop etish</button>
<a class="btn alt" href="/orders">Buyurtmalarim</a><a class="btn alt" href="/">Bosh sahifa</a></div>
</main></body></html>"""

LIST_HTML = """<!doctype html><html lang="uz"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1"><meta name="robots" content="noindex">
<title>Buyurtmalarim | AgriEko</title><style>""" + SHEET_CSS + """</style></head><body><main class="sheet">
<div class="top"><div><h1>Buyurtmalarim</h1><div class="muted">Siz xaridor yoki sotuvchi bo'lgan buyurtmalar</div></div></div>
{% if rows %}<div class="tw"><table><thead><tr><th>Raqam</th><th>Sana</th><th>Kim bilan</th><th class="r">Jami</th><th></th></tr></thead><tbody>
{% for r in rows %}<tr><td><b>{{ r.number }}</b></td><td>{{ r.date }}</td><td>{{ r.who }}</td><td class="r">{{ r.total }} UZS</td>
<td class="r"><a href="/order/{{ r.id }}">Ochish</a></td></tr>{% endfor %}</tbody></table></div>
{% else %}<p class="muted" style="margin-top:20px">Hozircha buyurtma yo'q.</p>{% endif %}
<div class="actions no-print"><a class="btn alt" href="/">Bosh sahifa</a></div></main></body></html>"""


@app.route("/order/<int:oid>")
def order_sheet(oid):
    u = current_user()
    if not u:
        return redirect("/?auth=login")
    with db() as conn:
        o = load_order(conn, oid)
    if not o:
        abort(404)
    if u["id"] not in (o["buyer_id"], o["seller_id"]):
        return "Bu varaqa sizga tegishli emas", 403
    lines = [{"title": l["title"], "qty": f"{l['qty']:g}", "price": money(l["price"]), "sum": money(l["sum"])}
             for l in json.loads(o["items"])]
    return render_template_string(
        SHEET_HTML, number=order_number(o["id"]), date=str(o["created_at"])[:16],
        bname=o["bname"] or "-", bphone=("+998" + o["bphone"]) if o["bphone"] else "-",
        sname=o["sname"] or "-", sphone=("+998" + o["sphone"]) if o["sphone"] else "-", stg=o["stg"] or "",
        lines=lines, subtotal=money(o["subtotal"]), lname=LOGISTICS.get(o["logistics_type"], ("-", 0))[0],
        lcost=money(o["logistics_cost"]), total=money(o["total"]))


@app.route("/orders")
def my_orders():
    u = current_user()
    if not u:
        return redirect("/?auth=login")
    with db() as conn:
        rows = run(conn, """SELECT o.*, b.name AS bname, s.name AS sname FROM orders o
                            LEFT JOIN users b ON b.id = o.buyer_id LEFT JOIN users s ON s.id = o.seller_id
                            WHERE o.buyer_id=? OR o.seller_id=? ORDER BY o.id DESC LIMIT 100""",
                    (u["id"], u["id"])).fetchall()
    view = [{"id": r["id"], "number": order_number(r["id"]), "date": str(r["created_at"])[:10],
             "who": (r["sname"] if r["buyer_id"] == u["id"] else r["bname"]) or "-", "total": money(r["total"])}
            for r in rows]
    return render_template_string(LIST_HTML, rows=view)


if __name__ == "__main__":
    app.run(debug=True)
