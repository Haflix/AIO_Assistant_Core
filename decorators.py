import functools
import inspect
import logging
import traceback
from typing import Callable, Any, Optional
from exceptions import PluginTypeMissmatchError


def _check_type(func, expected_type, correct_decorator):
    """
    Helper function to check if the function type matches the expected type.
    """
    if expected_type == "sync":
        if inspect.iscoroutinefunction(func):
            raise PluginTypeMissmatchError(
                f"Function {func.__name__} is a coroutine. Use @async_{correct_decorator} instead. Fix in called plugin."
            )
        if inspect.isasyncgenfunction(func):
            raise PluginTypeMissmatchError(
                f"Function {func.__name__} is an async generator. Use @async_gen_{correct_decorator} instead. Fix in called plugin."
            )
        if inspect.isgeneratorfunction(func):
            raise PluginTypeMissmatchError(
                f"Function {func.__name__} is a generator. Use @gen_{correct_decorator} instead. Fix in called plugin."
            )
    elif expected_type == "async":
        if not inspect.iscoroutinefunction(func):
            if inspect.isgeneratorfunction(func):
                raise PluginTypeMissmatchError(
                    f"Function {func.__name__} is a generator. Use @gen_{correct_decorator} instead. Fix in called plugin."
                )
            if inspect.isasyncgenfunction(func):
                raise PluginTypeMissmatchError(
                    f"Function {func.__name__} is an async generator. Use @async_gen_{correct_decorator} instead. Fix in called plugin."
                )
            raise PluginTypeMissmatchError(
                f"Function {func.__name__} is not a coroutine. Use @{correct_decorator} instead. Fix in called plugin."
            )
    elif expected_type == "gen":
        if not inspect.isgeneratorfunction(func):
            if inspect.iscoroutinefunction(func):
                raise PluginTypeMissmatchError(
                    f"Function {func.__name__} is a coroutine. Use @async_{correct_decorator} instead. Fix in called plugin."
                )
            if inspect.isasyncgenfunction(func):
                raise PluginTypeMissmatchError(
                    f"Function {func.__name__} is an async generator. Use @async_gen_{correct_decorator} instead. Fix in called plugin."
                )
            raise PluginTypeMissmatchError(
                f"Function {func.__name__} is not a generator. Use @{correct_decorator} instead. Fix in called plugin."
            )
    elif expected_type == "async_gen":
        if not inspect.isasyncgenfunction(func):
            if inspect.iscoroutinefunction(func):
                raise PluginTypeMissmatchError(
                    f"Function {func.__name__} is a coroutine. Use @async_{correct_decorator} instead. Fix in called plugin."
                )
            if inspect.isgeneratorfunction(func):
                raise PluginTypeMissmatchError(
                    f"Function {func.__name__} is a generator. Use @gen_{correct_decorator} instead. Fix in called plugin."
                )
            raise PluginTypeMissmatchError(
                f"Function {func.__name__} is not an async generator. Use @{correct_decorator} or appropriate decorator. Fix in called plugin."
            )


def log_errors(logger: Optional[logging.Logger] = None):
    """
    Decorator to log exceptions without affecting the function's behavior.

    Args:
        logger: Optional logger to use. If None, will use the instance's logger.

    Example:
        @log_errors()
        def my_function():
            # Function code here
    """

    def decorator(func):
        _check_type(func, "sync", "log_errors")

        @functools.wraps(func)
        def wrapper(*args, **kwargs):
            # Get the logger from the first argument (self) if not provided
            nonlocal logger
            _logger = logger
            if _logger is None and args and hasattr(args[0], "_logger"):
                _logger = args[0]._logger

            try:
                return func(*args, **kwargs)
            except Exception as e:
                # Get useful information about where the error occurred
                func_name = func.__name__
                file_name = func.__code__.co_filename
                line_no = func.__code__.co_firstlineno

                # Log the error with the correct source information
                if _logger:
                    _logger.error(
                        f"Error in (async) {func_name}:{line_no} at {file_name}: {type(e).__name__}: {e}",
                        extra={
                            "func_name": func_name,
                            "file_name": file_name,
                            "line_no": line_no,
                        },
                    )
                    _logger.debug(f"Traceback: {traceback.format_exc()}")
                raise  # Re-raise the exception

        return wrapper

    # Handle case where decorator is used without parentheses
    if callable(logger):
        func = logger
        logger = None
        return decorator(func)
    return decorator


