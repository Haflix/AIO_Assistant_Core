from utils import Plugin
from decorators import async_log_errors, log_errors


class NotifierPublisher(Plugin):
    """Test plugin that publishes to topics to test the notifier system."""

    @log_errors
    def on_load(self, *args, **kwargs):
        pass

    @async_log_errors
    async def on_enable(self):
        self._logger.info("NotifierPublisher enabled")

    @async_log_errors
    async def on_disable(self):
        pass

    @async_log_errors
    async def _test_notify(self, topic, args):
        """Fire-and-forget notify."""
        count = await self.notify(topic, args)
        return {"topic": topic, "subscribers_called": count}

    @async_log_errors
    async def _test_request(self, topic, args):
        """Request-by-topic with response."""
        result = await self.request_topic(topic, args)
        return {"topic": topic, "result": result}

    @async_log_errors
    async def _test_request_stream(self, topic, args):
        """Streaming request-by-topic, collects all chunks."""
        chunks = []
        async for chunk in self.request_topic_stream(topic, args):
            chunks.append(chunk)
        return {"topic": topic, "chunks": chunks}

    @async_log_errors
    async def _test_all(self):
        """Run a suite of notifier tests and return results."""
        results = {}

        # 1. Notify (fire-and-forget) — greet topic
        count = await self.notify("test/greet", "World")
        results["notify_greet"] = {"subscribers_called": count}

        # 2. Request-by-topic — greet (returns response)
        greet_result = await self.request_topic("test/greet", "Dusti")
        results["request_greet"] = greet_result

        # 3. Request-by-topic — wildcard math
        math_result = await self.request_topic("test/math/add", {"a": 3, "b": 7})
        results["request_math_wildcard"] = math_result

        # 4. Notify code-driven subscription
        code_count = await self.notify("test/code_event", {"data": {"key": "value"}})
        results["notify_code_event"] = {"subscribers_called": code_count}

        # 5. Streaming request-by-topic
        stream_chunks = []
        async for chunk in self.request_topic_stream("test/stream", {"n": 3}):
            stream_chunks.append(chunk)
        results["request_stream"] = {"chunks": stream_chunks}

        # 6. Get subscriber's event log to verify everything landed
        log = await self.execute("NotifierSubscriber", "get_log")
        results["subscriber_log"] = log

        return results
