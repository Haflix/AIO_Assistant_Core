class ConfigException(Exception):
    def __init__(self, message):
        self.message = message
        super().__init__(self.message)


class RequestException(Exception):
    def __init__(self, message):
        self.message = message
        super().__init__(self.message)


class NetworkRequestException(RequestException):
    def __init__(self, message):
        self.message = message
        super().__init__(self.message)


# locked #13: typed exception for "no local subscriber matched" path on
# request_event / request_event_stream remote dispatch. Distinct from
# NetworkRequestException so caller fall-through can keep the strict
# semantics of locked #6.
class NoLocalSubException(RequestException):
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
