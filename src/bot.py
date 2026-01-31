import asyncio
import base64
import logging
import time
from io import BytesIO

import httpx
from aiogram import Bot, Dispatcher, Router, F
from aiogram.types import (
    Message, LinkPreviewOptions, BufferedInputFile,
    CallbackQuery, InlineKeyboardMarkup, InlineKeyboardButton,
    LabeledPrice, PreCheckoutQuery
)
from aiogram.filters import CommandStart, Command
from aiogram.enums import ParseMode
from aiogram.client.default import DefaultBotProperties
from aiogram.exceptions import TelegramBadRequest

from PIL import Image, ImageFilter

logger = logging.getLogger(__name__)

from src.config import (
    TELEGRAM_BOT_TOKEN, SEARCH_COST_STARS, SEARCH_PACK_5_STARS,
    UNLOCK_SINGLE_STARS, UNLOCK_ALL_STARS, ADMIN_CHAT_ID,
    API_BALANCE_ALERT_THRESHOLD
)
from src.facecheck_client import FaceCheckClient
from src import database as db
from src import vk_client

router = Router()
facecheck = FaceCheckClient()

# Version for debugging deployments
BOT_VERSION = "v4.0-eng"

async def check_api_balance_and_alert(bot: Bot):
    """Check FaceCheck API balance and send notification after each search."""
    if not ADMIN_CHAT_ID:
        return

    try:
        info = await facecheck.get_info()
        if not info:
            return

        remaining = info.get('remaining_credits', 0)

        # Always notify about remaining balance
        warning = ""
        if remaining <= API_BALANCE_ALERT_THRESHOLD:
            warning = "\n\n LOW BALANCE! Top up at facecheck.id"

        await bot.send_message(
            chat_id=ADMIN_CHAT_ID,
            text=f"Search completed\n"
                 f"API credits remaining: <b>{remaining}</b>{warning}"
        )
        logger.info(f"Balance notification sent: {remaining} credits remaining")

    except Exception as e:
        logger.error(f"Balance check error: {e}")

# Store pending search results temporarily (search_id -> {result, created_at, user_id, unlocked})
pending_results: dict[str, dict] = {}

# Store pending photos for paid search (user_id -> image_bytes)
pending_photos: dict[int, bytes] = {}

# Store last search_id for each user (for /debug command)
last_search_by_user: dict[int, str] = {}

# Store pending reminder tasks (search_id -> asyncio.Task)
pending_reminders: dict[str, asyncio.Task] = {}

# Results expiration time in seconds
RESULTS_EXPIRATION_SECONDS = 30 * 60  # 30 minutes

# Reminder time (5 minutes before expiration)
REMINDER_DELAY_SECONDS = 25 * 60  # 25 minutes

# Free search shows only 3 results (paid shows 10)
FREE_RESULTS_COUNT = 3


async def schedule_expiry_reminder(bot: Bot, user_id: int, search_id: str):
    """Schedule a reminder 5 minutes before results expire."""
    try:
        await asyncio.sleep(REMINDER_DELAY_SECONDS)

        # Check if results still exist and not unlocked
        if search_id in pending_results:
            result = pending_results[search_id]
            if not result.get("_unlocked", False):
                # Send reminder
                try:
                    await bot.send_message(
                        chat_id=user_id,
                        text="⏰ <b>5 minutes left!</b>\n\n"
                             "Your search results will expire soon.\n"
                             f"Unlock all for just <b>{UNLOCK_ALL_STARS} ⭐</b>",
                        reply_markup=get_unlock_all_keyboard(search_id)
                    )
                    logger.info(f"Reminder sent to {user_id} for search {search_id}")
                except Exception as e:
                    logger.error(f"Failed to send reminder: {e}")

    except asyncio.CancelledError:
        pass
    finally:
        # Cleanup
        if search_id in pending_reminders:
            del pending_reminders[search_id]


def mask_name(name: str) -> str:
    """Mask name like 'Anna Kozlova' -> 'An***a Ko***va'"""
    if not name:
        return "***"

    parts = name.split()
    masked_parts = []

    for part in parts:
        if len(part) <= 2:
            masked_parts.append(part[0] + "***")
        elif len(part) <= 4:
            masked_parts.append(part[0] + "***" + part[-1])
        else:
            masked_parts.append(part[:2] + "***" + part[-2:])

    return " ".join(masked_parts)


