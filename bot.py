"""
bot.py — Telegram Archive Bot with persistent ReplyKeyboard navigation.

Navigation model
----------------
Every message the user sends is a button label.
The bot resolves the label to a menu node or file, then updates
the persistent bottom keyboard to reflect that level.

State (per user, stored in context.user_data)
---------------------------------------------
  stack : list[int]  -- stack of menu_id values (current path from root)
                        empty  -> we are at root
                        [3]    -> we are inside menu 3
                        [3, 7] -> inside menu 7 which is a child of 3

Admin system
------------
Two-tier:
  SUPERADMIN_IDS  -- hardcoded in config, can never be removed
  DB admins       -- added via /addadmin @username, stored in admins table
                     confirmed once the user messages the bot (pending until then)

Admin commands:
  /addadmin @username   -- add a pending admin
  /removeadmin @username -- remove an admin
  /admins               -- list all admins
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

# Config

BOT_TOKEN = os.environ.get(
    "BOT_TOKEN", "8855275808:AAFaHABjCMLc5T2sVh4_wm2bA86oLlJvUhU"
)

# Superadmins: hardcoded, can add/remove other admins, cannot be removed themselves
SUPERADMIN_IDS: set[int] = {
    776738328,
}

logging.basicConfig(
    format="%(asctime)s | %(levelname)s | %(message)s",
    level=logging.INFO,
)
logger = logging.getLogger(__name__)

# Special button labels
BTN_BACK = "⬅️ رجوع"
BTN_HOME = "🏠 الرئيسية"
BTN_ADMIN = "⚙️ إدارة"
BTN_ADD_MENU = "➕ قسم جديد"
BTN_ADD_FILE = "📎 رفع ملف"
BTN_RENAME = "✏️ إعادة تسمية"
BTN_DELETE = "🗑 حذف القسم"
BTN_CANCEL = "❌ إلغاء"
BTN_ADD_ADMIN = "👤 إضافة مشرف"
BTN_LIST_ADMINS = "👥 قائمة المشرفين"
BTN_REMOVE_ADMIN = "🗑 إزالة مشرف"
BTN_SKIP = "⏭ تخطي"

# Conversation states
(
    ADM_CHOOSING,
    ADM_TYPING_LABEL,
    ADM_UPLOADING_FILE,
    ADM_TYPING_CAPTION,
    ADM_TYPING_MESSAGE,
    ADM_TYPING_RENAME,
    ADM_TYPING_USERNAME,
    ADM_TYPING_REMOVE,
) = range(8)

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


# Admin check (superadmin OR confirmed DB admin)


def is_admin(user_id: int) -> bool:
    return user_id in SUPERADMIN_IDS or db.is_db_admin(user_id)


def is_superadmin(user_id: int) -> bool:
    return user_id in SUPERADMIN_IDS


# Navigation helpers


def current_menu_id(ctx: ContextTypes.DEFAULT_TYPE) -> int | None:
    stack = ctx.user_data.get("stack", [])
    return stack[-1] if stack else None


def build_keyboard(menu_id: int | None, user_id: int) -> ReplyKeyboardMarkup:
    rows: list[list[KeyboardButton]] = []

    children = db.get_root_menus() if menu_id is None else db.get_children(menu_id)
    child_btns = [KeyboardButton(f" {ch['label']}") for ch in children]

    file_btns = []
    if menu_id is not None:
        for f in db.get_files(menu_id):
            emoji = TYPE_EMOJI.get(f["file_type"], "📄")
            label = f["caption"] or f"ملف {f['id']}"
            file_btns.append(KeyboardButton(f"{emoji} {label}"))

    all_items = child_btns + file_btns
    for i in range(0, len(all_items), 2):
        rows.append(all_items[i : i + 2])

    nav = []
    if menu_id is not None:
        nav.append(KeyboardButton(BTN_BACK))
    nav.append(KeyboardButton(BTN_HOME))
    rows.append(nav)

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
    text = message or breadcrumb(menu_id)
    kb = build_keyboard(menu_id, update.effective_user.id)
    await update.effective_message.reply_text(
        text, reply_markup=kb, parse_mode="Markdown"
    )


# Label resolution


def resolve_label(label: str, menu_id: int | None):
    clean = label
    if len(label) > 2 and label[1] == " ":
        clean = label[2:]
    if len(label) > 3 and label[2] == " ":
        clean = label[3:]

    children = db.get_root_menus() if menu_id is None else db.get_children(menu_id)
    for ch in children:
        if ch["label"] == clean or f" {ch['label']}" == label:
            return ("menu", ch)

    if menu_id is not None:
        for f in db.get_files(menu_id):
            emoji = TYPE_EMOJI.get(f["file_type"], "📄")
            fallback = "ملف " + str(f["id"])
            btn = f"{emoji} {f['caption'] or fallback}"
            if btn == label:
                return ("file", f)

    return None


# /start


async def start(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    ctx.user_data["stack"] = []
    user = update.effective_user
    # Confirm pending admin if applicable
    if user.username:
        promoted = db.confirm_admin(user.id, user.username)
        if promoted:
            logger.info(
                "Promoted @%s (id=%d) to admin via /start", user.username, user.id
            )
    await show_level(
        update, ctx, None, f"أهلاً *{user.first_name}* 👋\nاختر قسماً للبدء:"
    )


# /help


async def help_cmd(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    lines = ["📚 *مكتبة الأرشيف*\n", "/start — القائمة الرئيسية", "/help  — المساعدة"]
    if is_admin(update.effective_user.id):
        lines += [
            "\n🔧 *للمشرفين*",
            "اضغط ⚙️ إدارة من أي قسم لإضافة أو تعديل المحتوى.\n",
            "/addadmin @username — إضافة مشرف جديد",
            "/removeadmin @username — إزالة مشرف",
            "/admins — قائمة المشرفين",
        ]
    await update.message.reply_text("\n".join(lines), parse_mode="Markdown")


# /addadmin


async def addadmin_cmd(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    user_id = update.effective_user.id
    if not is_admin(user_id):
        await update.message.reply_text("⛔ غير مصرح لك.")
        return

    args = ctx.args
    if not args or not args[0].startswith("@"):
        await update.message.reply_text(
            "📝 الاستخدام: `/addadmin @username`", parse_mode="Markdown"
        )
        return

    username = args[0].lstrip("@").strip().lower()
    result = db.add_pending_admin(username, added_by=user_id)

    if result == "already_admin":
        await update.message.reply_text(f"ℹ️ @{username} مشرف بالفعل.")
    elif result == "already_pending":
        await update.message.reply_text(
            f"⏳ @{username} مضاف بالفعل وبانتظار التفعيل.\n"
            f"سيصبح مشرفاً فور إرسال أي رسالة للبوت."
        )
    else:
        await update.message.reply_text(
            f"✅ تم إضافة @{username} كمشرف معلّق.\n\n"
            f"⏳ سيصبح مشرفاً نشطاً فور إرساله أي رسالة للبوت.",
        )
        logger.info("Admin @%s added by user_id=%d (pending)", username, user_id)


# /removeadmin


async def removeadmin_cmd(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    user_id = update.effective_user.id
    if not is_admin(user_id):
        await update.message.reply_text("⛔ غير مصرح لك.")
        return

    args = ctx.args
    if not args or not args[0].startswith("@"):
        await update.message.reply_text(
            "📝 الاستخدام: `/removeadmin @username`", parse_mode="Markdown"
        )
        return

    username = args[0].lstrip("@").strip().lower()

    # Protect superadmins
    row = next(
        (r for r in db.get_all_admins() if r["username"] == username),
        None,
    )
    if row and row["user_id"] and row["user_id"] in SUPERADMIN_IDS:
        await update.message.reply_text("⛔ لا يمكن إزالة المشرف الرئيسي.")
        return

    removed = db.remove_admin(username)
    if removed:
        await update.message.reply_text(f"🗑 تم إزالة @{username} من المشرفين.")
        logger.info("Admin @%s removed by user_id=%d", username, user_id)
    else:
        await update.message.reply_text(
            f"⚠️ لم يتم العثور على @{username} في قائمة المشرفين."
        )


# /admins


async def admins_cmd(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    if not is_admin(update.effective_user.id):
        await update.message.reply_text("⛔ غير مصرح لك.")
        return

    rows = db.get_all_admins()

    lines = ["👥 *قائمة المشرفين*\n"]

    # Superadmins (hardcoded)
    lines.append("🔑 *مشرفون رئيسيون (ثابتون):*")
    for uid in SUPERADMIN_IDS:
        lines.append(f"  • ID: `{uid}`")

    # DB admins
    if rows:
        lines.append("\n📋 *مشرفون مضافون:*")
        for r in rows:
            status = "✅ نشط" if r["confirmed"] else "⏳ معلّق"
            uid_str = f" `(ID: {r['user_id']})`" if r["user_id"] else ""
            lines.append(f"  • @{r['username']}{uid_str} — {status}")
    else:
        lines.append("\n_لا يوجد مشرفون مضافون بعد._")

    await update.message.reply_text("\n".join(lines), parse_mode="Markdown")


# Pending-admin confirmation hook (runs on every incoming message)


async def maybe_confirm_admin(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    """
    Runs before all other handlers via group=-1.
    If the sender has a username that matches a pending admin row,
    confirm them and notify.
    """
    user = update.effective_user
    if not user or not user.username:
        return

    promoted = db.confirm_admin(user.id, user.username)
    if promoted:
        logger.info("Confirmed admin @%s (id=%d)", user.username, user.id)
        await update.effective_message.reply_text(
            f"🎉 تم تفعيل صلاحيات المشرف لـ @{user.username}!\n"
            "يمكنك الآن استخدام زر ⚙️ إدارة.",
        )


# Main message handler (navigation)


async def handle_message(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    text = update.message.text.strip()
    user_id = update.effective_user.id
    stack = ctx.user_data.setdefault("stack", [])
    menu_id = stack[-1] if stack else None

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
        # `message` is the optional text the admin attached when uploading;
        # it gets sent together with the file as its Telegram caption.
        extra = row["message"] or ""
        if ftype == "audio":
            await update.message.reply_audio(audio=fid, caption=extra)
        elif ftype == "video":
            await update.message.reply_video(video=fid, caption=extra)
        else:
            await update.message.reply_document(document=fid, caption=extra)


# Admin conversation


def admin_keyboard(is_super: bool = False) -> ReplyKeyboardMarkup:
    rows = [
        [KeyboardButton(BTN_ADD_MENU), KeyboardButton(BTN_ADD_FILE)],
        [KeyboardButton(BTN_RENAME), KeyboardButton(BTN_DELETE)],
    ]
    if is_super:
        rows.append([KeyboardButton(BTN_ADD_ADMIN), KeyboardButton(BTN_LIST_ADMINS)])
        rows.append([KeyboardButton(BTN_REMOVE_ADMIN)])
    rows.append([KeyboardButton(BTN_CANCEL)])
    return ReplyKeyboardMarkup(rows, resize_keyboard=True)


async def admin_entry(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    if not is_admin(update.effective_user.id):
        return
    menu_id = current_menu_id(ctx)
    menu = db.get_menu(menu_id) if menu_id else None
    loc = f"«{menu['label']}»" if menu else "القائمة الرئيسية"
    await update.message.reply_text(
        f"🔧 *إدارة {loc}*\n\nاختر العملية:",
        reply_markup=admin_keyboard(is_super=is_superadmin(update.effective_user.id)),
        parse_mode="Markdown",
    )
    return ADM_CHOOSING


async def adm_choose(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    text = update.message.text.strip()
    menu_id = current_menu_id(ctx)

    # If the user pressed a navigation or menu button instead of an admin option,
    # exit the conversation and re-dispatch to normal navigation.
    ADMIN_BTNS = {
        BTN_ADD_MENU,
        BTN_ADD_FILE,
        BTN_RENAME,
        BTN_DELETE,
        BTN_CANCEL,
        BTN_ADD_ADMIN,
        BTN_LIST_ADMINS,
        BTN_REMOVE_ADMIN,
    }
    if text not in ADMIN_BTNS:
        await show_level(update, ctx, menu_id, "❌ خروج من الإدارة.")
        await handle_message(update, ctx)
        return ConversationHandler.END

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
        db.delete_menu(menu_id)
        stack = ctx.user_data.get("stack", [])
        if stack and stack[-1] == menu_id:
            stack.pop()
        new_menu_id = stack[-1] if stack else None
        await show_level(
            update, ctx, new_menu_id, f"🗑 تم حذف القسم «{menu['label']}» وكل محتوياته."
        )
        return ConversationHandler.END

    if text == BTN_ADD_ADMIN:
        if not is_superadmin(update.effective_user.id):
            await update.message.reply_text("⛔ فقط المشرف الرئيسي يمكنه إضافة مشرفين.")
            return ADM_CHOOSING
        await update.message.reply_text(
            "👤 أرسل *يوزرنيم* المشرف الجديد بصيغة @username:",
            reply_markup=ReplyKeyboardMarkup(
                [[KeyboardButton(BTN_CANCEL)]], resize_keyboard=True
            ),
            parse_mode="Markdown",
        )
        return ADM_TYPING_USERNAME

    if text == BTN_REMOVE_ADMIN:
        if not is_superadmin(update.effective_user.id):
            await update.message.reply_text("⛔ فقط المشرف الرئيسي يمكنه إزالة مشرفين.")
            return ADM_CHOOSING
        rows = db.get_all_admins()
        if not rows:
            await update.message.reply_text("📭 لا يوجد مشرفون مضافون.")
            return ADM_CHOOSING
        # Build keyboard of removable admins
        btns = [[KeyboardButton(f"🗑 @{r['username']}")] for r in rows]
        btns.append([KeyboardButton(BTN_CANCEL)])
        await update.message.reply_text(
            "👥 اختر المشرف الذي تريد إزالته:",
            reply_markup=ReplyKeyboardMarkup(btns, resize_keyboard=True),
        )
        return ADM_TYPING_REMOVE

    if text == BTN_LIST_ADMINS:
        rows = db.get_all_admins()
        lines = ["👥 *قائمة المشرفين*\n"]
        lines.append("🔑 *مشرفون رئيسيون (ثابتون):*")
        for uid in SUPERADMIN_IDS:
            lines.append(f"  • ID: `{uid}`")
        if rows:
            lines.append("\n📋 *مشرفون مضافون:*")
            for r in rows:
                status = "✅ نشط" if r["confirmed"] else "⏳ معلّق"
                uid_str = f" `(ID: {r['user_id']})`" if r["user_id"] else ""
                lines.append(f"  • @{r['username']}{uid_str} — {status}")
        else:
            lines.append("\n_لا يوجد مشرفون مضافون بعد._")
        await update.message.reply_text("\n".join(lines), parse_mode="Markdown")
        return ADM_CHOOSING

    await update.message.reply_text("⚠️ اختر أحد الخيارات أعلاه.")
    return ADM_CHOOSING


async def adm_typing_label(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    text = update.message.text.strip()
    menu_id = current_menu_id(ctx)
    if text == BTN_CANCEL:
        await show_level(update, ctx, menu_id, "❌ تم الإلغاء.")
        return ConversationHandler.END
    db.create_menu(menu_id, text)
    await show_level(update, ctx, menu_id, f"✅ تم إنشاء القسم *{text}* بنجاح!")
    return ConversationHandler.END


async def adm_uploading_file(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    msg = update.message
    menu_id = current_menu_id(ctx)

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
        f"📝 أرسل *اسم الزر* الذي سيظهر للمستخدمين\n_(مثال: {default})_",
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
    # Save the button label, then ask for an optional message that will be
    # sent together with the file whenever a user taps the button.
    ctx.user_data["adm_caption"] = text
    await update.message.reply_text(
        "💬 أرسل *رسالة اختيارية* سترافق الملف عند إرساله للمستخدم\n"
        "_(أو اضغط ⏭ تخطي لعدم إضافة رسالة)_",
        reply_markup=ReplyKeyboardMarkup(
            [[KeyboardButton(BTN_SKIP)], [KeyboardButton(BTN_CANCEL)]],
            resize_keyboard=True,
        ),
        parse_mode="Markdown",
    )
    return ADM_TYPING_MESSAGE


async def adm_typing_message(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    text = update.message.text.strip()
    menu_id = current_menu_id(ctx)
    if text == BTN_CANCEL:
        ctx.user_data.pop("adm_file_id", None)
        ctx.user_data.pop("adm_file_type", None)
        ctx.user_data.pop("adm_default", None)
        ctx.user_data.pop("adm_caption", None)
        await show_level(update, ctx, menu_id, "❌ تم الإلغاء.")
        return ConversationHandler.END

    extra_message = "" if text == BTN_SKIP else text
    caption = ctx.user_data["adm_caption"]

    db.add_file(
        menu_id,
        ctx.user_data["adm_file_id"],
        caption,
        ctx.user_data["adm_file_type"],
        extra_message,
    )
    ctx.user_data.pop("adm_file_id", None)
    ctx.user_data.pop("adm_file_type", None)
    ctx.user_data.pop("adm_default", None)
    ctx.user_data.pop("adm_caption", None)

    confirm = f"✅ تمت إضافة الملف *{caption}*!"
    if extra_message:
        confirm += "\n💬 وتم حفظ الرسالة المرافقة له."
    await show_level(update, ctx, menu_id, confirm)
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


async def adm_typing_username(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    text = update.message.text.strip()
    menu_id = current_menu_id(ctx)
    if text == BTN_CANCEL:
        await show_level(update, ctx, menu_id, "❌ تم الإلغاء.")
        return ConversationHandler.END
    if not text.startswith("@"):
        await update.message.reply_text(
            "⚠️ يجب أن يبدأ اليوزرنيم بـ @\nمثال: @hisham\n\nأرسل مجدداً أو اضغط ❌ إلغاء"
        )
        return ADM_TYPING_USERNAME
    username = text.lstrip("@").strip().lower()
    result = db.add_pending_admin(username, added_by=update.effective_user.id)
    if result == "already_admin":
        msg = f"ℹ️ @{username} مشرف بالفعل."
    elif result == "already_pending":
        msg = f"⏳ @{username} مضاف بالفعل وبانتظار التفعيل."
    else:
        msg = (
            f"✅ تم إضافة @{username} كمشرف معلّق.\n\n"
            f"⏳ سيصبح نشطاً فور إرساله أي رسالة للبوت."
        )
        logger.info(
            "Admin @%s added via button by user_id=%d",
            username,
            update.effective_user.id,
        )
    await show_level(update, ctx, menu_id, msg)
    return ConversationHandler.END


async def adm_typing_remove(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    text = update.message.text.strip()
    menu_id = current_menu_id(ctx)
    if text == BTN_CANCEL:
        await show_level(update, ctx, menu_id, "❌ تم الإلغاء.")
        return ConversationHandler.END
    # Buttons are formatted as "🗑 @username"
    username = text.replace("🗑", "").replace("@", "").strip().lower()
    if not username:
        await update.message.reply_text("⚠️ اختر مشرفاً من القائمة أو اضغط ❌ إلغاء.")
        return ADM_TYPING_REMOVE
    # Protect superadmins
    for r in db.get_all_admins():
        if (
            r["username"] == username
            and r["user_id"]
            and r["user_id"] in SUPERADMIN_IDS
        ):
            await update.message.reply_text("⛔ لا يمكن إزالة المشرف الرئيسي.")
            return ADM_TYPING_REMOVE
    removed = db.remove_admin(username)
    if removed:
        msg = f"🗑 تم إزالة @{username} من المشرفين."
        logger.info(
            "Admin @%s removed via button by user_id=%d",
            username,
            update.effective_user.id,
        )
    else:
        msg = f"⚠️ لم يتم العثور على @{username}."
    await show_level(update, ctx, menu_id, msg)
    return ConversationHandler.END


async def adm_cancel(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    menu_id = current_menu_id(ctx)
    await show_level(update, ctx, menu_id, "❌ تم الإلغاء.")
    return ConversationHandler.END


# Main


def main():
    db.init_db()

    app = Application.builder().token(BOT_TOKEN).build()

    # Pending-admin confirmation hook — runs on EVERY message before all other handlers
    app.add_handler(
        MessageHandler(filters.ALL, maybe_confirm_admin),
        group=-1,
    )

    # Admin management commands
    app.add_handler(CommandHandler("addadmin", addadmin_cmd))
    app.add_handler(CommandHandler("removeadmin", removeadmin_cmd))
    app.add_handler(CommandHandler("admins", admins_cmd))

    # Admin conversation (menu/file management)
    admin_conv = ConversationHandler(
        entry_points=[
            MessageHandler(
                filters.Text([BTN_ADMIN]),
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
                    filters.Document.ALL
                    | filters.AUDIO
                    | filters.VOICE
                    | filters.VIDEO,
                    adm_uploading_file,
                ),
                MessageHandler(filters.TEXT & ~filters.COMMAND, adm_uploading_file),
            ],
            ADM_TYPING_CAPTION: [
                MessageHandler(filters.TEXT & ~filters.COMMAND, adm_typing_caption)
            ],
            ADM_TYPING_MESSAGE: [
                MessageHandler(filters.TEXT & ~filters.COMMAND, adm_typing_message)
            ],
            ADM_TYPING_RENAME: [
                MessageHandler(filters.TEXT & ~filters.COMMAND, adm_typing_rename)
            ],
            ADM_TYPING_USERNAME: [
                MessageHandler(filters.TEXT & ~filters.COMMAND, adm_typing_username)
            ],
            ADM_TYPING_REMOVE: [
                MessageHandler(filters.TEXT & ~filters.COMMAND, adm_typing_remove)
            ],
        },
        fallbacks=[
            CommandHandler("cancel", adm_cancel),
            MessageHandler(filters.Text([BTN_CANCEL, BTN_HOME, BTN_BACK]), adm_cancel),
        ],
    )

    app.add_handler(CommandHandler("start", start))
    app.add_handler(CommandHandler("help", help_cmd))
    app.add_handler(admin_conv)
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, handle_message))

    logger.info("Archive Bot running...")
    app.run_polling(drop_pending_updates=True)


if __name__ == "__main__":
    main()
