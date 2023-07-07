import time
import logging
from typing import Generator, Callable, AsyncGenerator
import json
import uuid
import os
import base64
import sys
import subprocess

import httpx
import jwt
import tempfile
from pathlib import Path
# from OpenAIAuth import Auth0 as Authenticator

from _exception import (
    BotError,
    AccessTokenError,
    AccessTokenExpiredError,
    OpenAIError,
)

bot_logger = logging.getLogger("bot")
CAPTCHA_URL = os.getenv("CAPTCHA_URL", "https://bypass.churchless.tech/captcha/")


class Bot:
    def __init__(
        self,
        email: str,
        access_token: str,
        proxy: str = None,
        puid: str = None,
        base_url: str = "https://bypass.churchless.tech/",
        **kwargs,
    ):
        """Init a chatbot

        Args:
            email (str): chatgpt account
            token (str): chatgpt token
            proxy (str, optional): proxy for requests. Defaults to None.
            puid (str, optional): plus user id. Defaults to None.
            base_url (str, optional): . Defaults to https://bypass.churchless.tech/.
        """
        self.email = email
        self.access_token = access_token
        self.puid = puid
        self.check_access_token()

        self.headers = {
            "Accept": "text/event-stream",
            "Authorization": f"Bearer {access_token}",
            "Content-Type": "application/json",
            "User-Agent": (
                "Mozilla/5.0 (X11; Linux x86_64) "
                "AppleWebKit/537.36 (KHTML, like Gecko) "
                "Chrome/113.0.0.0 Safari/537.36"
            ),
        }
        if self.puid:
            self.headers.update({"PUID": self.puid})
        self.proxies = {"http": proxy, "https": proxy} if proxy else None

        self.httpx_cli = httpx.Client(
            base_url=base_url,
            proxies=self.proxies,
            headers=self.headers,
            http2=True,
        )

        self.support_models = []

    def close(self):
        self.httpx_cli.close()

    def check_access_token(self):
        try:
            token = jwt.decode(self.access_token, options={"verify_signature": False})
        except Exception as e:
            bot_logger.error(f"invalid_token {e}")
            raise AccessTokenError("invalid_token")
        if token["exp"] < time.time():
            raise AccessTokenExpiredError("token_expired")

    def update(self, token: str, puid: str = None):
        if token and self.access_token != token:
            self.access_token = token
            self.headers.update({"Authorization": f"Bearer {token}"})
        if puid and self.puid != puid:
            self.puid = puid
            self.headers.update({"PUID": puid})

    def captcha_solver(self, images: list[str], challenge_details: dict) -> int:
        # Create tempfile
        with tempfile.TemporaryDirectory() as tempdir:
            filenames: list[Path] = []

            for idx, image in enumerate(images):
                filename = Path(tempdir, f"{idx}.jpeg")
                with open(filename, "wb") as f:
                    f.write(base64.b64decode(image))
                print(f"Saved captcha image to {filename}")
                # If MacOS, open the image
                if sys.platform == "darwin":
                    subprocess.call(["open", filename])
                if sys.platform == "linux":
                    subprocess.call(["xdg-open", filename])
                if sys.platform == "win32":
                    os.startfile(filename)
                filenames.append(filename)

            print(f'Captcha instructions: {challenge_details.get("instructions")}')
            print(
                "Developer instructions: The captcha images have an index starting from 0 from left to right",
            )
            print(
                "Enter the index of the images that matches the captcha instructions:"
            )
            index = int(input())

            return index

    def get_arkose_token(
        self,
        download_images: bool = True,
        solver: Callable = captcha_solver,
    ) -> str:
        """
        The solver function should take in a list of images in base64 and a dict of challenge details
        and return the index of the image that matches the challenge details

        Challenge details:
            game_type: str - Audio or Image
            instructions: str - Instructions for the captcha
            URLs: list[str] - URLs of the images or audio files
        """
        resp = httpx.get(
            (CAPTCHA_URL + "start?download_images=true")
            if download_images
            else CAPTCHA_URL + "start",
        )
        resp_json: dict = resp.json()
        if resp.status_code == 200:
            return resp_json.get("token")
        if resp.status_code != 511:
            raise Exception(resp_json.get("error", "Unknown error"))

        if resp_json.get("status") != "captcha":
            raise Exception("unknown error")

        challenge_details: dict = resp_json.get("session", {}).get("concise_challenge")
        if not challenge_details:
            raise Exception("missing details")

        images: list[str] = resp_json.get("images")

        index = solver(images, challenge_details)

        resp = httpx.post(
            CAPTCHA_URL + "verify",
            json={"session": resp_json.get("session"), "index": index},
        )
        if resp.status_code != 200:
            raise Exception("Failed to verify captcha")
        return resp_json.get("token")

    def __check_response(self, response: httpx.Response) -> None:
        """Make sure response is success

        Args:
            response (_type_): _description_

        Raises:
            Error: _description_
        """
        try:
            response.raise_for_status()
        except httpx.HTTPStatusError as e:
            raise OpenAIError(
                response.text,
                response.status_code,
                e
            )

    def check_fields(self, data: dict) -> bool:
        try:
            data["message"]["content"]
        except KeyError:
            return False
        return True

    def __send_request(
        self,
        data: dict,
        auto_continue: bool = False,
        timeout: float = 360,
    ) -> Generator[dict, None, None]:
        """Start a conversation
        Args:
            data (dict): message send to chatgpt
            auto_continue (bool, optional): Auto finish the conversation. Defaults to False.
        data example:
            {
                "action": "next",
                "messages": [
                    {
                    "id": "aaa21205-c2b5-4483-8f12-e654f0cc97f4",
                    "author": {
                        "role": "user"
                    },
                    "content": {
                        "content_type": "text",
                        "parts": [
                        "are u ok?"
                        ]
                    },
                    "metadata": {}
                    }
                ],
                "conversation_id": "5655d909-c218-4f2e-9dbb-4683fec27630",
                "parent_message_id": "a45633e7-1496-47a3-8aa1-d13d10168916",
                "model": "text-davinci-002-render-sha",
                "timezone_offset_min": -480,
                "history_and_training_disabled": false,
                "arkose_token": null,
                "supports_modapi": false
            }
        """
        bot_logger.debug("Sending the payload")
        if data.get("model", "").startswith("gpt-4"):
            try:
                data["arkose_token"] = self.get_arkose_token()
            except Exception as e:
                bot_logger.error(e)

        t1 = time.time()
        with self.httpx_cli.stream(
                method="POST",
                url="conversation",
                json=data,
                timeout=timeout,
        ) as response:
            cid = data.get("conversation_id", "")
            bot_logger.info(
                "Request conversation %s took %.2f seconds. Action %s",
                cid,
                time.time() - t1,
                data["action"],
            )

            self.__check_response(response)

            finish_details = None
            for line in response.iter_lines():
                # remove b' and ' at the beginning and end and ignore case
                if line.lower() == "internal server error":
                    bot_logger.error(f"Internal Server Error: {line}")
                    raise OpenAIError("internal_server_error")
                if not line or line is None:
                    continue
                if "data: " in line:
                    line = line[6:]
                if line == "[DONE]":
                    break
                try:
                    line = json.loads(line)
                except json.decoder.JSONDecodeError as e:
                    bot_logger.warning(f"Decode response failed: {e}")
                    continue
                if not self.check_fields(line):
                    bot_logger.error(f"Invalid response: {line}")
                    raise OpenAIError("invalid_response")
                if line.get("message").get("author").get("role") != "assistant":
                    continue
                cid = line["conversation_id"]
                pid = line["message"]["id"]
                metadata = line["message"].get("metadata", {})
                message_exists = False
                author = {}
                if line.get("message"):
                    author = metadata.get("author", {}) or line["message"].get("author", {})
                    if line["message"].get("content"):
                        if line["message"]["content"].get("parts"):
                            if len(line["message"]["content"]["parts"]) > 0:
                                message_exists = True
                message: str = (
                    line["message"]["content"]["parts"][0] if message_exists else ""
                )
                model = metadata.get("model_slug", None)
                finish_details = metadata.get("finish_details", {"type": None})["type"]
                yield {
                    "author": author,
                    "message": message,
                    "conversation_id": cid,
                    "parent_id": pid,
                    "model": model,
                    "finish_details": finish_details,
                    "end_turn": line["message"].get("end_turn", True),
                    "recipient": line["message"].get("recipient", "all"),
                    "citations": metadata.get("citations", []),
                }

            if not (auto_continue and finish_details == "max_tokens"):
                return
            message = message.strip("\n")
            for i in self.continue_write(
                conversation_id=cid,
                model=model,
                timeout=timeout,
                auto_continue=True,
                history_and_training_disabled=data["history_and_training_disabled"],
            ):
                i["message"] = message + i["message"]
                yield i

    def post_messages(
        self,
        messages: list[dict],
        conversation_id: str | None = None,
        parent_id: str | None = None,
        plugin_ids: list = [],
        model: str | None = None,
        auto_continue: bool = False,
        timeout: float = 360,
        **kwargs,
    ) -> Generator[dict, None, None]:
        """Ask a question to the chatbot with a list of structured messages
        Args:
            messages (list[dict]): The messages to send
                messages example:
                    [
                        {
                            "id": "aaa21205-c2b5-4483-8f12-e654f0cc97f4",
                            "author": {
                                "role": "user"
                            },
                            "content": {
                                "content_type": "text",
                                "parts": [
                                    "are u ok?"
                                ]
                            },
                            "metadata": {}
                        }
                    ]
            conversation_id (str | None, optional): UUID for the conversation to continue on. Defaults to None.
            parent_id (str | None, optional): UUID for the message to continue on. Defaults to None.
            model (str | None, optional): The model to use. Defaults to None.
            auto_continue (bool, optional): Whether to continue the conversation automatically. Defaults to False.
            timeout (float, optional): Timeout for getting the full response, unit is second. Defaults to 360.

        Yields: Generator[dict, None, None] - The response from the chatbot
            dict: {
                "message": str,
                "conversation_id": str,
                "parent_id": str,
                "model": str,
                "finish_details": str, # "max_tokens" or "stop"
                "end_turn": bool,
                "recipient": str,
                "citations": list[dict],
            }
        """
        if bool(parent_id) != bool(conversation_id):
            raise TypeError(
                "Both 'conversation_id' and 'parent_id' must be set or empty simultaneously"
            )
        if not conversation_id and not parent_id:
            parent_id = str(uuid.uuid4())
        model = model or "text-davinci-002-render-sha"
        data = {
            "action": "next",
            "messages": messages,
            "conversation_id": conversation_id,
            "parent_message_id": parent_id,
            "model": model,
            "history_and_training_disabled": kwargs.get(
                "history_and_training_disabled", False
            ),
        }
        plugin_ids = plugin_ids
        if len(plugin_ids) > 0 and not conversation_id:
            data["plugin_ids"] = plugin_ids

        yield from self.__send_request(
            data,
            auto_continue=auto_continue,
            timeout=timeout,
        )

    def ask(
        self,
        prompt: str,
        conversation_id: str | None = None,
        parent_id: str = "",
        model: str = "",
        plugin_ids: list = [],
        auto_continue: bool = False,
        timeout: float = 360,
        **kwargs,
    ) -> Generator[dict, None, None]:
        """Ask a question to the chatbot with a prompt
        Args:
            prompt (str): The question
            conversation_id (str, optional): UUID for the conversation to continue on. Defaults to None.
            parent_id (str, optional): UUID for the message to continue on. Defaults to "".
            model (str, optional): The model to use. Defaults to "".
            auto_continue (bool, optional): Whether to continue the conversation automatically. Defaults to False.
            timeout (float, optional): Timeout for getting the full response, unit is second. Defaults to 360.

        Yields: The response from the chatbot
            dict: {
                "message": str,
                "conversation_id": str,
                "parent_id": str,
                "model": str,
                "finish_details": str, # "max_tokens" or "stop"
                "end_turn": bool,
                "recipient": str,
            }
        """
        messages = [
            {
                "id": str(uuid.uuid4()),
                "author": {"role": "user"},
                "content": {"content_type": "text", "parts": [prompt]},
                "metadata": {},
            },
        ]
        if bool(parent_id) != bool(conversation_id):
            raise TypeError(
                "Both 'conversation_id' and 'parent_id' must be set or empty simultaneously"
            )
        parent_id = parent_id or str(uuid.uuid4())
        model = model or "text-davinci-002-render-sha"
        data = {
            "action": "next",
            "messages": messages,
            "conversation_id": conversation_id,
            "parent_message_id": parent_id,
            "model": model,
            "history_and_training_disabled": kwargs.get(
                "history_and_training_disabled", False
            ),
        }
        if len(plugin_ids) > 0 and not conversation_id:
            data["plugin_ids"] = plugin_ids

        yield from self.__send_request(
            data,
            auto_continue=auto_continue,
            timeout=timeout,
        )

    def continue_write(
        self,
        conversation_id: str,
        parent_id: str,
        model: str,
        auto_continue: bool = False,
        history_and_training_disabled: bool = False,
        timeout: float = 360,
    ) -> Generator[dict, None, None]:
        """call chatgpt to finish the conversation

        Args:
            conversation_id (str): the conversation id which you want to continue
            parent_id (str): the parent message id
            model (str): the model the conversation last used
            auto_continue (bool, optional): . Defaults to False.
            timeout (float, optional): http request timeout. Defaults to 360.

        Yields:
            Generator[dict, None, None]: _description_
        """
        data = {
            "action": "continue",
            "conversation_id": conversation_id,
            "parent_message_id": parent_id,
            "model": model,
            "history_and_training_disabled": history_and_training_disabled,
        }
        yield from self.__send_request(
            data,
            timeout=timeout,
            auto_continue=auto_continue,
        )

    def get_conversations(
        self,
        offset: int = 0,
        limit: int = 20,
        encoding: str | None = None,
    ) -> list:
        """
        Get conversations
        :param offset: Integer
        :param limit: Integer
        """
        url = f"/conversations?offset={offset}&limit={limit}"
        response = self.httpx_cli.get(url)
        self.__check_response(response)
        if encoding is not None:
            response.encoding = encoding
        data = json.loads(response.text)
        return data["items"]

    def get_msg_history(self, convo_id: str, encoding: str | None = None) -> list:
        """
        Get message history
        :param id: UUID of conversation
        :param encoding: String
        """
        url = f"/conversation/{convo_id}"
        response = self.httpx_cli.get(url)
        self.__check_response(response)
        if encoding is not None:
            response.encoding = encoding
        return response.json()

    def share_conversation(
        self,
        convo_id: str,
        node_id: str,
        title: str = None,
        anonymous: bool = True,
    ) -> str:
        """
        Creates a share link to a conversation
        :param convo_id: UUID of conversation
        :param node_id: UUID of node
        :param anonymous: Boolean
        :param title: String

        Returns:
            str: A URL to the shared link
        """
        convo_id = convo_id
        node_id = node_id
        headers = {
            "Content-Type": "application/json",
            "origin": "https://chat.openai.com",
            "referer": f"https://chat.openai.com/c/{convo_id}",
        }
        # First create the share
        payload = {
            "conversation_id": convo_id,
            "current_node_id": node_id,
            "is_anonymous": anonymous,
        }
        url = "/share/create"
        response = self.httpx_cli.post(url, data=json.dumps(payload), headers=headers)
        self.__check_response(response)
        resp = response.json()
        share_url = resp.get("share_url")
        # Then patch the share to make public
        share_id = resp.get("share_id")
        url = f"/share/{share_id}"
        payload = {
            "share_id": share_id,
            "highlighted_message_id": node_id,
            "title": title or resp.get("title", "New chat"),
            "is_public": True,
            "is_visible": True,
            "is_anonymous": True,
        }
        response = self.httpx_cli.patch(url, data=json.dumps(payload), headers=headers)
        self.__check_response(response)
        return share_url

    def gen_title(self, convo_id: str, message_id: str) -> str:
        """
        Generate title for conversation
        :param id: UUID of conversation
        :param message_id: UUID of message
        """
        response = self.httpx_cli.post(
            url=f"/conversation/gen_title/{convo_id}",
            data=json.dumps(
                {"message_id": message_id, "model": "text-davinci-002-render"},
            ),
        )
        self.__check_response(response)
        return response.json().get("title", "Error generating title")

    def change_title(self, convo_id: str, title: str) -> None:
        """
        Change title of conversation
        :param id: UUID of conversation
        :param title: String
        """
        url = f"/conversation/{convo_id}"
        response = self.httpx_cli.patch(url, data=json.dumps({"title": title}))
        self.__check_response(response)

    def delete_conversation(self, convo_id: str) -> None:
        """
        Delete conversation
        :param id: UUID of conversation
        """
        url = f"/conversation/{convo_id}"
        response = self.httpx_cli.patch(url, data='{"is_visible": false}')
        self.__check_response(response)

    def clear_conversations(self) -> None:
        """
        Delete all conversations
        """
        url = "/conversations"
        response = self.httpx_cli.patch(url, data='{"is_visible": false}')
        self.__check_response(response)

    def get_plugins(self, offset: int = 0, limit: int = 250, status: str = "approved"):
        """
        Get plugins
        :param offset: Integer. Offset (Only supports 0)
        :param limit: Integer. Limit (Only below 250)
        :param status: String. Status of plugin (approved)
        """
        url = f"/aip/p?offset={offset}&limit={limit}&statuses={status}"
        response = self.httpx_cli.get(url)
        self.__check_response(response)
        # Parse as JSON
        return json.loads(response.text)

    def install_plugin(self, plugin_id: str):
        """
        Install plugin by ID
        :param plugin_id: String. ID of plugin
        """
        url = f"/aip/p/{plugin_id}/user-settings"
        payload = {"is_installed": True}

        response = self.httpx_cli.patch(url, data=json.dumps(payload))
        self.__check_response(response)

    def get_unverified_plugin(self, domain: str, install: bool = True) -> dict:
        """
        Get unverified plugin by domain
        :param domain: String. Domain of plugin
        :param install: Boolean. Install plugin if found
        """
        url = f"/aip/p/domain?domain={domain}"
        response = self.httpx_cli.get(url)
        self.__check_response(response)
        if install:
            self.install_plugin(response.json().get("id"))
        return response.json()