def is_result_expired(search_id: str) -> bool:
    """Check if search result has expired."""
    if search_id not in pending_results:
        return True

    result = pending_results[search_id]
    created_at = result.get("_created_at", 0)
    return (time.time() - created_at) > RESULTS_EXPIRATION_SECONDS

WELCOME_MESSAGE = f"""<b>🔍 Face Search Bot</b>

Send a photo — I'll find matching profiles online.

<b>💰 Pricing:</b>
• First search: <b>FREE</b> ({FREE_RESULTS_COUNT} preview results)
• Unlock all results: <b>{UNLOCK_ALL_STARS} ⭐</b>
• Full search: <b>{SEARCH_COST_STARS} ⭐</b> (10 results + links)
• 5 searches: <b>{SEARCH_PACK_5_STARS} ⭐</b> (save {SEARCH_COST_STARS * 5 - SEARCH_PACK_5_STARS} ⭐)

⏰ <i>Results expire in 30 minutes</i>

<b>📋 Commands:</b>
/buy — Buy searches
/info — Your credits

<i>Results from public sources. Photos not stored.</i>"""


def blur_image(img_bytes: bytes, blur_radius: int = 30) -> bytes:
    """Apply heavy blur to image."""
    img = Image.open(BytesIO(img_bytes))
    blurred = img.filter(ImageFilter.GaussianBlur(radius=blur_radius))
    output = BytesIO()
    blurred.save(output, format="JPEG", quality=70)
    return output.getvalue()


async def fetch_image_from_url(url: str) -> bytes | None:
    """Fetch image from URL."""
    try:
        async with httpx.AsyncClient(timeout=10.0, follow_redirects=True) as client:
            response = await client.get(url)
            if response.status_code == 200:
                content_type = response.headers.get("content-type", "")
                if "image" in content_type or url.lower().endswith(('.jpg', '.jpeg', '.png', '.webp', '.gif')):
                    return response.content
    except Exception as e:
        logger.error(f"Failed to fetch image from {url}: {e}")
    return None


async def get_image_bytes(face: dict) -> bytes | None:
    """Get image bytes from face result - try base64 first, then URL."""
    # Try base64 first
    base64_img = face.get("base64", "")
    if base64_img and base64_img.startswith("data:image"):
        try:
            img_data = base64_img.split(",", 1)[1]
            return base64.b64decode(img_data)
        except Exception as e:
            logger.error(f"Base64 decode error: {e}")

    # Try image_url or thumb_url from API
    for url_field in ["image_url", "thumb_url", "url"]:
        url = face.get(url_field)
        if url and url.startswith("http"):
            img_bytes = await fetch_image_from_url(url)
            if img_bytes:
                return img_bytes

    return None


async def extract_names_from_results(faces: list[dict]) -> dict[str, str]:
    """Extract names from VK profiles in search results."""
    urls = [face.get("url", "") for face in faces if face.get("url")]
    return await vk_client.extract_names_from_urls(urls)


async def send_name_summary(message: Message, names: dict[str, str]):
    """Send summary of found names."""
    if not names:
        return

    lines = ["<b>Names found:</b>\n"]
    for url, name in names.items():
        lines.append(f"- <b>{name}</b>\n  {url}")

    await message.answer(
        "\n".join(lines),
        link_preview_options=LinkPreviewOptions(is_disabled=True)
    )


def get_search_keyboard() -> InlineKeyboardMarkup:
    """Create keyboard for buying a paid search."""
    return InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(
            text=f"🔍 Search — {SEARCH_COST_STARS} ⭐",
            callback_data="paid_search"
        )],
    ])


def get_unlock_keyboard(search_id: str, result_index: int) -> InlineKeyboardMarkup:
    """Create keyboard to unlock a single result link."""
    return InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(
            text=f"🔓 Unlock — {UNLOCK_SINGLE_STARS} ⭐",
            callback_data=f"unlock_{search_id}_{result_index}"
        )],
    ])


def get_unlock_all_keyboard(search_id: str) -> InlineKeyboardMarkup:
    """Create keyboard to unlock all results at once."""
    return InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(
            text=f"🔓 Unlock ALL 10 — {UNLOCK_ALL_STARS} ⭐",
            callback_data=f"unlock_all_{search_id}"
        )],
    ])