def handle_errors(default_return: Any = None, logger: Optional[logging.Logger] = None):
    """
    Decorator to catch exceptions and return a default value instead.

    Args:
        default_return: Value to return if an exception occurs
        logger: Optional logger to use. If None, will use the instance's logger.

    Example:
        @handle_errors(default_return=None)
        def my_function():
            # Function code here
    """

    def decorator(func):
        _check_type(func, "sync", "handle_errors")

        @functools.wraps(func)
        def wrapper(*args, **kwargs):
            # Get the logger from the first argument (self) if not provided
            nonlocal logger
            _logger = logger
            if _logger is None and args and hasattr(args[0], "_logger"):
                _logger = args[0]._logger

            try:
                return func(*args, **kwargs)
            except Exception as e:
                # Get useful information about where the error occurred
                func_name = func.__name__
                file_name = func.__code__.co_filename
                line_no = func.__code__.co_firstlineno

                # Log the error with the correct source information
                if _logger:
                    _logger.error(
                        f"Error in (async) {func_name}:{line_no} at {file_name}: {type(e).__name__}: {e}",
                        extra={
                            "func_name": func_name,
                            "file_name": file_name,
                            "line_no": line_no,
                        },
                    )
                    _logger.debug(f"Traceback: {traceback.format_exc()}")

                # Return the default value
                return default_return

        return wrapper

    return decorator


def async_log_errors(func):
    """
    Decorator for async functions to log exceptions without affecting the function's behavior.
    """
    _check_type(func, "async", "log_errors")

    @functools.wraps(func)
    async def wrapper(*args, **kwargs):
        # Get the logger from the first argument (self) if available
        _logger = None
        if args and hasattr(args[0], "_logger"):
            _logger = args[0]._logger
        elif hasattr(func, "_logger"):
            _logger = func._logger

        try:
            return await func(*args, **kwargs)
        except Exception as e:
            # Get useful information about where the error occurred
            func_name = func.__name__
            file_name = func.__code__.co_filename
            line_no = func.__code__.co_firstlineno

            # Log the error with the correct source information
            if _logger:
                _logger.error(
                    f"Error in (async) {func_name}:{line_no} at {file_name}: {type(e).__name__}: {e}",
                    extra={
                        "func_name": func_name,
                        "file_name": file_name,
                        "line_no": line_no,
                    },
                )
                _logger.debug(f"Traceback: {traceback.format_exc()}")
            raise  # Re-raise the exception

    return wrapper


def async_handle_errors(default_return=None):
    """
    Decorator for async functions to catch exceptions and return a default value instead.

    Args:
        default_return: Value to return if an exception occurs

    Example:
        @async_handle_errors(default_return=None)
        async def my_function():
            # Function code here
    """

    def decorator(func):
        _check_type(func, "async", "handle_errors")

        @functools.wraps(func)
        async def wrapper(*args, **kwargs):
            # Get the logger from the first argument (self) if available
            _logger = None
            if args and hasattr(args[0], "_logger"):
                _logger = args[0]._logger
            elif hasattr(func, "_logger"):
                _logger = func._logger

            try:
                return await func(*args, **kwargs)

            except Exception as e:
                # Get useful information about where the error occurred
                func_name = func.__name__
                file_name = func.__code__.co_filename
                line_no = func.__code__.co_firstlineno

                # Log the error with the correct source information
                if _logger:
                    _logger.error(
                        f"Error in (async) {func_name}:{line_no} at {file_name}: {type(e).__name__}: {e}",
                        extra={
                            "func_name": func_name,
                            "file_name": file_name,
                            "line_no": line_no,
                        },
                    )
                    _logger.debug(f"Traceback: {traceback.format_exc()}")

                return default_return

        return wrapper

    # Handle case where decorator is used without parentheses
    if callable(default_return):
        func = default_return
        default_return = None
        return decorator(func)

    return decorator


def gen_log_errors(logger: Optional[logging.Logger] = None):
    """
    Decorator for generator functions to log exceptions without affecting the generator's behavior.

    Args:
        logger: Optional logger to use. If None, will use the instance's logger.

    Example:
        @gen_log_errors()
        def my_generator():
            for i in range(10):
                yield i
    """

    def decorator(func):
        _check_type(func, "gen", "log_errors")

        @functools.wraps(func)
        def wrapper(*args, **kwargs):
            # Get the logger from the first argument (self) if not provided
            nonlocal logger
            _logger = logger
            if _logger is None and args and hasattr(args[0], "_logger"):
                _logger = args[0]._logger

            # Get useful information about where the error occurred
            func_name = func.__name__
            file_name = func.__code__.co_filename
            line_no = func.__code__.co_firstlineno

            # Create the generator
            generator = func(*args, **kwargs)

            # Iterate over the generator with error handling
            while True:
                try:
                    yield next(generator)
                except StopIteration:
                    # Normal generator exhaustion
                    return
                except Exception as e:
                    # Log the error with the correct source information
                    if _logger:
                        _logger.error(
                            f"Error in generator {func_name}:{line_no} at {file_name}: {type(e).__name__}: {e}",
                            extra={
                                "func_name": func_name,
                                "file_name": file_name,
                                "line_no": line_no,
                            },
                        )
                        _logger.debug(f"Traceback: {traceback.format_exc()}")
                    raise  # Re-raise the exception

        return wrapper

    # Handle case where decorator is used without parentheses
    if callable(logger):
        func = logger
        logger = None
        return decorator(func)
    return decorator


