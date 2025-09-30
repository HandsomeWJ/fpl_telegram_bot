import os
import re
import json
import logging
import requests
from bs4 import BeautifulSoup
from dotenv import load_dotenv
from telegram import Update
from telegram.constants import ParseMode
from telegram.ext import Application, CommandHandler, ContextTypes
import datetime
import zoneinfo

# --- Basic Logging & Configuration ---
logging.basicConfig(
    format='%(asctime)s - %(name)s - %(levelname)s - %(message)s',
    level=logging.INFO
)
logger = logging.getLogger(__name__)

STATE_FILE = "transfers.json"

# --- Constants & Schedule Config (unchanged) ---
FIX_LOGIN_URL = "https://www.fantasyfootballfix.com/signin/"
FIX_ORIGIN = "https://www.fantasyfootballfix.com"
TARGET_URL = "https://www.fantasyfootballfix.com/reveal/"
TIMEZONE = zoneinfo.ZoneInfo("Asia/Singapore")
REPORT_TIME = datetime.time(hour=23, minute=20, second=0, tzinfo=TIMEZONE)

# --- Helper Functions (unchanged) ---
def escape_markdown(text: str) -> str:
    escape_chars = r'_*[]()~`>#+-=|{}.!'
    return ''.join(f'\\{char}' if char in escape_chars else char for char in text)

# --- UPDATED: Helper functions for the gameweek-aware state file ---
def load_state() -> dict:
    """Loads the state (gameweek and transfers) from the JSON file."""
    try:
        with open(STATE_FILE, 'r') as f:
            data = json.load(f)
            # Basic validation for the new structure
            if isinstance(data, dict) and 'gameweek' in data and 'transfers' in data:
                return data
            # If the file has the old list format, reset it
            logger.warning("Old state file format detected. Starting fresh.")
            return {"gameweek": None, "transfers": []}
    except (FileNotFoundError, json.JSONDecodeError):
        return {"gameweek": None, "transfers": []}

def save_state(gameweek: int, transfers: list) -> None:
    """Saves the current state to the JSON file."""
    state = {"gameweek": gameweek, "transfers": transfers}
    with open(STATE_FILE, 'w') as f:
        json.dump(state, f, indent=2)

# --- FPL Scraping Logic (unchanged) ---
# The login_to_fix and scrape_target_transfers functions are the same as before.
# I've omitted them here for brevity, but they should remain in your script.
def login_to_fix(session, email, password):
    # (This function remains the same as before)
    logger.info("Attempting to log in to Fantasy Football Fix...")
    headers = {"User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/108.0.0.0 Safari/537.36", "Referer": FIX_LOGIN_URL}
    try:
        res = session.get(FIX_LOGIN_URL, headers=headers)
        res.raise_for_status()
        soup = BeautifulSoup(res.text, 'html.parser')
        csrf_token_form = soup.find('input', {'name': 'csrfmiddlewaretoken'})['value']
        csrf_token_cookie = session.cookies.get('csrftoken')
        email_headers = headers.copy()
        email_headers.update({'Origin': FIX_ORIGIN,'Content-Type': 'application/x-www-form-urlencoded','X-CSRFToken': csrf_token_cookie})
        email_payload = {"email": email, "csrfmiddlewaretoken": csrf_token_form}
        res_email = session.post(FIX_LOGIN_URL, data=email_payload, headers=email_headers)
        res_email.raise_for_status()
        soup_pass = BeautifulSoup(res_email.text, 'html.parser')
        if not soup_pass.find('input', {'type': 'password'}):
            logger.error("Failed at email submission step. No password field found.")
            return False
        csrf_token_pass = soup_pass.find('input', {'name': 'csrfmiddlewaretoken'})['value']
        password_payload = {"password": password,"csrfmiddlewaretoken": csrf_token_pass,"email": email}
        res_pass = session.post(FIX_LOGIN_URL, data=password_payload, headers=email_headers)
        res_pass.raise_for_status()
        if "Logout" in res_pass.text or "My Account" in res_pass.text:
            logger.info("Fantasy Football Fix login successful!")
            return True
        else:
            logger.error("Login failed at password step. Check credentials.")
            return False
    except (requests.exceptions.RequestException, KeyError, AttributeError) as e:
        logger.error(f"An error occurred during login: {e}")
        return False

