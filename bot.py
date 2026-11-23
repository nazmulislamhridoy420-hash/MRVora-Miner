from __future__ import annotations

import logging
import os
import re
import time
from datetime import datetime, timezone
from html import escape
from traceback import format_exception

from telegram import (
    InlineKeyboardButton,
    InlineKeyboardMarkup,
    ReplyKeyboardMarkup,
    Update,
)
from telegram.ext import (
    Application,
    CallbackQueryHandler,
    CommandHandler,
    ContextTypes,
    MessageHandler,
    filters,
)

from . import database


logging.basicConfig(
    format="%(asctime)s %(levelname)s %(name)s: %(message)s",
    level=os.getenv("LOG_LEVEL", "INFO").upper(),
)
logging.getLogger("httpx").setLevel(logging.WARNING)
logging.getLogger("telegram.ext._updater").setLevel(logging.DEBUG)


class _TokenRedactionFilter(logging.Filter):
    def filter(self, record: logging.LogRecord) -> bool:
        token = os.getenv("TELEGRAM_BOT_TOKEN")
        if not token:
            return True

        record.msg = record.getMessage().replace(token, "[REDACTED]")
        record.args = ()
        if record.exc_info:
            record.exc_text = "".join(format_exception(*record.exc_info)).replace(
                token, "[REDACTED]"
            )
            record.exc_info = None
        elif record.exc_text:
            record.exc_text = record.exc_text.replace(token, "[REDACTED]")
        return True


for _handler in logging.getLogger().handlers:
    _handler.addFilter(_TokenRedactionFilter())

logger = logging.getLogger("mrvora_mining")

KEYBOARD = ReplyKeyboardMarkup(
    [
        ["⛏ Mine", "💰 Balance"],
        ["🎁 Daily reward", "👥 Invite"],
        ["🏆 Leaderboard", "👑 VIP Membership"],
    ],
    resize_keyboard=True,
    is_persistent=True,
)
REFERRAL_ARGUMENT = re.compile(r"^ref_(\d+)$")
VIP_REVIEW_CALLBACK = re.compile(r"^vip:review:([1-9]\d*):([1-5])$")
VIP_BUY_CALLBACK = re.compile(r"^vip:buy:([1-9]\d*):([1-5])$")
VIP_CANCEL_CALLBACK = re.compile(r"^vip:cancel:([1-9]\d*)$")


def _vip_page(
    telegram_id: int,
    level: int,
    balance: int,
    history: list[object],
) -> tuple[str, InlineKeyboardMarkup | None]:
    current_tier = database.VIP_TIERS[level]
    lines = [
        "👑 <b>VIP Membership</b>",
        f"Current level: <b>{current_tier.name}</b>",
        f"Balance: <b>{balance:,} coins</b>",
        (
            "Your benefits: "
            f"<b>{current_tier.mine_reward} coins</b> per mine "
            f"(+{current_tier.mining_bonus_percent}%), "
            f"<b>{current_tier.daily_reward} coins</b> daily."
        ),
        "",
        "<b>VIP tier benefits and upgrade costs</b>",
    ]
    for tier in database.VIP_TIERS[1:]:
        lines.append(
            f"{tier.name}: mine {tier.mine_reward} coins "
            f"(+{tier.mining_bonus_percent}%), daily {tier.daily_reward}, "
            f"upgrade cost {tier.upgrade_cost:,} coins."
        )

    next_level = level + 1
    keyboard: InlineKeyboardMarkup | None = None
    if next_level < len(database.VIP_TIERS):
        next_tier = database.VIP_TIERS[next_level]
        cost = next_tier.upgrade_cost
        if cost is None:
            raise RuntimeError(f"VIP tier {next_level} has no upgrade cost")
        shortfall = max(0, cost - balance)
        requirement = f"Next upgrade: <b>{next_tier.name}</b> for <b>{cost:,} coins</b>."
        if shortfall:
            requirement += f" You need <b>{shortfall:,} more coins</b>."
        else:
            requirement += " You have enough coins to upgrade."
        lines.extend(["", requirement])
        keyboard = InlineKeyboardMarkup(
            [
                [
                    InlineKeyboardButton(
                        f"Review {next_tier.name} upgrade",
                        callback_data=f"vip:review:{telegram_id}:{next_level}",
                    )
                ]
            ]
        )
    else:
        lines.extend(["", "You have reached the highest VIP level."])

    if history:
        lines.extend(["", "<b>Purchase history</b>"])
        for item in history:
            purchased_on = datetime.fromtimestamp(
                int(item["purchased_at"]), timezone.utc
            ).strftime("%Y-%m-%d")
            lines.append(
                f"{item['new_level']}: {int(item['cost']):,} coins "
                f"({purchased_on} UTC)"
            )

    return "\n".join(lines), keyboard