@router.message(CommandStart())
async def cmd_start(message: Message):
    user = await db.get_or_create_user(
        message.from_user.id,
        message.from_user.username
    )

    # Track event
    await db.track_event(message.from_user.id, "bot_start")

    # Check for daily free search
    granted = await db.check_and_grant_daily_free_search(message.from_user.id)
    if granted:
        await message.answer(
            "🎁 <b>Daily bonus!</b>\n"
            "You got 1 free search today!\n\n" + WELCOME_MESSAGE
        )
    else:
        await message.answer(WELCOME_MESSAGE)


@router.message(Command("info"))
async def cmd_info(message: Message):
    credits = await db.get_user_credits(message.from_user.id)
    free = credits.get("free_searches", 0)
    paid = credits.get("paid_searches", 0)
    total = free + paid

    info = await facecheck.get_info()
    api_credits = "N/A"
    if info:
        api_credits = info.get('remaining_credits', 'N/A')

    await message.answer(
        f"<b>Your credits</b>\n\n"
        f"Free searches: {free}\n"
        f"Paid searches: {paid}\n"
        f"Total: {total}\n\n"
        f"API credits: {api_credits}\n"
        f"Bot version: {BOT_VERSION}"
    )


@router.message(Command("buy"))
async def cmd_buy(message: Message):
    credits = await db.get_user_credits(message.from_user.id)
    free = credits.get("free_searches", 0)
    paid = credits.get("paid_searches", 0)

    keyboard = InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(
            text=f"🔍 1 search — {SEARCH_COST_STARS} ⭐",
            callback_data="buy_1_search"
        )],
        [InlineKeyboardButton(
            text=f"🔥 5 searches — {SEARCH_PACK_5_STARS} ⭐ (save {SEARCH_COST_STARS * 5 - SEARCH_PACK_5_STARS} ⭐)",
            callback_data="buy_5_searches"
        )],
    ])

    await message.answer(
        f"<b>💰 Buy Searches</b>\n\n"
        f"Your credits: <b>{free + paid}</b>\n\n"
        f"Each search = 10 results with direct links.",
        reply_markup=keyboard
    )


@router.message(Command("reset"))
async def cmd_reset(message: Message):
    """Reset user credits - ADMIN ONLY."""
    # Check if user is admin
    if str(message.from_user.id) != ADMIN_CHAT_ID:
        await message.answer("This command is not available.")
        return

    success = await db.reset_user_credits(message.from_user.id)
    if success:
        await message.answer(
            "Credits reset! You have 1 free search."
        )
    else:
        await message.answer("Failed to reset credits.")


@router.message(Command("debug"))
async def cmd_debug(message: Message):
    """Show all results from last search (for debugging)."""
    user_id = message.from_user.id

    if user_id not in last_search_by_user:
        await message.answer(
            "Search not found. Send a photo first."
        )
        return

    search_id = last_search_by_user[user_id]

    if search_id not in pending_results:
        await message.answer(
            "Search results expired. Make a new search."
        )
        return

    result = pending_results[search_id]
    output = result.get("output", {})
    faces = output.get("items", [])

    if not faces:
        await message.answer("No results in last search.")
        return

    # Build text list of ALL results
    lines = [f"<b>Debug: All {len(faces)} results</b>\n"]

    for i, face in enumerate(faces, 1):
        score = face.get("score", 0)
        url = face.get("url", "N/A")
        lines.append(f"{i}. [{score}%] {url}")

    # Split into chunks if too long (Telegram limit ~4096 chars)
    full_text = "\n".join(lines)

    if len(full_text) <= 4000:
        await message.answer(full_text, link_preview_options=LinkPreviewOptions(is_disabled=True))
    else:
        # Send in chunks
        chunk_lines = []
        chunk_len = 0
        for line in lines:
            if chunk_len + len(line) + 1 > 4000:
                await message.answer("\n".join(chunk_lines), link_preview_options=LinkPreviewOptions(is_disabled=True))
                chunk_lines = []
                chunk_len = 0
            chunk_lines.append(line)
            chunk_len += len(line) + 1

        if chunk_lines:
            await message.answer("\n".join(chunk_lines), link_preview_options=LinkPreviewOptions(is_disabled=True))


