import asyncio
import logging
import sys
from logging import Logger, StreamHandler, DEBUG
from typing import Union
import threading
from uuid import uuid4
import time

class LogUtil(Logger):
    """Logging class
    """
    __FORMATTER = "%(asctime)s — %(name)s — %(levelname)s — %(module)s.%(funcName)s:%(lineno)d — %(message)s"

    def _init__(
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

    def _get_stream_handler(self) -> StreamHandler:
        handler = StreamHandler(sys.stdout)
        handler.setFormatter(self.formatter)
        return handler

    @staticmethod
    def create(log_level: str = 'DEBUG') -> Logger:
        logging.setLoggerClass(LogUtil)
        logger = logging.getLogger('AIO_AI')
        logger.setLevel(log_level)
        return logger


class Event_ts(asyncio.Event):
    def set(self):
        # FIXME: The _loop attribute is not documented as public API!
        self._loop.call_soon_threadsafe(super().set)

    def clear(self):
        # Setzt das Event zurück, sodass es erneut gewartet werden kann.
        self._loop.call_soon_threadsafe(super().clear)

    async def wait_for_event(self, timeout=None):
        try:
            await asyncio.wait_for(self.wait(), timeout)
            return True
        except asyncio.TimeoutError:
            return False


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

#    @property
#    def result(self):
#        return self.__result
#
#    @result.setter
#    def result(self, value):
#        self.__result = value

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

    def _init__(self, author_host, author, target, args, request_lock, timeout=None) -> None:
        self.__author_host = author_host
        self.__author = author  # Creator of the request
        self.__id = uuid4().hex  # Unique ID for each request
        self.__target = target  # Plugin and function name
        self.__request_lock = request_lock
        self.__args = args  #
        self.__collected = False
        self.__timeout = False
        self.__ready = False
        self.__error = False
        self.__result = None
        self.__created_at = time.time()
        self.__timeout_duration = timeout
        self.__lock = threading.Lock()
        self.__condition = threading.Condition(self.lock)  # Condition for waiting and notifying

    def set_result(self, result, error=False) -> None:
        """Sets the result of the request and notifies 
           waiting threads
        """
        with self.__condition:  # Acquire lock and notify waiting threads
            self.__result = result
            self.__error = error
            self.__ready = True
            self.__condition.notify_all()  # Notify all waiting threads

    def set_collected(self):
        self.__collected = True

    def get_result(self):
        with self.__request_lock:
            self.set_collected()
            if self.__error:
                raise Exception(self.__result)
            else:
                return self.__result

    def wait_for_result(self):
        """Waits until the result is made
        """
        with self.__condition:  # Acquire lock
            while not self.__ready and not self.__timeout:
                if self.__timeout_duration != None:
                    remaining_time = self.__timeout_duration - (time.time() - self.__created_at)
                    if remaining_time <= 0:
                        self.__timeout = True
                        break
                    self.__condition.wait(timeout=remaining_time)  # Wait for notification or timeout
                else:
                    self.__condition.wait()  # Wait for notification
        return self.__result, self.error, self.timeout

    
    