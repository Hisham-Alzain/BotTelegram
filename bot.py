"""
bot.py — Telegram Archive Bot with persistent ReplyKeyboard navigation.

Navigation model
----------------
Every message the user sends is a button label.
The bot resolves the label to a menu node or file, then updates
the persistent bottom keyboard to reflect that level.

State (per user, stored in context.user_data)
---------------------------------------------
  stack : list[int]   — stack of menu_id values (current path from root)
                        empty  → we are at root
                        [3]    → we are inside menu 3
                        [3, 7] → inside menu 7 which is a child of 3

Admin flow
----------
Admins see an extra "⚙️ إدارة" button in every keyboard.
Tapping it enters an admin sub-flow (add menu / add file / rename / delete).
"""

import logging
import os
from telegram import (
    Update,
    ReplyKeyboardMarkup,
    KeyboardButton,
    ReplyKeyboardRemove,
)
from telegram.ext import (
    Application,
    CommandHandler,
    MessageHandler,
    ConversationHandler,
    filters,
    ContextTypes,
)
import db

# ── Config ────────────────────────────────────────────────────────────────────

BOT_TOKEN = os.environ.get(
    "BOT_TOKEN", "8855275808:AAFaHABjCMLc5T2sVh4_wm2bA86oLlJvUhU"
)

# Add your numeric Telegram user IDs here  →  find yours via @userinfobot
ADMIN_IDS: set[int] = {
    776738328,  # ← replace with real admin IDs
}

logging.basicConfig(
    format="%(asctime)s | %(levelname)s | %(message)s",
    level=logging.INFO,
)
logger = logging.getLogger(__name__)

# ── Special button labels (fixed strings) ─────────────────────────────────────
BTN_BACK = "⬅️ رجوع"
BTN_HOME = "🏠 الرئيسية"
BTN_ADMIN = "⚙️ إدارة"
BTN_ADD_MENU = "➕ قسم جديد"
BTN_ADD_FILE = "📎 رفع ملف"
BTN_RENAME = "✏️ إعادة تسمية"
BTN_DELETE = "🗑 حذف القسم"
BTN_DONE = "✅ تم"
BTN_CANCEL = "❌ إلغاء"

# Conversation states
(
    ADM_CHOOSING,
    ADM_TYPING_LABEL,
    ADM_UPLOADING_FILE,
    ADM_TYPING_CAPTION,
    ADM_TYPING_RENAME,
) = range(5)

TYPE_EMOJI = {"document": "📄", "audio": "🎵", "video": "🎬"}

MIME_TO_TYPE = {
    "application/pdf": "document",
    "application/vnd.ms-powerpoint": "document",
    "application/vnd.openxmlformats-officedocument.presentationml.presentation": "document",
    "audio/mpeg": "audio",
    "audio/ogg": "audio",
    "audio/wav": "audio",
    "audio/mp4": "audio",
    "audio/x-m4a": "audio",
    "video/mp4": "video",
    "video/webm": "video",
}

# ── Helpers ───────────────────────────────────────────────────────────────────


def is_admin(user_id: int) -> bool:
    return user_id in ADMIN_IDS


def current_menu_id(ctx: ContextTypes.DEFAULT_TYPE) -> int | None:
    stack = ctx.user_data.get("stack", [])
    return stack[-1] if stack else None


def build_keyboard(menu_id: int | None, user_id: int) -> ReplyKeyboardMarkup:
    """
    Build the persistent bottom keyboard for the given menu level.
    Buttons are laid out 2 per row (matching the screenshot style).
    """
    rows: list[list[KeyboardButton]] = []

    # ── Child submenus ──
    children = db.get_root_menus() if menu_id is None else db.get_children(menu_id)
    child_labels = [KeyboardButton(f"📁 {ch['label']}") for ch in children]

    # ── Files ──
    file_labels = []
    if menu_id is not None:
        for f in db.get_files(menu_id):
            emoji = TYPE_EMOJI.get(f["file_type"], "📄")
            label = f["caption"] or f"ملف {f['id']}"
            file_labels.append(KeyboardButton(f"{emoji} {label}"))

    all_items = child_labels + file_labels

    # Lay out 2 per row
    for i in range(0, len(all_items), 2):
        rows.append(all_items[i : i + 2])

    # ── Navigation row ──
    nav = []
    if menu_id is not None:
        nav.append(KeyboardButton(BTN_BACK))
    nav.append(KeyboardButton(BTN_HOME))
    if nav:
        rows.append(nav)

    # ── Admin row ──
    if is_admin(user_id):
        rows.append([KeyboardButton(BTN_ADMIN)])

    return ReplyKeyboardMarkup(rows, resize_keyboard=True)


