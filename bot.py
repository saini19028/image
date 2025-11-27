import os
import logging
from io import BytesIO
import asyncio
from datetime import datetime

from PIL import Image, ImageDraw, ImageFont
from motor.motor_asyncio import AsyncIOMotorClient
from telegram import (
    Update,
    InlineKeyboardButton,
    InlineKeyboardMarkup,
)
from telegram.ext import (
    Application,
    CommandHandler,
    MessageHandler,
    CallbackQueryHandler,
    ContextTypes,
    filters,
)

logging.basicConfig(
    format="%(asctime)s — %(name)s — %(levelname)s — %(message)s",
    level=logging.INFO
)

logger = logging.getLogger(__name__)

# ------------------------------------------------------------
# ENV VARIABLES
# ------------------------------------------------------------
BOT_TOKEN = os.getenv("BOT_TOKEN")
MONGO_URL = os.getenv("MONGODB_URL")
OWNER_ID = int(os.getenv("OWNER_ID", "0"))

if not BOT_TOKEN:
    raise RuntimeError("BOT_TOKEN env missing")
if not MONGO_URL:
    raise RuntimeError("MONGODB_URL env missing")

# ------------------------------------------------------------
# MONGO CLIENT
# ------------------------------------------------------------
mongo = AsyncIOMotorClient(MONGO_URL)
db = mongo["watermark_bot"]
users_col = db["users"]

# ------------------------------------------------------------
# TEMP MEMORY
# ------------------------------------------------------------
PENDING = {}      # user_id -> {"img": bytes, "task": asyncio.Task, "chat_id": int}
USER_STATE = {}   # user_id -> state string

TIMEOUT = 20
DEFAULT_WATERMARK = "@RPSC_RSMSSB_BOARD"

# ------------------------------------------------------------
# DEFAULT SETTINGS
# ------------------------------------------------------------
DEFAULT_SETTINGS = {
    "size_factor": 1.0,
    "color": (255, 255, 255),
    "alpha": 220,
    "position": "bottom_right",
    "font_key": "sans_default",
    "transform": "normal",
}

# ------------------------------------------------------------
# WORKING FONT STYLES (DejaVu family based)
# ------------------------------------------------------------
# हर style के लिए: key -> (label, [paths...])
FONT_STYLES = {
    # Sans-serif
    "sans_default": (
        "Sans Regular",
        [
            "/usr/share/fonts/truetype/dejavu/DejaVuSans.ttf",
            "/usr/share/fonts/truetype/dejavu/DejaVuSans-Bold.ttf",
        ],
    ),
    "sans_bold": (
        "Sans Bold",
        ["/usr/share/fonts/truetype/dejavu/DejaVuSans-Bold.ttf"],
    ),
    "sans_italic": (
        "Sans Italic",
        ["/usr/share/fonts/truetype/dejavu/DejaVuSans-Oblique.ttf"],
    ),
    "sans_condensed": (
        "Sans Condensed",
        ["/usr/share/fonts/truetype/dejavu/DejaVuSansCondensed.ttf"],
    ),

    # Serif
    "serif_regular": (
        "Serif Classic",
        ["/usr/share/fonts/truetype/dejavu/DejaVuSerif.ttf"],
    ),
    "serif_bold": (
        "Serif Bold",
        ["/usr/share/fonts/truetype/dejavu/DejaVuSerif-Bold.ttf"],
    ),

    # Monospace
    "mono_regular": (
        "Mono Code",
        ["/usr/share/fonts/truetype/dejavu/DejaVuSansMono.ttf"],
    ),

    # Extra variations (fallback same paths, but अलग नाम for user)
    "title_heavy": (
        "Title Heavy",
        [
            "/usr/share/fonts/truetype/dejavu/DejaVuSans-Bold.ttf",
            "/usr/share/fonts/truetype/dejavu/DejaVuSerif-Bold.ttf",
        ],
    ),
    "soft_script_like": (
        "Soft Script-ish",
        [
            "/usr/share/fonts/truetype/dejavu/DejaVuSerif.ttf",
            "/usr/share/fonts/truetype/dejavu/DejaVuSans-Oblique.ttf",
        ],
    ),
}

