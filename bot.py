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

PENDING = {}          # pending watermark (photo + 20 sec)
USER_SETTINGS = {}    # प्रति user settings
USER_STATE = {}       # extra state (jaise transparency)

DEFAULT_WATERMARK = "@RPSC_RSMSSB_BOARD"
TIMEOUT_SECONDS = 20

DEFAULT_SETTINGS = {
    "size_factor": 1.0,          # 1x size
    "color": (255, 255, 255),    # white
    "alpha": 220,                # 0–255
    "position": "bottom_right",
    "font": "default",           # font key (नीचे map में)
}

# ---------------- FONT PATH MAP ----------------
# अगर कोई खास font चाहिए तो उसकी .ttf file
# repo में "fonts/" folder में डालकर नाम वही रखो
FONT_PATHS = {
    # fallback / default sans-serif
    "default": [
        "/usr/share/fonts/truetype/dejavu/DejaVuSans-Bold.ttf",
        "/usr/share/fonts/truetype/freefont/FreeSansBold.ttf",
    ],

    # Serif Fonts
    "tnr": ["fonts/Times New Roman.ttf", "fonts/times.ttf"],
    "garamond": ["fonts/Garamond.ttf"],
    "georgia": ["fonts/Georgia.ttf"],
    "bodoni": ["fonts/Bodoni.ttf"],
    "baskerville": ["fonts/Baskerville.ttf"],
    "cambria": ["fonts/Cambria.ttf"],
    "playfair": ["fonts/PlayfairDisplay.ttf"],

    # Sans-serif Fonts
    "arial": ["fonts/Arial.ttf"],
    "helvetica": ["fonts/Helvetica.ttf"],
    "calibri": ["fonts/Calibri.ttf"],
    "verdana": ["fonts/Verdana.ttf"],
    "opensans": ["fonts/OpenSans-Regular.ttf"],
    "roboto": ["fonts/Roboto-Regular.ttf"],
    "lato": ["fonts/Lato-Regular.ttf"],
    "futura": ["fonts/Futura.ttf"],
    "montserrat": ["fonts/Montserrat-Regular.ttf"],
    "publicsans": ["fonts/PublicSans-Regular.ttf"],

    # Script Fonts
    "brushscript": ["fonts/BrushScript.ttf"],
    "pacifico": ["fonts/Pacifico.ttf"],
    "greatvibes": ["fonts/GreatVibes-Regular.ttf"],
    "lucidahand": ["fonts/LucidaHandwriting.ttf"],
    "segoescript": ["fonts/SegoeScript.ttf"],
    "zapfino": ["fonts/Zapfino.ttf"],

    # Monospace Fonts
    "couriernew": ["fonts/Courier New.ttf", "fonts/courbd.ttf"],
    "consolas": ["fonts/Consolas.ttf"],
    "lucidaconsole": ["fonts/LucidaConsole.ttf"],
    "monaco": ["fonts/Monaco.ttf"],

    # Display / Decorative
    "impact": ["fonts/Impact.ttf"],
    "papyrus": ["fonts/Papyrus.ttf"],
    "comicsans": ["fonts/Comic Sans MS.ttf"],
    "copperplate": ["fonts/Copperplate Gothic.ttf"],
    "curlz": ["fonts/Curlz MT.ttf"],
}


def get_user_settings(user_id: int) -> dict:
    if user_id not in USER_SETTINGS:
        USER_SETTINGS[user_id] = dict(DEFAULT_SETTINGS)
    return USER_SETTINGS[user_id]


def load_font(font_key: str, font_size: int):
    """font_key से font load करने की कोशिश, नहीं मिला तो default fallback."""
    paths = FONT_PATHS.get(font_key, []) + FONT_PATHS["default"]
    for p in paths:
        try:
            return ImageFont.truetype(p, font_size)
        except Exception:
            continue
    return ImageFont.load_default()


