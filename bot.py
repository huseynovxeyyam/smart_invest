import logging
import os
from dotenv import load_dotenv
from telegram import ReplyKeyboardMarkup, KeyboardButton, InlineKeyboardButton, InlineKeyboardMarkup
from telegram.ext import Updater, CommandHandler, MessageHandler, Filters, CallbackContext, ConversationHandler, CallbackQueryHandler
from db import init_db, create_user, get_user_by_telegram, get_user_by_refcode, add_investment, add_active_investment, list_user_investments, update_user_balance, add_withdrawal_request, get_referrals_of, get_investment_by_id, add_receipt, mark_investment_active, get_user_by_id, list_all_users, get_pending_investments, get_all_receipts
from utils import gen_referral_code
from payments import daily_payouts
from apscheduler.schedulers.background import BackgroundScheduler
import pytz
from datetime import datetime, timezone
import time

# Load env
load_dotenv()
TOKEN = os.getenv('TELEGRAM_TOKEN')
DB_FILE = os.getenv('DATABASE_FILE', 'data.db')
# Payment recipient (users should send investments here via M10)
ADMIN_PAYMENT_ACCOUNT = '2200248061442058'
ADMIN_PAYMENT_NAME = 'Abdulla Azizov'

# Admin lists are loaded from env; use load_admins() to refresh in-memory state
ADMIN_CHAT_IDS = []
ADMIN_TELEGRAM_ID = None

def load_admins():
    global ADMIN_CHAT_IDS, ADMIN_TELEGRAM_ID
    raw = os.getenv('ADMIN_CHAT_IDS', '') or ''
    lst = [x.strip() for x in raw.split(',') if x.strip()]
    try:
        ADMIN_CHAT_IDS = [int(x) for x in lst]
    except Exception:
        ADMIN_CHAT_IDS = []
    ADMIN_TELEGRAM_ID = None
    if os.getenv('ADMIN_TELEGRAM_ID'):
        try:
            ADMIN_TELEGRAM_ID = int(os.getenv('ADMIN_TELEGRAM_ID'))
        except Exception:
            ADMIN_TELEGRAM_ID = None

# initial load
load_admins()

# Logging
logging.basicConfig(level=logging.DEBUG)

# Initialize DB
init_db(DB_FILE)

# Conversation states
AMOUNT = 1

# Keyboard
main_kb = ReplyKeyboardMarkup([["🔗 Referallarım", "💼 Yatırım"], ["📈 Qazancım", "💰 Balans"], ["💸 Çıxarış", "🆘 Dəstək"]], resize_keyboard=True)
# Use an Inline keyboard for admin menu so pressing buttons does NOT send text messages
admin_kb = InlineKeyboardMarkup([
    [InlineKeyboardButton('👥 İstifadəçilər', callback_data='admin_list_users'), InlineKeyboardButton('✉️ Mesajlar', callback_data='admin_messages')],
    [InlineKeyboardButton('🛒 Alışlar', callback_data='admin_alish'), InlineKeyboardButton('📥 Ödənişlər', callback_data='admin_payments')],
    [InlineKeyboardButton('📢 Toplu Mesaj', callback_data='admin_broadcast'), InlineKeyboardButton('◀️ Geri', callback_data='admin_back')]
])

def start(update, context: CallbackContext):
    args = context.args
    tg_user = update.effective_user
    referrer_id = None
    if args:
        code = args[0]
        ref = get_user_by_refcode(code)
        if ref:
            referrer_id = ref['id']
    # If user already exists, reuse their referral code. Only generate and create for new users.
    existing_user = get_user_by_telegram(tg_user.id)
    if existing_user:
        code = existing_user.get('referral_code') or gen_referral_code()
    else:
        # generate unique referral code and create the user record
        code = gen_referral_code()
        create_user(tg_user.id, tg_user.username or '', code, referrer_id)
        # credit the referrer with 1 AZN when a new user registers with their code
        try:
            if referrer_id and referrer_id != None:
                update_user_balance(referrer_id, 1.0)
                # try to notify the referrer via telegram if we have their telegram_id
                try:
                    ref_row = get_user_by_id(referrer_id)
                    if ref_row and ref_row.get('telegram_id'):
                        context.bot.send_message(chat_id=ref_row.get('telegram_id'), text=f"🎉 Sizin referalınız qeyd olundu — 1 AZN bonus əlavə edildi.")
                except Exception:
                    logging.exception('Failed to notify referrer after credit')
        except Exception:
            logging.exception('Failed to credit referrer for new signup')
    # build referral link if bot username available
    bot_username = context.bot.username or ''
    ref_link = f"https://t.me/{bot_username}?start={code}" if bot_username else f"/start {code}"
    welcome = (
        f"👋 Salam, {tg_user.full_name}!\n\n"
        f"💼 Xoş gəlmisiniz! Biz sizin kiçik sərmayənizi böyütmək üçün buradayıq — etibarlı və şəffaf.\n\n" 
        f"🔑 Sizin referal kodunuz: {code}\n"
        f"🔗 Paylaşın və hər yeni referaldan 1 AZN + referalın yatırdığı məbləğin 10%-ni qazanın.\n\n"
        f"🚀 Referal link: {ref_link}\n\n"
        f"Gündəlik 45 AZN istədiyin vaxt kartında💳\n\n"
        f"💰 Sizin qazancınız üçün çalışırıq — uğurlar!"
    )
    # If admin, show admin keyboard
    is_admin = False
    try:
        if (ADMIN_TELEGRAM_ID and tg_user.id == ADMIN_TELEGRAM_ID) or (tg_user.id in ADMIN_CHAT_IDS):
            is_admin = True
    except Exception:
        is_admin = False
    update.message.reply_text(welcome, reply_markup=admin_kb if is_admin else main_kb)

def help_cmd(update, context):
    update.message.reply_text('ℹ️ Kömək: Aşağıdakı düymələrdən istifadə edin və sualınız varsa "🆘 Dəstək" bölməsinə yazın.', reply_markup=main_kb)