def breadcrumb(menu_id: int | None) -> str:
    if menu_id is None:
        return "🏠 *القائمة الرئيسية*"
    crumbs = db.get_breadcrumb(menu_id)
    return "📍 " + " › ".join(crumbs)


async def show_level(
    update: Update,
    ctx: ContextTypes.DEFAULT_TYPE,
    menu_id: int | None,
    message: str | None = None,
):
    """Send a message with the keyboard for the given menu level."""
    text = message or breadcrumb(menu_id)
    kb = build_keyboard(menu_id, update.effective_user.id)
    await update.effective_message.reply_text(
        text, reply_markup=kb, parse_mode="Markdown"
    )


# ── Resolve a button label to a menu or file ──────────────────────────────────


def resolve_label(label: str, menu_id: int | None):
    """
    Given the current menu_id and a button label the user tapped,
    return either:
      ("menu", menu_row)   if it's a child submenu
      ("file", file_row)   if it's a file
      None                 if not found
    """
    # Strip the leading emoji+space that we prefix buttons with
    clean = label
    if len(label) > 2 and label[1] == " ":
        clean = label[2:]  # e.g. "📁 الانشاد" → "الانشاد"
    if len(label) > 3 and label[2] == " ":
        clean = label[3:]  # handles multi-byte emoji like 📎

    children = db.get_root_menus() if menu_id is None else db.get_children(menu_id)
    for ch in children:
        if ch["label"] == clean or f"📁 {ch['label']}" == label:
            return ("menu", ch)

    if menu_id is not None:
        for f in db.get_files(menu_id):
            emoji = TYPE_EMOJI.get(f["file_type"], "📄")
            fallback = "ملف " + str(f["id"])
            btn = f"{emoji} {f['caption'] or fallback}"
            if btn == label:
                return ("file", f)

    return None


# ── /start ────────────────────────────────────────────────────────────────────


