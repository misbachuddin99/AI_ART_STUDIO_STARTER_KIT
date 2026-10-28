import os
import json
import base64
import re
import shutil
import subprocess
import time
from io import BytesIO
from PIL import Image
import piexif
import requests

from telegram import Update, InlineKeyboardButton, InlineKeyboardMarkup
from telegram.request import HTTPXRequest
from telegram.ext import (
    ApplicationBuilder,
    CommandHandler,
    MessageHandler,
    CallbackQueryHandler,
    ContextTypes,
    filters,
)

BASE_DIR = os.path.dirname(os.path.abspath(__file__))
CONFIG_PATH = os.path.join(BASE_DIR, "config.json")

with open(CONFIG_PATH, "r", encoding="utf-8") as f:
    CFG = json.load(f)

ARTIST_NAME = CFG.get("ARTIST_NAME", "Seniman")
ARTIST_EMAIL = CFG.get("ARTIST_EMAIL", "email@example.com")
IG_USER = CFG.get("INSTAGRAM_USERNAME", "")
TELEGRAM_TOKEN = CFG.get("TELEGRAM_BOT_TOKEN", "")
AUTHORIZED_USER_ID = int(CFG.get("AUTHORIZED_USER_ID", 0))
LM_STUDIO_URL = CFG.get("LM_STUDIO_URL", "http://localhost:1234/v1/chat/completions")

DEFAULT_SKETCH_SIZE = CFG.get("DEFAULT_SKETCH_SIZE", "29.7 cm x 42 cm")
DEFAULT_SKETCH_MEDIA = CFG.get("DEFAULT_SKETCH_MEDIA", "Tinta di atas kertas")
DEFAULT_PRICE = CFG.get("DEFAULT_SKETCH_PRICE_USD", 100)

SKETCH_DIR = os.path.join(BASE_DIR, "SKETCH")
SKETCH_RAW = os.path.join(SKETCH_DIR, "SKETCH_RAW")
SKETCH_CATALOG = os.path.join(SKETCH_DIR, "sketch_catalog.json")
SKETCH_WEB = os.path.join(SKETCH_DIR, "web_galeri")
SKETCH_IMG = os.path.join(SKETCH_WEB, "images")

for d in [SKETCH_RAW, SKETCH_WEB, SKETCH_IMG]:
    os.makedirs(d, exist_ok=True)

USER_SESSIONS = {}
AWAITING_RECURATE = {}

def get_next_id():
    if not os.path.exists(SKETCH_CATALOG):
        return "SKT-001"
    try:
        with open(SKETCH_CATALOG, "r", encoding="utf-8-sig") as f:
            cat = json.load(f)
    except Exception:
        cat = []
    max_n = 0
    for item in cat:
        m = re.search(r"(\d+)", item.get("ID", ""))
        if m:
            val = int(m.group(1))
            if val > max_n:
                max_n = val
    return f"SKT-{max_n + 1:03d}"

def compress_img(b, max_dim=768, quality=80):
    with Image.open(BytesIO(b)) as img:
        img = img.convert("RGB")
        img.thumbnail((max_dim, max_dim), Image.Resampling.LANCZOS)
        buf = BytesIO()
        img.save(buf, format="JPEG", quality=quality, optimize=True)
        return buf.getvalue()

def inject_exif(filepath, title, stmt, cur, tags):
    try:
        desc = f"{stmt}\n\nKuratorial: {cur}\n\nTags: {tags}\nArtist: {ARTIST_NAME}"
        zeroth = {
            piexif.ImageIFD.ImageDescription: desc[:2000].encode("utf-8", errors="ignore"),
            piexif.ImageIFD.XPTitle: title.encode("utf-16le", errors="ignore"),
            piexif.ImageIFD.XPKeywords: tags.encode("utf-16le", errors="ignore"),
            piexif.ImageIFD.XPComment: stmt.encode("utf-16le", errors="ignore"),
            piexif.ImageIFD.Artist: ARTIST_NAME.encode("utf-8", errors="ignore"),
            piexif.ImageIFD.Copyright: f"{ARTIST_NAME} - All Rights Reserved".encode("utf-8", errors="ignore")
        }
        exif_bytes = piexif.dump({"0th": zeroth, "Exif": {}, "GPS": {}, "1st": {}, "thumbnail": None})
        piexif.insert(exif_bytes, filepath)
    except Exception:
        pass

