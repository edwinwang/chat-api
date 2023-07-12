import os
import asyncio
import logging
import time
import random

from cryptography.fernet import Fernet
from limits import storage, strategies, RateLimitItemPerMinute, RateLimitItemPerHour
import jwt
from sqlalchemy import select
from OpenAIAuth import Auth0 as Auth

import models
from bot import AsyncBot
from _exception import OpenAIError

aes = Fernet(os.getenv('account_key'))
redis_storage = storage.RedisStorage(uri=os.getenv('redis_uri'))
moving_window = strategies.MovingWindowRateLimiter(redis_storage)
rate_limit_per_minute = RateLimitItemPerMinute(1)
rate_limit_per_hour = RateLimitItemPerHour(60)

logger = logging.getLogger("botmgr")


def decrypt(data):
    return aes.decrypt(data).decode('utf-8')


def encrypt(data: str):
    if not isinstance(data, bytes):
        data = data.encode()  # 默认编码是 utf-8
    return aes.encrypt(data)


async def test_limit(key):
    return await asyncio.to_thread(moving_window.test, rate_limit_per_hour, 'botmgr', key)


async def hit_limit(key):
    return await asyncio.to_thread(moving_window.hit, rate_limit_per_hour, 'botmgr', key)


class ApiBotManager:
    def __init__(self):
        '''
            accounts [{
                email: string
                password: string
                access_token: string
                puid: string
            }, {...}]
            apibot_pool [ApiBot]
        '''
        self.accounts = {}
        self.apibot_pool = {}
        self.wait_auth = {}
        self.wait_auth_lock = asyncio.Lock()

    async def init(self):
        await self.load_accounts()
        self.tasks = [
            asyncio.create_task(self.check_account_task()),
            asyncio.create_task(self.login_task()),
        ]

    async def close(self):
        for task in self.tasks:
            task.cancel()
        for bot in self.apibot_pool.values():
            await bot.close()

    async def load_accounts(self):
        async with models.Session() as session:
            stmt = select(models.Account).filter(models.Account.is_active == 1)
            accounts = (await session.execute(stmt)).scalars().all()
            for account in accounts:
                if not (account.email and account.password):
                    continue
                self.accounts[account.email] = dict(
                    email=account.email,
                    password=decrypt(account.password),
                    access_token=account.access_token,
                    puid=account.puid,
                )

    def token_expire(self, token):
        '''get token expire time'''
        try:
            return jwt.decode(token, options={"verify_signature": False})["exp"]
        except Exception as e:
            logger.warning(f"invalid token {e}")
            return 0

    async def update_account(self, email: str, token: str, puid: str = None):
        '''update token and puid for account'''
        async with models.Session() as session:
            account = self.accounts[email]
            stmt = select(models.Account).filter(models.Account.email == email)
            m_account = (await session.execute(stmt)).scalar_one_or_none()
            account['access_token'] = token
            account['puid'] = puid
            if m_account:
                m_account.access_token = token
                m_account.puid = puid
            self.update_bot(email, token, puid)
            await session.commit()

    async def login_task(self):
        '''
            登录账号 控制账号登陆频率，避免风控
        '''
        while True:
            try:
                async with self.wait_auth_lock:
                    if not self.wait_auth:
                        continue
                    for email, exp in self.wait_auth.items():
                        account = self.accounts.get(email, None)
                        if not account:
                            continue
                        if exp > 86400:
                            continue
                        else:
                            logger.debug(f'{account["email"]} query token')
                            auth = Auth(account["email"], account["password"])
                            access_token = await asyncio.to_thread(auth.get_access_token)
                            await self.update_account(email, access_token, "")
                        del self.wait_auth[email]
                        break
            except Exception as e:
                logger.error(f"login_task {e}")
            finally:
                await asyncio.sleep(random.randint(1, 5) * 60)

    async def check_account(self):
        async with self.wait_auth_lock:
            now = time.time()
            for account in self.accounts.values():
                access_token = account.get('access_token', None)
                email = account['email']
                if not access_token:
                    self.wait_auth[email] = 0
                    continue
                exp = self.token_expire(access_token)
                if exp - now < 60 * 60:
                    self.wait_auth[email] = exp - now
                    self.remove_bot(email)
                else:
                    self.wait_auth[email] = exp - now
                    self.update_bot(email, access_token, puid=account.get('puid', None))
            if self.wait_auth:
                self.wait_auth = dict(sorted(self.wait_auth.items(), key=lambda x: x[1]))

    async def check_account_task(self):
        '''定时检查账号状态'''
        while True:
            try:
                await self.check_account()
            except Exception as e:
                logger.error(f"check_account_task {e}")
            finally:
                await asyncio.sleep(60 * 60)

    async def add_account(self, email: str, password: str):
        async with models.Session() as session:
            stmt = select(models.Account).filter(models.Account.email == email)
            account = (await session.execute(stmt)).scalar_one_or_none()
            if not account:
                account = models.Account()
                account.email = email
                account.password = encrypt(password)
                account.is_active = 1
                session.add(account)
                self.accounts[email] = dict(
                    email=account.email,
                    password=password,
                    access_token=None,
                    puid=account.puid,
                )
                await session.commit()
                await self.check_account()

    def update_bot(self, email: str, access_token: str, puid: str = None):
        '''
        '''
        bot = self.apibot_pool.get(email, None)
        if bot:
            bot.update(access_token, puid)
        else:
            bot = AsyncBot(
                email=email,
                access_token=access_token,
                base_url=os.getenv("CHATGPT_BASE_URL"),
                puid=puid
            )
            self.apibot_pool[bot.email] = bot

    async def remove_bot(self, email):
        if email in self.apibot_pool:
            await self.apibot_pool[email].close()
            del self.apibot_pool[email]

    async def get_available_apibot(self, email: str = None):
        '''
            @userid str if userid provided, we need find the binded apibot
            @return apibot
        '''
        if email:
            bot = self.apibot_pool.pop(email, None)
            if not bot:
                logger.debug(f"{email} is offline")
                return None
            self.apibot_pool[email] = bot
            if await test_limit(email):
                return bot
            else:
                logger.info(f"{email} is busy")
                return None
        else:
            cnt = len(self.apibot_pool)
            while cnt > 0:
                cnt -= 1
                first = next(iter(self.apibot_pool))
                bot = self.apibot_pool.pop(first)
                self.apibot_pool[first] = bot
                if await test_limit(first):
                    return bot
            return None

    async def record_chat(self, email: str, openid: str, response: dict):
        if not openid:
            return
        assert email, "no email"
        conversation_id = response.get('conversation_id')
        parent_id = response.get('parent_id')
        async with models.Session() as session:
            user = await models.User.get_by_openid_with_session(session, openid)
            if not user:
                user = models.User(openid=openid, conversation_id=conversation_id)
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
                if user.conversation:
                    user.conversation.current_node = parent_id
                else:
                    conversation = models.Conversation(
                        conversation_id=conversation_id,
                        current_node=parent_id,
                        owner_email=email,
                        user_id=user.id
                    )
                    session.add(conversation)
            await session.commit()

    async def new_conversation(self, openid):
        async with models.Session() as session:
            user = await models.User.get_by_openid_with_session(session, openid)
            if user:
                user.conversation_id = ''
            await session.commit()

    async def work(self, func: dict, email: str = None, timeout: int = 60):
        retry = 0
        func_name = func.pop("name")
        while True:
            apibot = await self.get_available_apibot(email)
            if apibot is not None:
                logger.info(f"{apibot.email} working...")
                await hit_limit(apibot.email)
                try:
                    exec = getattr(apibot, func_name)
                    resp = None
                    async for resp in exec(**func):
                        resp = resp
                    resp["email"] = apibot.email
                    logger.info(f"{apibot.email} work done")
                except OpenAIError as e:
                    logger.error(f"{apibot.email} openai server error {e}")
                    if e.code == 404:
                        logger.warning(f"{apibot.email} conversation not found")
                        return False, "conversation_not_found"
                    elif e.code == 429:
                        logger.warning(f"{apibot.email} too many requests")
                        return False, "too_many_requests"
                    return False, "server_error"
                except Exception as e:
                    logger.error(f"{apibot.email} work failed {e}")
                    retry += 1
                    if retry > 3:
                        break
                    continue
                return resp, 'success'
            else:
                if timeout > 0:
                    logger.info("no bot available, wait...")
                    await asyncio.sleep(1)
                    timeout -= 1
                else:
                    return False, "timeout"
        return False, "max_retry"

    async def api_request(self, data):
        data["history_and_training_disabled"] = True
        parmas = {
            "name": "post_messages",
            **data
        }
        resp, reason = await self.work(parmas)
        resp.pop("email", None)
        if not resp:
            logger.error(reason)
        else:
            return resp.get("message", "")

    async def prompt(
        self,
        message: str,
        model: str = "text-davinci-002-render-sha",
        openid: str = None,
        new_chat: bool = False,
        timeout: int = 60
    ):
        message = message.strip()
        chat_info = None
        if openid and not new_chat:
            chat_info = await models.User.get_chat_info(openid)
        chat_info = chat_info or {}
        email = chat_info.get("email", None)
        converstation_id = chat_info.get("conversation_id", None)
        parent_id = chat_info.get("parent_id", None)
        func = dict(
            name="ask",
            prompt=message,
            conversation_id=converstation_id,
            parent_id=parent_id,
            model=model,
            auto_continue=True,
            history_and_training_disabled=True
        )
        resp, reason = await self.work(func, email, timeout)
        if resp:
            email = resp.pop("email", None) or email
            await self.record_chat(email, openid, resp)
            return resp.get("message", "")
        else:
            logger.error(reason)
            if openid and reason == "conversation_missing":
                await self.new_conversation(openid)
            return ""


if __name__ == "__main__":
    pass
