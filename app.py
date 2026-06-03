import os
import threading
import logging
from flask import Flask, request, jsonify
from telegram import InlineKeyboardButton, InlineKeyboardMarkup
from telegram.ext import Updater, CommandHandler, CallbackQueryHandler
from dotenv import load_dotenv
import psycopg2
from psycopg2.extras import execute_values
from cryptobot import CryptoBotClient
from cryptobot.models import Asset, Status, ButtonName

load_dotenv()

# --- Переменные окружения ---
BOT_TOKEN = os.getenv("BOT_TOKEN")
CRYPTOBOT_API_TOKEN = os.getenv("CRYPTOBOT_API_TOKEN")
PDF_DOWNLOAD_LINK = os.getenv("PDF_DOWNLOAD_LINK", "https://drive.google.com/your-link")
PRICE_USDT = float(os.getenv("PRICE_USDT", "10"))
DATABASE_URL = os.getenv("DATABASE_URL")

if not DATABASE_URL:
    raise ValueError("DATABASE_URL не задана! Добавьте переменную окружения.")

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)

# --- Инициализация PostgreSQL ---
def get_db_connection():
    return psycopg2.connect(DATABASE_URL)

def init_db():
    conn = get_db_connection()
    c = conn.cursor()
    c.execute('''
        CREATE TABLE IF NOT EXISTS purchases (
            id SERIAL PRIMARY KEY,
            user_id INTEGER NOT NULL,
            invoice_id TEXT NOT NULL,
            paid BOOLEAN DEFAULT TRUE,
            price_usdt REAL NOT NULL,
            purchased_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
            UNIQUE(user_id, invoice_id)
        )
    ''')
    c.execute('''
        CREATE TABLE IF NOT EXISTS pending_invoices (
            invoice_id TEXT PRIMARY KEY,
            user_id INTEGER NOT NULL,
            created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
        )
    ''')
    conn.commit()
    conn.close()
    logger.info("Database initialized")

def add_purchase(user_id, invoice_id, price_usdt):
    conn = get_db_connection()
    c = conn.cursor()
    c.execute('''
        INSERT INTO purchases (user_id, invoice_id, paid, price_usdt, purchased_at)
        VALUES (%s, %s, TRUE, %s, CURRENT_TIMESTAMP)
        ON CONFLICT (user_id, invoice_id) DO NOTHING
    ''', (user_id, invoice_id, price_usdt))
    conn.commit()
    conn.close()
    logger.info(f"Purchase saved: user {user_id}, invoice {invoice_id}")

def is_purchased(user_id):
    conn = get_db_connection()
    c = conn.cursor()
    c.execute('SELECT 1 FROM purchases WHERE user_id = %s AND paid = TRUE LIMIT 1', (user_id,))
    result = c.fetchone()
    conn.close()
    return result is not None

def add_pending_invoice(invoice_id, user_id):
    conn = get_db_connection()
    c = conn.cursor()
    c.execute('INSERT INTO pending_invoices (invoice_id, user_id) VALUES (%s, %s) ON CONFLICT (invoice_id) DO NOTHING', (invoice_id, user_id))
    conn.commit()
    conn.close()

def get_pending_user(invoice_id):
    conn = get_db_connection()
    c = conn.cursor()
    c.execute('SELECT user_id FROM pending_invoices WHERE invoice_id = %s', (invoice_id,))
    row = c.fetchone()
    conn.close()
    return row[0] if row else None

def remove_pending_invoice(invoice_id):
    conn = get_db_connection()
    c = conn.cursor()
    c.execute('DELETE FROM pending_invoices WHERE invoice_id = %s', (invoice_id,))
    conn.commit()
    conn.close()

def get_pending_invoice_for_user(user_id):
    conn = get_db_connection()
    c = conn.cursor()
    c.execute('SELECT invoice_id FROM pending_invoices WHERE user_id = %s ORDER BY created_at DESC LIMIT 1', (user_id,))
    row = c.fetchone()
    conn.close()
    return row[0] if row else None

# --- CryptoBot client ---
client = CryptoBotClient(api_token=CRYPTOBOT_API_TOKEN, is_mainnet=True)

# --- Функции оплаты ---
def generate_crypto_invoice(user_id):
    try:
        invoice = client.create_invoice(
            asset=Asset.USDT,
            amount=PRICE_USDT,
            description=f"Payment for user {user_id}",
            payload=str(user_id),
        )
        logger.info(f"Invoice created: {invoice.invoice_id} for user {user_id}")
        add_pending_invoice(invoice.invoice_id, user_id)
        return invoice.bot_invoice_url, invoice.invoice_id
    except Exception as e:
        logger.error(f"Error creating invoice: {e}")
        return None, None

def send_product(bot, user_id):
    try:
        if PDF_DOWNLOAD_LINK:
            bot.send_message(user_id, f"✅ ¡Pago confirmado! Descarga el curso aquí: {PDF_DOWNLOAD_LINK}")
        else:
            bot.send_message(user_id, "❌ El enlace no ha sido configurado.")
    except Exception as e:
        logger.error(f"Failed to send product: {e}")