def handle_text(update, context):
    text = update.message.text
    try:
        logging.debug('handle_text called: update_id=%s message_id=%s from=%s text=%s', getattr(update, 'update_id', None), update.message.message_id if update.message else None, update.effective_user.id if update.effective_user else None, text)
    except Exception:
        pass
    user = get_user_by_telegram(update.effective_user.id)
    
    # Broadcast mesajı bekleme durumu
    if context.user_data.get('awaiting_broadcast_message'):
        context.user_data.pop('awaiting_broadcast_message', None)
        broadcast_text = text.strip()
        
        # Önizleme göster
        kb = InlineKeyboardMarkup([
            [InlineKeyboardButton('✅ Göndər', callback_data='broadcast_confirm'), 
             InlineKeyboardButton('❌ Ləğv et', callback_data='broadcast_cancel')]
        ])
        context.user_data['broadcast_text'] = broadcast_text
        context.user_data.pop('broadcast_photo', None)
        
        preview = f"📋 *Mesaj Önizləməsi:*\n\n{'─'*30}\n\n{broadcast_text}\n\n{'─'*30}\n\nBu mesajı bütün istifadəçilərə göndərmək istəyirsiniz?"
        update.message.reply_text(preview, reply_markup=kb, parse_mode='Markdown')
        return
    
    # Allow admin self-activation by sending a secret code (or bot numeric id) in private chat
    try:
        txt_strip = text.strip()
    except Exception:
        txt_strip = ''
    admin_setup_code = os.getenv('ADMIN_SETUP_CODE') or os.getenv('ADMIN_TELEGRAM_ID') or ''
    try:
        bot_id_str = str(context.bot.id)
    except Exception:
        bot_id_str = ''
    # If admin code was requested via /myid, handle the next message as the code
    if context.user_data.get('awaiting_admin_code') and update.effective_chat.type == 'private' and txt_strip:
        # accept either env code, bot id, or hardcoded expected admin code
        expected = os.getenv('ADMIN_SETUP_CODE') or os.getenv('ADMIN_TELEGRAM_ID') or '8232696082'
        if txt_strip == expected or (bot_id_str and txt_strip == bot_id_str):
            # add this user's telegram id to ADMIN_CHAT_IDS and persist to .env
            try:
                uid = update.effective_user.id
                if uid not in ADMIN_CHAT_IDS:
                    ADMIN_CHAT_IDS.append(uid)
                # persist to .env (update ADMIN_CHAT_IDS line)
                env_path = os.path.join(os.getcwd(), '.env')
                try:
                    if os.path.exists(env_path):
                        with open(env_path, 'r', encoding='utf-8') as f:
                            lines = f.readlines()
                    else:
                        lines = []
                    found = False
                    new_lines = []
                    for ln in lines:
                        if ln.strip().startswith('ADMIN_CHAT_IDS='):
                            found = True
                            # build csv
                            existing = ln.split('=',1)[1].strip()
                            if existing:
                                parts = [p.strip() for p in existing.split(',') if p.strip()]
                            else:
                                parts = []
                            if str(uid) not in parts:
                                parts.append(str(uid))
                            new_lines.append('ADMIN_CHAT_IDS=' + ','.join(parts) + '\n')
                        else:
                            new_lines.append(ln)
                    if not found:
                        new_lines.append('\nADMIN_CHAT_IDS=' + str(uid) + '\n')
                    with open(env_path, 'w', encoding='utf-8') as f:
                        f.writelines(new_lines)
                    # reload admin config from .env into memory
                    try:
                        load_admins()
                    except Exception:
                        logging.exception('Failed to reload admins after persisting .env')
                except Exception:
                    logging.exception('Failed to persist admin to .env')
                update.message.reply_text('✅ Siz admin olaraq əlavə olundunuz. /start yazın və admin menyunu görün.')
            except Exception:
                logging.exception('Error setting admin')
                update.message.reply_text('Admin əlavə edilərkən xəta baş verdi.')
        else:
            update.message.reply_text('Kod doğru deyil. Yenidən /myid yazıb təkrar cəhd edin.')
        context.user_data.pop('awaiting_admin_code', None)
        return
    
    if update.effective_chat.type == 'private' and txt_strip and (txt_strip == admin_setup_code or (bot_id_str and txt_strip == bot_id_str)):
        # add this user's telegram id to ADMIN_CHAT_IDS and persist to .env
        try:
            uid = update.effective_user.id
            if uid not in ADMIN_CHAT_IDS:
                ADMIN_CHAT_IDS.append(uid)
            # persist to .env (update ADMIN_CHAT_IDS line)
            env_path = os.path.join(os.getcwd(), '.env')
            try:
                if os.path.exists(env_path):
                    with open(env_path, 'r', encoding='utf-8') as f:
                        lines = f.readlines()
                else:
                    lines = []
                found = False
                new_lines = []
                for ln in lines:
                    if ln.strip().startswith('ADMIN_CHAT_IDS='):
                        found = True
                        # build csv
                        existing = ln.split('=',1)[1].strip()
                        if existing:
                            parts = [p.strip() for p in existing.split(',') if p.strip()]
                        else:
                            parts = []
                        if str(uid) not in parts:
                            parts.append(str(uid))
                        new_lines.append('ADMIN_CHAT_IDS=' + ','.join(parts) + '\n')
                    else:
                        new_lines.append(ln)
                if not found:
                    new_lines.append('\nADMIN_CHAT_IDS=' + str(uid) + '\n')
                    with open(env_path, 'w', encoding='utf-8') as f:
                        f.writelines(new_lines)
                # reload admin config from .env into memory
                try:
                    load_admins()
                except Exception:
                    logging.exception('Failed to reload admins after persisting .env')
            except Exception:
                logging.exception('Failed to persist admin to .env')
            update.message.reply_text('✅ Siz admin olaraq əlavə olundunuz. /start yazın və admin menyunu görün.')
        except Exception:
            logging.exception('Error setting admin')
            update.message.reply_text('Admin əlavə edilərkən xəta baş verdi.')
        return
    # handle ongoing flows: withdrawal card/name, support message
    try:
        txt_strip = text.strip()
    except Exception:
        txt_strip = ''

    # Support flow: if awaiting support message, forward to admins and ack
    if context.user_data.get('awaiting_support'):
        context.user_data.pop('awaiting_support', None)
        # forward to admins
        for aid in ADMIN_CHAT_IDS:
            try:
                uid = update.effective_user.id
                uname = update.effective_user.username or ''
                caption = f"📩 Dəstək mesajı\n\n👤 İstifadəçi: @{uname if uname else uid} (ID: {uid})\n\n{txt_strip}\n\n💬 Cavab vermək üçün aşağıdakı düymədən istifadə edin."
                kb = InlineKeyboardMarkup([[InlineKeyboardButton('✍️ Cavab ver', callback_data=f'support_reply:{uid}')]])
                context.bot.send_message(chat_id=int(aid), text=caption, reply_markup=kb)
            except Exception:
                pass
        update.message.reply_text('✅ Mesajınız alındı — komandamız tezliklə cavab verəcək. Səbr üçün təşəkkürlər!', reply_markup=main_kb)
        return

    # Withdrawal flow: expect 16-digit card
    if context.user_data.get('awaiting_withdraw_card'):
        if txt_strip.isdigit() and len(txt_strip) == 16:
            context.user_data.pop('awaiting_withdraw_card', None)
            context.user_data['withdraw_card'] = txt_strip
            context.user_data['awaiting_withdraw_name'] = True
            update.message.reply_text('Kart sahibinin tam ad və soyadını yazın:', reply_markup=main_kb)
        else:
            update.message.reply_text('Zəhmət olmasa 16 rəqəmli kart nömrəsini tam yazın (sadəcə rəqəmlər).', reply_markup=main_kb)
        return
    if context.user_data.get('awaiting_withdraw_name'):
        # store name and show amount buttons
        name = text.strip()
        context.user_data.pop('awaiting_withdraw_name', None)
        context.user_data['withdraw_name'] = name
        kb = InlineKeyboardMarkup([[InlineKeyboardButton('💸 50 AZN', callback_data='withdraw_amt:50'), InlineKeyboardButton('💸 100 AZN', callback_data='withdraw_amt:100'), InlineKeyboardButton('💸 150 AZN', callback_data='withdraw_amt:150')]])
        # Show card and name as separate monospaced messages for easy copying
        card = context.user_data.get('withdraw_card') or ''
        info_header = '🔒 Kart məlumatı (təhlükəsiz saxlayın)\n'
        try:
            # send card number in its own monospaced message
            update.message.reply_text(info_header + "Kart nömrəsi:\n<code>" + card + "</code>", reply_markup=None, parse_mode='HTML')
            # send card owner name in a separate monospaced message together with amount buttons
            update.message.reply_text("Kart sahibi:\n<code>" + name + "</code>", reply_markup=kb, parse_mode='HTML')
        except Exception:
            # fallback without formatting: send two plain messages
            update.message.reply_text(info_header + f"Kart: {card}")
            update.message.reply_text(f"Ad: {name}", reply_markup=kb)
        return

    # route admin users to admin handler
    is_admin = False
    try:
        if (ADMIN_TELEGRAM_ID and update.effective_user.id == ADMIN_TELEGRAM_ID) or (update.effective_user.id in ADMIN_CHAT_IDS):
            is_admin = True
    except Exception:
        is_admin = False
    if is_admin:
        return handle_admin_text(update, context)
    text = text.lower()
    if text in ['referallarım', '🔗 referallarım', '🔗 Referallarım']:
        refs = get_referrals_of(user['id'])
        code = user['referral_code']
        # compute referral earnings: 1 AZN per referal + 10% of their investments
        ref_count = len(refs)
        ref_bonus = 0.0
        for r in refs:
            try:
                invs = list_user_investments(r.get('id'))
                total = sum(float(i.get('amount') or 0) for i in invs)
                ref_bonus += total * 0.10
            except Exception:
                pass
        ref_bonus += ref_count * 1.0
        update.message.reply_text(f"Sizin referal kodunuz: {code}\nReferallar sayı: {ref_count}\nReferal qazancı: {ref_bonus:.2f} AZN\nReferal link: https://t.me/{context.bot.username}?start={code}", reply_markup=main_kb)
        return
    if text in ['yatırım', '💼 yatırım', '💼 Yatırım']:
        kb = InlineKeyboardMarkup([[InlineKeyboardButton('💳 50 AZN', callback_data='select_amt:50'), InlineKeyboardButton('💳 100 AZN', callback_data='select_amt:100'), InlineKeyboardButton('💳 150 AZN', callback_data='select_amt:150')]])
        # Send friendly explanation first, then send account and name as separate monospaced messages
        explain = (
            "💳 Investisiya seçin:\n\n"
            "Ödənişi M10 vasitəsilə göndərin.\n\n"
            "Ödəniş etdikdən sonra 'Ödənişi təsdiq et' düyməsinə basın.\n\n"
            "Beynəlxalq kartınız varsa, Rusiya kartlarına köçürmə seçimi də mövcuddur (bu üsul daha sürətli təsdiq olunur). Məbləği seçdikdən sonra ödəniş təlimatları göstəriləcək."
        )
        # initial human-readable message
        try:
            update.message.reply_text(explain)
        except Exception:
            pass
        # send account and name as separate monospaced messages so users can long-press and copy
        try:
            update.message.reply_text("Kart nömrəsi:\n<code>" + ADMIN_PAYMENT_ACCOUNT + "</code>", parse_mode='HTML')
            update.message.reply_text("Ad:\n<code>" + ADMIN_PAYMENT_NAME + "</code>", reply_markup=kb, parse_mode='HTML')
        except Exception:
            # fallback: single plain message with keyboard
            update.message.reply_text(f"Kart nömrəsi: {ADMIN_PAYMENT_ACCOUNT}\nAd: {ADMIN_PAYMENT_NAME}", reply_markup=kb)
        return
    if text in ['50', '100', '150']:
        amount = float(text)
        # Send payment instructions with confirm button
        pay_text = (
            f"💳 Investisiya: {int(amount)} AZN\n\n"
            f"Ödənişi M10 vasitəsilə göndərin:\n"
            f"Hesab: {ADMIN_PAYMENT_ACCOUNT}\n"
            f"Ad: {ADMIN_PAYMENT_NAME}\n\n"
            f"Ödəniş etdikdən sonra aşağıdakı " + '"Təsdiq et"' + " düyməsinə basın.\n"
            f"Ödəniş yoxlanıldıqdan sonra yatırım hesabınıza əlavə ediləcək."
        )
        kb = InlineKeyboardMarkup([[InlineKeyboardButton('✅ Təsdiq et', callback_data=f'confirm_pay:{int(amount)}'), InlineKeyboardButton('❌ Ləğv et', callback_data='cancel_pay')]])
        update.message.reply_text(pay_text, reply_markup=kb)
        return
    if text in ['balans', '💰 balans', '💰 Balans']:
        update.message.reply_text(f"💰 Balansınız: {user['balance']:.2f} AZN", reply_markup=main_kb)
        return
    if text in ['qazancım', '📈 qazancım', '📈 Qazancım']:
        invs = list_user_investments(user['id'])
        # only count active investments
        active_invs = [i for i in invs if int(i.get('active') or 0) == 1]
        total = sum(float(i.get('amount') or 0) for i in active_invs)
        # referrals info
        refs = get_referrals_of(user['id'])
        ref_count = len(refs)
        ref_bonus = 0.0
        for r in refs:
            try:
                invs_r = list_user_investments(r.get('id'))
                total_r = sum(float(i.get('amount') or 0) for i in invs_r if int(i.get('active') or 0) == 1)
                ref_bonus += total_r * 0.10
            except Exception:
                pass
        ref_bonus += ref_count * 1.0
        update.message.reply_text(f"📈 Aktiv yatırımlar: {len(active_invs)}\n💼 Cəmi aktiv yatırım: {total:.2f} AZN\n🔗 Referallar: {ref_count} — Referal qazancı: {ref_bonus:.2f} AZN\n💰 Balans: {user['balance']:.2f} AZN", reply_markup=main_kb)
        return
    if text in ['çıxarış', '💸 çıxarış', '💸 Çıxarış']:
        # start withdrawal flow: ask for 16-digit card number
        context.user_data['awaiting_withdraw_card'] = True
        update.message.reply_text('💸 Çıxarış üçün 16 rəqəmli bank kart nömrəsini yazın:', reply_markup=main_kb)
        return
    

    if text in ['dəstək', '🆘 dəstək', '🆘 Dəstək']:
        context.user_data['awaiting_support'] = True
        update.message.reply_text('🆘 Dəstək üçün mesaj yazın — komandamız tezliklə cavab verəcək. Nə qədər konkret olsanız, o qədər sürətli kömək edə bilərik.', reply_markup=main_kb)
        return

    update.message.reply_text('Başa düşmədim. Aşağıdakı düymələrdən istifadə edin.', reply_markup=main_kb)