class AsyncBot(Bot):
    def __init__(
        self,
        email: str,
        access_token: str,
        proxy: str = None,
        puid: str = None,
        base_url: str = "https://bypass.churchless.tech/",
        **kwargs,
    ):
        super().__init__(email, access_token, proxy, puid, base_url, **kwargs)
        self.httpx_cli = httpx.AsyncClient(
            base_url=base_url,
            headers=self.headers,
            proxies=self.proxies,
        )

    async def close(self):
        await self.httpx_cli.aclose()

    async def __send_request(
        self,
        data: dict,
        auto_continue: bool = False,
        timeout: float = 360,
    ) -> AsyncGenerator[dict, None]:
        """Start a conversation
        Args:
            data (dict): message send to chatgpt
            auto_continue (bool, optional): Auto finish the conversation. Defaults to False.
        data example:
            {
                "action": "next",
                "messages": [
                    {
                    "id": "aaa21205-c2b5-4483-8f12-e654f0cc97f4",
                    "author": {
                        "role": "user"
                    },
                    "content": {
                        "content_type": "text",
                        "parts": [
                        "are u ok?"
                        ]
                    },
                    "metadata": {}
                    }
                ],
                "conversation_id": "5655d909-c218-4f2e-9dbb-4683fec27630",
                "parent_message_id": "a45633e7-1496-47a3-8aa1-d13d10168916",
                "model": "text-davinci-002-render-sha",
                "timezone_offset_min": -480,
                "history_and_training_disabled": false,
                "arkose_token": null,
                "supports_modapi": false
            }
        """
        bot_logger.debug("Sending the payload")
        if not self.support_models:
            await self.models()
        model = data.get("model", "")
        if model and model not in self.support_models:
            raise BotError(f"unsupported_model:{model}. Supported models: {self.support_models}")
        if data.get("model", "").startswith("gpt-4"):
            try:
                data["arkose_token"] = self.get_arkose_token()
            except Exception as e:
                bot_logger.error(e)

        t1 = time.time()
        async with self.httpx_cli.stream(
                method="POST",
                url="conversation",
                json=data,
                timeout=timeout,
        ) as response:
            cid = data.get("conversation_id", "")
            bot_logger.info(
                "Request conversation %s took %.2f seconds. Action %s",
                cid,
                time.time() - t1,
                data["action"],
            )

            await self.__check_response(response)

            finish_details = None
            async for line in response.aiter_lines():
                bot_logger.debug(line)
                # remove b' and ' at the beginning and end and ignore case
                if line.lower() == "internal server error":
                    bot_logger.error(f"Internal Server Error: {line}")
                    raise OpenAIError("internal_server_error")
                if not line or line is None:
                    continue
                if "data: " in line:
                    line = line[6:]
                if line == "[DONE]":
                    break
                try:
                    line = json.loads(line)
                except json.decoder.JSONDecodeError as e:
                    bot_logger.warning(f"Decode response failed: {e}")
                    continue
                if not self.check_fields(line):
                    bot_logger.error(f"Invalid response: {line}")
                    raise OpenAIError("invalid_response")
                if line.get("message").get("author").get("role") != "assistant":
                    continue
                cid = line["conversation_id"]
                pid = line["message"]["id"]
                metadata = line["message"].get("metadata", {})
                message_exists = False
                author = {}
                if line.get("message"):
                    author = metadata.get("author", {}) or line["message"].get("author", {})
                    if line["message"].get("content"):
                        if line["message"]["content"].get("parts"):
                            if len(line["message"]["content"]["parts"]) > 0:
                                message_exists = True
                message: str = (
                    line["message"]["content"]["parts"][0] if message_exists else ""
                )
                model = metadata.get("model_slug", None)
                finish_details = metadata.get("finish_details", {"type": None})["type"]
                yield {
                    "author": author,
                    "message": message,
                    "conversation_id": cid,
                    "parent_id": pid,
                    "model": model,
                    "finish_details": finish_details,
                    "end_turn": line["message"].get("end_turn", True),
                    "recipient": line["message"].get("recipient", "all"),
                    "citations": metadata.get("citations", []),
                }

            if not (auto_continue and finish_details == "max_tokens"):
                return
            message = message.strip("\n")
            async for i in self.continue_write(
                conversation_id=cid,
                model=model,
                timeout=timeout,
                auto_continue=True,
                history_and_training_disabled=data["history_and_training_disabled"],
            ):
                i["message"] = message + i["message"]
                yield i

    async def post_messages(
        self,
        messages: list[dict],
        conversation_id: str | None = None,
        parent_id: str | None = None,
        plugin_ids: list = [],
        model: str | None = None,
        auto_continue: bool = False,
        timeout: float = 360,
        **kwargs,
    ) -> AsyncGenerator[dict, None]:
        """Ask a question to the chatbot with a list of structured messages
        Args:
            messages (list[dict]): The messages to send
                messages example:
                    [
                        {
                            "id": "aaa21205-c2b5-4483-8f12-e654f0cc97f4",
                            "author": {
                                "role": "user"
                            },
                            "content": {
                                "content_type": "text",
                                "parts": [
                                    "are u ok?"
                                ]
                            },
                            "metadata": {}
                        }
                    ]
            conversation_id (str | None, optional): UUID for the conversation to continue on. Defaults to None.
            parent_id (str | None, optional): UUID for the message to continue on. Defaults to None.
            model (str | None, optional): The model to use. Defaults to None.
            auto_continue (bool, optional): Whether to continue the conversation automatically. Defaults to False.
            timeout (float, optional): Timeout for getting the full response, unit is second. Defaults to 360.

        Yields: AsyncGenerator[dict, None] - The response from the chatbot
            dict: {
                "message": str,
                "conversation_id": str,
                "parent_id": str,
                "model": str,
                "finish_details": str, # "max_tokens" or "stop"
                "end_turn": bool,
                "recipient": str,
                "citations": list[dict],
            }
        """
        if bool(parent_id) != bool(conversation_id):
            raise TypeError(
                "Both 'conversation_id' and 'parent_id' must be set or empty simultaneously"
            )
        if not conversation_id and not parent_id:
            parent_id = str(uuid.uuid4())
        model = model or "text-davinci-002-render-sha"
        data = {
            "action": "next",
            "messages": messages,
            "conversation_id": conversation_id,
            "parent_message_id": parent_id,
            "model": model,
            "history_and_training_disabled": kwargs.get(
                "history_and_training_disabled", False
            ),
        }
        plugin_ids = plugin_ids
        if len(plugin_ids) > 0 and not conversation_id:
            data["plugin_ids"] = plugin_ids

        async for msg in self.__send_request(
            data,
            auto_continue=auto_continue,
            timeout=timeout,
        ):
            yield msg

    async def ask(
        self,
        prompt: str,
        conversation_id: str = None,
        parent_id: str = "",
        model: str = "",
        plugin_ids: list = [],
        auto_continue: bool = False,
        timeout: float = 360,
        **kwargs,
    ) -> AsyncGenerator[dict, None]:
        """Ask a question to the chatbot with a prompt
        Args:
            prompt (str): The question
            conversation_id (str, optional): UUID for the conversation to continue on. Defaults to None.
            parent_id (str, optional): UUID for the message to continue on. Defaults to "".
            model (str, optional): The model to use. Defaults to "".
            auto_continue (bool, optional): Whether to continue the conversation automatically. Defaults to False.
            timeout (float, optional): Timeout for getting the full response, unit is second. Defaults to 360.

        Yields: The response from the chatbot
            dict: {
                "message": str,
                "conversation_id": str,
                "parent_id": str,
                "model": str,
                "finish_details": str, # "max_tokens" or "stop"
                "end_turn": bool,
                "recipient": str,
            }
        """
        messages = [
            {
                "id": str(uuid.uuid4()),
                "author": {"role": "user"},
                "content": {"content_type": "text", "parts": [prompt]},
                "metadata": {},
            },
        ]
        if bool(parent_id) != bool(conversation_id):
            raise TypeError(
                "Both 'conversation_id' and 'parent_id' must be set or empty simultaneously"
            )
        parent_id = parent_id or str(uuid.uuid4())
        model = model or "text-davinci-002-render-sha"
        data = {
            "action": "next",
            "messages": messages,
            "conversation_id": conversation_id,
            "parent_message_id": parent_id,
            "model": model,
            "history_and_training_disabled": kwargs.get(
                "history_and_training_disabled", False
            ),
        }
        if len(plugin_ids) > 0 and not conversation_id:
            data["plugin_ids"] = plugin_ids

        async for msg in self.__send_request(
            data,
            auto_continue=auto_continue,
            timeout=timeout,
        ):
            yield msg

    async def continue_write(
        self,
        conversation_id: str,
        parent_id: str,
        model: str,
        auto_continue: bool = False,
        history_and_training_disabled: bool = False,
        timeout: float = 360,
    ) -> AsyncGenerator[dict, None]:
        """call chatgpt to finish the conversation

        Args:
            conversation_id (str): the conversation id which you want to continue
            parent_id (str): the parent message id
            model (str): the model the conversation last used
            auto_continue (bool, optional): . Defaults to False.
            timeout (float, optional): http request timeout. Defaults to 360.

        Yields:
            Generator[dict, None]: _description_
        """
        data = {
            "action": "continue",
            "conversation_id": conversation_id,
            "parent_message_id": parent_id,
            "model": model,
            "history_and_training_disabled": history_and_training_disabled,
        }
        async for msg in self.__send_request(
            data,
            timeout=timeout,
            auto_continue=auto_continue,
        ):
            yield msg

    async def get_conversations(self, offset: int = 0, limit: int = 20) -> list:
        """
        Get conversations
        :param offset: Integer
        :param limit: Integer
        """
        url = f"conversations?offset={offset}&limit={limit}"
        response = await self.httpx_cli.get(url)
        await self.__check_response(response)
        data = json.loads(response.text)
        return data["items"]

    async def get_msg_history(
        self,
        convo_id: str,
        encoding: str | None = "utf-8",
    ) -> dict:
        """
        Get message history
        :param id: UUID of conversation
        """
        url = f"conversation/{convo_id}"
        response = await self.httpx_cli.get(url)
        if encoding is not None:
            response.encoding = encoding
            await self.__check_response(response)
            return json.loads(response.text)
        return None

    async def share_conversation(
        self,
        title: str = None,
        convo_id: str = None,
        node_id: str = None,
        anonymous: bool = True,
    ) -> str:
        """
        Creates a share link to a conversation
        :param convo_id: UUID of conversation
        :param node_id: UUID of node

        Returns:
            str: A URL to the shared link
        """
        convo_id = convo_id or self.conversation_id
        node_id = node_id or self.parent_id
        # First create the share
        payload = {
            "conversation_id": convo_id,
            "current_node_id": node_id,
            "is_anonymous": anonymous,
        }
        url = "share/create"
        response = await self.httpx_cli.post(
            url,
            data=json.dumps(payload),
        )
        await self.__check_response(response)
        share_url = response.json().get("share_url")
        # Then patch the share to make public
        share_id = response.json().get("share_id")
        url = f"share/{share_id}"
        payload = {
            "share_id": share_id,
            "highlighted_message_id": node_id,
            "title": title or response.json().get("title", "New chat"),
            "is_public": True,
            "is_visible": True,
            "is_anonymous": True,
        }
        response = await self.httpx_cli.patch(
            url,
            data=json.dumps(payload),
        )
        await self.__check_response(response)
        return share_url

    async def gen_title(self, convo_id: str, message_id: str) -> None:
        """
        Generate title for conversation
        """
        url = f"conversation/gen_title/{convo_id}"
        response = await self.httpx_cli.post(
            url,
            data=json.dumps(
                {"message_id": message_id, "model": "text-davinci-002-render"},
            ),
        )
        await self.__check_response(response)

    async def change_title(self, convo_id: str, title: str) -> None:
        """
        Change title of conversation
        :param convo_id: UUID of conversation
        :param title: String
        """
        url = f"conversation/{convo_id}"
        response = await self.httpx_cli.patch(url, data=f'{{"title": "{title}"}}')
        await self.__check_response(response)

    async def delete_conversation(self, convo_id: str) -> None:
        """
        Delete conversation
        :param convo_id: UUID of conversation
        """
        url = f"conversation/{convo_id}"
        response = await self.httpx_cli.patch(url, data='{"is_visible": false}')
        await self.__check_response(response)

    async def clear_conversations(self) -> None:
        """
        Delete all conversations
        """
        url = "conversations"
        response = await self.httpx_cli.patch(url, data='{"is_visible": false}')
        await self.__check_response(response)

    async def __check_response(self, response: httpx.Response) -> None:
        try:
            response.raise_for_status()
        except httpx.HTTPStatusError as ex:
            await response.aread()
            error = OpenAIError(
                response.text,
                response.status_code,
                ex
            )
            raise error

    async def models(self):
        url = "models?history_and_training_disabled=false"
        response = await self.httpx_cli.get(url)
        await self.__check_response(response)
        self.support_models = []
        for model in response.json()["models"]:
            self.support_models.append(model["slug"])
        puid = response.cookies.get("_puid", "")
        if puid:
            self.puid = puid
        return response


# async def bot_run(bot):
#     async for resp in bot.ask(
#         """ 翻译一下内容到中文：
#         """,
#         conversation_id='32ca4d45-9313-4849-8da7-b33f6851db0e',
#         parent_id='35eb04ba-93f9-4370-9a12-dba2bf8c68f8',
#         history_and_training_disabled=True
#     ):
#         # print(resp)
#         pass
async def bot_run(bot):
    async for resp in bot.continue_write(
        conversation_id='32ca4d45-9313-4849-8da7-b33f6851db0e',
        parent_id='0008dd8d-7060-479c-b934-b34481bb82ac',
        model='text-davinci-002-render-sha',
        history_and_training_disabled=True
    ):
        pass

if __name__ == "__main__":
    pass