async def start(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    ctx.user_data["stack"] = []
    user = update.effective_user
    await show_level(
        update, ctx, None, f"أهلاً *{user.first_name}* 👋\nاختر قسماً للبدء:"
    )


# ── /help ─────────────────────────────────────────────────────────────────────


async def help_cmd(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    lines = [
        "📚 *مكتبة الأرشيف*\n",
        "/start — القائمة الرئيسية",
        "/help  — المساعدة",
    ]
    if is_admin(update.effective_user.id):
        lines += [
            "\n🔧 *للمشرفين*",
            "اضغط ⚙️ إدارة من أي قسم لإضافة أو تعديل المحتوى.",
        ]
    await update.message.reply_text("\n".join(lines), parse_mode="Markdown")


# ── Main message handler (navigation) ─────────────────────────────────────────


async def handle_message(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    text = update.message.text.strip()
    user_id = update.effective_user.id
    stack = ctx.user_data.setdefault("stack", [])
    menu_id = stack[-1] if stack else None

    # ── System nav buttons ──
    if text == BTN_HOME:
        ctx.user_data["stack"] = []
        await show_level(update, ctx, None)
        return

    if text == BTN_BACK:
        if stack:
            stack.pop()
        menu_id = stack[-1] if stack else None
        await show_level(update, ctx, menu_id)
        return

    # ── Resolve label ──
    result = resolve_label(text, menu_id)

    if result is None:
        await update.message.reply_text(
            "⚠️ لم يتم التعرف على هذا الزر. اضغط 🏠 الرئيسية للبدء من جديد."
        )
        return

    kind, row = result

    if kind == "menu":
        stack.append(row["id"])
        await show_level(update, ctx, row["id"])

    elif kind == "file":
        ftype = row["file_type"]
        fid = row["file_id"]
        cap = row["caption"] or ""
        if ftype == "audio":
            await update.message.reply_audio(audio=fid, caption=cap)
        elif ftype == "video":
            await update.message.reply_video(video=fid, caption=cap)
        else:
            await update.message.reply_document(document=fid, caption=cap)


# ── Admin conversation ────────────────────────────────────────────────────────


def admin_keyboard() -> ReplyKeyboardMarkup:
    return ReplyKeyboardMarkup(
        [
            [KeyboardButton(BTN_ADD_MENU), KeyboardButton(BTN_ADD_FILE)],
            [KeyboardButton(BTN_RENAME), KeyboardButton(BTN_DELETE)],
            [KeyboardButton(BTN_CANCEL)],
        ],
        resize_keyboard=True,
    )


async def admin_entry(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    """Triggered when user taps ⚙️ إدارة."""
    if not is_admin(update.effective_user.id):
        return
    menu_id = current_menu_id(ctx)
    menu = db.get_menu(menu_id) if menu_id else None
    loc = f"«{menu['label']}»" if menu else "القائمة الرئيسية"
    await update.message.reply_text(
        f"🔧 *إدارة {loc}*\n\nاختر العملية:",
        reply_markup=admin_keyboard(),
        parse_mode="Markdown",
    )
    return ADM_CHOOSING


async def adm_choose(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    text = update.message.text.strip()
    user_id = update.effective_user.id
    menu_id = current_menu_id(ctx)

    if text == BTN_CANCEL:
        await show_level(update, ctx, menu_id, "❌ تم الإلغاء.")
        return ConversationHandler.END

    if text == BTN_ADD_MENU:
        ctx.user_data["adm_op"] = "add_menu"
        await update.message.reply_text(
            "✏️ أرسل *اسم القسم الجديد*:",
            reply_markup=ReplyKeyboardMarkup(
                [[KeyboardButton(BTN_CANCEL)]], resize_keyboard=True
            ),
            parse_mode="Markdown",
        )
        return ADM_TYPING_LABEL

    if text == BTN_ADD_FILE:
        if menu_id is None:
            await update.message.reply_text(
                "⚠️ يجب الدخول إلى قسم أولاً قبل رفع الملف."
            )
            await show_level(update, ctx, menu_id)
            return ConversationHandler.END
        ctx.user_data["adm_op"] = "add_file"
        await update.message.reply_text(
            "📎 أرسل الملف الآن (PDF، PPT، صوت، فيديو...):",
            reply_markup=ReplyKeyboardMarkup(
                [[KeyboardButton(BTN_CANCEL)]], resize_keyboard=True
            ),
        )
        return ADM_UPLOADING_FILE

    if text == BTN_RENAME:
        if menu_id is None:
            await update.message.reply_text("⚠️ لا يمكن إعادة تسمية القائمة الرئيسية.")
            await show_level(update, ctx, menu_id)
            return ConversationHandler.END
        menu = db.get_menu(menu_id)
        ctx.user_data["adm_op"] = "rename"
        await update.message.reply_text(
            f"✏️ الاسم الحالي: *{menu['label']}*\n\nأرسل الاسم الجديد:",
            reply_markup=ReplyKeyboardMarkup(
                [[KeyboardButton(BTN_CANCEL)]], resize_keyboard=True
            ),
            parse_mode="Markdown",
        )
        return ADM_TYPING_RENAME

    if text == BTN_DELETE:
        if menu_id is None:
            await update.message.reply_text("⚠️ لا يمكن حذف القائمة الرئيسية.")
            await show_level(update, ctx, menu_id)
            return ConversationHandler.END
        menu = db.get_menu(menu_id)
        parent = menu["parent_id"]
        db.delete_menu(menu_id)
        # Pop the deleted menu from the stack
        stack = ctx.user_data.get("stack", [])
        if stack and stack[-1] == menu_id:
            stack.pop()
        new_menu_id = stack[-1] if stack else None
        await show_level(
            update, ctx, new_menu_id, f"🗑 تم حذف القسم «{menu['label']}» وكل محتوياته."
        )
        return ConversationHandler.END

    # Unknown button in admin menu
    await update.message.reply_text("⚠️ اختر أحد الخيارات أعلاه.")
    return ADM_CHOOSING


async def adm_typing_label(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    text = update.message.text.strip()
    menu_id = current_menu_id(ctx)

    if text == BTN_CANCEL:
        await show_level(update, ctx, menu_id, "❌ تم الإلغاء.")
        return ConversationHandler.END

    new_id = db.create_menu(menu_id, text)
    await show_level(update, ctx, menu_id, f"✅ تم إنشاء القسم *{text}* بنجاح!")
    return ConversationHandler.END


async def adm_uploading_file(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    msg = update.message
    menu_id = current_menu_id(ctx)

    # Cancel via text
    if msg.text and msg.text.strip() == BTN_CANCEL:
        await show_level(update, ctx, menu_id, "❌ تم الإلغاء.")
        return ConversationHandler.END

    doc = msg.document
    audio = msg.audio
    voice = msg.voice
    video = msg.video

    if doc:
        mime = doc.mime_type or ""
        file_id = doc.file_id
        file_type = MIME_TO_TYPE.get(mime, "document")
        default = doc.file_name or "ملف"
    elif audio:
        file_id, file_type, default = (
            audio.file_id,
            "audio",
            (audio.title or audio.file_name or "مقطع صوتي"),
        )
    elif voice:
        file_id, file_type, default = voice.file_id, "audio", "رسالة صوتية"
    elif video:
        file_id, file_type, default = (
            video.file_id,
            "video",
            (video.file_name or "مقطع مرئي"),
        )
    else:
        await msg.reply_text("⚠️ نوع غير مدعوم. أرسل PDF أو PPT أو صوت أو فيديو.")
        return ADM_UPLOADING_FILE

    ctx.user_data["adm_file_id"] = file_id
    ctx.user_data["adm_file_type"] = file_type
    ctx.user_data["adm_default"] = default

    await msg.reply_text(
        f"✅ استُلم الملف.\n\n"
        f"📝 أرسل *اسم الزر* الذي سيظهر للمستخدمين\n"
        f"_(مثال: {default})_",
        reply_markup=ReplyKeyboardMarkup(
            [[KeyboardButton(BTN_CANCEL)]], resize_keyboard=True
        ),
        parse_mode="Markdown",
    )
    return ADM_TYPING_CAPTION


async def adm_typing_caption(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    text = update.message.text.strip()
    menu_id = current_menu_id(ctx)

    if text == BTN_CANCEL:
        await show_level(update, ctx, menu_id, "❌ تم الإلغاء.")
        return ConversationHandler.END

    db.add_file(
        menu_id,
        ctx.user_data["adm_file_id"],
        text,
        ctx.user_data["adm_file_type"],
    )
    ctx.user_data.pop("adm_file_id", None)
    ctx.user_data.pop("adm_file_type", None)
    ctx.user_data.pop("adm_default", None)

    await show_level(update, ctx, menu_id, f"✅ تمت إضافة الملف *{text}*!")
    return ConversationHandler.END


async def adm_typing_rename(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    text = update.message.text.strip()
    menu_id = current_menu_id(ctx)

    if text == BTN_CANCEL:
        await show_level(update, ctx, menu_id, "❌ تم الإلغاء.")
        return ConversationHandler.END

    db.rename_menu(menu_id, text)
    await show_level(update, ctx, menu_id, f"✅ تمت إعادة التسمية إلى *{text}*")
    return ConversationHandler.END


async def adm_cancel(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    menu_id = current_menu_id(ctx)
    await show_level(update, ctx, menu_id, "❌ تم الإلغاء.")
    return ConversationHandler.END


# ── Main ──────────────────────────────────────────────────────────────────────


def main():
    db.init_db()

    app = Application.builder().token(BOT_TOKEN).build()

    # Admin conversation handler
    admin_conv = ConversationHandler(
        entry_points=[
            MessageHandler(
                filters.Text([BTN_ADMIN]) & filters.User(list(ADMIN_IDS)),
                admin_entry,
            )
        ],
        states={
            ADM_CHOOSING: [MessageHandler(filters.TEXT & ~filters.COMMAND, adm_choose)],
            ADM_TYPING_LABEL: [
                MessageHandler(filters.TEXT & ~filters.COMMAND, adm_typing_label)
            ],
            ADM_UPLOADING_FILE: [
                MessageHandler(
                    (
                        filters.Document.ALL
                        | filters.AUDIO
                        | filters.VOICE
                        | filters.VIDEO
                    ),
                    adm_uploading_file,
                ),
                MessageHandler(filters.TEXT & ~filters.COMMAND, adm_uploading_file),
            ],
            ADM_TYPING_CAPTION: [
                MessageHandler(filters.TEXT & ~filters.COMMAND, adm_typing_caption)
            ],
            ADM_TYPING_RENAME: [
                MessageHandler(filters.TEXT & ~filters.COMMAND, adm_typing_rename)
            ],
        },
        fallbacks=[
            CommandHandler("cancel", adm_cancel),
            MessageHandler(filters.Text([BTN_CANCEL]), adm_cancel),
        ],
    )

    app.add_handler(CommandHandler("start", start))
    app.add_handler(CommandHandler("help", help_cmd))
    app.add_handler(admin_conv)

    # Regular navigation (non-admin or admin outside conversation)
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, handle_message))

    logger.info("🚀 Archive Bot (ReplyKeyboard) running…")
    app.run_polling(drop_pending_updates=True)


if __name__ == "__main__":
    main()