def error_handler(update, context):
    logging.exception('Exception while handling update: %s', context.error)


def confirm_payment_cb(update, context: CallbackContext):
    query = update.callback_query
    query.answer()
    data = query.data
    user = get_user_by_telegram(query.from_user.id)
    # handle selection of amount button
    if data.startswith('select_amt:'):
        try:
            amt = float(data.split(':', 1)[1])
        except Exception:
            query.edit_message_text('Xəta: məbləğ oxunmadı.', reply_markup=main_kb)
            return
        # edit message to add more payment info and present confirm/cancel
        new_text = query.message.text + "\n\nRusiya kartlarına köçürmə"
        kb = InlineKeyboardMarkup([[InlineKeyboardButton('Ödənişi təsdiq et', callback_data=f'confirm_pay:{int(amt)}'), InlineKeyboardButton('Ləğv et', callback_data='cancel_pay')]])
        try:
            query.edit_message_text(new_text, reply_markup=kb)
        except Exception:
            try:
                query.message.reply_text(new_text, reply_markup=kb)
            except Exception:
                pass
        return
    if data == 'cancel_pay':
        query.edit_message_text('Maliyyə çətinliyinizi başa düşürük — biz məhz bunun üçün burdayıq. Lazım olsa, komandamızla əlaqə saxlayın.')
        try:
            query.message.reply_text('Əsas menyu:', reply_markup=main_kb)
        except Exception:
            pass
        return
    if data.startswith('confirm_pay:'):
        try:
            amt = float(data.split(':', 1)[1])
        except Exception:
            query.edit_message_text('Xəta: məbləğ oxunmadı.', reply_markup=main_kb)
            return
        # Create pending investment and ask for receipt
        inv_id = add_investment(user['id'], amt, f"plan_{int(amt)}")
        context.user_data['pending_investment'] = inv_id
        # edit the inline message (no reply keyboard) and ask user to upload receipt
        try:
            query.edit_message_text(f'✅ Qəbul edildi — {int(amt)} AZN üçün yatırma qeydə alındı. Zəhmət olmasa ödəmə qəbzini (şəkil və ya sənəd) göndərin.')
        except Exception:
            pass
        # provide an inline prompt button to remind user to upload receipt
        kb_upload = InlineKeyboardMarkup([[InlineKeyboardButton('📎 Qəbz əlavə et', callback_data=f'prompt_upload:{inv_id}')]])
        try:
            query.message.reply_text('📎 Qəbzi yükləyin: şəkil və ya sənəd göndərin.', reply_markup=kb_upload)
        except Exception:
            pass
        # confirmation message for user (additional friendly text)
        try:
            context.bot.send_message(chat_id=user['telegram_id'], text='💰 Təşəkkürlər! Investisiyanız qəbul edildi. Maliyyə şöbəsi təsdiq etdikdə sizə məlumat göndərəcəyik — uğurlar və bol qazanc! 🚀')
        except Exception:
            try:
                query.message.reply_text('💰 Təşəkkürlər! Investisiyanız qəbul edildi. Maliyyə şöbəsi təsdiq etdikdə sizə məlumat göndərəcəyik — uğurlar və bol qazanc! 🚀')
            except Exception:
                pass
        # finally send main menu as normal message
        try:
            query.message.reply_text('Əsas menyu:', reply_markup=main_kb)
        except Exception:
            pass

