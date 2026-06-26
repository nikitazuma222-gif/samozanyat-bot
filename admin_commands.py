from telegram import Update
from telegram.ext import ContextTypes

ADMIN_USERNAME = "milorky"
user_data = {}

async def stats(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if update.effective_user.username != ADMIN_USERNAME:
        await update.message.reply_text("⛔️ Доступ запрещён.")
        return
    total = len(user_data)
    paid = sum(1 for u in user_data.values() if u.get("paid", False))
    used = sum(1 for u in user_data.values() if u.get("count", 0) > 0)
    await update.message.reply_text(
        f"📊 *Статистика бота*\n\n"
        f"👥 Всего пользователей: {total}\n"
        f"📄 Создали документы: {used}\n"
        f"💳 Активных подписок: {paid}",
        parse_mode='Markdown'
    )

async def users(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if update.effective_user.username != ADMIN_USERNAME:
        await update.message.reply_text("⛔️ Доступ запрещён.")
        return
    if not user_data:
        await update.message.reply_text("Пока нет пользователей.")
        return
    msg = "📋 *Последние пользователи:*\n\n"
    for uid, data in list(user_data.items())[-10:]:
        msg += f"• {uid} — документов: {data.get('count',0)}, подписка: {'✅' if data.get('paid',False) else '❌'}\n"
    await update.message.reply_text(msg, parse_mode='Markdown')

async def user_info(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if update.effective_user.username != ADMIN_USERNAME:
        await update.message.reply_text("⛔️ Доступ запрещён.")
        return
    if not context.args:
        await update.message.reply_text("Укажи ID: /user 123456789")
        return
    try:
        uid = int(context.args[0])
        data = user_data.get(uid)
        if not data:
            await update.message.reply_text("Пользователь не найден.")
            return
        await update.message.reply_text(
            f"🔍 *Пользователь* {uid}\n\n"
            f"📄 Документов: {data.get('count',0)}\n"
            f"💳 Подписка: {'✅ Активна' if data.get('paid',False) else '❌ Нет'}\n"
            f"🕐 Последняя активность: {data.get('last_activity', 'неизвестно')}",
            parse_mode='Markdown'
        )
    except:
        await update.message.reply_text("Ошибка: ID должен быть числом")
