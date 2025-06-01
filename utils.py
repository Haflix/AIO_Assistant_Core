import asyncio
import logging
import sys
from logging import Logger, StreamHandler, DEBUG
from typing import Union
from uuid import uuid4
import time
#from plugin_collection import PluginCollection



class LogUtil(Logger):
    """Logging class
    """
    __FORMATTER = "%(asctime)s — %(name)s — %(levelname)s — %(module)s.%(funcName)s:%(lineno)d — %(message)s"

    def __init__(
            self,
            name: str,
            log_format: str = __FORMATTER,
            level: Union[int, str] = DEBUG,
            *args,
            **kwargs
    ) -> None:
        super().__init__(name, level)
        self.formatter = logging.Formatter(log_format)
        self.addHandler(self.__get_stream_handler())

    def __get_stream_handler(self) -> StreamHandler:
        handler = StreamHandler(sys.stdout)
        handler.setFormatter(self.formatter)
        return handler

    @staticmethod
    def create(log_level: str = 'DEBUG') -> Logger:
        logging.setLoggerClass(LogUtil)
        logger = logging.getLogger('AIO_AI')
        logger.setLevel(log_level)
        return logger


class Plugin(object):
    """Base class that each plugin must inherit from. This class sets standard variables and functions that each plugin must have.
    """
    #from plugin_collection import PluginCollection
    def __init__(self, logger: Logger, plugin_collection_c):
        self.description = "UNKNOWN"
        self.plugin_name = "UNKNOWN"
        self.version = "0.0.0"
        self._logger = logger
        self._plugin_collection = plugin_collection_c
        self.asynced = False
        self.loop_running = False
        self.loop_req = False
        self.event_loop = asyncio.get_event_loop()
    
    def comm_layer(self, function, *args):
        """The method that makes it possible for synchronous and asynchronous methods to call methods from plugins
        """
        try:
            if self.asynced:
                if asyncio.iscoroutinefunction(function):  # Check if function is async
                    return self.event_loop.run_until_complete(function(*args))  # Now safe
                else:
                    self._logger.error(f"Function {function.__name__} is not async but called in async mode!")
                    return function(*args)  # Just call it normally
                
            else:
                # Sync plugin: Run async functions in the event loop
                if asyncio.iscoroutinefunction(function):
                    return self.event_loop.run_until_complete(function(*args))
                else:
                    return function(*args)
        except Exception as e:
            self._logger.error(f"Error in comm_layer: {e}")
            return None
    
    async def comm_layer_async(self, function, *args):
        """The method that makes it possible for synchronous and asynchronous methods to call methods from plugins
        """
        try:
            if self.asynced:
                if asyncio.iscoroutinefunction(function):  # Check if function is async
                    return self.event_loop.run_until_complete(function(*args))  # Now safe
                else:
                    self._logger.error(f"Function {function.__name__} is not async but called in async mode!")
                    return function(*args)  # Just call it normally
            else:
                return function(*args)
        except Exception as e:
            self._logger.error(f"Error in comm_layer: {e}")
            return None
    
    def loop_start(self):
        """The method that we expect all plugins, which need a loop, to implement. This is the
            method that starts the needed loops
        """
        raise NotImplementedError
    
    def loop(self):
        pass
    
    
    
    def perform_operation(self, argument):
        """The method that we expect all plugins to implement. This is the
            basic method
        """
        raise NotImplementedError