def scrape_target_transfers(session):
    # (This function remains the same as before)
    target_div_id = "team2"
    logger.info(f"Scraping transfers for manager from {TARGET_URL}")
    headers = {"User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/91.0.4472.124 Safari/537.36"}
    try:
        response = session.get(TARGET_URL, headers=headers)
        response.raise_for_status()
        soup = BeautifulSoup(response.text, 'html.parser')
        manager_section = soup.find('div', id=target_div_id)
        if not manager_section:
            logger.warning("Could not find the target manager's section on the page.")
            return [], None, None
        gameweek, active_chip, scraped_transfers = None, None, []
        gameweek_header = manager_section.find('h3', string=re.compile(r"Gameweek \d+"))
        if gameweek_header:
            match = re.search(r'Gameweek (\d+)', gameweek_header.text)
            if match: gameweek = match.group(1)
        active_chip_li = manager_section.find('li', class_='rchip--active')
        if active_chip_li:
            active_chip_span = active_chip_li.find('span', class_='rchip__chip')
            if active_chip_span: active_chip = active_chip_span.text.strip()
        transfers_ul = manager_section.find('ul', class_='rtransfers__ul')
        if transfers_ul:
            transfer_items = transfers_ul.find_all('li', class_='rtransfers__transfer')
            for item in transfer_items:
                player_divs = item.find_all('div', class_='rtransfers__player')
                if len(player_divs) == 2:
                    player_out_tag = player_divs[0].find('p', class_='rtransfers__name')
                    player_in_tag = player_divs[1].find('p', class_='rtransfers__name')
                    if player_out_tag and player_in_tag:
                        p_out = player_out_tag.text.strip()
                        p_in = player_in_tag.text.strip()
                        if "Default Player" not in p_out and "Default Player" not in p_in:
                             scraped_transfers.append([p_out, p_in]) # Use list for JSON
        return scraped_transfers, active_chip, gameweek
    except requests.exceptions.RequestException as e:
        logger.error(f"Error requesting target page: {e}")
        return [], None, None
    except Exception as e:
        logger.error(f"An unexpected error occurred during scraping: {e}", exc_info=True)
        return [], None, None


# --- REWRITTEN: Main logic now handles gameweek changes ---
def check_for_new_transfers(fix_email: str, fix_password: str) -> tuple[str, bool]:
    """
    Checks for new transfers, accounting for gameweek changes.
    Returns:
        (str): The message to be sent.
        (bool): True if a noteworthy update (new transfers) occurred.
    """
    with requests.Session() as s:
        if not login_to_fix(s, fix_email, fix_password):
            return "❌ *Login Failed*\nCould not log in to Fantasy Football Fix\.", False

        current_transfers, chip, current_gameweek_str = scrape_target_transfers(s)
        if current_gameweek_str is None:
            return "⚠️ *Scraping Error*\nCould not find the current gameweek\.", False
        
        current_gameweek = int(current_gameweek_str)
        saved_state = load_state()
        saved_gameweek = saved_state.get('gameweek')
        saved_transfers = saved_state.get('transfers', [])

        # Case 1: Gameweek has changed
        if current_gameweek != saved_gameweek:
            logger.info(f"Gameweek changed from {saved_gameweek} to {current_gameweek}. Resetting transfers.")
            save_state(current_gameweek, current_transfers)
            
            if not current_transfers:
                return f" Gameweek has updated to *GW {current_gameweek}*\. No transfers made yet\.", False
            else:
                message = [f"🚀 *First Transfers for GW {current_gameweek} Detected* 🚀\n"]
                escaped_chip = escape_markdown(chip or 'None')
                message.append(f"Chip Active: *{escaped_chip}*\n")
                for p_out, p_in in current_transfers:
                    message.append(f"🔴 OUT: `{escape_markdown(p_out)}`")
                    message.append(f"🟢 IN: `{escape_markdown(p_in)}`\n")
                return "\n".join(message), True

        # Case 2: Same gameweek, check for new transfers
        else:
            seen_set = {tuple(t) for t in saved_transfers}
            current_set = {tuple(t) for t in current_transfers}
            new_transfers = current_set - seen_set

            if not new_transfers:
                logger.info(f"No new transfers found for GW {current_gameweek}.")
                return f"✅ *No new transfers for GW {current_gameweek}* since the last check\.", False
            
            logger.info(f"Found {len(new_transfers)} new transfer(s) for GW {current_gameweek}.")
            save_state(current_gameweek, current_transfers)

            message = [f"🚨 *New Transfers Detected for GW {current_gameweek}* 🚨\n"]
            escaped_chip = escape_markdown(chip or 'None')
            message.append(f"Chip Active: *{escaped_chip}*\n")
            message.append("*New Transfers:*\n")
            for p_out, p_in in new_transfers:
                message.append(f"🔴 OUT: `{escape_markdown(p_out)}`")
                message.append(f"🟢 IN: `{escape_markdown(p_in)}`\n")
            
            return "\n".join(message), True

