import os
import json
import asyncio
import logging
import time
import random
from collections import deque
from functools import partial

from cryptography.fernet import Fernet
from chatbot import ApiBot
from typing import List, Union
from limits import storage, strategies, RateLimitItemPerMinute, RateLimitItemPerHour
from revChatGPT.typings import Error as RevChatGPTError

import models

key = 'w79zZ6C9UJdb6MGhgD9CWR-yA5JA-dRNUCLiGF_sPFQ='
aes = Fernet(key)
redis_storage = storage.RedisStorage(uri=os.getenv('redis_uri'))
moving_window = strategies.MovingWindowRateLimiter(redis_storage)
rate_limit_per_minute = RateLimitItemPerMinute(1)
rate_limit_per_hour = RateLimitItemPerHour(60)
moving_window.test(rate_limit_per_hour, 'botmgr', "122y")

logger = logging.getLogger(__name__)

def decrypt(data):
    return aes.decrypt(data).decode('utf-8')

def test_limit(key):
    return moving_window.test(rate_limit_per_hour, 'botmgr', key)

def hit_limit(key):
    return moving_window.hit(rate_limit_per_hour, 'botmgr', key)

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
            })
            time.sleep(random.uniform(3, 8))
            self.apibot_pool.append(apibot)
    
    def reset_bot(self, bot, e):
        for idx, apibot in enumerate(self.apibot_pool):
            if apibot.email == bot.email:
                logger.warn("reset bot %s, [%s] [%s]", bot.email, e.code, e.message)
                self.apibot_pool[idx] = ApiBot(config=apibot.dump())
                break

    def get_available_apibot(self, email:str=None):
        '''
            @userid str if userid provided, we need find the binded apibot
            @return apibot
        '''
        if email:
            for _, apibot in enumerate(self.apibot_pool):
                if apibot.email == email:
                    if test_limit(email):
                        return apibot
                    else:
                        logger.info(f"{email} is busy")
                        return None
        else:
            for _ in range(len(self.apibot_pool)):  # We will check each apibot at most once
                apibot = self.apibot_pool.popleft()  # Pop an apibot from the left
                if test_limit(apibot.email):
                    self.apibot_pool.append(apibot)  # If the apibot is available, append it back to the right
                    return apibot
                else:
                    self.apibot_pool.append(apibot)  # If the apibot is not available, still append it back to the right
            return None  # If we checked all apibots and none are available, return None

    async def record_chat(self, email: str, openid: str, response: dict):
        if not openid:
            return
        conversation_id = response.get('conversation_id')
        parent_id = response.get('parent_id')
        async with models.Session() as session:
            user = await models.User.get_by_openid_with_session(session, openid)
            if not user:
                user = models.User(openid=openid, conversation_id=response.get('conversation_id'))
                session.add(user)
                await session.flush()
                conversation = models.Conversation(
                    conversation_id=conversation_id,
                    current_node=parent_id,
                    owner_email=email,
                    user_id=user.id
                )
                session.add(conversation)
            else:
                user.conversation_id = conversation_id
                user.conversation.current_node = parent_id
            await session.commit()
    
    async def new_conversation(self, openid):
        with models.Session() as session:
            user = await models.User.get_by_openid_with_session(session, openid)
            if user:
                user.conversation_id = None
            await session.commit()

    async def get_completion(self, message: str, model: str=None, openid: str=None, new_chat: bool=False, timeout: int=60):
        message = message.strip()
        chat_info = None
        if openid and not new_chat:
            chat_info = await models.User.get_chat_info(openid)
        chat_info = chat_info or {}
        email = chat_info.get("email", None)
        converstation_id = chat_info.get("conversation_id", None)
        parent_id = chat_info.get("parent_id", None)
        retry = 0
        while True:
            apibot = self.get_available_apibot(email)
            if apibot is not None:
                logger.info(f"{apibot.email} working...")
                hit_limit(apibot.email)
                try:
                    func = partial(apibot.get_completion,
                        message = message,
                        conversation_id = converstation_id,
                        parent_id = parent_id,
                        model = model
                    )
                    resp = await asyncio.to_thread(func)
                    if resp:
                        await self.record_chat(apibot.email, openid, resp)
                    logger.info(f"{apibot.email} work done")
                except RevChatGPTError as e:
                    resp = json.loads(e.message)
                    openai_code = resp and resp.get("detail", {}).get("code", "")
                    if openai_code == "history_disabled_conversation_not_found":
                        logger.error("conversation mismatch for user [%s] [%s]", openid, converstation_id)
                        return None
                    if e.code == 429:
                        logger.error("[%s] rate limit", apibot.email)
                        return None
                    elif e.code == 400:
                        logger.warning("conversaion missing for user [%s] [%s]", openid, converstation_id)
                        if openid:
                            await self.new_conversation(openid)
                        return "会话丢失，请开启新的聊天"
                    self.reset_bot(apibot, e)
                    retry += 1
                    if retry > 3: break
                    continue
                return resp and resp["message"] or ""
            else:
                if timeout > 0:
                    logger.info("no bot available, wait...")
                    await asyncio.sleep(1)
                    timeout -= 1
                else:
                    return None


if __name__ == "__main__":
    print(aes.encrypt(b"123123"))
