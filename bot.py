import os
import logging
from io import BytesIO
import asyncio

from PIL import Image, ImageDraw, ImageFont
from telegram import Update
from telegram.ext import (
    Application,
    CommandHandler,
    MessageHandler,
    ContextTypes,
    filters,
)

logging.basicConfig(
    format="%(asctime)s - %(name)s - %(levelname)s - %(message)s",
    level=logging.INFO,
)
logger = logging.getLogger(__name__)

# Pending watermark requests per user_id
PENDING = {}

DEFAULT_WATERMARK = "@RPSC_RSMSSB_BOARD"
TIMEOUT_SECONDS = 20


def add_watermark(image_bytes: bytes, text: str) -> bytes:
    """Return new image bytes with text watermark."""
    img = Image.open(BytesIO(image_bytes)).convert("RGBA")

    # Transparent layer for text
    txt_layer = Image.new("RGBA", img.size, (255, 255, 255, 0))
    draw = ImageDraw.Draw(txt_layer)

    # Font size relative to image width
    font_size = max(20, img.size[0] // 20)

    # Try a TTF font, fallback to default
    font = None
    for path in [
        "/usr/share/fonts/truetype/dejavu/DejaVuSans-Bold.ttf",  # linux common
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

    # Text size
    bbox = draw.textbbox((0, 0), text, font=font)
    tw = bbox[2] - bbox[0]
    th = bbox[3] - bbox[1]

    margin = 20
    x = img.size[0] - tw - margin
    y = img.size[1] - th - margin
    if x < margin:
        x = margin
    if y < margin:
        y = margin

    # Shadow
    draw.text((x + 2, y + 2), text, font=font, fill=(0, 0, 0, 160))
    # Main text
    draw.text((x, y), text, font=font, fill=(255, 255, 255, 220))

    watermarked = Image.alpha_composite(img, txt_layer).convert("RGB")
    out = BytesIO()
    out.name = "watermarked.jpg"
    watermarked.save(out, format="JPEG", quality=90)
    out.seek(0)
    return out.read()


async def start(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    await update.message.reply_text(
        "👋 Namaste!\n"
        "Mujhe koi bhi photo bhejo.\n"
        "Main uspar text watermark laga dunga.\n\n"
        f"Photo ke baad {TIMEOUT_SECONDS} sec ke andar watermark text bhejna.\n"
        f"Agar nahi bheja toh default '{DEFAULT_WATERMARK}' use hoga."
    )


async def default_watermark_task(app: Application, user_id: int) -> None:
    """Run when user didn't send text in TIMEOUT_SECONDS."""
    try:
        await asyncio.sleep(TIMEOUT_SECONDS)
    except asyncio.CancelledError:
        # Text aa gaya, task cancel ho chuka
        return

    pending = PENDING.get(user_id)
    if not pending:
        return  # Already processed

    chat_id = pending["chat_id"]
    image_bytes = pending["image_bytes"]

    try:
        wm_bytes = add_watermark(image_bytes, DEFAULT_WATERMARK)
    except Exception:
        logger.exception("Error while adding default watermark")
        await app.bot.send_message(
            chat_id=chat_id,
            text="❌ Koi error aa gaya default watermark lagate time.",
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

    # Largest quality photo
    photo = update.message.photo[-1]
    file = await photo.get_file()
    bio = BytesIO()
    await file.download_to_memory(out=bio)
    bio.seek(0)
    image_bytes = bio.read()

    # Purana task ho to cancel
    old = PENDING.get(user.id)
    if old and old.get("task"):
        old["task"].cancel()

    # Naya timeout task
    task = context.application.create_task(
        default_watermark_task(context.application, user.id)
    )

    PENDING[user.id] = {
        "image_bytes": image_bytes,
        "task": task,
        "chat_id": chat_id,
    }

    await update.message.reply_text(
        f"👍 Photo mil gayi!\n"
        f"Ab {TIMEOUT_SECONDS} sec ke andar watermark text bhejo.\n"
        f"Agar kuch nahi bheja toh default '{DEFAULT_WATERMARK}' use hoga."
    )


async def handle_text(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """If user has pending image, treat this text as watermark."""
    user = update.effective_user
    chat_id = update.effective_chat.id
    text = (update.message.text or "").strip()

    # Ignore commands like /start
    if text.startswith("/"):
        return

    pending = PENDING.get(user.id)
    if not pending:
        # No pending image, normal message – ignore
        return

    # Cancel timeout task
    task = pending.get("task")
    if task:
        task.cancel()

    image_bytes = pending["image_bytes"]

    try:
        wm_bytes = add_watermark(image_bytes, text)
    except Exception as e:
        logger.exception("Error while adding custom watermark")
        await update.message.reply_text("❌ Koi error aa gaya watermark lagate time.")
        PENDING.pop(user.id, None)
        return

    PENDING.pop(user.id, None)

    out = BytesIO(wm_bytes)
    out.name = "watermarked.jpg"
    await context.bot.send_photo(
        chat_id=chat_id,
        photo=out,
        caption=f"✅ Watermark laga diya: {text}",
    )


def main() -> None:
    token = os.getenv("BOT_TOKEN")
    if not token:
        raise RuntimeError("BOT_TOKEN environment variable set nahi hai!")

    application = Application.builder().token(token).build()

    application.add_handler(CommandHandler("start", start))
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
