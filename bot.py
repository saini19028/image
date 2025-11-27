import os
import logging
from io import BytesIO
import asyncio

from PIL import Image, ImageDraw, ImageFont
from telegram import (
    Update,
    InlineKeyboardMarkup,
    InlineKeyboardButton,
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
    format="%(asctime)s - %(name)s - %(levelname)s - %(message)s",
    level=logging.INFO,
)
logger = logging.getLogger(__name__)

# --- Globals ---

# image → text pending (20 sec वाला सिस्टम)
PENDING = {}

# प्रति user watermark settings
USER_SETTINGS = {}

# extra state (जैसे transparency पूछते समय)
USER_STATE = {}

DEFAULT_WATERMARK = "@RPSC_RSMSSB_BOARD"
TIMEOUT_SECONDS = 20

# default settings
DEFAULT_SETTINGS = {
    "size_factor": 1.0,                      # 1x size
    "color": (255, 255, 255),               # white
    "alpha": 220,                           # transparency (0–255)
    "position": "bottom_right",
    "style": "normal",
}


# ---------- Helper functions ----------

def get_user_settings(user_id: int) -> dict:
    """Per-user settings, default copy if not present."""
    if user_id not in USER_SETTINGS:
        USER_SETTINGS[user_id] = dict(DEFAULT_SETTINGS)
    return USER_SETTINGS[user_id]


def style_text(text: str, style: str) -> str:
    """Different text styles."""
    if style == "upper":
        return text.upper()
    if style == "lower":
        return text.lower()
    if style == "spaced":
        # प्रत्येक अक्षर के बीच space
        return " ".join(list(text))
    if style == "boxed":
        return f"【{text}】"
    return text