# fallback list अगर ऊपर कुछ भी fail हो जाए
FALLBACK_FONTS = [
    "/usr/share/fonts/truetype/dejavu/DejaVuSans.ttf",
    "/usr/share/fonts/truetype/dejavu/DejaVuSans-Bold.ttf",
]


# ------------------------------------------------------------
# DATABASE FUNCTIONS
# ------------------------------------------------------------
async def get_user(user_id: int) -> dict:
    user = await users_col.find_one({"_id": user_id})
    if not user:
        user = {
            "_id": user_id,
            "settings": DEFAULT_SETTINGS.copy(),
            "joined": datetime.utcnow(),
        }
        await users_col.insert_one(user)
    else:
        # पुरानी entry में कुछ key ना हो तो default से fill
        for k, v in DEFAULT_SETTINGS.items():
            if k not in user["settings"]:
                user["settings"][k] = v
    return user


async def get_settings(user_id: int) -> dict:
    user = await get_user(user_id)
    return user["settings"]


async def update_settings(user_id: int, settings: dict):
    await users_col.update_one(
        {"_id": user_id},
        {"$set": {"settings": settings}},
        upsert=True,
    )


# ------------------------------------------------------------
# FONT HELPERS
# ------------------------------------------------------------
def load_font(font_key: str, size: int) -> ImageFont.FreeTypeFont:
    entry = FONT_STYLES.get(font_key) or FONT_STYLES["sans_default"]
    label, paths = entry

    for p in paths:
        try:
            return ImageFont.truetype(p, size)
        except Exception:
            continue

    for p in FALLBACK_FONTS:
        try:
            return ImageFont.truetype(p, size)
        except Exception:
            continue

    return ImageFont.load_default()


def font_label(font_key: str) -> str:
    entry = FONT_STYLES.get(font_key)
    if not entry:
        return "Sans Regular"
    return entry[0]


def apply_transform(text: str, transform: str) -> str:
    if transform == "upper":
        return text.upper()
    if transform == "lower":
        return text.lower()
    if transform == "spaced":
        return " ".join(list(text))
    if transform == "boxed":
        return f"【{text}】"
    return text