# --- Telegram Bot Logic (unchanged) ---
# The send_daily_report, check, start, and main functions are the same as before.
# They will work correctly with the new logic in check_for_new_transfers.
async def send_daily_report(context: ContextTypes.DEFAULT_TYPE) -> None:
    logger.info("Running scheduled daily report job...")
    fix_email = os.getenv("FIX_EMAIL")
    fix_password = os.getenv("FIX_PASSWORD")
    message, has_updates = check_for_new_transfers(fix_email, fix_password)
    if has_updates:
        await context.bot.send_message(chat_id=context.job.chat_id, text=message, parse_mode=ParseMode.MARKDOWN_V2)
    else:
        logger.info("Scheduled check complete. No new transfers, so no message sent.")

async def check(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    logger.info(f"Received /check command from chat_id: {update.effective_chat.id}")
    await update.message.reply_text("🔎 Checking for new transfers...")
    fix_email = os.getenv("FIX_EMAIL")
    fix_password = os.getenv("FIX_PASSWORD")
    result_message, _ = check_for_new_transfers(fix_email, fix_password)
    await update.message.reply_text(result_message, parse_mode=ParseMode.MARKDOWN_V2)

async def start(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    user_id = update.effective_user.id
    chat_id = update.effective_chat.id
    admin_id = os.getenv("ADMIN_USER_ID")
    if not admin_id:
        await update.message.reply_text("⚠️ **Admin Not Configured**\n The bot owner has not set the `ADMIN_USER_ID`\.")
        return
    if str(user_id) != admin_id:
        await update.message.reply_text("❌ **Unauthorized**\n You are not authorized to run this command\.")
        return
    current_jobs = context.job_queue.get_jobs_by_name(str(chat_id))
    if current_jobs:
        for job in current_jobs:
            job.schedule_removal()
        logger.info(f"Removed {len(current_jobs)} existing jobs for chat_id {chat_id}.")
    context.job_queue.run_daily(send_daily_report, time=REPORT_TIME, chat_id=chat_id, name=str(chat_id))
    time_str = REPORT_TIME.strftime("%H:%M")
    tz_str = REPORT_TIME.tzinfo
    await update.message.reply_text(
        f"✅ *Successfully Scheduled*\nI will send a daily transfer report to this chat every day at *{escape_markdown(time_str)} {tz_str}*\. I will only message you if there are new transfers\.",
        parse_mode=ParseMode.MARKDOWN_V2
    )

def main() -> None:
    load_dotenv()
    bot_token = os.getenv("TELEGRAM_BOT_TOKEN")
    if not bot_token:
        logger.critical("TELEGRAM_BOT_TOKEN environment variable is not set! Exiting.")
        return
    application = Application.builder().token(bot_token).build()
    application.add_handler(CommandHandler("start", start))
    application.add_handler(CommandHandler("check", check))
    logger.info("Bot is starting...")
    application.run_polling()

if __name__ == "__main__":
    main()