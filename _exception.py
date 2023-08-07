

class BotError(Exception):
    def __init__(self, *args: object) -> None:
        super().__init__(*args)


class AccessTokenError(BotError):
    pass


class AccessTokenExpiredError(AccessTokenError):
    pass


class BotOfflineError(BotError):
    pass


class BotBusyError(BotError):
    pass


class OpenAIError(BotError):
    def __init__(self, message: str, code: int = 0, *args: object) -> None:
        self.message = message
        self.code = code
        super().__init__(*args)

    def __str__(self) -> str:
        return f"code={self.code}, message={self.message!r}"
