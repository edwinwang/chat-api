from dotenv import load_dotenv; load_dotenv()  # noqa: E702
import os
import time
import logging
from enum import Enum
from fastapi import FastAPI, Depends, HTTPException, Header
from fastapi.responses import JSONResponse, Response
from fastapi.security import HTTPAuthorizationCredentials, HTTPBearer
from starlette.middleware.base import BaseHTTPMiddleware
from starlette.types import ASGIApp, Receive, Scope, Send
from starlette.requests import Request
from pydantic import BaseModel
from typing import Optional, Annotated
import yaml
import uvicorn

from botmgr import ApiBotManager
from api_convert import APIRequest, convert_api_2_chatgpt, new_chat_completion

logger = logging.getLogger()
logger.setLevel(logging.INFO)
handler = logging.StreamHandler()
format = logging.Formatter('%(asctime)s - %(name)s - %(levelname)s - %(message)s', datefmt='%Y-%m-%d %I:%M:%S %p')
handler.setFormatter(format)
logger.addHandler(handler)

logger = logging.getLogger("api")

app = FastAPI()
security = HTTPBearer()

bot_manager = ApiBotManager()

AUTH_TOKEN = os.getenv("auth_token")


class TimingMiddleware(BaseHTTPMiddleware):
    async def dispatch(self, request: Request, call_next):
        start_time = time.time()
        trace_id = request.headers.get("trace_id")
        logger.info(f"[{trace_id}] Processing request {request.url}")
        response = await call_next(request)

        process_time = time.time() - start_time
        logger.info(f"[{trace_id}] Request processed in {process_time} secs")

        return response


class CheckHostMiddleware:
    def __init__(self, app: ASGIApp, allowed_hosts: list[str]) -> None:
        self.app = app
        self.allowed_hosts = allowed_hosts

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope['type'] == 'http':
            host = None
            for key, value in scope['headers']:
                if key == b'host':
                    host = value.decode()
                    break

            if host not in self.allowed_hosts:
                await send({
                    'type': 'http.response.start',
                    'status': 403,
                    'headers': []
                })
                await send({
                    'type': 'http.response.body',
                    'body': b''
                })
            else:
                await self.app(scope, receive, send)
        else:
            await self.app(scope, receive, send)


app.add_middleware(TimingMiddleware)
app.add_middleware(CheckHostMiddleware, allowed_hosts=os.getenv('allowed_hosts', '').split(','))


def load_accounts():
    with open('accounts.yaml', 'r') as stream:
        try:
            return yaml.safe_load(stream)
        except yaml.YAMLError as exc:
            logger.critical(exc)


@app.get('/ping')
def ping():
    return 'pong'


def verify_access_token(credentials: HTTPAuthorizationCredentials = Depends(security)):
    if credentials.scheme != "Bearer":
        raise HTTPException(
            status_code=401, detail="Invalid authentication scheme."
        )
    token = credentials.credentials
    if token != AUTH_TOKEN:
        raise HTTPException(
            status_code=403, detail="Invalid access token."
        )
    return True


class Account(BaseModel):
    email: str
    password: str


@app.post('/admin/add_bot', dependencies=[Depends(verify_access_token)])
async def add_bot(account: Account):
    await bot_manager.add_account(account.email, account.password)
    return "success"


@app.options('/v1/chat/completions')
def options():
    headers = {
        'Access-Control-Allow-Origin': '*',
        'Access-Control-Allow-Methods': 'POST',
        'Access-Control-Allow-Headers': '*',
    }
    return JSONResponse(status_code=200, headers=headers)


@app.post('/v1/chat/completions', dependencies=[Depends(verify_access_token)])
async def completions(api_request: APIRequest):
    request = convert_api_2_chatgpt(api_request)
    resp = await bot_manager.api_request(request.dict())
    if not resp:
        raise HTTPException(status_code=404, detail="No response found")
    return JSONResponse(status_code=200, content=new_chat_completion(resp))


class ChatModel(str, Enum):
    gpt3 = "text-davinci-002-render-sha"
    gpt3_mobile = "text-davinci-002-render-sha-mobile"


class PromptRequest(BaseModel):
    content: str
    model: Optional[ChatModel] = ChatModel.gpt3
    openid: Optional[str] = None
    new_chat: Optional[bool] = False


@app.post('/v1/chat/prompt', dependencies=[Depends(verify_access_token)])
async def prompt(prompt: PromptRequest, accept: Annotated[str | None, Header()] = None):
    resp = await bot_manager.prompt(
        message=prompt.content,
        model=prompt.model.value,
        openid=prompt.openid,
        new_chat=prompt.new_chat
    )
    if not resp:
        raise HTTPException(status_code=404, detail="No response found")
    if accept == "application/json":
        return JSONResponse(status_code=200, content=resp)
    else:
        return Response(status_code=200, content=resp)


@app.on_event("startup")
async def startup_event():
    await bot_manager.init()


@app.on_event("shutdown")
async def shutdown_event():
    await bot_manager.close()

if __name__ == "__main__":
    uvicorn.run(
        app,
        # host="0.0.0.0",
        port=int(os.getenv("port") or 9000),
        loop='asyncio',
        # ssl_keyfile=os.getenv("ssl_keyfile"),
        # ssl_certfile=os.getenv("ssl_certfile"),
    )