def _display_name(update: Update) -> str:
    user = update.effective_user
    if not user:
        return "Miner"
    return escape(user.first_name or "Miner")


async def start(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    user = update.effective_user
    message = update.effective_message
    if not user or not message:
        return
    logger.info("Received Telegram /start command")

    referrer_id: int | None = None
    if context.args:
        match = REFERRAL_ARGUMENT.fullmatch(context.args[0])
        if match:
            referrer_id = int(match.group(1))

    is_new, balance, referral_applied = database.register_user(
        telegram_id=user.id,
        username=user.username,
        first_name=user.first_name or "Miner",
        referrer_id=referrer_id,
    )

    greeting = (
        f"Welcome to <b>MRVora Mining</b>, {_display_name(update)}!\n\n"
        "Tap <b>⛏ Mine</b> to collect coins, claim a daily reward, invite friends, "
        "and climb the leaderboard."
    )
    if referral_applied:
        greeting += "\nYour friend received a referral bonus for bringing you in."
    elif not is_new:
        greeting += f"\nYour current balance is <b>{balance:,} coins</b>."

    await message.reply_text(greeting, reply_markup=KEYBOARD, parse_mode="HTML")
    logger.info("Sent Telegram /start response")


async def mine(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    user = update.effective_user
    message = update.effective_message
    if not user or not message:
        return

    success, balance, wait = database.mine(
        user.id,
        time.time(),
        username=user.username,
        first_name=user.first_name or "Miner",
    )
    if success:
        response = (
            f"⛏ You mined <b>{database.MINE_REWARD} coins</b>!\n"
            f"Balance: <b>{balance:,} coins</b>\n"
            f"Mine again in {database.MINE_COOLDOWN_SECONDS} seconds."
        )
    else:
        response = (
            f"Your balance is <b>{balance:,} coins</b>.\n"
            f"Mining is cooling down. Try again in <b>{wait} seconds</b>."
        )
    await message.reply_text(response, reply_markup=KEYBOARD, parse_mode="HTML")


async def show_balance(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    user = update.effective_user
    message = update.effective_message
    if not user or not message:
        return
    balance = database.ensure_user(
        user.id,
        username=user.username,
        first_name=user.first_name or "Miner",
    )
    await message.reply_text(
        f"💰 Your balance: <b>{balance:,} coins</b>",
        reply_markup=KEYBOARD,
        parse_mode="HTML",
    )


async def daily_reward(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    user = update.effective_user
    message = update.effective_message
    if not user or not message:
        return

    today = datetime.now(timezone.utc).date().isoformat()
    claim_status, balance = database.claim_daily(
        user.id,
        today,
        username=user.username,
        first_name=user.first_name or "Miner",
    )
    if claim_status == "claimed":
        response = (
            f"🎁 Daily reward claimed: <b>{database.DAILY_REWARD} coins</b>!\n"
            f"Balance: <b>{balance:,} coins</b>\n"
            "Come back tomorrow for another reward."
        )
    elif claim_status == "repaired":
        response = (
            f"🎁 Today's reward was restored: "
            f"<b>{database.DAILY_REWARD} coins</b>!\n"
            f"Balance: <b>{balance:,} coins</b>\n"
            "Your saved claim was kept. Come back tomorrow for another reward."
        )
    else:
        response = (
            f"You already claimed today's reward.\n"
            f"Your balance is <b>{balance:,} coins</b>. Come back tomorrow."
        )
    await message.reply_text(response, reply_markup=KEYBOARD, parse_mode="HTML")


async def vip_membership(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    user = update.effective_user
    message = update.effective_message
    if not user or not message:
        return
    level, balance, history = database.get_vip_membership(
        user.id,
        username=user.username,
        first_name=user.first_name or "Miner",
    )
    response, keyboard = _vip_page(user.id, level, balance, history)
    await message.reply_text(
        response,
        reply_markup=keyboard,
        parse_mode="HTML",
    )


async def review_vip_upgrade(
    update: Update, context: ContextTypes.DEFAULT_TYPE
) -> None:
    query = update.callback_query
    user = update.effective_user
    if not query or not user:
        return
    match = VIP_REVIEW_CALLBACK.fullmatch(query.data or "")
    if not match:
        await query.answer("Invalid VIP upgrade request.", show_alert=True)
        return
    if int(match.group(1)) != user.id:
        await query.answer(
            "This VIP membership menu belongs to another user.",
            show_alert=True,
        )
        return
    await query.answer()
    target_level = int(match.group(2))
    level, balance, history = database.get_vip_membership(
        user.id,
        username=user.username,
        first_name=user.first_name or "Miner",
    )
    page, page_keyboard = _vip_page(user.id, level, balance, history)
    if target_level <= level:
        await query.edit_message_text(
            f"You already own {database.VIP_TIERS[target_level].name}. "
            "No coins were deducted.\n\n" + page,
            reply_markup=page_keyboard,
            parse_mode="HTML",
        )
        return
    if target_level != level + 1:
        await query.edit_message_text(
            "VIP upgrades must be purchased one level at a time.\n\n" + page,
            reply_markup=page_keyboard,
            parse_mode="HTML",
        )
        return

    tier = database.VIP_TIERS[target_level]
    cost = tier.upgrade_cost
    if cost is None:
        raise RuntimeError(f"VIP tier {target_level} has no upgrade cost")
    if balance < cost:
        await query.edit_message_text(
            f"You need {cost - balance:,} more coins for {tier.name}.\n\n" + page,
            reply_markup=page_keyboard,
            parse_mode="HTML",
        )
        return

    confirmation = (
        f"Upgrade to <b>{tier.name}</b> for <b>{cost:,} coins</b>?\n"
        f"Your balance after purchase will be <b>{balance - cost:,} coins</b>.\n"
        f"Benefits: mine {tier.mine_reward} coins "
        f"(+{tier.mining_bonus_percent}%), daily reward {tier.daily_reward} coins."
    )
    confirmation_keyboard = InlineKeyboardMarkup(
        [
            [
                InlineKeyboardButton(
                    f"Confirm {tier.name}",
                    callback_data=f"vip:buy:{user.id}:{target_level}",
                ),
                InlineKeyboardButton(
                    "Cancel",
                    callback_data=f"vip:cancel:{user.id}",
                ),
            ]
        ]
    )
    await query.edit_message_text(
        confirmation,
        reply_markup=confirmation_keyboard,
        parse_mode="HTML",
    )


async def confirm_vip_upgrade(
    update: Update, context: ContextTypes.DEFAULT_TYPE
) -> None:
    query = update.callback_query
    user = update.effective_user
    if not query or not user:
        return
    match = VIP_BUY_CALLBACK.fullmatch(query.data or "")
    if not match:
        await query.answer("Invalid VIP purchase request.", show_alert=True)
        return
    if int(match.group(1)) != user.id:
        await query.answer(
            "This VIP membership menu belongs to another user.",
            show_alert=True,
        )
        return
    await query.answer()
    target_level = int(match.group(2))
    status, level, balance = database.purchase_vip(
        user.id,
        target_level,
        username=user.username,
        first_name=user.first_name or "Miner",
    )
    current_level, current_balance, history = database.get_vip_membership(
        user.id,
        username=user.username,
        first_name=user.first_name or "Miner",
    )
    page, keyboard = _vip_page(
        user.id, current_level, current_balance, history
    )

    if status == "purchased":
        result = (
            f"✅ Upgrade complete: <b>{database.VIP_TIERS[level].name}</b>.\n"
            f"<b>{database.VIP_TIERS[target_level].upgrade_cost:,} coins</b> "
            f"deducted. Balance: <b>{balance:,} coins</b>."
        )
    elif status == "already_owned":
        result = (
            f"{database.VIP_TIERS[target_level].name} is already active. "
            "No additional coins were deducted."
        )
    elif status == "insufficient_coins":
        cost = database.VIP_TIERS[target_level].upgrade_cost or 0
        result = (
            f"Not enough coins for {database.VIP_TIERS[target_level].name}. "
            f"Cost: {cost:,}; balance: {balance:,}. Nothing was deducted."
        )
    elif status == "max_level":
        result = "You already have the highest VIP level."
    else:
        result = "That upgrade is no longer available. No coins were deducted."

    await query.edit_message_text(
        result + "\n\n" + page,
        reply_markup=keyboard,
        parse_mode="HTML",
    )


async def cancel_vip_upgrade(
    update: Update, context: ContextTypes.DEFAULT_TYPE
) -> None:
    query = update.callback_query
    user = update.effective_user
    if not query or not user:
        return
    match = VIP_CANCEL_CALLBACK.fullmatch(query.data or "")
    if not match:
        await query.answer("Invalid cancellation request.", show_alert=True)
        return
    if int(match.group(1)) != user.id:
        await query.answer(
            "This VIP membership menu belongs to another user.",
            show_alert=True,
        )
        return
    await query.answer()
    level, balance, history = database.get_vip_membership(
        user.id,
        username=user.username,
        first_name=user.first_name or "Miner",
    )
    page, keyboard = _vip_page(user.id, level, balance, history)
    await query.edit_message_text(
        "Upgrade canceled. No coins were deducted.\n\n" + page,
        reply_markup=keyboard,
        parse_mode="HTML",
    )


async def invite(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    user = update.effective_user
    message = update.effective_message
    if not user or not message:
        return

    count, _ = database.get_referral_stats(user.id)
    bot_info = await context.bot.get_me()
    link = f"https://t.me/{bot_info.username}?start=ref_{user.id}"
    await message.reply_text(
        f"👥 Invite friends with your personal link:\n{link}\n\n"
        f"You earn <b>{database.REFERRAL_REWARD} coins</b> for each new miner "
        f"who joins through it.\nYour referrals: <b>{count}</b>",
        reply_markup=KEYBOARD,
        parse_mode="HTML",
        disable_web_page_preview=True,
    )


async def leaderboard(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    message = update.effective_message
    if not message:
        return

    entries = database.get_leaderboard()
    if not entries:
        response = "🏆 No miners yet. Be the first to start mining!"
    else:
        lines = ["🏆 <b>MRVora Mining leaderboard</b>", ""]
        for position, entry in enumerate(entries, start=1):
            name = (
                f"@{escape(entry['username'])}"
                if entry["username"]
                else escape(entry["first_name"] or "Miner")
            )
            lines.append(
                f"<b>{position}.</b> {name} — "
                f"<b>{int(entry['balance']):,} coins</b>"
            )
        response = "\n".join(lines)

    await message.reply_text(response, reply_markup=KEYBOARD, parse_mode="HTML")


async def help_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    message = update.effective_message
    if not message:
        return
    await message.reply_text(
        "<b>MRVora Mining</b>\n"
        "Mine every minute, claim one daily reward, invite friends for referral "
        "coins, and compete on the leaderboard.\n\n"
        "These are in-game points only; the bot does not handle cryptocurrency "
        "or withdrawals.",
        reply_markup=KEYBOARD,
        parse_mode="HTML",
    )


async def unknown_message(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    message = update.effective_message
    if message:
        await message.reply_text(
            "Choose an option from the menu or use /help.",
            reply_markup=KEYBOARD,
        )


async def on_error(update: object, context: ContextTypes.DEFAULT_TYPE) -> None:
    error = context.error
    error_name = type(error).__name__ if error else "UnknownError"
    error_detail = str(error) if error else "No exception details available"
    logger.error(
        "Unhandled Telegram update error (%s): %s",
        error_name,
        error_detail,
    )


def main() -> None:
    token = os.getenv("TELEGRAM_BOT_TOKEN")
    if not token:
        raise SystemExit(
            "TELEGRAM_BOT_TOKEN is missing. Add it in Replit Secrets, then restart the bot."
        )

    database.initialize_database()
    application = Application.builder().token(token).build()
    application.add_handler(CommandHandler("start", start))
    application.add_handler(CommandHandler("help", help_command))
    application.add_handler(
        MessageHandler(filters.Regex(r"^⛏ Mine$"), mine)
    )
    application.add_handler(
        MessageHandler(filters.Regex(r"^💰 Balance$"), show_balance)
    )
    application.add_handler(
        MessageHandler(filters.Regex(r"^🎁 Daily reward$"), daily_reward)
    )
    application.add_handler(
        MessageHandler(filters.Regex(r"^👑 VIP Membership$"), vip_membership)
    )
    application.add_handler(
        MessageHandler(filters.Regex(r"^👥 Invite$"), invite)
    )
    application.add_handler(
        MessageHandler(filters.Regex(r"^🏆 Leaderboard$"), leaderboard)
    )
    application.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, unknown_message))
    application.add_handler(
        CallbackQueryHandler(review_vip_upgrade, pattern=VIP_REVIEW_CALLBACK)
    )
    application.add_handler(
        CallbackQueryHandler(confirm_vip_upgrade, pattern=VIP_BUY_CALLBACK)
    )
    application.add_handler(
        CallbackQueryHandler(cancel_vip_upgrade, pattern=VIP_CANCEL_CALLBACK)
    )
    application.add_error_handler(on_error)

    logger.info("Starting MRVora Mining Telegram bot")
    application.run_polling(allowed_updates=Update.ALL_TYPES)


if __name__ == "__main__":
    main()