@router.message(Command("stats"))
async def cmd_stats(message: Message):
    """Show bot statistics - ADMIN ONLY."""
    if str(message.from_user.id) != ADMIN_CHAT_ID:
        await message.answer("This command is not available.")
        return

    stats = await db.get_stats()

    events_text = "\n".join([
        f"  • {k}: {v}" for k, v in stats.get("events", {}).items()
    ]) or "  No events yet"

    await message.answer(
        f"<b>📊 Bot Statistics</b>\n\n"
        f"👥 Total users: <b>{stats['total_users']}</b>\n"
        f"💰 Paying users: <b>{stats['paying_users']}</b>\n"
        f"📈 Conversion: <b>{stats['conversion_rate']}%</b>\n"
        f"⭐ Total revenue: <b>{stats['total_stars']} stars</b>\n\n"
        f"<b>Events:</b>\n{events_text}"
    )


@router.callback_query(F.data == "paid_search")
async def handle_paid_search_request(callback: CallbackQuery, bot: Bot):
    """User wants to do a paid search - send invoice."""
    await db.track_event(callback.from_user.id, "payment_clicked", {"type": "paid_search"})
    await bot.send_invoice(
        chat_id=callback.from_user.id,
        title="Face Search",
        description="10 results with links",
        payload="paid_search",
        currency="XTR",
        prices=[LabeledPrice(label="Face Search", amount=SEARCH_COST_STARS)],
    )
    await callback.answer()


@router.callback_query(F.data == "buy_1_search")
async def handle_buy_1_search(callback: CallbackQuery, bot: Bot):
    """Buy 1 search credit."""
    await db.track_event(callback.from_user.id, "payment_clicked", {"type": "buy_1_search"})
    await bot.send_invoice(
        chat_id=callback.from_user.id,
        title="1 Search",
        description="10 results with links",
        payload="buy_1_search",
        currency="XTR",
        prices=[LabeledPrice(label="1 Search", amount=SEARCH_COST_STARS)],
    )
    await callback.answer()


@router.callback_query(F.data == "buy_5_searches")
async def handle_buy_5_searches(callback: CallbackQuery, bot: Bot):
    """Buy 5 searches pack."""
    await db.track_event(callback.from_user.id, "payment_clicked", {"type": "buy_5_searches"})
    await bot.send_invoice(
        chat_id=callback.from_user.id,
        title="5 Searches Pack",
        description=f"50 results total, save {SEARCH_COST_STARS * 5 - SEARCH_PACK_5_STARS}",
        payload="buy_5_searches",
        currency="XTR",
        prices=[LabeledPrice(label="5 Searches", amount=SEARCH_PACK_5_STARS)],
    )
    await callback.answer()


@router.callback_query(F.data.startswith("unlock_all_"))
async def handle_unlock_all(callback: CallbackQuery, bot: Bot):
    """Unlock all 10 results at once."""
    search_id = callback.data.replace("unlock_all_", "")
    await db.track_event(callback.from_user.id, "unlock_clicked", {"type": "unlock_all", "search_id": search_id})
    await bot.send_invoice(
        chat_id=callback.from_user.id,
        title="Unlock all 10",
        description="Get all 10 links",
        payload=f"unlock_all_{search_id}",
        currency="XTR",
        prices=[LabeledPrice(label="Unlock all", amount=UNLOCK_ALL_STARS)],
    )
    await callback.answer()


@router.callback_query(F.data.startswith("unlock_"))
async def handle_unlock(callback: CallbackQuery, bot: Bot):
    # Skip if it's unlock_all (handled separately)
    if callback.data.startswith("unlock_all_"):
        return

    parts = callback.data.split("_")
    search_id = parts[1]
    result_index = int(parts[2])

    await db.track_event(callback.from_user.id, "unlock_clicked", {"type": "unlock_single", "search_id": search_id})

    # Send invoice for unlocking the link
    await bot.send_invoice(
        chat_id=callback.from_user.id,
        title="Unlock link",
        description="Get the source link",
        payload=f"unlock_{search_id}_{result_index}",
        currency="XTR",
        prices=[LabeledPrice(label="Unlock link", amount=UNLOCK_SINGLE_STARS)],
    )
    await callback.answer()


