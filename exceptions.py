class ConfigException(Exception):
    def __init__(self, message):
        self.message = message
        super().__init__(self.message)


class RequestException(Exception):
    def __init__(self, message):
        self.message = message
        super().__init__(self.message)


class NetworkRequestException(Exception):
    def __init__(self, message):
        self.message = message
        super().__init__(self.message)


class NodeException(Exception):
    def __init__(self, message):
        self.message = message
        super().__init__(self.message)


class PluginTypeMissmatchError(Exception):
    def __init__(self, message):
        self.message = message
        super().__init__(self.message)
