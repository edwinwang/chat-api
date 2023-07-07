from pydantic import BaseModel, Field
from typing import Optional, Dict, Any
from uuid import uuid4
import json
import logging

logger = logging.getLogger("api")

api_prompt = """
Keep all response without formatting.I've provided a list of useful functions
that deliver real-time and trustworthy information.
Depending on the user's request, you can decide whether to call a function for additional data.
Please remember, that you can call multiple functions at once to obtain all the necessary information.
Always return a JSON object with the properties 'content', 'function_calls', and 'explanation'.
'content' is the answer displayed to the user;
if additional information is required via function calls, this should be null.
'function_calls' is a list if objects that specifies the function to be invoked,
using its name and arguments, arguments should be in json string format.
'explanation' is meant for debug information.
Function invocation is an intermediate step in answering user queries and is transparent to the user.
Please refrain from publishing content in 'content' until results are obtained.
"""

api_prompt = """
rules:
    1. Depending on the user's request, you can decide whether to call
        functions from functions list for additional data.
    2. If no functions are needed call, response user`s request based on your knowledge.
    3. Output should be a JSON object with follow properties:
        1) 'content' -- display to user, if not function is needed, this should not be null
        2) 'function_calls' -- list of functions and parameters
        3) 'explanation' -- debug info
        4) 'finish_reason' -- stop, length, function_call, content_filter.
    4. Avoid use markdown syntax or any line breaks in your responses.
    Example JSON object:
    {{
        "content": null,
        "function_calls": [{{"function_name": "get_weather", "arguments": "{{\"location\": \"Shanghai\"}}"}}, {{"function_name": "get_news", "arguments": "{{\"category\": \"technology\"}}"}}],
        "explanation": "Calling functions.",
        "finish_reason": "function_call"
    }}
functions: [{functions}]
"""  # noqa E501


class ChatGPTAuthor(BaseModel):
    role: str


class ChatGPTContent(BaseModel):
    content_type: str
    parts: list[str]


class ChatGPTMessage(BaseModel):
    id: str
    author: ChatGPTAuthor
    content: Optional[ChatGPTContent]


class ChatGPTRequest(BaseModel):
    action: str = "next"
    messages: list[ChatGPTMessage] = []
    conversation_id: str = None
    parent_message_id: str = Field(default_factory=lambda: str(uuid4()))
    model: str = "text-davinci-002-render-sha"
    history_and_training_disabled: bool = True

    def add_message(self, role: str, content: str):
        self.messages.append(
            ChatGPTMessage(
                id=str(uuid4()),
                author=ChatGPTAuthor(role=role),
                content=ChatGPTContent(content_type="text", parts=[content]),
            )
        )

    def add_functions(self, functions: list[str]):
        msg = api_prompt.format(functions=','.join(functions))
        self.messages.insert(
            0,
            ChatGPTMessage(
                id=str(uuid4()),
                author=ChatGPTAuthor(role="critic"),
                content=ChatGPTContent(content_type="text", parts=[msg]),
            )
        )


class APIMessage(BaseModel):
    role: str
    name: Optional[str]
    content: Optional[str]
    function_call: Optional[Dict[str, str]]
    function_calls: Optional[list[Dict[str, str]]]


class APIFunction(BaseModel):
    name: str
    description: str
    parameters: Dict[str, Any]


class APIRequest(BaseModel):
    messages: list[APIMessage]
    stream: Optional[bool]
    model: str
    functions: list[APIFunction]


def convert_api_2_chatgpt(api_request: APIRequest) -> ChatGPTRequest:
    chatgpt_request = ChatGPTRequest()

    if api_request.model.startswith("gpt-4"):
        chatgpt_request.model = "gpt-4"
        if api_request.model in ["gpt-4-browsing", "gpt-4-plugins", "gpt-4-mobile", "gpt-4-code-interpreter"]:
            chatgpt_request.model = api_request.model

    if api_request.functions:
        chatgpt_request.add_functions(
            [x.json() for x in api_request.functions]
        )
    for api_message in api_request.messages:
        if api_message.role == "system":
            api_message.role = "critic"
        elif api_message.role == "assistant":
            functions = []
            if api_message.function_call:
                if type(api_message.function_call) == list:
                    functions.extend(api_message.function_call)
                else:
                    functions.append(api_message.function_call)
            if api_message.function_calls:
                functions.extend(api_message.function_calls)
            api_message.content = json.dumps({"function_calls": functions})
        if api_message.role == "function":
            api_message.role = "critic"
            content = {
                "role": "function",
                "name": api_message.name,
                "response": api_message.content,
            }
            api_message.content = json.dumps(content)
        chatgpt_request.add_message(api_message.role, api_message.content)

    return chatgpt_request


def new_chat_completion(full_text: str) -> Dict:
    content = None
    function_call = None
    finish_reason = 'stop'
    if full_text and 'function_calls' in full_text and 'explanation' in full_text:
        try:
            data = json.loads(full_text)
            function_call = data['function_calls']
            finish_reason = data['finish_reason']
        except Exception as e:
            logger.warning(e)
            content = full_text
    else:
        content = full_text
    return {
        "id": "chatcmpl-QXlha2FBbmROaXhpZUFyZUF3ZXNvbWUK",
        "object": "chat.completion",
        "created": 0,
        "model": "gpt-3.5",
        "usage": {
            "prompt_tokens": 0,
            "completion_tokens": 0,
            "total_tokens": 0,
        },
        "choices": [
            {
                "message": {
                    "content": content,
                    "function_call": function_call,
                    "role": "assistant",
                },
                "index": 0,
                "finish_reason": finish_reason
            },
        ],
    }


if __name__ == "__main__":
    request = ChatGPTRequest()
    request.add_message('system', 'Hello, world!')
    print(request.json())