def add_watermark(image_bytes: bytes, text: str, settings: dict) -> bytes:
    """Return new image bytes with text watermark as per settings."""
    img = Image.open(BytesIO(image_bytes)).convert("RGBA")

    size_factor = settings.get("size_factor", 1.0)
    color = settings.get("color", (255, 255, 255))
    alpha = settings.get("alpha", 220)
    position = settings.get("position", "bottom_right")
    font_key = settings.get("font", "default")

    txt_layer = Image.new("RGBA", img.size, (255, 255, 255, 0))
    draw = ImageDraw.Draw(txt_layer)

    base_font_size = max(20, img.size[0] // 20)
    font_size = max(10, int(base_font_size * size_factor))

    font = load_font(font_key, font_size)

    # text size
    bbox = draw.textbbox((0, 0), text, font=font)
    tw = bbox[2] - bbox[0]
    th = bbox[3] - bbox[1]

    W, H = img.size
    margin = 20

    r, g, b = color
    main_fill = (r, g, b, max(0, min(alpha, 255)))
    shadow_fill = (0, 0, 0, 160)

    def draw_at(x, y, d: ImageDraw.ImageDraw):
        d.text((x + 2, y + 2), text, font=font, fill=shadow_fill)
        d.text((x, y), text, font=font, fill=main_fill)

    # ---- normal (non-diagonal) positions ----
    if position == "top_right":
        x = max(margin, W - tw - margin)
        y = margin
        draw_at(x, y, draw)

    elif position == "top_left":
        x = margin
        y = margin
        draw_at(x, y, draw)

    elif position == "bottom_left":
        x = margin
        y = max(margin, H - th - margin)
        draw_at(x, y, draw)

    elif position == "center":
        x = max(margin, (W - tw) // 2)
        y = max(margin, (H - th) // 2)
        draw_at(x, y, draw)

    elif position == "bottom_right":
        x = max(margin, W - tw - margin)
        y = max(margin, H - th - margin)
        draw_at(x, y, draw)

    # ---- diagonal single-text positions (तिरछा एक बार) ----
    elif position in ("diag_tl_br", "diag_bl_tr"):
        temp = Image.new("RGBA", img.size, (255, 255, 255, 0))
        d2 = ImageDraw.Draw(temp)

        cx = (W - tw) // 2
        cy = (H - th) // 2
        draw_at(cx, cy, d2)

        angle = -35 if position == "diag_tl_br" else 35
        rotated = temp.rotate(angle, expand=True)
        rw, rh = rotated.size
        left = max(0, (rw - W) // 2)
        top = max(0, (rh - H) // 2)
        cropped = rotated.crop((left, top, left + W, top + H))

        txt_layer = Image.alpha_composite(txt_layer, cropped)

    else:
        x = max(margin, W - tw - margin)
        y = max(margin, H - th - margin)
        draw_at(x, y, draw)

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
                InlineKeyboardButton("🔠 Size", callback_data="wm_size_menu"),
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
                    "📝 Text Font", callback_data="wm_style_menu"
                ),
            ],
            [
                InlineKeyboardButton("⬅ Back", callback_data="back_main"),
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

    # ---- Back buttons ----
    if data == "back_main":
        await query.message.reply_text(
            "Main menu:", reply_markup=main_menu_keyboard()
        )
        return

    if data == "back_settings":
        USER_STATE.pop(user_id, None)
        await query.message.reply_text(
            "Image watermark settings:", reply_markup=settings_menu_keyboard()
        )
        return

    # ---- Open menus ----
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
                [
                    InlineKeyboardButton("⬅ Back", callback_data="back_settings"),
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
                [
                    InlineKeyboardButton("⬅ Back", callback_data="back_settings"),
                ],
            ]
        )
        await query.message.reply_text("Watermark colour चुनें:", reply_markup=kb)
        return

    if data == "wm_position_menu":
        kb = InlineKeyboardMarkup(
            [
                [
                    InlineKeyboardButton("Right Top", callback_data="set_pos_tr"),
                    InlineKeyboardButton("Left Top", callback_data="set_pos_tl"),
                ],
                [
                    InlineKeyboardButton("Bottom Right", callback_data="set_pos_br"),
                    InlineKeyboardButton("Bottom Left", callback_data="set_pos_bl"),
                ],
                [
                    InlineKeyboardButton("Center", callback_data="set_pos_center"),
                ],
                [
                    InlineKeyboardButton(
                        "LeftTop → RightBottom",
                        callback_data="set_pos_diag_tl_br",
                    ),
                ],
                [
                    InlineKeyboardButton(
                        "LeftBottom → RightTop",
                        callback_data="set_pos_diag_bl_tr",
                    ),
                ],
                [
                    InlineKeyboardButton("⬅ Back", callback_data="back_settings"),
                ],
            ]
        )
        await query.message.reply_text("Watermark position चुनें:", reply_markup=kb)
        return

    if data == "wm_transparency_menu":
        USER_STATE[user_id] = "awaiting_transparency"
        kb = InlineKeyboardMarkup(
            [
                [
                    InlineKeyboardButton("⬅ Back", callback_data="back_settings"),
                ]
            ]
        )
        await query.message.reply_text(
            "Transparency (%) bhejo (0 = हल्का, 100 = ज़्यादा गाढ़ा).\n"
            "उदाहरण: 60",
            reply_markup=kb,
        )
        return

    if data == "wm_style_menu":
        # Fonts menu
        kb = InlineKeyboardMarkup(
            [
                # Serif
                [
                    InlineKeyboardButton("Times New Roman", callback_data="font_tnr"),
                    InlineKeyboardButton("Garamond", callback_data="font_garamond"),
                ],
                [
                    InlineKeyboardButton("Georgia", callback_data="font_georgia"),
                    InlineKeyboardButton("Baskerville", callback_data="font_baskerville"),
                ],
                [
                    InlineKeyboardButton("Cambria", callback_data="font_cambria"),
                    InlineKeyboardButton("Playfair Display", callback_data="font_playfair"),
                ],
                [
                    InlineKeyboardButton("Bodoni", callback_data="font_bodoni"),
                ],

                # Sans-serif
                [
                    InlineKeyboardButton("Arial", callback_data="font_arial"),
                    InlineKeyboardButton("Helvetica", callback_data="font_helvetica"),
                ],
                [
                    InlineKeyboardButton("Calibri", callback_data="font_calibri"),
                    InlineKeyboardButton("Verdana", callback_data="font_verdana"),
                ],
                [
                    InlineKeyboardButton("Open Sans", callback_data="font_opensans"),
                    InlineKeyboardButton("Roboto", callback_data="font_roboto"),
                ],
                [
                    InlineKeyboardButton("Lato", callback_data="font_lato"),
                    InlineKeyboardButton("Futura", callback_data="font_futura"),
                ],
                [
                    InlineKeyboardButton("Montserrat", callback_data="font_montserrat"),
                    InlineKeyboardButton("Public Sans", callback_data="font_publicsans"),
                ],

                # Script
                [
                    InlineKeyboardButton("Brush Script", callback_data="font_brushscript"),
                    InlineKeyboardButton("Pacifico", callback_data="font_pacifico"),
                ],
                [
                    InlineKeyboardButton("Great Vibes", callback_data="font_greatvibes"),
                    InlineKeyboardButton("Lucida Handwriting", callback_data="font_lucidahand"),
                ],
                [
                    InlineKeyboardButton("Segoe Script", callback_data="font_segoescript"),
                    InlineKeyboardButton("Zapfino", callback_data="font_zapfino"),
                ],

                # Monospace
                [
                    InlineKeyboardButton("Courier New", callback_data="font_couriernew"),
                    InlineKeyboardButton("Consolas", callback_data="font_consolas"),
                ],
                [
                    InlineKeyboardButton("Lucida Console", callback_data="font_lucidaconsole"),
                    InlineKeyboardButton("Monaco", callback_data="font_monaco"),
                ],

                # Display / decorative
                [
                    InlineKeyboardButton("Impact", callback_data="font_impact"),
                    InlineKeyboardButton("Papyrus", callback_data="font_papyrus"),
                ],
                [
                    InlineKeyboardButton("Comic Sans MS", callback_data="font_comicsans"),
                    InlineKeyboardButton("Copperplate Gothic", callback_data="font_copperplate"),
                ],
                [
                    InlineKeyboardButton("Curlz MT", callback_data="font_curlz"),
                ],

                [
                    InlineKeyboardButton("⬅ Back", callback_data="back_settings"),
                ],
            ]
        )
        await query.message.reply_text("Text font चुनें:", reply_markup=kb)
        return

    # ---- setters ----

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

    # fonts
    if data.startswith("font_"):
        fmap = {
            "font_tnr": "tnr",
            "font_garamond": "garamond",
            "font_georgia": "georgia",
            "font_bodoni": "bodoni",
            "font_baskerville": "baskerville",
            "font_cambria": "cambria",
            "font_playfair": "playfair",

            "font_arial": "arial",
            "font_helvetica": "helvetica",
            "font_calibri": "calibri",
            "font_verdana": "verdana",
            "font_opensans": "opensans",
            "font_roboto": "roboto",
            "font_lato": "lato",
            "font_futura": "futura",
            "font_montserrat": "montserrat",
            "font_publicsans": "publicsans",

            "font_brushscript": "brushscript",
            "font_pacifico": "pacifico",
            "font_greatvibes": "greatvibes",
            "font_lucidahand": "lucidahand",
            "font_segoescript": "segoescript",
            "font_zapfino": "zapfino",

            "font_couriernew": "couriernew",
            "font_consolas": "consolas",
            "font_lucidaconsole": "lucidaconsole",
            "font_monaco": "monaco",

            "font_impact": "impact",
            "font_papyrus": "papyrus",
            "font_comicsans": "comicsans",
            "font_copperplate": "copperplate",
            "font_curlz": "curlz",
        }
        if data in fmap:
            settings["font"] = fmap[data]
            await query.message.reply_text("✅ Text font सेट हो गया.")
        return


# ----- watermark flow -----

async def default_watermark_task(app: Application, user_id: int) -> None:
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

    # transparency mode
    if USER_STATE.get(user_id) == "awaiting_transparency":
        if text.startswith("/"):
            # command भेज दिया, ignore + state साफ
            USER_STATE.pop(user_id, None)
            return
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

    if text.startswith("/"):
        return

    pending = PENDING.get(user_id)
    if not pending:
        return

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


# ---------- main ----------

def main() -> None:
    token = os.getenv("BOT_TOKEN")
    if not token:
        raise RuntimeError("BOT_TOKEN environment variable set nahi hai!")

    application = Application.builder().token(token).build()

    application.add_handler(CommandHandler("start", start))
    application.add_handler(CommandHandler("image_watermark", cmd_image_watermark))

    application.add_handler(CallbackQueryHandler(button_callback))

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
