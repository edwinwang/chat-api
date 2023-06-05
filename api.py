import os, time
import logging
from fastapi import FastAPI, Depends, HTTPException
from fastapi.responses import JSONResponse, Response
from fastapi.security import HTTPAuthorizationCredentials, HTTPBearer
from starlette.middleware.base import BaseHTTPMiddleware
from starlette.requests import Request
from pydantic import BaseModel
import yaml
import uvicorn
from dotenv import load_dotenv
load_dotenv(override=True)

from botmgr import ApiBotManager
from api_convert import APIRequest, convert_api_2_chatgpt, new_chat_completion


app = FastAPI()
security = HTTPBearer()

bot_manager = ApiBotManager()

AUTH_TOKEN = os.getenv("auth_token")

class TimingMiddleware(BaseHTTPMiddleware):
    async def dispatch(self, request: Request, call_next):
        start_time = time.time()
        trace_id = request.headers.get("trace_id")
        logging.info(f"[{trace_id}] Processing request {request.url}")
        response = await call_next(request)

        process_time = time.time() - start_time
        logging.info(f"[{trace_id}] Request processed in {process_time} secs")

        return response

app.add_middleware(TimingMiddleware)

def setup_logger():
    logger = logging.getLogger()
    logger.setLevel(logging.INFO)
    handler = logging.StreamHandler()
    format = logging.Formatter('%(asctime)s - %(name)s - %(levelname)s - %(message)s', datefmt='%Y-%m-%d %I:%M:%S %p')
    handler.setFormatter(format)
    logger.addHandler(handler)

def load_accounts():
    with open('accounts.yaml', 'r') as stream:
        try:
            return yaml.safe_load(stream)
        except yaml.YAMLError as exc:
            print(exc)

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


@app.options('/v1/chat/completions')
def options():
    headers = {
        'Access-Control-Allow-Origin': '*',
        'Access-Control-Allow-Methods': 'POST',
        'Access-Control-Allow-Headers': '*',
    }
    return JSONResponse(status_code=200, headers=headers)


@app.post('/v1/chat/completions')
async def completions(api_request: APIRequest):
    prompt = convert_api_2_chatgpt(api_request)
    resp = await bot_manager.get_completion(prompt.json())
    if not resp:
        raise HTTPException(status_code=404, detail="No response found")
    return JSONResponse(status_code=200, content=new_chat_completion(resp))

class Message(BaseModel):
    content: str

@app.post('/v1/chat/prompt', dependencies=[Depends(verify_access_token)])
async def completions(prompt: Message):
    resp = await bot_manager.get_completion(prompt.content)
    if not resp:
        raise HTTPException(status_code=404, detail="No response found")
    return Response(status_code=200, content=resp)

@app.on_event("startup")
def startup_event():
    global bot_manager
    

if __name__ == "__main__":
    bot_manager.load_accounts(
        load_accounts()
    )
    setup_logger()
    uvicorn.run(
        app,
        host="0.0.0.0",
        port=int(os.getenv("port") or 9000),
        loop='asyncio',
        ssl_keyfile=os.getenv("ssl_keyfile"),
        ssl_certfile=os.getenv("ssl_certfile"),
    )