import os
import re
import sqlite3
import uuid

from flask import Flask, abort, jsonify, redirect, request, send_from_directory

BASE_DIR = os.path.dirname(os.path.abspath(__file__))
# DIQQAT: Vercelda /tmp vaqtinchalik. Ma'lumot saqlanib qolishi uchun haqiqiy bazaga o'tish kerak.
DB_PATH = os.environ.get("DB_PATH", "/tmp/database.db")
UPLOAD_FOLDER = os.environ.get("UPLOAD_FOLDER", "/tmp/uploads")
ALLOWED_EXT = {"png", "jpg", "jpeg"}
CATEGORIES = {"Sabzavotlar", "Mevalar", "Poliz", "Ko'katlar"}
REGIONS = {"Toshkent shahri", "Toshkent viloyati", "Andijon", "Buxoro", "Farg'ona", "Jizzax", "Namangan", "Navoiy", "Qashqadaryo", "Samarqand", "Sirdaryo", "Surxondaryo", "Xorazm", "Qoraqalpog'iston Respublikasi"}
PAGES = {"elon.html", "chat.html", "Eloncss.css", "favicon.png"}

app = Flask(__name__, static_folder=None)
app.config["MAX_CONTENT_LENGTH"] = 6 * 1024 * 1024


def get_db():
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    return conn


def init_db():
    with get_db() as conn:
        conn.execute("""
            CREATE TABLE IF NOT EXISTS products (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                title TEXT NOT NULL, category TEXT NOT NULL, region TEXT NOT NULL,
                price REAL NOT NULL, quantity REAL NOT NULL, min_order REAL NOT NULL,
                phone TEXT NOT NULL, description TEXT, image TEXT,
                created_at TEXT DEFAULT CURRENT_TIMESTAMP
            )""")


init_db()


def serve(name):
    # Fayl templates/ papkasida bo'lsa shu yerdan, bo'lmasa asosiy papkadan beriladi
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
    return serve(name)


@app.route("/uploads/<path:name>")
def uploads(name):
    return send_from_directory(UPLOAD_FOLDER, name)


@app.route("/api/products/")
def api_products():
    with get_db() as conn:
        rows = conn.execute("SELECT * FROM products ORDER BY id DESC").fetchall()
    return jsonify([{
        "id": r["id"], "title": r["title"], "category": r["category"], "region": r["region"],
        "price_per_kg": r["price"], "available_qty": r["quantity"], "min_order": r["min_order"],
        "phone_number": r["phone"], "image_url": r["image"] or "", "seller_name": "Fermer",
        "harvest_date": "Yangi e'lon", "quality": "STANDARD", "availability": "READY",
    } for r in rows])


def bad(msg):
    return f"Xato: {msg}. Orqaga qaytib tuzating.", 400


@app.route("/elon-yuborish", methods=["POST"])
def elon_yuborish():
    f = request.form
    title = (f.get("title") or "").strip()
    category, region = f.get("category"), f.get("region")
    try:
        price, quantity, min_order = (float(f.get(k)) for k in ("price", "quantity", "min_order"))
    except (TypeError, ValueError):
        return bad("narx, hajm va min. buyurtma raqam bo'lishi kerak")

    digits = re.sub(r"\D", "", f.get("phone") or "")
    if digits.startswith("998"):
        digits = digits[3:]
    if not title or len(title) > 80:
        return bad("mahsulot nomi bo'sh yoki juda uzun")
    if category not in CATEGORIES or region not in REGIONS:
        return bad("kategoriya yoki viloyat noto'g'ri")
    if price <= 0 or quantity <= 0 or min_order <= 0 or min_order > quantity:
        return bad("narx, hajm yoki min. buyurtma noto'g'ri")
    if len(digits) != 9:
        return bad("telefon raqami 9 xonali bo'lishi kerak")

    image_url = ""
    file = request.files.get("file-upload")
    if file and file.filename:
        ext = file.filename.rsplit(".", 1)[-1].lower()
        if ext not in ALLOWED_EXT:
            return bad("rasm faqat PNG yoki JPG bo'lishi mumkin")
        try:
            os.makedirs(UPLOAD_FOLDER, exist_ok=True)
            name = f"{uuid.uuid4().hex}.{ext}"
            file.save(os.path.join(UPLOAD_FOLDER, name))
            image_url = f"/uploads/{name}"
        except OSError:
            image_url = ""

    with get_db() as conn:
        conn.execute(
            "INSERT INTO products (title, category, region, price, quantity, min_order, phone, description, image) "
            "VALUES (?,?,?,?,?,?,?,?,?)",
            (title, category, region, price, quantity, min_order, "+998" + digits,
             (f.get("description") or "").strip()[:500], image_url))
    return redirect("/?elon=1")


if __name__ == "__main__":
    app.run(debug=True)