def admin_cb(update, context: CallbackContext):
    query = update.callback_query
    query.answer()
    data = query.data

    def edit_cb_message(text, reply_markup=None, parse_mode=None):
        try:
            query.edit_message_text(text=text, reply_markup=reply_markup, parse_mode=parse_mode)
            return
        except Exception:
            pass
        try:
            query.edit_message_caption(caption=text, reply_markup=reply_markup, parse_mode=parse_mode)
            return
        except Exception:
            pass
        try:
            query.message.reply_text(text=text, reply_markup=reply_markup, parse_mode=parse_mode)
        except Exception:
            pass
    
    # Broadcast callbacks
    if data == 'admin_broadcast':
        context.user_data['awaiting_broadcast_message'] = True
        context.user_data.pop('broadcast_photo', None)
        context.user_data.pop('broadcast_text', None)
        kb = InlineKeyboardMarkup([[InlineKeyboardButton('◀️ Geri', callback_data='admin_back')]])
        query.edit_message_text(
            '📢 *Toplu Mesaj Göndərmə*\n\n'
            'Bütün istifadəçilərə göndərmək istədiyiniz mesajı *və ya şəkli* göndərin.\n\n'
            '🖼️ Şəkil göndərsəniz caption (mətn) əlavə edə bilərsiniz.\n\n'
            '📝 Mesajınız Markdown formatında ola bilər:\n'
            '• *qalın* - qalın yazı\n'
            '• _italik_ - italik yazı\n'
            '• `kod` - kod formatı\n\n'
            'Mesajı və ya şəkli göndərin:',
            reply_markup=kb,
            parse_mode='Markdown'
        )
        return
    
    if data == 'broadcast_confirm':
        broadcast_text = context.user_data.get('broadcast_text')
        broadcast_photo = context.user_data.get('broadcast_photo')
        if not broadcast_text and not broadcast_photo:
            edit_cb_message('❌ Mesaj tapılmadı. Yenidən cəhd edin.')
            return
        
        # Tüm kullanıcıları al
        users = list_all_users()
        total_users = len(users)
        success_count = 0
        fail_count = 0
        
        edit_cb_message(
            f'📤 *Mesaj göndərilir...*\n\n'
            f'Ümumi istifadəçi: `{total_users}`\n'
            f'Zəhmət olmasa gözləyin...',
            parse_mode='Markdown'
        )
        
        # Mesajları gönder
        for u in users:
            try:
                tg_id = u.get('telegram_id')
                if tg_id:
                    if broadcast_photo:
                        caption = '📢 *Admin Mesajı*'
                        if broadcast_text:
                            caption += f'\n\n{broadcast_text}'
                        context.bot.send_photo(
                            chat_id=tg_id,
                            photo=broadcast_photo,
                            caption=caption,
                            parse_mode='Markdown'
                        )
                    else:
                        context.bot.send_message(
                            chat_id=tg_id,
                            text=f'📢 *Admin Mesajı*\n\n{broadcast_text}',
                            parse_mode='Markdown'
                        )
                    success_count += 1
                    time.sleep(0.05)  # Rate limiting
            except Exception as e:
                fail_count += 1
                logging.error(f'İstifadəçi {u.get("id")}-ə mesaj göndərilərkən xəta: {e}')
        
        # Sonuç
        kb = InlineKeyboardMarkup([[InlineKeyboardButton('◀️ Admin Paneli', callback_data='admin_back')]])
        success_ratio = (success_count / total_users * 100) if total_users else 0.0
        edit_cb_message(
            f'✅ *Mesaj Göndərilməsi Tamamlandı!*\n\n'
            f'📊 *Nəticələr:*\n'
            f'✔️ Uğurlu: `{success_count}`\n'
            f'❌ Uğursuz: `{fail_count}`\n'
            f'📈 Ümumi: `{total_users}`\n'
            f'📊 Uğur Nisbəti: `{success_ratio:.1f}%`',
            reply_markup=kb,
            parse_mode='Markdown'
        )
        
        # Context-i temizle
        context.user_data.pop('broadcast_text', None)
        context.user_data.pop('broadcast_photo', None)
        return
    
    if data == 'broadcast_cancel':
        context.user_data.pop('broadcast_text', None)
        context.user_data.pop('broadcast_photo', None)
        context.user_data.pop('awaiting_broadcast_message', None)
        edit_cb_message('❌ Mesaj göndərilməsi ləğv edildi.')
        query.message.reply_text('👑 Admin menyu:', reply_markup=admin_kb)
        return
    
    # New admin menu callbacks
    if data == 'admin_list_users':
        users = list_all_users()
        if not users:
            query.edit_message_text('Heç bir istifadəçi yoxdur.', reply_markup=admin_kb)
            return
        for u in users[:50]:
            pending = get_pending_investments()
            flag = '🔴' if any(p['user_id']==u.get('id') for p in pending) else '🟢'
            kb = InlineKeyboardMarkup([[InlineKeyboardButton('Aç', callback_data=f'admin_user:{u.get("id")}')]])
            try:
                context.bot.send_message(chat_id=update.effective_chat.id, text=f"{flag} {u.get('username') or ''} — ID:{u.get('id')} — Balans: {u.get('balance'):.2f} AZN", reply_markup=kb)
            except Exception:
                pass
        query.edit_message_text('İstifadəçilər listi (ən çox 50 göstərildi).', reply_markup=admin_kb)
        return
    if data == 'admin_messages':
        query.edit_message_text('Mesaj göndərmək üçün istifadəçini seçin: İstifadəçilər bölməsindən bir istifadəçi açın və "Mesaj göndər" düyməsinə basın.', reply_markup=admin_kb)
        return
    if data == 'admin_alish':
        query.edit_message_text('İstifadəçinin qarşısında alış əlavə etmək üçün əvvəlcə İstifadəçilər -> Aç -> Alış et istifadə edin.', reply_markup=admin_kb)
        return
    # admin: view user details
    if data.startswith('admin_user:'):
        try:
            uid = int(data.split(':',1)[1])
        except Exception:
            query.edit_message_text('İstifadəçi tapılmadı.')
            return
        u = get_user_by_id(uid)
        if not u:
            query.edit_message_text('İstifadəçi tapılmadı.')
            return
        # check pending
        pending = get_pending_investments()
        has_pending = any(p['user_id']==uid for p in pending)
        txt = f"İstifadəçi: {u.get('username') or ''}\nID: {u.get('id')}\nTelegram ID: {u.get('telegram_id')}\nBalans: {u.get('balance'):.2f} AZN\nReferal ID: {u.get('referrer_id') or '—'}\nPending ödəniş: {'🔴 Var' if has_pending else '🟢 Yox'}"
        kb = InlineKeyboardMarkup([
            [InlineKeyboardButton('✉️ Mesaj göndər', callback_data=f'admin_msg:{uid}'), InlineKeyboardButton('🛒 Alış et', callback_data=f'admin_alish:{uid}')],
            [InlineKeyboardButton('◀️ Geri', callback_data='admin_back')]
        ])
        query.edit_message_text(txt, reply_markup=kb)
        return
    if data == 'admin_back':
        query.edit_message_text('🧾 Admin menyu', reply_markup=None)
        query.message.reply_text('👑 Admin menyu:', reply_markup=admin_kb)
        return
    if data.startswith('support_reply:'):
        # admin clicked reply on a forwarded support message
        try:
            uid = int(data.split(':',1)[1])
        except Exception:
            query.edit_message_text('İstifadəçi tapılmadı.')
            return
        # set this admin's context to send the next message to uid (telegram id)
        context.user_data['admin_msg_target'] = uid
        query.edit_message_text(f'İndi mesaj yazın — bu mesaj seçilmiş istifadəçiyə göndəriləcək (Telegram ID {uid}).')
        return
    if data.startswith('admin_msg:'):
        try:
            uid = int(data.split(':',1)[1])
        except Exception:
            query.edit_message_text('Hedef istifadəçi tapılmadı.')
            return
        # uid here is the DB user id; map to telegram_id
        u = get_user_by_id(uid)
        if not u:
            query.edit_message_text('Hedef istifadəçi tapılmadı (db record yoxdur).')
            return
        tg = u.get('telegram_id')
        if not tg:
            query.edit_message_text('Hedef istifadəçinin Telegram ID-si tapılmadı.')
            return
        context.user_data['admin_msg_target'] = tg
        # show helpful text with username if available
        query.edit_message_text(f'İndi mesaj yazın — bu mesaj seçilmiş istifadəçiyə göndəriləcək (Telegram ID {tg}, username: @{u.get("username") or "—"}).')
        return
    if data.startswith('admin_alish:'):
        try:
            uid = int(data.split(':',1)[1])
        except Exception:
            query.edit_message_text('Hedef istifadəçi tapılmadı.')
            return
        kb = InlineKeyboardMarkup([[InlineKeyboardButton('50 AZN', callback_data=f'admin_buy:{uid}:50'), InlineKeyboardButton('100 AZN', callback_data=f'admin_buy:{uid}:100'), InlineKeyboardButton('150 AZN', callback_data=f'admin_buy:{uid}:150')]])
        query.edit_message_text(f'İstifadəçi ID {uid} üçün plan seçin:', reply_markup=kb)
        return
    if data.startswith('admin_buy:'):
        parts = data.split(':')
        try:
            uid = int(parts[1]); amt = float(parts[2])
        except Exception:
            query.edit_message_text('Parametr xətası.')
            return
        # create active investment for user so it appears in qazancım
        add_active_investment(uid, amt, f'plan_{int(amt)}')
        # notify admin and user (map DB id -> telegram id)
        query.edit_message_text(f'{amt:.0f} AZN aktiv yatırım istifadəçiyə əlavə edildi.')
        u_row = get_user_by_id(uid)
        if u_row and u_row.get('telegram_id'):
            try:
                context.bot.send_message(chat_id=u_row.get('telegram_id'), text=f'✅ Admin tərəfindən sizin hesabınıza {int(amt)} AZN investisiya əlavə edildi.')
            except Exception:
                logging.exception('Failed to notify user about admin_buy')
        else:
            logging.info('admin_buy: could not find telegram_id for user id %s', uid)
        return
    if data == 'admin_payments':
        # show receipts
        receipts = get_all_receipts()
        if not receipts:
            query.edit_message_text('Hələ heç bir qəbz yoxdur.')
            return
        # send a list and inline open buttons
        for r in receipts[:20]:
            inv = get_investment_by_id(r.get('investment_id'))
            u = get_user_by_id(r.get('user_id'))
            caption = f"📎 Qəbz ID: {r.get('id')}\n👤 İstifadəçi ID: {u.get('id') if u else r.get('user_id')}\n💼 Invest ID: {r.get('investment_id')}\n💰 Məbləğ: {inv.get('amount') if inv else '—'} AZN"
            try:
                if r.get('file_type') == 'photo':
                    context.bot.send_photo(chat_id=update.effective_chat.id, photo=r.get('file_id'), caption=caption, reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton('✅ Təsdiq et', callback_data=f'admin_verify:{r.get("investment_id")}'), InlineKeyboardButton('◀️ Geri', callback_data='admin_back')]]))
                else:
                    context.bot.send_document(chat_id=update.effective_chat.id, document=r.get('file_id'), caption=caption, reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton('✅ Təsdiq et', callback_data=f'admin_verify:{r.get("investment_id")}'), InlineKeyboardButton('◀️ Geri', callback_data='admin_back')]]))
            except Exception:
                logging.exception('Failed to send receipt to admin view')
        query.edit_message_text('Göndərdim (əgər çoxdursa, ən son 20 göstərildi).')
        return
    if data.startswith('admin_verify:'):
        try:
            inv_id = int(data.split(':',1)[1])
        except Exception:
            query.edit_message_text('Invalid invest id')
            return
        inv = get_investment_by_id(inv_id)
        if not inv:
            query.edit_message_text('Invest tapılmadı.')
            return
        if inv.get('active') == 1:
            query.edit_message_text('Artıq aktivdir.')
            return
        mark_investment_active(inv_id)
        update_user_balance(inv.get('user_id'), float(inv.get('amount') or 0))
        query.edit_message_text(f'✅ Invest {inv_id} təsdiq edildi və balans yeniləndi.')
        # notify user by Telegram ID (map DB user id -> telegram_id)
        try:
            u_row = get_user_by_id(inv.get('user_id'))
            if u_row and u_row.get('telegram_id'):
                context.bot.send_message(chat_id=u_row.get('telegram_id'), text=f'🎉 Uğurlu! Sizin yatırımınız (ID {inv_id}) admin tərəfindən təsdiq edildi. Bol qazanc! 💰')
            else:
                logging.info('admin_verify: telegram_id not found for user %s', inv.get('user_id'))
        except Exception:
            logging.exception('Failed to notify user after admin_verify')
        return