@router.pre_checkout_query()
async def handle_pre_checkout(pre_checkout: PreCheckoutQuery, bot: Bot):
    await bot.answer_pre_checkout_query(pre_checkout.id, ok=True)


@router.message(F.successful_payment)
async def handle_successful_payment(message: Message, bot: Bot):
    payload = message.successful_payment.invoice_payload
    payment_id = message.successful_payment.telegram_payment_charge_id
    stars = message.successful_payment.total_amount
    user_id = message.from_user.id

    # Track payment completed event
    await db.track_event(user_id, "payment_completed", {"type": payload, "stars": stars})

    if payload == "paid_search":
        # User paid for a search - now execute it
        await db.record_payment(user_id, stars, 1, payment_id)

        if user_id not in pending_photos:
            await message.answer(
                "Payment received, but photo not found. Send a new photo."
            )
            return

        image_bytes = pending_photos.pop(user_id)
        await execute_paid_search(message, bot, image_bytes)

    elif payload == "buy_1_search":
        # Add 1 search credit
        await db.add_paid_searches(user_id, 1)
        await db.record_payment(user_id, stars, 1, payment_id)
        await message.answer(
            "✅ <b>1 search added!</b>\n\n"
            "📸 Send a photo to start searching."
        )

    elif payload == "buy_5_searches":
        # Add 5 search credits
        await db.add_paid_searches(user_id, 5)
        await db.record_payment(user_id, stars, 5, payment_id)
        await message.answer(
            "✅ <b>5 searches added!</b>\n\n"
            "📸 Send a photo to start searching."
        )

    elif payload.startswith("unlock_all_"):
        search_id = payload.replace("unlock_all_", "")

        # Cancel reminder for this search
        if search_id in pending_reminders:
            pending_reminders[search_id].cancel()
            del pending_reminders[search_id]

        if search_id in pending_results and not is_result_expired(search_id):
            results = pending_results[search_id]
            results["_unlocked"] = True  # Mark as unlocked
            faces = results.get("output", {}).get("items", [])[:10]

            lines = ["🔓 <b>All links unlocked!</b>\n"]
            for i, face in enumerate(faces, 1):
                score = face.get("score", 0)
                url = face.get("url", "N/A")
                lines.append(f"{i}. [{score}%] {url}")

            await message.answer(
                "\n".join(lines),
                link_preview_options=LinkPreviewOptions(is_disabled=True)
            )

            # Upsell after unlock
            await message.answer(
                "🔍 <b>Want to search again?</b>\n"
                f"Buy more searches for <b>{SEARCH_COST_STARS} ⭐</b> each!",
                reply_markup=InlineKeyboardMarkup(inline_keyboard=[
                    [InlineKeyboardButton(
                        text=f"🔥 5 searches — {SEARCH_PACK_5_STARS} ⭐",
                        callback_data="buy_5_searches"
                    )]
                ])
            )
        else:
            await message.answer(
                "⏰ <b>Results expired!</b>\n\n"
                "Send a new photo to search again."
            )

        await db.record_payment(user_id, stars, 0, payment_id)

    elif payload.startswith("unlock_"):
        parts = payload.split("_")
        search_id = parts[1]
        result_index = int(parts[2])

        if search_id in pending_results and not is_result_expired(search_id):
            results = pending_results[search_id]
            faces = results.get("output", {}).get("items", [])

            if result_index < len(faces):
                face = faces[result_index]
                url = face.get("url", "N/A")

                await message.answer(
                    f"🔓 <b>Link unlocked!</b>\n\n"
                    f"Match: {face.get('score', 0)}%\n"
                    f"🔗 {url}",
                    link_preview_options=LinkPreviewOptions(is_disabled=True)
                )
        else:
            await message.answer(
                "⏰ <b>Results expired!</b>\n\n"
                "Send a new photo to search again."
            )

        await db.record_payment(user_id, stars, 0, payment_id)