# --- Telegram handlers (испанский интерфейс) ---
def main_menu(bot, chat_id, text="Menú principal:"):
    keyboard = [
        [InlineKeyboardButton("📚 Comprar curso", callback_data="buy")],
        [InlineKeyboardButton("ℹ️ Mi suscripción", callback_data="my_sub")],
        [InlineKeyboardButton("✅ Verificar pago", callback_data="check_payment")],
    ]
    bot.send_message(chat_id, text, reply_markup=InlineKeyboardMarkup(keyboard))

def start(update, context):
    user_id = update.message.from_user.id
    main_menu(context.bot, user_id, "¡Bienvenido! Elige una opción:")

def cancel_invoice(update, context):
    user_id = update.message.from_user.id
    inv_id = get_pending_invoice_for_user(user_id)
    if inv_id:
        try:
            client.delete_invoice(invoice_id=inv_id)
            remove_pending_invoice(inv_id)
            update.message.reply_text("✅ El pago pendiente ha sido cancelado. Ahora puedes crear uno nuevo.")
        except Exception as e:
            update.message.reply_text(f"❌ Error al cancelar: {e}")
    else:
        update.message.reply_text("No hay pagos pendientes.")

def force_clean(update, context):
    """Удаляет запись о pending_invoice из БД для текущего пользователя (принудительно)."""
    user_id = update.message.from_user.id
    conn = get_db_connection()
    cur = conn.cursor()
    cur.execute("DELETE FROM pending_invoices WHERE user_id = %s", (user_id,))
    deleted = cur.rowcount
    conn.commit()
    conn.close()
    if deleted:
        update.message.reply_text("✅ La orden de pago pendiente ha sido eliminada. Ahora puedes crear una nueva.")
    else:
        update.message.reply_text("No hay órdenes pendientes para eliminar.")

def my_sub_callback(update, context):
    query = update.callback_query
    query.answer()
    user_id = query.from_user.id

    if is_purchased(user_id):
        # У пользователя есть доступ — отправляем ссылку
        if PDF_DOWNLOAD_LINK:
            context.bot.send_message(
                user_id,
                f"✅ Tienes acceso activo. Descarga el curso aquí: {PDF_DOWNLOAD_LINK}"
            )
            query.edit_message_text("✅ Acceso confirmado. Revisa tu chat, he enviado el enlace.")
        else:
            query.edit_message_text("❌ El enlace no ha sido configurado.")
    else:
        query.edit_message_text("❌ No tienes acceso. Presiona «Comprar curso».")
        main_menu(context.bot, user_id, "Menú principal:")

def check_payment_callback(update, context):
    query = update.callback_query
    query.answer()
    user_id = query.from_user.id

    # Если уже есть доступ
    if is_purchased(user_id):
        query.edit_message_text("✅ Ya recibiste el acceso. El curso ha sido enviado.")
        main_menu(context.bot, user_id, "¿Algo más?")
        return

    inv_id = get_pending_invoice_for_user(user_id)
    if not inv_id:
        query.edit_message_text("No se encontraron pagos pendientes. Inicia una nueva compra.")
        main_menu(context.bot, user_id, "Menú principal:")
        return

    # Преобразуем invoice_id в целое число, так как API CryptoBot требует integer
    try:
        inv_id_int = int(inv_id)
    except (ValueError, TypeError):
        logger.error(f"Invalid invoice_id format: {inv_id} (user {user_id})")
        query.edit_message_text("❌ Error: formato de ID de factura inválido. Por favor, crea una nueva orden.")
        # Удаляем некорректную запись из БД
        remove_pending_invoice(inv_id)
        main_menu(context.bot, user_id, "Menú principal:")
        return

    # Запрос статуса через API CryptoBot
    try:
        invoices = client.get_invoices(invoice_ids=[inv_id_int])
        if invoices:
            invoice = invoices[0]
            if invoice.status == Status.paid:
                # Платёж подтверждён
                add_purchase(user_id, inv_id, PRICE_USDT)
                remove_pending_invoice(inv_id)
                send_product(context.bot, user_id)
                query.edit_message_text("✅ ¡Pago confirmado! El curso ha sido enviado.")
            else:
                query.edit_message_text(f"⏳ El pago aún no se ha recibido. Estado actual: {invoice.status.value}. Inténtalo más tarde.")
        else:
            query.edit_message_text("❌ Error al verificar el estado. No se encontró la factura.")
    except Exception as e:
        logger.error(f"Check payment error: {e}")
        query.edit_message_text("❌ Error al verificar el estado. Inténtalo más tarde.")

    main_menu(context.bot, user_id, "Menú principal:")

    # Запрос статуса через CryptoBot API
    try:
        invoices = client.get_invoices(invoice_ids=[inv_id])
        if invoices:
            invoice = invoices[0]
            if invoice.status == Status.paid:
                add_purchase(user_id, inv_id, PRICE_USDT)
                remove_pending_invoice(inv_id)
                send_product(context.bot, user_id)
                query.edit_message_text("✅ ¡Pago confirmado! El curso ha sido enviado.")
            else:
                query.edit_message_text(f"⏳ El pago aún no se ha recibido. Estado: {invoice.status.value}. Inténtalo más tarde.")
        else:
            query.edit_message_text("❌ Error al verificar el estado. Invoice no encontrado.")
    except Exception as e:
        logger.error(f"Check payment error: {e}")
        query.edit_message_text("❌ Error al verificar el estado. Inténtalo más tarde.")
    main_menu(context.bot, user_id, "Menú principal:")