def forward_receipt_to_admins(bot, from_user, investment, file_id, file_type, caption_extra: str = ''):
    caption = (
        f"📥 Yeni ödəmə qəbzi\n\n"
        f"👤 İstifadəçi: {from_user.get('username') or from_user.get('telegram_id')}\n"
        f"💼 Yatırım ID: {investment.get('id')}\n"
        f"💰 Məbləğ: {investment.get('amount')} AZN\n"
        f"{caption_extra}"
    )
    # Send to configured admin chat ids
    for aid in ADMIN_CHAT_IDS:
        try:
            # try as int
            chat_id = int(aid)
        except Exception:
            continue
        try:
            if file_type == 'photo':
                bot.send_photo(chat_id=chat_id, photo=file_id, caption=caption)
            else:
                bot.send_document(chat_id=chat_id, document=file_id, caption=caption)
        except Exception as e:
            logging.exception('Failed to forward receipt to admin %s: %s', aid, e)

    # Also send to any additional recipients listed in env ADDITIONAL_RECIPIENTS (comma-separated)
    # Items can be numeric chat IDs or @usernames. Phone numbers are NOT supported by Bot API.
    extra = os.getenv('ADDITIONAL_RECIPIENTS', '') or ''
    for item in [x.strip() for x in extra.split(',') if x.strip()]:
        # try numeric id first
        try:
            chat = int(item)
        except Exception:
            # ensure username starts with @
            chat = item if item.startswith('@') else f"@{item}"
        try:
            if file_type == 'photo':
                bot.send_photo(chat_id=chat, photo=file_id, caption=caption)
            else:
                bot.send_document(chat_id=chat, document=file_id, caption=caption)
        except Exception:
            logging.exception('Failed to forward receipt to additional recipient %s', item)


