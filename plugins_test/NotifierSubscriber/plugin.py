from utils import Plugin
from decorators import async_log_errors, async_gen_log_errors, log_errors
import asyncio


class NotifierSubscriber(Plugin):
    """Test plugin that subscribes to topics via config and code."""

    @log_errors
    def on_load(self, *args, **kwargs):
        self.event_log = []

    @async_log_errors
    async def on_enable(self):
        # Code-driven subscription (in addition to config-driven ones)
        self._code_sub_id = await self.subscribe(
            "test/code_event", self._handle_code_event
        )
        self._logger.info(
            f"NotifierSubscriber enabled, code subscription: {self._code_sub_id}"
        )

    @async_log_errors
    async def on_disable(self):
        if hasattr(self, "_code_sub_id"):
            await self.unsubscribe(self._code_sub_id)

    # -- Config-driven handlers --

    @async_log_errors
    async def _handle_greet(self, name):
        self._logger.info(f"Greet handler called with: {name}")
        self.event_log.append(("greet", name))
        return f"Hello, {name}!"

    @async_log_errors
    async def _handle_math(self, a, b):
        self._logger.info(f"Math handler called with: a={a}, b={b}")
        self.event_log.append(("math", a, b))
        return a + b

    @async_gen_log_errors
    async def _stream_count(self, n):
        self._logger.info(f"Stream handler called with: n={n}")
        for i in range(n):
            await asyncio.sleep(0.05)
            yield f"item_{i}"
        self.event_log.append(("stream", n))

    # -- Code-driven handler --

    async def _handle_code_event(self, data):
        self._logger.info(f"Code event handler called with: {data}")
        self.event_log.append(("code_event", data))
        return {"handled": True, "data": data}

    # -- Utility --

    @async_log_errors
    async def _get_log(self):
        return list(self.event_log)
