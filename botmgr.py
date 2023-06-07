import asyncio
import logging
from collections import deque

from cryptography.fernet import Fernet
from chatbot import ApiBot
from typing import List
from limits import storage, strategies, RateLimitItemPerMinute
from revChatGPT.typings import Error as RevChatGPTError

key = 'w79zZ6C9UJdb6MGhgD9CWR-yA5JA-dRNUCLiGF_sPFQ='
aes = Fernet(key)
memory_storage = storage.MemoryStorage()
moving_window = strategies.MovingWindowRateLimiter(memory_storage)
rate_limit_per_minute = RateLimitItemPerMinute(1)

logger = logging.getLogger(__name__)

def decrypt(data):
    return aes.decrypt(data).decode('utf-8')

def test_limit(key):
    return moving_window.test(rate_limit_per_minute, 'botmgr', key)

def hit_limit(key):
    return moving_window.hit(rate_limit_per_minute, 'botmgr', key)

class ApiBotManager:
    def __init__(self):
        self.apibot_pool = deque()

    def load_accounts(self, accounts: List[dict]):
        for account in accounts:
            email = account["email"]
            passwd = account["passwd"]
            logger.info(f"{email} log in")
            apibot = ApiBot(config={
                "email": email,
                "password": decrypt(passwd),
                # "model": "text-davinci-002-render-sha-mobile",
            })
            apibot.clear_conversations()
            self.apibot_pool.append(apibot)
    
    def reset_bot(self, bot, e):
        for idx, apibot in enumerate(self.apibot_pool):
            if apibot.email == bot.email:
                logger.warn("reset bot %s, [%s]", bot.email, e.code)
                self.apibot_pool[idx] = ApiBot(config=apibot.dump())
                self.apibot_pool[idx].clear_conversations()
                break

    def get_available_apibot(self):
        for _ in range(len(self.apibot_pool)):  # We will check each apibot at most once
            apibot = self.apibot_pool.popleft()  # Pop an apibot from the left
            if test_limit(apibot.email):
                self.apibot_pool.append(apibot)  # If the apibot is available, append it back to the right
                return apibot
            else:
                self.apibot_pool.append(apibot)  # If the apibot is not available, still append it back to the right
        return None  # If we checked all apibots and none are available, return None

    async def get_completion(self, message, timeout=60):
        message = message.strip()
        retry = 0
        while True:
            apibot = self.get_available_apibot()
            if apibot is not None:
                logger.info(f"{apibot.email} working...")
                hit_limit(apibot.email)
                try:
                    resp = await asyncio.to_thread(apibot.get_completion, message)
                    logger.info(f"{apibot.email} work done")
                except RevChatGPTError as e:
                    self.reset_bot(apibot, e)
                    retry += 1
                    if retry > 3: break
                    continue
                return resp
            else:
                if timeout > 0:
                    logger.info("wait...")
                    await asyncio.sleep(1)
                    timeout -= 1
                else:
                    return None


if __name__ == "__main__":
    print(aes.encrypt(b"123123"))
