import base64
import os

from revChatGPT.V1 import Chatbot, logger

class ApiBot(Chatbot):
    #@logger(is_timed=True)
    def __init__(self, *args, **kwargs) -> None:
        assert 'email' in kwargs['config']
        email_encode = base64.b64encode(kwargs['config']['email'].encode('utf-8')).decode('utf-8')
        cache_path = os.path.join('cache', f'{email_encode}.json')
        kwargs['config']['cache_path'] = cache_path
        super().__init__(*args, **kwargs)


    def dump(self):
        return {
            "email": self.config["email"],
            "password": self.config["password"],
            "access_token": self.config.get("access_token", None),
        }

    def __check_conversations(self) -> None:
        if len(self.conversation_id_prev_queue) > 10:
            self.clear_conversations()
    
    def get_completion(self, message: str) -> str:
        self.__check_conversations()
        self.reset_chat()
        for data in self.ask(message, auto_continue=True):
            response = data["message"]
        return response


    @property
    def email(self) -> str:
        return self.config["email"]