def analyze_ai(b64_img, item_id):
    prompt = (
        "Analisis karya sketsa / seni rupa ini secara mendalam sebagai kurator.\n"
        "Format JSON murni tanpa pengantar:\n"
        "{\n"
        '  "Judul": "Judul puitis yang sesuai visual ini",\n'
        '  "Tahun": "2026",\n'
        '  "Media": "Tinta di atas kertas",\n'
        '  "artist_statement_id": "Tulis 1-2 kalimat refleksi puitis filosofis seniman tentang tarikan garis dan ruang karya ini",\n'
        '  "deskripsi_kuratorial": "Ulasan kuratorial mengenai gestur tarikan garis, kontras gelap-terang, dan komposisi bentuknya",\n'
        '  "tags": "sketch, drawing, inkdrawing, contemporaryart"\n'
        "}\n"
        "Penting: artist_statement_id WAJIB kalimat puitis utuh bahasa Indonesia, bukan username/angka!"
    )
    payload = {
        "messages": [{"role": "user", "content": [
            {"type": "text", "text": prompt},
            {"type": "image_url", "image_url": {"url": f"data:image/jpeg;base64,{b64_img}"}}
        ]}],
        "temperature": 0.35,
        "max_tokens": 700
    }
    try:
        resp = requests.post(LM_STUDIO_URL, json=payload, timeout=60)
        resp.raise_for_status()
        raw = resp.json()["choices"][0]["message"]["content"].strip()
        raw = re.sub(r"```(?:json)?", "", raw).strip()
        data = json.loads(raw)
        data["ai_online"] = True
        return data
    except Exception as e:
        return {
            "Judul": f"Studi Garis & Bentuk #{item_id}",
            "Tahun": "2026",
            "Media": DEFAULT_SKETCH_MEDIA,
            "artist_statement_id": "Goresan garis spontan yang menangkap pergerakan rasa dalam keheningan ruang.",
            "deskripsi_kuratorial": f"Karya {item_id} menampilkan karakter garis tegas dan ritme visual yang ekspresif.",
            "tags": "sketch, drawing, art",
            "ai_online": False
        }