def withdraw_cb(update, context: CallbackContext):
    query = update.callback_query
    query.answer()
    try:
        logging.info('withdraw_cb triggered; data=%s user=%s', query.data, query.from_user.id)
    except Exception:
        pass
    data = query.data
    if not data.startswith('withdraw_amt:'):
        return
    try:
        amt = float(data.split(':',1)[1])
    except Exception:
        query.edit_message_text('Xəta: məbləğ oxunmadı.')
        return
    uid = query.from_user.id
    user = get_user_by_telegram(uid)
    if not user:
        query.edit_message_text('İstifadəçi tapılmadı.')
        return
    balance = float(user.get('balance') or 0)
    if amt > balance:
        query.edit_message_text('Kifayət qədər balans yoxdur.')
        try:
            query.message.reply_text('Əsas menyu:', reply_markup=main_kb)
        except Exception:
            pass
        # notify admins about insufficient attempt (optional)
        try:
            for aid in ADMIN_CHAT_IDS:
                try:
                    context.bot.send_message(chat_id=int(aid), text=f"⚠️ Çıxarış cəhdi: istifadəçi {query.from_user.id} seçdi {amt:.2f} AZN amma balans yetərli deyil.")
                except Exception:
                    pass
        except Exception:
            pass
        return
    # check registration days if available
    try:
        urow = get_user_by_id(user.get('id'))
        created = urow.get('created_at') or urow.get('created') or urow.get('created_on')
        days_passed = None
        if created:
            try:
                # try parsing ISO
                dt = datetime.fromisoformat(created)
            except Exception:
                try:
                    dt = datetime.fromtimestamp(float(created))
                except Exception:
                    dt = None
            if dt:
                if dt.tzinfo is None:
                    dt = dt.replace(tzinfo=timezone.utc)
                days_passed = (datetime.now(timezone.utc) - dt).days
        if days_passed is None:
            days_passed = 999
    except Exception:
        days_passed = 999
    if days_passed < 10:
        remaining = 10 - days_passed
        # inform user about waiting period
        query.edit_message_text(f"{days_passed}/10 — Üzr istəyirik, traderlərimiz üçün bir qədər vaxt verin. Çıxarış üçün {remaining} gün sonra müraciət edin.")
        # notify admins that user attempted withdrawal but must wait
        card = context.user_data.get('withdraw_card')
        name = context.user_data.get('withdraw_name')
        try:
            for aid in ADMIN_CHAT_IDS:
                try:
                    msg = f"ℹ️ Gözləmə: İstifadəçi {query.from_user.id} çıxarış üçün {amt:.2f} AZN seçdi. Qeydiyyatdan sonra {days_passed}/10 tamamlanıb — {remaining} gün gözləmə var."
                    if card:
                        msg += f"\nKart: {card}"
                    if name:
                        msg += f"\nAd: {name}"
                    context.bot.send_message(chat_id=int(aid), text=msg)
                except Exception:
                    pass
        except Exception:
            pass
        return
    # proceed with withdrawal
    try:
        add_withdrawal_request(user.get('id'), amt)
        update_user_balance(user.get('id'), -amt)
        query.edit_message_text(f'Çıxarış sorğunuz qəbul edildi: {amt} AZN')
        try:
            query.message.reply_text('Əsas menyu:', reply_markup=main_kb)
        except Exception:
            pass
        # notify admins about new withdrawal request
        card = context.user_data.get('withdraw_card')
        name = context.user_data.get('withdraw_name')
        try:
            for aid in ADMIN_CHAT_IDS:
                try:
                    msg = f"✅ Yeni çıxarış sorğusu:\nİstifadəçi: {query.from_user.id}\nMəbləğ: {amt:.2f} AZN"
                    if name:
                        msg += f"\nAd: {name}"
                    if card:
                        msg += f"\nKart: {card}"
                    context.bot.send_message(chat_id=int(aid), text=msg)
                except Exception:
                    pass
        except Exception:
            pass
    except Exception:
        query.edit_message_text('Çıxarış zamanı xəta baş verdi. Zəhmət olmasa yenidən cəhd edin.', reply_markup=main_kb)


