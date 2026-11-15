import hmac
import os
import re
import secrets
import sqlite3
import urllib.error
import urllib.request
import uuid
from contextlib import contextmanager
from urllib.parse import parse_qsl, urlencode, urlsplit, urlunsplit

from flask import Flask, abort, jsonify, redirect, request, send_from_directory, session
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
        "id": r["id"], "title": r["title"], "category": r["category"], "region": r["region"],
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


if __name__ == "__main__":
    app.run(debug=True)