async def execute_paid_search(message: Message, bot: Bot, image_bytes: bytes):
    """Execute a paid search and show 5 results with links."""
    status_msg = await message.answer("Searching...")

    last_progress_text = ""

    async def on_progress(progress: int):
        nonlocal last_progress_text
        new_text = f"Searching... {progress}%"
        if new_text != last_progress_text:
            try:
                await status_msg.edit_text(new_text)
                last_progress_text = new_text
            except TelegramBadRequest:
                pass

    result = await facecheck.find_face(image_bytes, demo=False, on_progress=on_progress)

    if not result:
        await status_msg.edit_text("Search error. Try again.")
        return

    if result.get("error"):
        await status_msg.edit_text(f"Error: {result['error']}")
        return

    output = result.get("output", {})
    faces = output.get("items", [])

    searched = output.get('searchedFaces')
    searched_str = f"{searched:,}" if isinstance(searched, int) else "N/A"
    took_sec = output.get('tookSeconds') or 0

    stats = (
        f"<b>Search complete</b>\n\n"
        f"Faces scanned: {searched_str}\n"
        f"Time: {took_sec:.1f}s\n"
        f"Results: {min(len(faces), 10)}\n"
    )

    if not faces:
        await status_msg.edit_text(stats + "\n<i>No matches found.</i>")
        return

    # Store search results with timestamp
    search_id = result.get("id_search") or str(message.message_id)
    result["_created_at"] = time.time()
    pending_results[search_id] = result
    last_search_by_user[message.from_user.id] = search_id

    await status_msg.edit_text(stats + "\nSending results...")

    # Paid search: show 10 results with links
    for i, face in enumerate(faces[:10], 1):
        score = face.get("score", 0)
        url = face.get("url", "N/A")

        caption = f"<b>#{i}</b> - Match: {score}%\n{url}"

        img_bytes = await get_image_bytes(face)
        if img_bytes:
            try:
                photo_file = BufferedInputFile(img_bytes, filename=f"face_{i}.jpg")
                await message.answer_photo(
                    photo_file,
                    caption=caption,
                    link_preview_options=LinkPreviewOptions(is_disabled=True)
                )
            except Exception as e:
                logger.error(f"Send photo error: {e}")
                await message.answer(caption, link_preview_options=LinkPreviewOptions(is_disabled=True))
        else:
            await message.answer(caption, link_preview_options=LinkPreviewOptions(is_disabled=True))

    await status_msg.delete()

    # Extract and show names from VK profiles
    names = await extract_names_from_results(faces[:10])
    await send_name_summary(message, names)

    # Track search completed event
    await db.track_event(message.from_user.id, "search_completed", {"type": "paid", "results": min(len(faces), 10)})

    # Check API balance and alert if low
    await check_api_balance_and_alert(bot)


@router.message(F.photo)
async def handle_photo(message: Message, bot: Bot):
    user = await db.get_or_create_user(
        message.from_user.id,
        message.from_user.username
    )

    # Track event
    await db.track_event(message.from_user.id, "photo_sent")

    # Check for daily free search
    await db.check_and_grant_daily_free_search(message.from_user.id)

    credits = await db.get_user_credits(message.from_user.id)
    free_searches = credits.get("free_searches", 0)

    # Download the photo
    photo = message.photo[-1]
    file = await bot.get_file(photo.file_id)
    image_data = await bot.download_file(file.file_path)
    image_bytes = image_data.read()

    if free_searches > 0:
        # FREE SEARCH: 10 results with hidden links
        await execute_free_search(message, bot, image_bytes)
    else:
        # PAID SEARCH: Store photo and request payment
        pending_photos[message.from_user.id] = image_bytes
        await bot.send_invoice(
            chat_id=message.from_user.id,
            title="Face Search",
            description="10 results with links",
            payload="paid_search",
            currency="XTR",
            prices=[LabeledPrice(label="Face Search", amount=SEARCH_COST_STARS)],
        )


