from utils import Plugin
from decorators import log_errors, async_log_errors


class InteropCaller(Plugin):
    """Caller plugin that runs an interop matrix against InteropTarget."""

    @log_errors
    def on_load(self, *args, **kwargs):
        self.plugin_name = "InteropCaller"
        self.version = "0.0.1"
        self.description = (
            "Runs sync/async and generator interop tests against InteropTarget"
        )

    @async_log_errors
    async def on_enable(self):
        self.enabled = True
        self._logger.debug("InteropCaller.on_enable")

    @async_log_errors
    async def on_disable(self):
        self.enabled = False
        self._logger.debug("InteropCaller.on_disable")

    async def _log_case(self, case_id: str, passed: bool, detail: str = ""):
        level = self._logger.info if passed else self._logger.error
        status = "PASS" if passed else "FAIL"
        if detail:
            level(f"[{case_id}] {status} - {detail}")
        else:
            level(f"[{case_id}] {status}")

    @async_log_errors
    async def run_suite(self, host: str = "any"):
        """Run the async interop test matrix. Returns 'OK' or 'FAIL (N failed)'."""
        failures = 0

        # ---- Value calls ----
        try:
            # async -> async (tuple)
            res = await self.execute("InteropTarget", "it_async_add", (2, 3), host=host)
            ok = res == 5
            await self._log_case(
                "val_async_to_async_tuple", ok, f"got={res} expected=5"
            )
            failures += 0 if ok else 1
        except Exception as e:
            await self._log_case("val_async_to_async_tuple", False, str(e))
            failures += 1

        # NOTE: Do not call execute_sync from an async method

        try:
            # async -> sync (kwargs)
            res = await self.execute(
                "InteropTarget", "it_sync_add", {"a": 7, "b": 8}, host=host
            )
            ok = res == 15
            await self._log_case(
                "val_async_to_sync_kwargs", ok, f"got={res} expected=15"
            )
            failures += 0 if ok else 1
        except Exception as e:
            await self._log_case("val_async_to_sync_kwargs", False, str(e))
            failures += 1

        # NOTE: Do not call execute_sync from an async method

        try:
            # single-arg async calls (should use default b=1)
            res1 = await self.execute("InteropTarget", "it_sync_add", 5, host=host)
            res2 = await self.execute("InteropTarget", "it_async_add", 6, host=host)
            ok = (res1 == 6) and (res2 == 7)
            await self._log_case(
                "val_single_arg_async", ok, f"got=({res1},{res2}) expected=(6,7)"
            )
            failures += 0 if ok else 1
        except Exception as e:
            await self._log_case("val_single_arg_async", False, str(e))
            failures += 1

        # ---- Streaming calls ----
        try:
            # async stream of async-gen
            items = []
            async for it in self.execute_stream(
                "InteropTarget", "it_async_gen", {"n": 3, "prefix": "ag"}, host=host
            ):
                items.append(it)
            ok = items == ["ag0", "ag1", "ag2"]
            await self._log_case(
                "stream_asyncgen_async", ok, f"got={items} expected=['ag0','ag1','ag2']"
            )
            failures += 0 if ok else 1
        except Exception as e:
            await self._log_case("stream_asyncgen_async", False, str(e))
            failures += 1

        try:
            # async stream of sync-gen
            items = []
            async for it in self.execute_stream(
                "InteropTarget", "it_sync_gen", {"n": 3, "prefix": "g"}, host=host
            ):
                items.append(it)
            ok = items == ["g0", "g1", "g2"]
            await self._log_case(
                "stream_syngen_async", ok, f"got={items} expected=['g0','g1','g2']"
            )
            failures += 0 if ok else 1
        except Exception as e:
            await self._log_case("stream_syngen_async", False, str(e))
            failures += 1

        # NOTE: Do not use execute_stream_sync from an async method

        return "OK" if failures == 0 else f"FAIL ({failures} failed)"

    def _log_case_sync(self, case_id: str, passed: bool, detail: str = ""):
        level = self._logger.info if passed else self._logger.error
        status = "PASS" if passed else "FAIL"
        if detail:
            level(f"[{case_id}] {status} - {detail}")
        else:
            level(f"[{case_id}] {status}")

    @log_errors
    def run_suite_sync(self, host: str = "any"):
        """Run the sync interop test matrix using execute_sync/execute_stream_sync."""
        failures = 0

        # ---- Value calls (sync context) ----
        try:
            res = self.execute_sync("InteropTarget", "it_async_add", (4, 6), host=host)
            ok = res == 10
            self._log_case_sync("val_sync_to_async_tuple", ok, f"got={res} expected=10")
            failures += 0 if ok else 1
        except Exception as e:
            self._log_case_sync("val_sync_to_async_tuple", False, str(e))
            failures += 1

        try:
            res = self.execute_sync(
                "InteropTarget", "it_sync_add", {"a": 9, "b": 1}, host=host
            )
            ok = res == 10
            self._log_case_sync("val_sync_to_sync_kwargs", ok, f"got={res} expected=10")
            failures += 0 if ok else 1
        except Exception as e:
            self._log_case_sync("val_sync_to_sync_kwargs", False, str(e))
            failures += 1

        try:
            res1 = self.execute_sync("InteropTarget", "it_sync_add", 5, host=host)
            res2 = self.execute_sync("InteropTarget", "it_async_add", 6, host=host)
            ok = (res1 == 6) and (res2 == 7)
            self._log_case_sync(
                "val_single_arg_sync", ok, f"got=({res1},{res2}) expected=(6,7)"
            )
            failures += 0 if ok else 1
        except Exception as e:
            self._log_case_sync("val_single_arg_sync", False, str(e))
            failures += 1

        # ---- Streaming calls (sync context) ----
        try:
            items = []
            for it in self.execute_stream_sync(
                "InteropTarget", "it_async_gen", {"n": 2, "prefix": "ax"}, host=host
            ):
                items.append(it)
            ok = items == ["ax0", "ax1"]
            self._log_case_sync(
                "stream_asyncgen_sync", ok, f"got={items} expected=['ax0','ax1']"
            )
            failures += 0 if ok else 1
        except Exception as e:
            self._log_case_sync("stream_asyncgen_sync", False, str(e))
            failures += 1

        try:
            items = []
            for it in self.execute_stream_sync(
                "InteropTarget", "it_sync_gen", (2, "sx"), host=host
            ):
                items.append(it)
            ok = items == ["sx0", "sx1"]
            self._log_case_sync(
                "stream_syngen_sync", ok, f"got={items} expected=['sx0','sx1']"
            )
            failures += 0 if ok else 1
        except Exception as e:
            self._log_case_sync("stream_syngen_sync", False, str(e))
            failures += 1

        return "OK" if failures == 0 else f"FAIL ({failures} failed)"