def add_watermark(image_bytes: bytes, text: str, settings: dict) -> bytes:
    """Return new image bytes with text watermark as per settings."""
    img = Image.open(BytesIO(image_bytes)).convert("RGBA")

    # settings
    size_factor = settings.get("size_factor", 1.0)
    color = settings.get("color", (255, 255, 255))
    alpha = settings.get("alpha", 220)
    position = settings.get("position", "bottom_right")
    style = settings.get("style", "normal")

    text = style_text(text, style)

    # transparent layer for text
    txt_layer = Image.new("RGBA", img.size, (255, 255, 255, 0))
    draw = ImageDraw.Draw(txt_layer)

    # base font size relative to width, then multiply by user factor
    base_font_size = max(20, img.size[0] // 20)
    font_size = max(10, int(base_font_size * size_factor))

    # Try a TTF font, fallback to default
    font = None
    for path in [
        "/usr/share/fonts/truetype/dejavu/DejaVuSans-Bold.ttf",
        "/usr/share/fonts/truetype/freefont/FreeSansBold.ttf",
        "arial.ttf",
    ]:
        try:
            font = ImageFont.truetype(path, font_size)
            break
        except Exception:
            continue
    if font is None:
        font = ImageFont.load_default()

    # text size
    bbox = draw.textbbox((0, 0), text, font=font)
    tw = bbox[2] - bbox[0]
    th = bbox[3] - bbox[1]

    W, H = img.size
    margin = 20

    # RGBA colours
    r, g, b = color
    main_fill = (r, g, b, max(0, min(alpha, 255)))
    shadow_fill = (0, 0, 0, 160)

    def draw_at(x, y):
        # shadow
        draw.text((x + 2, y + 2), text, font=font, fill=shadow_fill)
        # main text
        draw.text((x, y), text, font=font, fill=main_fill)

    # positions
    if position == "top_right":
        x = max(margin, W - tw - margin)
        y = margin
        draw_at(x, y)

    elif position == "top_left":
        x = margin
        y = margin
        draw_at(x, y)

    elif position == "bottom_left":
        x = margin
        y = max(margin, H - th - margin)
        draw_at(x, y)

    elif position == "center":
        x = max(margin, (W - tw) // 2)
        y = max(margin, (H - th) // 2)
        draw_at(x, y)

    elif position == "diag_tl_br":
        # left top → right bottom, 3 बार repeat
        for frac in (0.1, 0.5, 0.9):
            x = (W - tw) * frac
            y = (H - th) * frac
            draw_at(x, y)

    elif position == "diag_bl_tr":
        # left bottom → right top
        for frac in (0.1, 0.5, 0.9):
            x = (W - tw) * frac
            y = H - th - (H - th) * frac
            draw_at(x, y)

    else:  # default = bottom_right
        x = max(margin, W - tw - margin)
        y = max(margin, H - th - margin)
        draw_at(x, y)

    watermarked = Image.alpha_composite(img, txt_layer).convert("RGB")
    out = BytesIO()
    out.name = "watermarked.jpg"
    watermarked.save(out, format="JPEG", quality=90)
    out.seek(0)
    return out.read()


def main_menu_keyboard() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        [
            [InlineKeyboardButton("🖼 Image Watermark", callback_data="wm_open_menu")],
        ]
    )


def settings_menu_keyboard() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        [
            [
                InlineKeyboardButton("🔠 Watermark size", callback_data="wm_size_menu"),
                InlineKeyboardButton("🎨 Colour", callback_data="wm_color_menu"),
            ],
            [
                InlineKeyboardButton("📍 Position", callback_data="wm_position_menu"),
            ],
            [
                InlineKeyboardButton(
                    "🌫 Transparency", callback_data="wm_transparency_menu"
                ),
                InlineKeyboardButton(
                    "📝 Text style", callback_data="wm_style_menu"
                ),
            ],
        ]
    )


# ---------- Handlers ----------

async def start(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    text = (
        "👋 Namaste!\n"
        "Mujhe koi bhi photo bhejo, main uspe watermark laga dunga.\n\n"
        "⚙ Settings ke liye niche 'Image Watermark' button ya /image_watermark use karo.\n"
        f"Photo ke baad {TIMEOUT_SECONDS} sec ke andar watermark text bhejna,\n"
        f"warna default '{DEFAULT_WATERMARK}' use hoga."
    )
    await update.message.reply_text(text, reply_markup=main_menu_keyboard())


async def cmd_image_watermark(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    await update.message.reply_text(
        "Image watermark settings:", reply_markup=settings_menu_keyboard()
    )


async def button_callback(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    query = update.callback_query
    user_id = query.from_user.id
    data = query.data
    settings = get_user_settings(user_id)

    await query.answer()

    # main menus
    if data == "wm_open_menu":
        await query.message.reply_text(
            "Image watermark settings:", reply_markup=settings_menu_keyboard()
        )
        return

    if data == "wm_size_menu":
        kb = InlineKeyboardMarkup(
            [
                [
                    InlineKeyboardButton("Small", callback_data="set_size_small"),
                    InlineKeyboardButton("Medium", callback_data="set_size_medium"),
                ],
                [
                    InlineKeyboardButton("Large", callback_data="set_size_large"),
                    InlineKeyboardButton("X-Large", callback_data="set_size_xlarge"),
                ],
            ]
        )
        await query.message.reply_text("Watermark size चुनें:", reply_markup=kb)
        return

    if data == "wm_color_menu":
        kb = InlineKeyboardMarkup(
            [
                [
                    InlineKeyboardButton("🔴 लाल", callback_data="set_color_red"),
                    InlineKeyboardButton("⚫ काला", callback_data="set_color_black"),
                ],
                [
                    InlineKeyboardButton("🟡 पीला", callback_data="set_color_yellow"),
                    InlineKeyboardButton("⚪ सफेद", callback_data="set_color_white"),
                ],
                [
                    InlineKeyboardButton("🌸 गुलाबी", callback_data="set_color_pink"),
                    InlineKeyboardButton("⚙ ग्रे", callback_data="set_color_gray"),
                ],
                [
                    InlineKeyboardButton("🔵 नीला", callback_data="set_color_blue"),
                    InlineKeyboardButton("🟢 हरा", callback_data="set_color_green"),
                ],
            ]
        )
        await query.message.reply_text("Watermark colour चुनें:", reply_markup=kb)
        return

    if data == "wm_position_menu":
        kb = InlineKeyboardMarkup(
            [
                [
                    InlineKeyboardButton(
                        "Right Top", callback_data="set_pos_tr"
                    ),
                    InlineKeyboardButton(
                        "Left Top", callback_data="set_pos_tl"
                    ),
                ],
                [
                    InlineKeyboardButton(
                        "Bottom Right", callback_data="set_pos_br"
                    ),
                    InlineKeyboardButton(
                        "Bottom Left", callback_data="set_pos_bl"
                    ),
                ],
                [
                    InlineKeyboardButton(
                        "Center", callback_data="set_pos_center"
                    ),
                ],
                [
                    InlineKeyboardButton(
                        "LeftTop → RightBottom", callback_data="set_pos_diag_tl_br"
                    ),
                ],
                [
                    InlineKeyboardButton(
                        "LeftBottom → RightTop", callback_data="set_pos_diag_bl_tr"
                    ),
                ],
            ]
        )
        await query.message.reply_text("Watermark position चुनें:", reply_markup=kb)
        return

    if data == "wm_transparency_menu":
        USER_STATE[user_id] = "awaiting_transparency"
        await query.message.reply_text(
            "Transparency (%) bhejo (0 = bilkul halka, 100 = full गाढ़ा).\n"
            "उदाहरण: 60"
        )
        return

    if data == "wm_style_menu":
        kb = InlineKeyboardMarkup(
            [
                [
                    InlineKeyboardButton(
                        "Normal", callback_data="set_style_normal"
                    ),
                    InlineKeyboardButton(
                        "UPPER", callback_data="set_style_upper"
                    ),
                ],
                [
                    InlineKeyboardButton(
                        "lower", callback_data="set_style_lower"
                    ),
                    InlineKeyboardButton(
                        "s p a c e d", callback_data="set_style_spaced"
                    ),
                ],
                [
                    InlineKeyboardButton(
                        "【Boxed】", callback_data="set_style_boxed"
                    ),
                ],
            ]
        )
        await query.message.reply_text("Text style चुनें:", reply_markup=kb)
        return

    # --- Actual setters ---

    # size
    if data.startswith("set_size_"):
        mapping = {
            "set_size_small": 0.7,
            "set_size_medium": 1.0,
            "set_size_large": 1.4,
            "set_size_xlarge": 1.8,
        }
        if data in mapping:
            settings["size_factor"] = mapping[data]
            await query.message.reply_text("✅ Watermark size अपडेट हो गया.")
        return

    # colour
    if data.startswith("set_color_"):
        cmap = {
            "set_color_red": (255, 0, 0),
            "set_color_black": (0, 0, 0),
            "set_color_yellow": (255, 255, 0),
            "set_color_white": (255, 255, 255),
            "set_color_pink": (255, 105, 180),
            "set_color_gray": (128, 128, 128),
            "set_color_blue": (0, 102, 255),
            "set_color_green": (0, 200, 0),
        }
        if data in cmap:
            settings["color"] = cmap[data]
            await query.message.reply_text("✅ Watermark colour सेट हो गया.")
        return

    # position
    if data.startswith("set_pos_"):
        pmap = {
            "set_pos_tr": "top_right",
            "set_pos_tl": "top_left",
            "set_pos_br": "bottom_right",
            "set_pos_bl": "bottom_left",
            "set_pos_center": "center",
            "set_pos_diag_tl_br": "diag_tl_br",
            "set_pos_diag_bl_tr": "diag_bl_tr",
        }
        if data in pmap:
            settings["position"] = pmap[data]
            await query.message.reply_text("✅ Watermark position सेट हो गया.")
        return

    # style
    if data.startswith("set_style_"):
        smap = {
            "set_style_normal": "normal",
            "set_style_upper": "upper",
            "set_style_lower": "lower",
            "set_style_spaced": "spaced",
            "set_style_boxed": "boxed",
        }
        if data in smap:
            settings["style"] = smap[data]
            await query.message.reply_text("✅ Text style सेट हो गया.")
        return


# ----- Watermark main flow (photo + 20 sec text) -----

async def default_watermark_task(app: Application, user_id: int) -> None:
    """Run when user didn't send text in TIMEOUT_SECONDS."""
    try:
        await asyncio.sleep(TIMEOUT_SECONDS)
    except asyncio.CancelledError:
        return

    pending = PENDING.get(user_id)
    if not pending:
        return

    chat_id = pending["chat_id"]
    image_bytes = pending["image_bytes"]
    settings = get_user_settings(user_id)

    try:
        wm_bytes = add_watermark(image_bytes, DEFAULT_WATERMARK, settings)
    except Exception:
        logger.exception("Error while adding default watermark")
        await app.bot.send_message(
            chat_id=chat_id,
            text="❌ Default watermark लगाते समय error आ गया.",
        )
        PENDING.pop(user_id, None)
        return

    PENDING.pop(user_id, None)

    out = BytesIO(wm_bytes)
    out.name = "watermarked.jpg"
    await app.bot.send_photo(
        chat_id=chat_id,
        photo=out,
        caption=f"⌛ Time khatam.\nDefault watermark laga diya: {DEFAULT_WATERMARK}",
    )


async def handle_photo(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    user = update.effective_user
    chat_id = update.effective_chat.id

    if not update.message.photo:
        return

    photo = update.message.photo[-1]
    file = await photo.get_file()
    bio = BytesIO()
    await file.download_to_memory(out=bio)
    bio.seek(0)
    image_bytes = bio.read()

    old = PENDING.get(user.id)
    if old and old.get("task"):
        old["task"].cancel()

    task = context.application.create_task(
        default_watermark_task(context.application, user.id)
    )

    PENDING[user.id] = {
        "image_bytes": image_bytes,
        "task": task,
        "chat_id": chat_id,
    }

    await update.message.reply_text(
        f"👍 Photo मिल गई!\n"
        f"{TIMEOUT_SECONDS} sec के अंदर watermark text भेजो.\n"
        f"नहीं भेजोगे तो default '{DEFAULT_WATERMARK}' लगेगा."
    )


async def handle_text(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    user = update.effective_user
    chat_id = update.effective_chat.id
    text = (update.message.text or "").strip()
    user_id = user.id

    # 1) अगर transparency सेट mode में हैं
    if USER_STATE.get(user_id) == "awaiting_transparency":
        try:
            val = int(text)
            val = max(0, min(100, val))
        except ValueError:
            await update.message.reply_text("कृपया 0 से 100 के बीच कोई संख्या भेजो, जैसे 60.")
            return

        settings = get_user_settings(user_id)
        settings["alpha"] = int(255 * (val / 100))
        USER_STATE.pop(user_id, None)
        await update.message.reply_text(f"✅ Transparency {val}% सेट हो गई.")
        return

    # commands को ignore
    if text.startswith("/"):
        return

    pending = PENDING.get(user_id)
    if not pending:
        # normal chat, ignore
        return

    # timeout task cancel
    task = pending.get("task")
    if task:
        task.cancel()

    image_bytes = pending["image_bytes"]
    settings = get_user_settings(user_id)

    try:
        wm_bytes = add_watermark(image_bytes, text, settings)
    except Exception:
        logger.exception("Error while adding custom watermark")
        await update.message.reply_text("❌ Watermark लगाते समय error आ गया.")
        PENDING.pop(user_id, None)
        return

    PENDING.pop(user_id, None)

    out = BytesIO(wm_bytes)
    out.name = "watermarked.jpg"
    await context.bot.send_photo(
        chat_id=chat_id,
        photo=out,
        caption=f"✅ Watermark laga diya: {text}",
    )


# ---------- Main ----------

def main() -> None:
    token = os.getenv("BOT_TOKEN")
    if not token:
        raise RuntimeError("BOT_TOKEN environment variable set nahi hai!")

    application = Application.builder().token(token).build()

    # commands
    application.add_handler(CommandHandler("start", start))
    application.add_handler(CommandHandler("image_watermark", cmd_image_watermark))

    # callbacks (buttons)
    application.add_handler(CallbackQueryHandler(button_callback))

    # messages
    application.add_handler(
        MessageHandler(filters.PHOTO & ~filters.COMMAND, handle_photo)
    )
    application.add_handler(
        MessageHandler(filters.TEXT & ~filters.COMMAND, handle_text)
    )

    logger.info("Bot started polling...")
    application.run_polling()


if __name__ == "__main__":
    main()
