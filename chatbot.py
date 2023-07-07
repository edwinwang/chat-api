import base64
import os


from revChatGPT.V1 import Chatbot


class ApiBot(Chatbot):
    def __init__(self, *args, **kwargs) -> None:
        assert 'email' in kwargs['config']
        email_encode = base64.b64encode(kwargs['config']['email'].encode('utf-8')).decode('utf-8')
        cache_path = os.path.join('cache', f'{email_encode}.json')
        kwargs['config']['cache_path'] = cache_path
        super().__init__(*args, **kwargs)
    
    @property
    def disable_history(self):
        return True
    
    @disable_history.setter
    def disable_history(self, value):
        pass

    def api_request(self, data) -> str:
        resp = {}
        for part in self._Chatbot__send_request(data, auto_continue=True):
            resp = part
        return resp
    
    def prompt(self, message: str, conversation_id: str=None, parent_id: str=None, model: str=None) -> str:
        """Get a completion from ChatGPT
        Args:
            message (str): the prompt to send
            conversation_id (str): _id of the conversation, used to continue on. Defaults to None.
            parent_id (str): _id of the previous response, used to continue on. Defaults to None.
            model (str, optional): "text-davinci-002-render-sha" or "text-davinci-002-render-sha-mobile". Defaults to None.

        Returns:
            dict {
                "author": str,
                "message": str,
                "conversation_id": str,
                "parent_id": str,
                "model": str,
                "finish_details": str, # "max_tokens" or "stop"
                "end_turn": bool,
            }
        """
        resp = {}
        for data in self.ask(message, auto_continue=True, model=model, conversation_id=conversation_id, parent_id=parent_id):
            resp = data
        return resp


    @property
    def email(self) -> str:
        return self.config["email"]
    
    def remove_access_token(self):
        self.config["access_token"] = None