class Request:
    """Carries the information about a request and
    waits for result to be returned
    """
    
    @property
    def author_host(self):
        return self.__author_host

    @author_host.setter
    def author_host(self, value):
        self.__author_host = value

    @property
    def author(self):
        return self.__author

    @author.setter
    def author(self, value):
        self.__author = value

    @property
    def id(self):
        return self.__id

    @id.setter
    def id(self, value):
        self.__id = value

    @property
    def target(self):
        return self.__target

    @target.setter
    def target(self, value):
        self.__target = value

    @property
    def args(self):
        return self.__args

    @args.setter
    def args(self, value):
        self.__args = value

    @property
    def collected(self):
        return self.__collected

    @collected.setter
    def collected(self, value):
        self.__collected = value

    @property
    def timeout(self):
        return self.__timeout

    @timeout.setter
    def timeout(self, value):
        self.__timeout = value

    @property
    def ready(self):
        return self.__ready

    @ready.setter
    def ready(self, value):
        self.__ready = value

    @property
    def error(self):
        return self.__error

    @error.setter
    def error(self, value):
        self.__error = value

    @property
    def result(self):
        return self.__result

    @result.setter
    def result(self, value):
        self.__result = value

    @property
    def created_at(self):
        return self.__created_at

    @created_at.setter
    def created_at(self, value):
        self.__created_at = value

    @property
    def timeout_duration(self):
        return self.__timeout_duration

    @timeout_duration.setter
    def timeout_duration(self, value):
        self.__timeout_duration = value

    def __init__(self, author_host, author, target, args, timeout=None) -> None:
        self.__author_host = author_host
        self.__author = author  # Creator of the request
        self.__id = uuid4().hex  # Unique ID for each request
        self.__target = target  # Plugin and function name
        self.__args = args  #
        self.__collected = False
        self.__timeout = False
        self.__ready = False
        self.__error = False
        self.__result = None
        self.__created_at = time.time()
        self.__timeout_duration = timeout
        self._future = asyncio.get_event_loop().create_future()
        
#        self.__lock = threading.Lock()
#        self.__condition = threading.Condition(self.lock)  # Condition for waiting and notifying

    def set_result(self, result, error=False) -> None:
        """Sets the result of the request and marks it as collected."""
        if not self._future.done():
            self.error = error
            self.__result = result
            self._future.set_result((result, error, False))
            self.ready = True

#    def set_result(self, result, error=False) -> None:
#        """Sets the result of the request and notifies 
#           waiting threads
#        """
#        with self.__condition:  # Acquire lock and notify waiting threads
#            self.__result = result
#            self.__error = error
#            self.__ready = True
#            self.__condition.notify_all()  # Notify all waiting threads

    def set_collected(self):
        self.__collected = True

#    def get_result(self):
#        with self.__request_lock:
#            self.set_collected()
#            if self.__error:
#                raise Exception(self.__result)
#            else:
#                return self.__result

    def get_result_sync(self):
        """Synchronous wrapper to retrieve the result (for backward compatibility)."""
        loop = asyncio.get_event_loop()
        result, error, timed_out = loop.run_until_complete(self.wait_for_result_async())
        try:
            if error:
                raise Exception(self._result)
            return self._result
        finally:
            self.set_collected()

    async def wait_for_result_async(self):
        """Waits asynchronously until the result is available, with an optional timeout."""
        try:
            if self.timeout_duration and self.timeout_duration + self.created_at > time.time():
                result, error, timed_out = await asyncio.wait_for(self._future, timeout=self.timeout_duration)
            else:
                result, error, timed_out = await self._future
            return result, error, timed_out
        except asyncio.TimeoutError:
            self.__result = f"Error: The Request({self.id}) timed out"
            self.error = True
            self.ready = True
            self.timeout = True
            return self.__result, True, True

#    def wait_for_result(self):
#        """Waits until the result is made
#        """
#        with self.__condition:  # Acquire lock
#            while not self.__ready and not self.__timeout:
#                if self.__timeout_duration != None:
#                    remaining_time = self.__timeout_duration - (time.time() - self.__created_at)
#                    if remaining_time <= 0:
#                        self.__timeout = True
#                        break
#                    self.__condition.wait(timeout=remaining_time)  # Wait for notification or timeout
#                else:
#                    self.__condition.wait()  # Wait for notification
#        return self.__result, self.error, self.timeout

    
    