async def execute_free_search(message: Message, bot: Bot, image_bytes: bytes):
    """Execute a free search and show 10 results with hidden links."""
    status_msg = await message.answer("Searching...")

    last_progress_text = ""

    async def on_progress(progress: int):
        nonlocal last_progress_text
        new_text = f"Searching... {progress}%"
        if new_text != last_progress_text:
            try:
                await status_msg.edit_text(new_text)
                last_progress_text = new_text
            except TelegramBadRequest:
                pass

    result = await facecheck.find_face(image_bytes, demo=False, on_progress=on_progress)

    if not result:
        await status_msg.edit_text("Search error. Try again.")
        return

    if result.get("error"):
        await status_msg.edit_text(f"Error: {result['error']}")
        return

    # Use free search credit
    await db.use_search(message.from_user.id)

    output = result.get("output", {})
    faces = output.get("items", [])

    searched = output.get('searchedFaces')
    searched_str = f"{searched:,}" if isinstance(searched, int) else "N/A"
    took_sec = output.get('tookSeconds') or 0

    stats = (
        f"<b>Free search complete</b>\n\n"
        f"Faces scanned: {searched_str}\n"
        f"Time: {took_sec:.1f}s\n"
        f"Results: {min(len(faces), 10)}\n"
    )

    if not faces:
        await status_msg.edit_text(stats + "\n<i>No matches found.</i>")
        return

    # Store search results with timestamp
    search_id = result.get("id_search") or str(message.message_id)
    result["_created_at"] = time.time()
    pending_results[search_id] = result
    last_search_by_user[message.from_user.id] = search_id

    # Calculate how many more results exist
    total_results = min(len(faces), 10)
    hidden_count = total_results - FREE_RESULTS_COUNT

    await status_msg.edit_text(
        stats +
        f"\n⏰ <b>Results expire in 30 minutes!</b>\n"
        f"<i>🔒 Showing {FREE_RESULTS_COUNT} of {total_results} results. "
        f"Unlock all {total_results} for {UNLOCK_ALL_STARS} ⭐</i>"
    )

    # Free search: show only FREE_RESULTS_COUNT results
    for i, face in enumerate(faces[:FREE_RESULTS_COUNT], 1):
        score = face.get("score", 0)

        caption = f"<b>#{i}</b> — Match: {score}%\n🔒 <i>Link hidden</i>"

        img_bytes = await get_image_bytes(face)
        if img_bytes:
            try:
                photo_file = BufferedInputFile(img_bytes, filename=f"face_{i}.jpg")
                await message.answer_photo(
                    photo_file,
                    caption=caption,
                    reply_markup=get_unlock_keyboard(search_id, i - 1)
                )
            except Exception as e:
                logger.error(f"Send photo error: {e}")
                await message.answer(caption, reply_markup=get_unlock_keyboard(search_id, i - 1))
        else:
            await message.answer(caption, reply_markup=get_unlock_keyboard(search_id, i - 1))

    # Show teaser for hidden results
    if hidden_count > 0:
        await message.answer(
            f"➕ <b>{hidden_count} more results hidden</b>\n"
            f"<i>Unlock all to see them!</i>"
        )

    # Extract names and show as teasers
    names = await extract_names_from_results(faces[:total_results])
    if names:
        teaser_lines = ["👤 <b>Names found (masked):</b>\n"]
        for url, name in list(names.items())[:5]:  # Show max 5 teasers
            masked = mask_name(name)
            teaser_lines.append(f"• {masked}")
        teaser_lines.append(f"\n<i>Unlock to see full names and links!</i>")
        await message.answer("\n".join(teaser_lines))

    # Add "Unlock All" button with urgency
    await message.answer(
        f"🔥 <b>Unlock all {total_results} results</b> — just <b>{UNLOCK_ALL_STARS} ⭐</b>\n\n"
        f"⏰ <b>Results expire in 30 min!</b>\n"
        f"<i>Don't lose these matches</i>",
        reply_markup=get_unlock_all_keyboard(search_id)
    )

    # Track search completed event
    await db.track_event(message.from_user.id, "search_completed", {"type": "free", "results": total_results})

    # Schedule reminder 5 minutes before expiration
    reminder_task = asyncio.create_task(
        schedule_expiry_reminder(bot, message.from_user.id, search_id)
    )
    pending_reminders[search_id] = reminder_task

    # Check API balance and alert if low
    await check_api_balance_and_alert(bot)


@router.message()
async def handle_other(message: Message):
    await message.answer(
        "📸 Send a photo to search for matching profiles."
    )


def create_bot() -> tuple[Bot, Dispatcher]:
    bot = Bot(
        token=TELEGRAM_BOT_TOKEN,
        default=DefaultBotProperties(parse_mode=ParseMode.HTML)
    )
    dp = Dispatcher()
    dp.include_router(router)
    return bot, dp
