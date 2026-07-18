import asyncio
import logging
from telethon import TelegramClient
from vip_bot.config import load_config
from vip_bot.db_store import PaymentStore
from vip_bot.helpers import send_log
from vip_bot.loops import polling_loop, broadcast_loop
from vip_bot.handlers import register_handlers

LOGGER = logging.getLogger("telegram_vip_bot")

async def start_bot():
    config = load_config()
    store = PaymentStore(config)
    client = TelegramClient("vip_bot", config.api_id, config.api_hash)
    qris_semaphore = asyncio.Semaphore(config.qris_create_concurrency)
    user_locks = {}
    withdrawal_states = {}
    
    register_handlers(client, config, store, qris_semaphore, user_locks, withdrawal_states)
    
    await client.start(bot_token=config.bot_token)
    await send_log(client, config, store, "<b>VIP bot started</b>")
    LOGGER.info("VIP bot started")
    asyncio.create_task(polling_loop(client, config, store))
    asyncio.create_task(broadcast_loop(client, config, store))
    await client.run_until_disconnected()

def run():
    try:
        asyncio.run(start_bot())
    except KeyboardInterrupt:
        pass