def credit_daily_returns():
    # iterate users and credit 10% of each active investment to their balance
    try:
        users = list_all_users()
        for u in users:
            uid = u.get('id')
            try:
                invs = list_user_investments(uid)
                payout = 0.0
                for i in invs:
                    try:
                        if int(i.get('active') or 0) == 1:
                            amount = float(i.get('amount') or 0)
                            payout += amount * 0.10
                    except Exception:
                        pass
                if payout > 0:
                    update_user_balance(uid, payout)
                    # optional notify user
                    try:
                        # send simple notification if bot available
                        from telegram import Bot
                        bot = Bot(token=TOKEN)
                        bot.send_message(chat_id=u.get('telegram_id'), text=f'📈 Gündəlik qazancınız əlavə edildi: {payout:.2f} AZN')
                    except Exception:
                        pass
            except Exception:
                logging.exception('Error calculating payout for user %s', uid)
    except Exception:
        logging.exception('credit_daily_returns failed')

def verify_command(update, context: CallbackContext):
    user = update.effective_user
    chat_id = update.effective_chat.id
    # Only allow admins
    is_admin = False
    try:
        if user.id == ADMIN_TELEGRAM_ID:
            is_admin = True
    except Exception:
        pass
    if user.id in ADMIN_CHAT_IDS:
        is_admin = True
    if not is_admin:
        update.message.reply_text('Siz admin deyilsiniz. Bu əmri icra edə bilmərsiniz.')
        return
    args = context.args
    if not args:
        update.message.reply_text('İstifadə: /verify <investment_id>')
        return
    try:
        inv_id = int(args[0])
    except Exception:
        update.message.reply_text('Yanlış invest_id. Nümunə: /verify 12')
        return
    inv = get_investment_by_id(inv_id)
    if not inv:
        update.message.reply_text(f'Invest ID {inv_id} tapılmadı.')
        return
    if inv.get('active') == 1:
        update.message.reply_text(f'Invest ID {inv_id} artıq aktivdir.')
        return
    # mark active and credit user's balance
    mark_investment_active(inv_id)
    uid = inv.get('user_id')
    amount = float(inv.get('amount') or 0)
    update_user_balance(uid, amount)
    # referral immediate payout: 1 AZN + 10% of investment to referrer (if any)
    user_row = get_user_by_id(uid)
    if user_row and user_row.get('referrer_id'):
        ref_id = user_row.get('referrer_id')
        bonus = 1.0 + (amount * 0.10)
        update_user_balance(ref_id, bonus)
        # notify referrer if possible
        try:
            for aid in ADMIN_CHAT_IDS:
                # optional: send notification to admins only
                pass
        except Exception:
            pass
    # notify investor
    try:
        u_row = get_user_by_id(inv.get('user_id'))
        if u_row and u_row.get('telegram_id'):
            context.bot.send_message(chat_id=u_row.get('telegram_id'), text=f'✅ Sizin yatırımınız (ID {inv_id}) təsdiq edildi və {amount:.2f} AZN balansınıza əlavə olundu.')
        else:
            logging.info('verify command: telegram_id not found for user %s', inv.get('user_id'))
    except Exception:
        # fallback: send to current chat
        update.message.reply_text('Investasiya təsdiq edildi, istifadəçiyə bildiriş göndərildi (əgər mümkünsə).')
    update.message.reply_text(f'Invest ID {inv_id} aktivləşdirildi və {amount:.2f} AZN istifadəçinin balansına əlavə edildi.')

def myid_command(update, context: CallbackContext):
    user = update.effective_user
    uid = user.id
    username = user.username or ''
    # show id and prompt for admin code to activate
    context.user_data['awaiting_admin_code'] = True
    update.message.reply_text(f'Sizin Telegram numeric ID: {uid}\nUsername: {username}\n\nAdmin nömrəsini daxil edin:')

def reload_admins_command(update, context: CallbackContext):
    try:
        load_admins()
        update.message.reply_text(f'Adminlar yeniləndi. Hazırki admin sayısı: {len(ADMIN_CHAT_IDS)}')
    except Exception as e:
        logging.exception('reload_admins failed')
        update.message.reply_text('Admin yeniləmək alınmadı.')

