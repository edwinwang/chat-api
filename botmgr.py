import asyncio

from cryptography.fernet import Fernet
from chatbot import ApiBot
from typing import List
from limits import storage, strategies, RateLimitItemPerMinute

key = 'w79zZ6C9UJdb6MGhgD9CWR-yA5JA-dRNUCLiGF_sPFQ='
aes = Fernet(key)
memory_storage = storage.MemoryStorage()
moving_window = strategies.MovingWindowRateLimiter(memory_storage)
rate_limit_per_minute = RateLimitItemPerMinute(1)

def decrypt(data):
    return aes.decrypt(data).decode('utf-8')

def test_limit(key):
    return moving_window.test(rate_limit_per_minute, 'botmgr', key)

def hit_limit(key):
    return moving_window.hit(rate_limit_per_minute, 'botmgr', key)

class ApiBotManager:
    def __init__(self):
        self.apibot_pool = []

    def load_accounts(self, accounts: List[dict]):
        for account in accounts:
            email = account["email"]
            passwd = account["passwd"]
            apibot = ApiBot(config={
                "email": email,
                "password": decrypt(passwd),
                # "model": "text-davinci-002-render-sha-mobile",
            })
            self.apibot_pool.append(apibot)

    def get_available_apibot(self):
        for apibot in self.apibot_pool:
            if test_limit(apibot.email):
                return apibot
        return None

    async def get_completion(self, message, timeout=60):
        message = message.strip()
        while True:
            apibot = self.get_available_apibot()
            if apibot is not None:
                resp = await asyncio.to_thread(apibot.get_completion, message)
                hit_limit(apibot.email)
                return resp
            else:
                if timeout > 0:
                    await asyncio.sleep(1)
                    timeout -= 1
                else:
                    return None
