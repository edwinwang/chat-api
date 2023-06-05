from pydantic import BaseModel, Field
from uuid import uuid4

class ChatGPTAuthor(BaseModel):
    role: str

class ChatGPTContent(BaseModel):
    content_type: str
    parts: list[str]

class ChatGPTMessage(BaseModel):
    id: str
    author: ChatGPTAuthor
    content: ChatGPTContent

class ChatGPTRequest(BaseModel):
    action: str = "next"
    messages: list[ChatGPTMessage] = []
    parent_message_id: str = Field(default_factory=lambda: str(uuid4()))
    model: str = "text-davinci-002-render-sha"
    history_and_training_disabled: bool = True

    def add_message(self, role: str, content: str):
        self.messages.append(
            ChatGPTMessage(
                id=str(uuid4()),
                author=ChatGPTAuthor(role=role),
                content=ChatGPTContent(content_type="text", parts=[content])
            )
        )

class APIMessage(BaseModel):
    role: str
    content: str

class APIRequest(BaseModel):
    messages: list[APIMessage]
    stream: bool
    model: str

def convert_api_2_chatgpt(api_request: APIRequest) -> ChatGPTRequest:
    chatgpt_request = ChatGPTRequest()

    if api_request.model.startswith("gpt-4"):
        chatgpt_request.model = "gpt-4"
        if api_request.model in ["gpt-4-browsing", "gpt-4-plugins", "gpt-4-mobile", "gpt-4-code-interpreter"]:
            chatgpt_request.model = api_request.model

    for api_message in api_request.messages:
        if api_message.role == "system":
            api_message.role = "critic"
        chatgpt_request.add_message(api_message.role, api_message.content)

    return chatgpt_request


if __name__ == "__main__":
    request = ChatGPTRequest()
    request.add_message('system', 'Hello, world!')
    print(request.json())