def handle_admin_text(update, context: CallbackContext):
    text = update.message.text
    try:
        logging.debug('handle_admin_text called: update_id=%s message_id=%s user=%s text=%s', getattr(update, 'update_id', None), update.message.message_id if update.message else None, update.effective_user.id if update.effective_user else None, text)
    except Exception:
        pass
    # admin messaging flow
    if 'admin_msg_target' in context.user_data:
        target = context.user_data.pop('admin_msg_target')
        try:
            # ensure target is int if possible
            try:
                chat_id = int(target)
            except Exception:
                chat_id = target
            context.bot.send_message(chat_id=chat_id, text=f'📩 Maliyyə xidməti\n\n{text}')
            update.message.reply_text('Mesaj göndərildi.', reply_markup=admin_kb)
        except Exception:
            # Log detailed error and give actionable message to admin
            logging.exception('Failed to send admin message to %s', target)
            err = None
            try:
                import telegram
                err = telegram
            except Exception:
                err = None
            # Common reason: bot hasn't been started by the user or blocked by user
            update.message.reply_text('Mesaj göndərilərkən xəta baş verdi. Ən çox rastlanan səbəb: istifadəçi botla söhbətə başlamayıb və ya bot bloklanıb. İstifadəçidən əvvəlcə /start yazmasını xahiş edin.', reply_markup=admin_kb)
        return
    # admin menu options
    if text == 'İstifadəçilər':
        users = list_all_users()
        if not users:
            update.message.reply_text('Heç bir istifadəçi yoxdur.', reply_markup=admin_kb)
            return
        # send brief list with inline buttons
        for u in users[:50]:
            pending = get_pending_investments()
            flag = '🔴' if any(p['user_id']==u.get('id') for p in pending) else '🟢'
            kb = InlineKeyboardMarkup([[InlineKeyboardButton('Aç', callback_data=f'admin_user:{u.get("id")}')]])
            update.message.reply_text(f"{flag} {u.get('username') or ''} — ID:{u.get('id')} — Balans: {u.get('balance'):.2f} AZN", reply_markup=kb)
        update.message.reply_text('İstifadəçilər listi (ən çox 50 göstərildi).', reply_markup=admin_kb)
        return
    if text == 'Mesajlar':
        update.message.reply_text('Mesaj göndərmək üçün istifadəçini seçin: İstifadəçilər bölməsindən bir istifadəçi açın və "Mesaj göndər" düyməsinə basın.', reply_markup=admin_kb)
        return
    if text == 'Alışlar':
        update.message.reply_text('İstifadəçinin qarşısında alış əlavə etmək üçün əvvəlcə İstifadəçilər -> Aç -> Alış et istifadə edin.', reply_markup=admin_kb)
        return
    if text == 'Ödənişlər':
        # show receipts via callback flow
        # create a small inline button to trigger payments view
        kb = InlineKeyboardMarkup([[InlineKeyboardButton('Qəbzləri göstər', callback_data='admin_payments')]])
        update.message.reply_text('Admin ödənişlər səhifəsi:', reply_markup=kb)
        return
    if text == 'Geri' or text.lower() == 'geri':
        update.message.reply_text('Geri', reply_markup=admin_kb)
        return
    # Build a single informative reply for unknown admin actions
    try:
        # Log summary info for debugging
        logging.info('Admin unknown operation. user=%s keys_count=%s', update.effective_user.id, len(list(context.user_data.keys())))
    except Exception:
        pass

    if 'admin_msg_target' in context.user_data:
        tgt = context.user_data.get('admin_msg_target')
        reply_text = f'Admin: mesaj məqsədi mövcuddur (ID: {tgt}). Mesajınızı yazın — yazdığınız növbəti mesaj seçilmiş istifadəçiyə göndəriləcək. Ləğv üçün /canceladmin.'
    else:
        reply_text = 'Admin: bilinməyən əməliyyat. Mesaj göndərmək üçün əvvəlcə İstifadəçilər → Aç → "Mesaj göndər" düyməsinə basın, sonra mesaj yazın.'

    # Deduplicate rapid identical replies per chat to avoid double responses
    try:
        chat_id = update.effective_chat.id
        last = context.chat_data.get('last_admin_reply')
        from time import time
        now = time()
        if last and last.get('text') == reply_text and (now - last.get('ts', 0) < 2.0):
            logging.debug('Skipping duplicate admin reply for chat %s', chat_id)
            return
        # send and record
        update.message.reply_text(reply_text, reply_markup=admin_kb)
        context.chat_data['last_admin_reply'] = {'text': reply_text, 'ts': now}
    except Exception:
        pass

def handle_receipt_photo(update, context: CallbackContext):
    if context.user_data.get('awaiting_broadcast_message'):
        context.user_data.pop('awaiting_broadcast_message', None)
        photo = update.message.photo[-1]
        broadcast_photo = photo.file_id
        broadcast_text = (update.message.caption or '').strip()

        kb = InlineKeyboardMarkup([
            [InlineKeyboardButton('✅ Göndər', callback_data='broadcast_confirm'),
             InlineKeyboardButton('❌ Ləğv et', callback_data='broadcast_cancel')]
        ])
        context.user_data['broadcast_photo'] = broadcast_photo
        context.user_data['broadcast_text'] = broadcast_text

        caption = '📋 *Şəkilli Mesaj Önizləməsi*\n\nBu şəkli bütün istifadəçilərə göndərmək istəyirsiniz?'
        if broadcast_text:
            caption += f'\n\n📝 Caption:\n{broadcast_text}'
        update.message.reply_photo(photo=broadcast_photo, caption=caption, reply_markup=kb, parse_mode='Markdown')
        return

    user = update.effective_user
    tg_user = get_user_by_telegram(user.id)
    if 'pending_investment' not in context.user_data:
        update.message.reply_text('Heç bir pending ödəniş tapılmadı. Əvvəlcə ödənişi təsdiq edin.', reply_markup=main_kb)
        return
    inv_id = context.user_data.pop('pending_investment')
    photo = update.message.photo[-1]
    file_id = photo.file_id
    add_receipt(tg_user['id'], inv_id, file_id, 'photo')
    investment = get_investment_by_id(inv_id)
    # forward to admins
    forward_receipt_to_admins(context.bot, {'telegram_id': user.id, 'username': user.username}, investment, file_id, 'photo')
    update.message.reply_text('Qəbz admin-ə göndərildi. Təsdiq gözlənilir.', reply_markup=main_kb)

def handle_receipt_document(update, context: CallbackContext):
    user = update.effective_user
    tg_user = get_user_by_telegram(user.id)
    if 'pending_investment' not in context.user_data:
        update.message.reply_text('Heç bir pending ödəniş tapılmadı. Əvvəlcə ödənişi təsdiq edin.', reply_markup=main_kb)
        return
    inv_id = context.user_data.pop('pending_investment')
    doc = update.message.document
    file_id = doc.file_id
    add_receipt(tg_user['id'], inv_id, file_id, 'document')
    investment = get_investment_by_id(inv_id)
    forward_receipt_to_admins(context.bot, {'telegram_id': user.id, 'username': user.username}, investment, file_id, 'document')
    update.message.reply_text('Qəbz admin-ə göndərildi. Təsdiq gözlənilir.', reply_markup=main_kb)


def main():
    if not TOKEN:
        print('TELEGRAM_TOKEN not set in environment. Create a .env file from .env.example')
        return
    updater = Updater(TOKEN, use_context=True)
    try:
        me = updater.bot.get_me()
        print(f"Bot account: @{me.username} (id: {me.id})")
    except Exception:
        print("Bot hesabı alınamadı - token düzgün olmayabilir.")
    dp = updater.dispatcher

    dp.add_handler(CommandHandler('start', start))
    dp.add_handler(CommandHandler('help', help_cmd))
    dp.add_handler(CommandHandler('myid', myid_command))
    dp.add_handler(MessageHandler(Filters.text & ~Filters.command, handle_text))
    # Admin-specific callbacks (prefix admin_)
    dp.add_handler(CallbackQueryHandler(admin_cb, pattern='^admin_'))
    # Broadcast callbacks
    dp.add_handler(CallbackQueryHandler(admin_cb, pattern='^broadcast_'))
    # Withdrawal callback (match withdraw_amt precisely)
    dp.add_handler(CallbackQueryHandler(withdraw_cb, pattern='^withdraw_amt:'))
    # Generic confirm payment callbacks (select amount + confirm)
    dp.add_handler(CallbackQueryHandler(confirm_payment_cb))

    # Receipt handlers (photo and document)
    dp.add_handler(MessageHandler(Filters.photo, handle_receipt_photo))
    dp.add_handler(MessageHandler(Filters.document, handle_receipt_document))

    # Admin verify command
    dp.add_handler(CommandHandler('verify', verify_command))
    dp.add_error_handler(error_handler)

    # Scheduler for daily payouts (runs every day at 00:00 server time)
    sched = BackgroundScheduler(timezone=pytz.UTC)
    sched.add_job(daily_payouts, 'interval', hours=24, timezone=pytz.UTC)
    # also credit daily returns (10% of active investments)
    sched.add_job(credit_daily_returns, 'interval', hours=24, timezone=pytz.UTC)
    sched.start()

    updater.start_polling()
    print('Bot started')
    updater.idle()

if __name__ == '__main__':
    main()