# ------------------------------------------------------------
# WATERMARK ENGINE
# ------------------------------------------------------------
def create_watermark(img_bytes: bytes, text: str, settings: dict) -> bytes:
    img = Image.open(BytesIO(img_bytes)).convert("RGBA")

    size_factor = settings.get("size_factor", 1.0)
    color = tuple(settings.get("color", (255, 255, 255)))
    alpha = int(settings.get("alpha", 220))
    position = settings.get("position", "bottom_right")
    font_key = settings.get("font_key", "sans_default")
    transform = settings.get("transform", "normal")

    text = apply_transform(text, transform)

    W, H = img.size
    txt_layer = Image.new("RGBA", img.size, (0, 0, 0, 0))
    draw = ImageDraw.Draw(txt_layer)

    base_size = max(20, W // 20)
    font_size = max(10, int(base_size * size_factor))
    font = load_font(font_key, font_size)

    bbox = draw.textbbox((0, 0), text, font=font)
    tw = bbox[2] - bbox[0]
    th = bbox[3] - bbox[1]
    margin = 20

    main_fill = (*color, max(0, min(alpha, 255)))
    shadow_fill = (0, 0, 0, 160)

    def put(x: int, y: int, dr: ImageDraw.ImageDraw):
        dr.text((x + 2, y + 2), text, font=font, fill=shadow_fill)
        dr.text((x, y), text, font=font, fill=main_fill)

    if position == "top_left":
        put(margin, margin, draw)
    elif position == "top_right":
        put(W - tw - margin, margin, draw)
    elif position == "bottom_left":
        put(margin, H - th - margin, draw)
    elif position == "bottom_right":
        put(W - tw - margin, H - th - margin, draw)
    elif position == "center":
        put((W - tw) // 2, (H - th) // 2, draw)
    elif position in ("diag_tl_br", "diag_bl_tr"):
        temp = Image.new("RGBA", img.size, (0, 0, 0, 0))
        d2 = ImageDraw.Draw(temp)
        put((W - tw) // 2, (H - th) // 2, d2)

        angle = -35 if position == "diag_tl_br" else 35
        rot = temp.rotate(angle, expand=True)
        rw, rh = rot.size
        left = max(0, (rw - W) // 2)
        top = max(0, (rh - H) // 2)
        cropped = rot.crop((left, top, left + W, top + H))
        txt_layer = Image.alpha_composite(txt_layer, cropped)
    else:
        put(W - tw - margin, H - th - margin, draw)

    watermarked = Image.alpha_composite(img, txt_layer).convert("RGB")
    out = BytesIO()
    out.name = "watermarked.jpg"
    watermarked.save(out, "JPEG", quality=90)
    out.seek(0)
    return out.read()


# ------------------------------------------------------------
# KEYBOARDS
# ------------------------------------------------------------
def main_menu() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup([
        [InlineKeyboardButton("🖼 Image Watermark", callback_data="wm_menu")]
    ])


def settings_menu() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup([
        [
            InlineKeyboardButton("🔠 Size", callback_data="size_menu"),
            InlineKeyboardButton("🎨 Colour", callback_data="color_menu"),
        ],
        [
            InlineKeyboardButton("📍 Position", callback_data="pos_menu"),
        ],
        [
            InlineKeyboardButton("🌫 Transparency", callback_data="trans_menu"),
            InlineKeyboardButton("📝 Text Style", callback_data="style_menu"),
        ],
        [InlineKeyboardButton("⬅ Back", callback_data="back_main")],
    ])


# ------------------------------------------------------------
# START
# ------------------------------------------------------------
async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await get_user(update.effective_user.id)
    await update.message.reply_text(
        "👋 Namaste!\n"
        "Mujhe koi bhi image bhejo, main uspe watermark laga dunga.\n\n"
        f"📌 Photo ke baad {TIMEOUT} sec ke andar watermark text bhejna,\n"
        f"warna default `{DEFAULT_WATERMARK}` lagega.\n\n"
        "⚙ Settings ke liye niche button use karo.",
        reply_markup=main_menu()
    )


# ------------------------------------------------------------
# BROADCAST (/broadcast message)
# ------------------------------------------------------------
async def broadcast(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if update.effective_user.id != OWNER_ID:
        return await update.message.reply_text("❌ Owner Only Command.")

    text = update.message.text.split(" ", 1)
    if len(text) < 2 or not text[1].strip():
        return await update.message.reply_text("❌ Use: /broadcast your_message")

    msg = text[1].strip()
    sent = 0

    async for user in users_col.find({}):
        uid = user["_id"]
        try:
            await context.bot.send_message(uid, msg)
            sent += 1
        except Exception:
            pass

    await update.message.reply_text(f"✅ Broadcast sent to {sent} users.")


# ------------------------------------------------------------
# BUTTON CALLBACK
# ------------------------------------------------------------
async def callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    user_id = query.from_user.id
    await query.answer()

    data = query.data
    settings = await get_settings(user_id)

    # Back to menus
    if data == "wm_menu":
        return await query.message.reply_text("Watermark Settings:", reply_markup=settings_menu())

    if data == "back_main":
        USER_STATE.pop(user_id, None)
        return await query.message.reply_text("Main Menu:", reply_markup=main_menu())

    # SIZE MENU
    if data == "size_menu":
        kb = InlineKeyboardMarkup([
            [
                InlineKeyboardButton("Small", callback_data="size_small"),
                InlineKeyboardButton("Medium", callback_data="size_med"),
            ],
            [
                InlineKeyboardButton("Large", callback_data="size_large"),
                InlineKeyboardButton("X-Large", callback_data="size_xl"),
            ],
            [InlineKeyboardButton("⬅ Back", callback_data="wm_menu")],
        ])
        return await query.message.reply_text("Watermark Size:", reply_markup=kb)

    if data.startswith("size_"):
        mp = {
            "size_small": 0.7,
            "size_med": 1.0,
            "size_large": 1.4,
            "size_xl": 1.8,
        }
        settings["size_factor"] = mp[data]
        await update_settings(user_id, settings)
        return await query.message.reply_text("✅ Size updated.")

    # COLOUR MENU
    if data == "color_menu":
        kb = InlineKeyboardMarkup([
            [
                InlineKeyboardButton("🔴 लाल", callback_data="c_red"),
                InlineKeyboardButton("⚫ काला", callback_data="c_black"),
            ],
            [
                InlineKeyboardButton("⚪ सफेद", callback_data="c_white"),
                InlineKeyboardButton("🟡 पीला", callback_data="c_yellow"),
            ],
            [
                InlineKeyboardButton("🟢 हरा", callback_data="c_green"),
                InlineKeyboardButton("🔵 नीला", callback_data="c_blue"),
            ],
            [
                InlineKeyboardButton("🌸 गुलाबी", callback_data="c_pink"),
                InlineKeyboardButton("⚙ ग्रे", callback_data="c_gray"),
            ],
            [InlineKeyboardButton("⬅ Back", callback_data="wm_menu")],
        ])
        return await query.message.reply_text("Watermark Colour:", reply_markup=kb)

    if data.startswith("c_"):
        cmap = {
            "c_red": (255, 0, 0),
            "c_black": (0, 0, 0),
            "c_white": (255, 255, 255),
            "c_yellow": (255, 255, 0),
            "c_green": (0, 200, 0),
            "c_blue": (0, 102, 255),
            "c_pink": (255, 105, 180),
            "c_gray": (128, 128, 128),
        }
        settings["color"] = cmap[data]
        await update_settings(user_id, settings)
        return await query.message.reply_text("✅ Colour updated.")

    # POSITION MENU
    if data == "pos_menu":
        kb = InlineKeyboardMarkup([
            [
                InlineKeyboardButton("Top-Left", callback_data="p_tl"),
                InlineKeyboardButton("Top-Right", callback_data="p_tr"),
            ],
            [
                InlineKeyboardButton("Bottom-Left", callback_data="p_bl"),
                InlineKeyboardButton("Bottom-Right", callback_data="p_br"),
            ],
            [
                InlineKeyboardButton("Center", callback_data="p_c"),
            ],
            [
                InlineKeyboardButton("Diag TL→BR", callback_data="p_d1"),
            ],
            [
                InlineKeyboardButton("Diag BL→TR", callback_data="p_d2"),
            ],
            [InlineKeyboardButton("⬅ Back", callback_data="wm_menu")],
        ])
        return await query.message.reply_text("Watermark Position:", reply_markup=kb)

    if data.startswith("p_"):
        pos = {
            "p_tl": "top_left",
            "p_tr": "top_right",
            "p_bl": "bottom_left",
            "p_br": "bottom_right",
            "p_c": "center",
            "p_d1": "diag_tl_br",
            "p_d2": "diag_bl_tr",
        }
        settings["position"] = pos[data]
        await update_settings(user_id, settings)
        return await query.message.reply_text("✅ Position updated.")

    # TRANSPARENCY
    if data == "trans_menu":
        USER_STATE[user_id] = "await_transparency"
        kb = InlineKeyboardMarkup([
            [InlineKeyboardButton("⬅ Back", callback_data="wm_menu")]
        ])
        return await query.message.reply_text(
            "Transparency (%) bhejo (0–100), example: 60",
            reply_markup=kb,
        )

    # STYLE MASTER MENU
    if data == "style_menu":
        kb = InlineKeyboardMarkup([
            [
                InlineKeyboardButton("Fonts", callback_data="font_menu"),
                InlineKeyboardButton("Transform", callback_data="transform_menu"),
            ],
            [InlineKeyboardButton("⬅ Back", callback_data="wm_menu")],
        ])
        return await query.message.reply_text("Text Style:", reply_markup=kb)

    # FONT MENU
    if data == "font_menu":
        rows = []
        temp = []
        for key, (label, paths) in FONT_STYLES.items():
            temp.append(InlineKeyboardButton(label, callback_data=f"font_{key}"))
            if len(temp) == 2:
                rows.append(temp)
                temp = []
        if temp:
            rows.append(temp)
        rows.append([InlineKeyboardButton("⬅ Back", callback_data="style_menu")])
        kb = InlineKeyboardMarkup(rows)
        return await query.message.reply_text("Choose Font:", reply_markup=kb)

    if data.startswith("font_"):
        font_key = data.replace("font_", "")
        if font_key in FONT_STYLES:
            settings["font_key"] = font_key
            await update_settings(user_id, settings)
            return await query.message.reply_text(
                f"✅ Font set: {font_label(font_key)}"
            )
        else:
            return await query.message.reply_text("❌ Font not found.")

    # TRANSFORM MENU
    if data == "transform_menu":
        kb = InlineKeyboardMarkup([
            [
                InlineKeyboardButton("Normal", callback_data="t_norm"),
                InlineKeyboardButton("UPPER", callback_data="t_up"),
            ],
            [
                InlineKeyboardButton("lower", callback_data="t_low"),
                InlineKeyboardButton("s p a c e d", callback_data="t_sp"),
            ],
            [
                InlineKeyboardButton("【Boxed】", callback_data="t_box"),
            ],
            [InlineKeyboardButton("⬅ Back", callback_data="style_menu")],
        ])
        return await query.message.reply_text("Text Transform:", reply_markup=kb)

    if data.startswith("t_"):
        mp = {
            "t_norm": "normal",
            "t_up": "upper",
            "t_low": "lower",
            "t_sp": "spaced",
            "t_box": "boxed",
        }
        settings["transform"] = mp[data]
        await update_settings(user_id, settings)
        return await query.message.reply_text("✅ Text transform updated.")


# ------------------------------------------------------------
# PHOTO HANDLER
# ------------------------------------------------------------
async def photo_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user = update.effective_user
    chat_id = update.effective_chat.id

    file = await update.message.photo[-1].get_file()
    bio = BytesIO()
    await file.download_to_memory(out=bio)
    img_bytes = bio.getvalue()

    # Cancel old
    old = PENDING.get(user.id)
    if old and old.get("task"):
        old["task"].cancel()

    task = context.application.create_task(timeout_task(context.application, user.id))

    PENDING[user.id] = {
        "img": img_bytes,
        "task": task,
        "chat_id": chat_id,
    }

    await update.message.reply_text(
        f"📷 Photo received!\n"
        f"{TIMEOUT} sec ke andar watermark text bhejo.\n"
        f"Warana default `{DEFAULT_WATERMARK}` use hoga."
    )


# ------------------------------------------------------------
# TEXT HANDLER
# ------------------------------------------------------------
async def text_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user = update.effective_user
    text = update.message.text.strip()

    # Broadcast
    if text.startswith("/broadcast"):
        return await broadcast(update, context)

    # Transparency input
    if USER_STATE.get(user.id) == "await_transparency":
        if not text.isdigit():
            return await update.message.reply_text("❌ Please send number 0–100.")
        val = max(0, min(100, int(text)))
        settings = await get_settings(user.id)
        settings["alpha"] = int(255 * (val / 100))
        await update_settings(user.id, settings)
        USER_STATE.pop(user.id, None)
        return await update.message.reply_text(f"✅ Transparency set to {val}%.")

    # If not pending image, ignore
    if user.id not in PENDING:
        return

    # Cancel timeout
    pending = PENDING[user.id]
    if pending["task"]:
        pending["task"].cancel()

    img_bytes = pending["img"]
    settings = await get_settings(user.id)

    wm_bytes = create_watermark(img_bytes, text, settings)
    out = BytesIO(wm_bytes)
    out.name = "watermarked.jpg"

    del PENDING[user.id]

    await update.message.reply_photo(
        out,
        caption=f"✅ Watermark added.\nFont: {font_label(settings['font_key'])}",
    )


# ------------------------------------------------------------
# TIMEOUT TASK
# ------------------------------------------------------------
async def timeout_task(app: Application, user_id: int):
    try:
        await asyncio.sleep(TIMEOUT)
    except asyncio.CancelledError:
        return

    pending = PENDING.get(user_id)
    if not pending:
        return

    img_bytes = pending["img"]
    chat_id = pending["chat_id"]
    settings = await get_settings(user_id)

    wm_bytes = create_watermark(img_bytes, DEFAULT_WATERMARK, settings)
    out = BytesIO(wm_bytes)
    out.name = "watermarked.jpg"

    del PENDING[user_id]

    await app.bot.send_photo(
        chat_id,
        out,
        caption=f"⌛ Time up! Default watermark added.\nFont: {font_label(settings['font_key'])}",
    )


# ------------------------------------------------------------
# MAIN
# ------------------------------------------------------------
def main():
    app = Application.builder().token(BOT_TOKEN).build()

    app.add_handler(CommandHandler("start", start))
    app.add_handler(CallbackQueryHandler(callback))
    app.add_handler(MessageHandler(filters.PHOTO, photo_handler))
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, text_handler))

    logger.info("Bot started...")
    app.run_polling()


if __name__ == "__main__":
    main()