def buy_callback(update, context):
    query = update.callback_query
    query.answer()
    user_id = query.from_user.id
    if is_purchased(user_id):
        query.edit_message_text("✅ ¡Ya has comprado este curso!")
        main_menu(context.bot, user_id, "Menú principal:")
        return
    # Проверяем, нет ли уже ожидающего платежа
    if get_pending_invoice_for_user(user_id):
        query.edit_message_text("Ya tienes un pago pendiente. Completa el pago o espera.")
        main_menu(context.bot, user_id, "Menú principal:")
        return
    link, inv_id = generate_crypto_invoice(user_id)
    if link:
        keyboard = [[InlineKeyboardButton("💸 Ir a pagar", url=link)]]
        query.edit_message_text(
            f"Se generó una orden de pago por {PRICE_USDT} USDT.\nDespués de pagar, presiona «Verificar pago».",
            reply_markup=InlineKeyboardMarkup(keyboard)
        )
        back_keyboard = [[InlineKeyboardButton("🔙 Volver al menú", callback_data="back_to_menu")]]
        context.bot.send_message(user_id, "O regresa al menú:", reply_markup=InlineKeyboardMarkup(back_keyboard))
    else:
        query.edit_message_text("❌ Error al crear la orden de pago. Inténtalo más tarde.")
        main_menu(context.bot, user_id, "Menú principal:")

def back_to_menu_callback(update, context):
    query = update.callback_query
    query.answer()
    user_id = query.from_user.id
    main_menu(context.bot, user_id, "Menú principal:")

def revoke_access(update, context):
    # Проверка: только администратор может отзывать доступ
    user_id = update.message.from_user.id
    if user_id != ADMIN_ID:
        update.message.reply_text("⛔ У вас нет прав для этой команды.")
        return

    # Получаем ID пользователя, которому нужно отозвать доступ
    try:
        target_user_id = int(context.args[0])
    except (IndexError, ValueError):
        update.message.reply_text("❌ Использование: /revoke <telegram_user_id>")
        return

    conn = get_db_connection()
    c = conn.cursor()
    c.execute("DELETE FROM purchases WHERE user_id = %s", (target_user_id,))
    deleted = c.rowcount
    conn.commit()
    conn.close()

    if deleted:
        update.message.reply_text(f"✅ Доступ пользователя {target_user_id} отозван.")
    else:
        update.message.reply_text(f"❌ Пользователь {target_user_id} не найден в базе.")

# --- Flask для вебхука (уведомления от CryptoBot) ---
flask_app = Flask(__name__)

@flask_app.route('/webhook', methods=['POST'])
def webhook():
    # В реальном проекте нужно проверить подпись, но для простоты пока пропустим
    raw_body = request.get_data()
    logger.info(f"Webhook received: {raw_body[:200]}")  # только первые 200 байт
    # Здесь можно разобрать уведомление и обновить статус автоматически,
    # но для демонстрации оставляем ручную проверку через кнопку.
    return jsonify({"status": "ok"}), 200

@flask_app.route('/health')
def health():
    return "OK", 200

def run_flask():
    port = int(os.environ.get("PORT", 8080))
    flask_app.run(host='0.0.0.0', port=port)

# --- Запуск ---
def main():
    init_db()
    threading.Thread(target=run_flask, daemon=True).start()
    updater = Updater(BOT_TOKEN)
    dp = updater.dispatcher
    dp.add_handler(CommandHandler("start", start))
    dp.add_handler(CallbackQueryHandler(buy_callback, pattern="^buy$"))
    dp.add_handler(CallbackQueryHandler(my_sub_callback, pattern="^my_sub$"))
    dp.add_handler(CallbackQueryHandler(check_payment_callback, pattern="^check_payment$"))
    dp.add_handler(CallbackQueryHandler(back_to_menu_callback, pattern="^back_to_menu$"))
    dp.add_handler(CommandHandler("cancel_invoice", cancel_invoice))
    dp.add_handler(CommandHandler("force_clean", force_clean))
    dp.add_handler(CommandHandler("revoke", revoke_access))
    updater.start_polling()
    updater.idle()

if __name__ == "__main__":
    main()