def rebuild_web():
    cat_data = []
    if os.path.exists(SKETCH_CATALOG):
        with open(SKETCH_CATALOG, "r", encoding="utf-8-sig") as f:
            cat_data = json.load(f)
    cache_id = int(time.time())
    cards = []
    for item in cat_data:
        i_id = item.get("ID")
        judul = item.get("Judul")
        thn = item.get("Tahun", "2026")
        uk = item.get("Ukuran", DEFAULT_SKETCH_SIZE)
        md = item.get("Media", DEFAULT_SKETCH_MEDIA)
        stm = item.get("artist_statement_id", "")
        kur = item.get("deskripsi_kuratorial", "")
        dm_url = f"[https://ig.me/m/](https://ig.me/m/){IG_USER}" if IG_USER else "#"
        
        cards.append(f'''
        <div class="card" data-id="{i_id}">
            <div class="img-container">
                <span class="badge badge-id">{i_id}</span>
                <span class="badge badge-year">{thn}</span>
                <img src="images/{i_id}.jpg?v={cache_id}" alt="{judul}" loading="lazy"
                     onerror="this.onerror=null; this.src='data:image/svg+xml;utf8,<svg xmlns=\'[http://www.w3.org/2000/svg](http://www.w3.org/2000/svg)\' width=\'400\' height=\'500\' viewBox=\'0 0 400 500\'><rect fill=\'%23161b22\' width=\'400\' height=\'500\'/><text fill=\'%238b949e\' font-family=\'sans-serif\' font-size=\'18\' dy=\'10.5\' font-weight=\'bold\' x=\'50%\' y=\'50%\' text-anchor=\'middle\'>{i_id}</text></svg>';" />
            </div>
            <div class="card-content">
                <h3 class="card-title">{judul}</h3>
                <p class="card-meta">{uk} • {md}</p>
                <div class="pricing-box">
                    <div class="price-col"><span class="label">Karya Original</span><span class="val">$ {DEFAULT_PRICE}</span></div>
                    <div class="price-col"><span class="label">Pengiriman</span><span class="val free">Bebas Ongkir</span></div>
                </div>
                <div class="contact-actions">
                    <a href="{dm_url}" target="_blank" class="btn btn-dm">✉️ DM IG</a>
                    <a href="mailto:{ARTIST_EMAIL}?subject=Inquiry%20{i_id}%20-%20{judul}" class="btn btn-email">✉️ Email</a>
                </div>
                <p class="statement">"{stm}"</p>
                <details class="detail-box">
                    <summary>• Detail & Kuratorial</summary>
                    <div class="curatorial-text"><p>{kur}</p></div>
                </details>
            </div>
        </div>
        ''')
    cards_html = "\n".join(cards)
    
    html = f'''<!DOCTYPE html>
<html lang="id">
<head>
    <meta charset="UTF-8">
    <meta name="viewport" content="width=device-width, initial-scale=1.0, maximum-scale=1.0, user-scalable=no">
    <title>Katalog Karya - {ARTIST_NAME}</title>
    <style>
        * {{ box-sizing: border-box; margin: 0; padding: 0; }}
        body {{ background-color: #0b0f17; color: #e6edf3; font-family: -apple-system, BlinkMacSystemFont, "Segoe UI", Roboto, sans-serif; padding: 25px 12px; width: 100%; overflow-x: hidden; }}
        .header {{ text-align: center; margin-bottom: 25px; }}
        .header h1 {{ font-size: 1.8rem; color: #58a6ff; margin-bottom: 6px; }}
        .header p {{ color: #8b949e; font-size: 0.9rem; }}
        .counter {{ display: inline-block; margin-top: 10px; background: rgba(56, 139, 253, 0.15); border: 1px solid rgba(56, 139, 253, 0.3); color: #58a6ff; padding: 5px 14px; border-radius: 20px; font-size: 0.8rem; }}
        .gallery-grid {{ display: grid; grid-template-columns: repeat(auto-fill, minmax(280px, 1fr)); gap: 20px; max-width: 1300px; margin: 0 auto; width: 100%; }}
        .card {{ background: #161b22; border: 1px solid #30363d; border-radius: 12px; overflow: hidden; display: flex; flex-direction: column; width: 100%; min-width: 0; }}
        .img-container {{ position: relative; width: 100%; aspect-ratio: 4/5; background: #010409; overflow: hidden; display: flex; align-items: center; justify-content: center; }}
        .img-container img {{ width: 100%; height: 100%; object-fit: contain; display: block; }}
        .badge {{ position: absolute; top: 10px; padding: 3px 8px; border-radius: 5px; font-size: 0.7rem; font-weight: bold; }}
        .badge-id {{ left: 10px; background: #1f6feb; color: #fff; }}
        .badge-year {{ left: 65px; background: rgba(255,255,255,0.15); color: #fff; }}
        .card-content {{ padding: 14px; display: flex; flex-direction: column; flex-grow: 1; }}
        .card-title {{ font-size: 1rem; margin-bottom: 4px; color: #f0f6fc; word-break: break-word; }}
        .card-meta {{ font-size: 0.75rem; color: #8b949e; margin-bottom: 10px; }}
        .pricing-box {{ display: flex; justify-content: space-between; background: rgba(0,0,0,0.25); border: 1px solid #30363d; padding: 8px 10px; border-radius: 6px; margin-bottom: 10px; }}
        .price-col .label {{ display: block; font-size: 0.65rem; color: #8b949e; }}
        .price-col .val {{ font-size: 0.95rem; font-weight: bold; color: #58a6ff; }}
        .price-col .val.free {{ color: #3fb950; font-size: 0.8rem; }}
        .contact-actions {{ display: grid; grid-template-columns: 1fr 1fr; gap: 6px; margin-bottom: 10px; }}
        .btn {{ text-align: center; padding: 7px; border-radius: 6px; text-decoration: none; font-size: 0.75rem; font-weight: 600; color: #fff; }}
        .btn-dm {{ background: #e1306c; }}
        .btn-email {{ background: #238636; }}
        .statement {{ font-size: 0.75rem; font-style: italic; color: #c9d1d9; line-height: 1.35; margin-bottom: 10px; }}
        .detail-box summary {{ font-size: 0.75rem; color: #58a6ff; cursor: pointer; padding: 4px 0; }}
        .curatorial-text {{ font-size: 0.72rem; color: #8b949e; line-height: 1.4; margin-top: 6px; background: #0d1117; padding: 8px; border-radius: 6px; }}
        footer {{ text-align: center; margin-top: 40px; font-size: 0.75rem; color: #484f58; }}

        /* MODE SMARTPHONE: KUNCI 2 KOLOM KARTU */
        @media screen and (max-width: 768px) {{
            body {{ padding: 12px 6px; }}
            .header h1 {{ font-size: 1.25rem; }}
            .header p {{ font-size: 0.75rem; }}
            .gallery-grid {{ grid-template-columns: 1fr 1fr !important; gap: 6px !important; }}
            .card {{ border-radius: 8px; }}
            .card-content {{ padding: 8px 6px; }}
            .card-title {{ font-size: 0.8rem; line-height: 1.2; margin-bottom: 2px; }}
            .card-meta {{ font-size: 0.65rem; margin-bottom: 6px; }}
            .pricing-box {{ padding: 4px 6px; margin-bottom: 6px; flex-direction: column; gap: 2px; }}
            .price-col .label {{ font-size: 0.55rem; }}
            .price-col .val {{ font-size: 0.8rem; }}
            .price-col .val.free {{ font-size: 0.7rem; }}
            .contact-actions {{ grid-template-columns: 1fr; gap: 4px; margin-bottom: 6px; }}
            .btn {{ padding: 5px; font-size: 0.65rem; }}
            .statement {{ font-size: 0.65rem; line-height: 1.2; margin-bottom: 6px; }}
            .detail-box summary {{ font-size: 0.65rem; }}
            .curatorial-text {{ font-size: 0.62rem; padding: 6px; }}
            .badge {{ top: 5px; padding: 2px 5px; font-size: 0.58rem; }}
            .badge-id {{ left: 5px; }}
            .badge-year {{ left: 45px; }}
        }}
    </style>
</head>
<body>
    <div class="header">
        <h1>Katalog Karya {ARTIST_NAME}</h1>
        <p>Arsip Studi Visual, Sketsa Spontan & Gambar Orisinal</p>
        <div class="counter">Total Terarsip: {len(cat_data)} Karya</div>
    </div>
    <div class="gallery-grid">{cards_html}</div>
    <footer>&copy; 2026 {ARTIST_NAME}. Seluruh Hak Cipta Dilindungi.</footer>
</body>
</html>'''
    with open(os.path.join(SKETCH_WEB, "index.html"), "w", encoding="utf-8") as f:
        f.write(html)
    with open(os.path.join(SKETCH_WEB, ".nojekyll"), "w") as f:
        pass