def gen_handle_errors(
    default_return: Any = None, logger: Optional[logging.Logger] = None
):
    """
    Decorator for generator functions to catch exceptions and stop the generator.

    Args:
        default_return: Kept for API consistency, but generator stops rather than yielding it.
        logger: Optional logger to use. If None, will use the instance's logger.

    Example:
        @gen_handle_errors(default_return=None)
        def my_generator():
            for i in range(10):
                yield i
    """

    def decorator(func):
        _check_type(func, "gen", "handle_errors")

        @functools.wraps(func)
        def wrapper(*args, **kwargs):
            # Get the logger from the first argument (self) if not provided
            nonlocal logger
            _logger = logger
            if _logger is None and args and hasattr(args[0], "_logger"):
                _logger = args[0]._logger

            # Get useful information about where the error occurred
            func_name = func.__name__
            file_name = func.__code__.co_filename
            line_no = func.__code__.co_firstlineno

            # Create the generator
            generator = func(*args, **kwargs)

            # Iterate over the generator with error handling
            while True:
                try:
                    yield next(generator)
                except StopIteration:
                    # Normal generator exhaustion
                    return
                except Exception as e:
                    # Log the error with the correct source information
                    if _logger:
                        _logger.error(
                            f"Error in generator {func_name}:{line_no} at {file_name}: {type(e).__name__}: {e}",
                            extra={
                                "func_name": func_name,
                                "file_name": file_name,
                                "line_no": line_no,
                            },
                        )
                        _logger.debug(f"Traceback: {traceback.format_exc()}")

                    # Stop the generator (don't yield default_return, just stop)
                    return

        return wrapper

    return decorator


def async_gen_log_errors(logger: Optional[logging.Logger] = None):
    """
    Decorator for async generator functions to log exceptions without affecting the generator's behavior.

    Args:
        logger: Optional logger to use. If None, will use the instance's logger.

    Example:
        @async_gen_log_errors()
        async def my_async_generator():
            for i in range(10):
                yield i
    """

    def decorator(func):
        _check_type(func, "async_gen", "log_errors")

        @functools.wraps(func)
        async def wrapper(*args, **kwargs):
            # Get the logger from the first argument (self) if available
            nonlocal logger
            _logger = logger
            if _logger is None and args and hasattr(args[0], "_logger"):
                _logger = args[0]._logger
            elif hasattr(func, "_logger"):
                _logger = func._logger

            # Get useful information about where the error occurred
            func_name = func.__name__
            file_name = func.__code__.co_filename
            line_no = func.__code__.co_firstlineno

            # Create the async generator
            async_gen = func(*args, **kwargs)

            # Iterate over the async generator with error handling
            try:
                async for item in async_gen:
                    yield item
            except Exception as e:
                # Log the error with the correct source information
                if _logger:
                    _logger.error(
                        f"Error in async generator {func_name}:{line_no} at {file_name}: {type(e).__name__}: {e}",
                        extra={
                            "func_name": func_name,
                            "file_name": file_name,
                            "line_no": line_no,
                        },
                    )
                    _logger.debug(f"Traceback: {traceback.format_exc()}")
                raise  # Re-raise the exception

        return wrapper

    # Handle case where decorator is used without parentheses
    if callable(logger):
        func = logger
        logger = None
        return decorator(func)
    return decorator


def async_gen_handle_errors(
    default_return: Any = None, logger: Optional[logging.Logger] = None
):
    """
    Decorator for async generator functions to catch exceptions and stop the generator.

    Args:
        default_return: Kept for API consistency, but generator stops rather than yielding it.
        logger: Optional logger to use. If None, will use the instance's logger.

    Example:
        @async_gen_handle_errors(default_return=None)
        async def my_async_generator():
            for i in range(10):
                yield i
    """

    def decorator(func):
        _check_type(func, "async_gen", "handle_errors")

        @functools.wraps(func)
        async def wrapper(*args, **kwargs):
            # Get the logger from the first argument (self) if available
            nonlocal logger
            _logger = logger
            if _logger is None and args and hasattr(args[0], "_logger"):
                _logger = args[0]._logger
            elif hasattr(func, "_logger"):
                _logger = func._logger

            # Get useful information about where the error occurred
            func_name = func.__name__
            file_name = func.__code__.co_filename
            line_no = func.__code__.co_firstlineno

            # Create the async generator
            async_gen = func(*args, **kwargs)

            # Iterate over the async generator with error handling
            try:
                async for item in async_gen:
                    yield item
            except Exception as e:
                # Log the error with the correct source information
                if _logger:
                    _logger.error(
                        f"Error in async generator {func_name}:{line_no} at {file_name}: {type(e).__name__}: {e}",
                        extra={
                            "func_name": func_name,
                            "file_name": file_name,
                            "line_no": line_no,
                        },
                    )
                    _logger.debug(f"Traceback: {traceback.format_exc()}")

                # Stop the async generator (don't yield default_return, just stop)
                return

        return wrapper

    # Handle case where decorator is used without parentheses
    if callable(default_return):
        func = default_return
        default_return = None
        return decorator(func)

    return decorator
