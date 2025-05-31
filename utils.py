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
    
    def __init__(self, author_host, author, target, args, timeout=30) -> None:
        self.author_host = author_host
        self.author = author    # Creator of the request
        self.id = uuid4().hex   # Unique ID for each request
        self.target = target    # Plugin and function name
        self.args = args        #
        self.collected = False
        self.timeout = False
        self.ready = False
        self.error = False
        self.result = None
        self.created_at = time.time()
        self.timeout_duration = timeout
        self.lock = threading.Lock()
        self.condition = threading.Condition(self.lock)  # Condition for waiting and notifying
        

    def set_result(self, result, error=False) -> None:
        """Sets the result of the request and notifies 
           waiting threads
        """
        with self.condition:  # Acquire lock and notify waiting threads
            self.result = result
            self.error = error
            self.ready = True
            self.condition.notify_all()  # Notify all waiting threads
            
    
    def set_collected(self):
        self.collected = True
        
    def get_result(self):
        self.set_collected()
        return self.result
    
    def wait_for_result(self):
        """Waits until the result is made
        """
        with self.condition:  # Acquire lock
            while not self.ready and not self.timeout:
                if self.timeout_duration != None:
                    remaining_time = self.timeout_duration - (time.time() - self.created_at)
                    if remaining_time <= 0:
                        self.timeout = True
                        break
                    self.condition.wait(timeout=remaining_time)  # Wait for notification or timeout
                else:
                    self.condition.wait()  # Wait for notification
        return self.result, self.error, self.timeout