async def start_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if update.effective_user.id != AUTHORIZED_USER_ID and AUTHORIZED_USER_ID != 0:
        return
    await update.message.reply_text(
        f"👋 <b>Asisten Studio {ARTIST_NAME} Siap!</b>\n\n"
        "📸 Kirimkan foto karya dari HP untuk kurasi otomatis dan publikasi instan ke Web.",
        parse_mode="HTML"
    )

async def handle_photo(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_id = update.effective_user.id
    if update.effective_user.id != AUTHORIZED_USER_ID and AUTHORIZED_USER_ID != 0:
        return
    
    AWAITING_RECURATE.pop(user_id, None)
    file_obj = await (update.message.photo[-1] if update.message.photo else update.message.document).get_file()
    raw_b = await file_obj.download_as_bytearray()
    opt_b = compress_img(raw_b)
    
    USER_SESSIONS[user_id] = {"raw": raw_b, "opt": opt_b}
    kb = [
        [InlineKeyboardButton("✏️ Terbitkan Karya Baru", callback_data="act_new")],
        [InlineKeyboardButton("🔄 Kurasi Ulang (Isi ID)", callback_data="act_recurate")]
    ]
    await update.message.reply_text("🎯 <b>Pilih Aksi untuk Karya Ini:</b>", reply_markup=InlineKeyboardMarkup(kb), parse_mode="HTML")

async def handle_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    user_id = query.from_user.id
    sess = USER_SESSIONS.get(user_id)
    if not sess:
        await query.edit_message_text("⚠️ Sesi habis. Kirim foto lagi.")
        return

    if query.data == "act_recurate":
        AWAITING_RECURATE[user_id] = True
        await query.edit_message_text("✏️ Ketik nomor ID karya yang ingin ditimpa (contoh: <code>1</code> atau <code>SKT-001</code>):", parse_mode="HTML")
        return

    target_id = get_next_id()
    await run_draft_flow(query.message, user_id, target_id)

async def run_draft_flow(msg, user_id, target_id):
    sess = USER_SESSIONS.get(user_id)
    b64 = base64.b64encode(sess["opt"]).decode("utf-8")
    ai = analyze_ai(b64, target_id)
    sess["id"] = target_id
    sess["ai"] = ai
    
    kb = [[InlineKeyboardButton("🚀 SETUJUI & PUBLIKASIKAN", callback_data="act_pub"),
           InlineKeyboardButton("❌ Batal", callback_data="act_cancel")]]
    
    text = (
        f"📋 <b>DRAF RESMI: {target_id}</b>\n\n"
        f"📌 <b>Judul:</b> {ai.get('Judul')}\n"
        f"📐 <b>Ukuran:</b> <code>{DEFAULT_SKETCH_SIZE}</code>\n"
        f"🖌 <b>Media:</b> {DEFAULT_SKETCH_MEDIA}\n\n"
        f"💬 <b>Refleksi:</b>\n<i>\"{ai.get('artist_statement_id')}\"</i>\n\n"
        f"📖 <b>Catatan Kuratorial:</b>\n{ai.get('deskripsi_kuratorial')}"
    )
    await msg.reply_text(text, reply_markup=InlineKeyboardMarkup(kb), parse_mode="HTML")

async def handle_text(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_id = update.effective_user.id
    if AWAITING_RECURATE.get(user_id):
        m = re.search(r"(\d+)", update.message.text.strip())
        if m:
            tid = f"SKT-{int(m.group(1)):03d}"
            AWAITING_RECURATE.pop(user_id, None)
            await run_draft_flow(update.message, user_id, tid)
        else:
            await update.message.reply_text("Ketik angka nomor karya (contoh: 1 atau SKT-001):")

async def publish_flow(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    user_id = query.from_user.id
    sess = USER_SESSIONS.get(user_id)
    if query.data == "act_cancel":
        USER_SESSIONS.pop(user_id, None)
        await query.edit_message_text("❌ Dibatalkan.")
        return

    tid = sess["id"]
    ai = sess["ai"]
    raw_b = sess["raw"]
    opt_b = sess["opt"]
    
    await query.edit_message_text(f"⏳ Menyimpan dan menerbitkan {tid}...")
    
    raw_f = os.path.join(SKETCH_RAW, f"{tid}.jpg")
    web_f = os.path.join(SKETCH_IMG, f"{tid}.jpg")
    with open(raw_f, "wb") as f: f.write(raw_b)
    with open(web_f, "wb") as f: f.write(opt_b)
    
    inject_exif(raw_f, ai["Judul"], ai["artist_statement_id"], ai["deskripsi_kuratorial"], ai["tags"])
    inject_exif(web_f, ai["Judul"], ai["artist_statement_id"], ai["deskripsi_kuratorial"], ai["tags"])
    
    cat = []
    if os.path.exists(SKETCH_CATALOG):
        try:
            with open(SKETCH_CATALOG, "r", encoding="utf-8-sig") as f: cat = json.load(f)
        except Exception: pass
    cat = [x for x in cat if x.get("ID") != tid]
    cat.append({
        "ID": tid, "Judul": ai["Judul"], "Tahun": ai.get("Tahun", "2026"),
        "Ukuran": DEFAULT_SKETCH_SIZE, "Media": DEFAULT_SKETCH_MEDIA,
        "artist_statement_id": ai["artist_statement_id"],
        "deskripsi_kuratorial": ai["deskripsi_kuratorial"], "tags": ai["tags"]
    })
    with open(SKETCH_CATALOG, "w", encoding="utf-8") as f:
        json.dump(cat, f, indent=2, ensure_ascii=False)
        
    rebuild_web()
    
    try:
        subprocess.run(["git", "add", "-A"], cwd=SKETCH_WEB, check=True)
        subprocess.run(["git", "commit", "-m", f"Publish {tid}: {ai['Judul']}"], cwd=SKETCH_WEB, check=True)
        subprocess.run(["git", "push", "origin", "main"], cwd=SKETCH_WEB, check=True)
        git_msg = "🌐 <b>Live:</b> Otomatis ter-update ke GitHub Pages & Vercel!"
    except Exception as e:
        git_msg = f"⚠️ Git Push Lokal Tersimpan ({e})"
        
    USER_SESSIONS.pop(user_id, None)
    await query.message.reply_text(f"🎉 <b>KARYA {tid} BERHASIL DIPUBLIKASIKAN!</b>\n\n📌 <b>Judul:</b> {ai['Judul']}\n• {git_msg}", parse_mode="HTML")

def main():
    print(f"=== ASISTEN STUDIO AI: {ARTIST_NAME} ===")
    print("Status: STANDBY MENERIMA FOTO DARI HP...")
    app = ApplicationBuilder().token(TELEGRAM_TOKEN).request(HTTPXRequest(connect_timeout=30, read_timeout=60)).build()
    app.add_handler(CommandHandler("start", start_cmd))
    app.add_handler(MessageHandler(filters.PHOTO | filters.Document.IMAGE, handle_photo))
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, handle_text))
    app.add_handler(CallbackQueryHandler(publish_flow, pattern="^act_pub$|^act_cancel$"))
    app.add_handler(CallbackQueryHandler(handle_callback))
    app.run_polling()

if __name__ == "__main__":
    main()
