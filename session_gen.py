import asyncio
import os
from telethon import TelegramClient
from telethon.sessions import StringSession

API_ID = int(os.environ["TELEGRAM_API_ID"])
API_HASH = os.environ["TELEGRAM_API_HASH"]
PHONE = os.environ["TELEGRAM_PHONE"]

async def main():
    client = TelegramClient(StringSession(), API_ID, API_HASH)
    await client.connect()
    
    print("Sending code request...")
    await client.send_code_request(PHONE)
    
    code = input("Enter the Telegram code you received: ")
    await client.sign_in(PHONE, code)
    
    session_string = client.session.save()
    print(f"\n=== YOUR SESSION STRING ===\n{session_string}\n===========================\n")
    print("Copy the string above and save it as TELEGRAM_SESSION in Railway variables.")
    await client.disconnect()

asyncio.run(main())
