import asyncio
import functools
import inspect
import logging
import traceback
from typing import Callable, Any, Optional
from .exceptions import PluginTypeMismatchError, RequestException


def _check_type(func, expected_type, correct_decorator):
    """
    Helper function to check if the function type matches the expected type.
    """
    if expected_type == "sync":
        if inspect.iscoroutinefunction(func):
            raise PluginTypeMismatchError(
                f"Function {func.__name__} is a coroutine. Use @async_{correct_decorator} instead. Fix in called plugin."
            )
        if inspect.isasyncgenfunction(func):
            raise PluginTypeMismatchError(
                f"Function {func.__name__} is an async generator. Use @async_gen_{correct_decorator} instead. Fix in called plugin."
            )
        if inspect.isgeneratorfunction(func):
            raise PluginTypeMismatchError(
                f"Function {func.__name__} is a generator. Use @gen_{correct_decorator} instead. Fix in called plugin."
            )
    elif expected_type == "async":
        if not inspect.iscoroutinefunction(func):
            if inspect.isgeneratorfunction(func):
                raise PluginTypeMismatchError(
                    f"Function {func.__name__} is a generator. Use @gen_{correct_decorator} instead. Fix in called plugin."
                )
            if inspect.isasyncgenfunction(func):
                raise PluginTypeMismatchError(
                    f"Function {func.__name__} is an async generator. Use @async_gen_{correct_decorator} instead. Fix in called plugin."
                )
            raise PluginTypeMismatchError(
                f"Function {func.__name__} is not a coroutine. Use @{correct_decorator} instead. Fix in called plugin."
            )
    elif expected_type == "gen":
        if not inspect.isgeneratorfunction(func):
            if inspect.iscoroutinefunction(func):
                raise PluginTypeMismatchError(
                    f"Function {func.__name__} is a coroutine. Use @async_{correct_decorator} instead. Fix in called plugin."
                )
            if inspect.isasyncgenfunction(func):
                raise PluginTypeMismatchError(
                    f"Function {func.__name__} is an async generator. Use @async_gen_{correct_decorator} instead. Fix in called plugin."
                )
            raise PluginTypeMismatchError(
                f"Function {func.__name__} is not a generator. Use @{correct_decorator} instead. Fix in called plugin."
            )
    elif expected_type == "async_gen":
        if not inspect.isasyncgenfunction(func):
            if inspect.iscoroutinefunction(func):
                raise PluginTypeMismatchError(
                    f"Function {func.__name__} is a coroutine. Use @async_{correct_decorator} instead. Fix in called plugin."
                )
            if inspect.isgeneratorfunction(func):
                raise PluginTypeMismatchError(
                    f"Function {func.__name__} is a generator. Use @gen_{correct_decorator} instead. Fix in called plugin."
                )
            raise PluginTypeMismatchError(
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
            # C-164: ``nonlocal logger`` removed — ``logger`` is only
            # read here (assigned to ``_logger``), never reassigned in
            # the wrapper. The declaration was misleading: closures can
            # read enclosing-scope variables without ``nonlocal``.
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
                        f"Error in {func_name}:{line_no} at {file_name}: {type(e).__name__}: {e}",
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

    R4-XX-1: supports both ``@handle_errors`` (no parens) and
    ``@handle_errors(default_return=...)`` (with parens) forms,
    matching the dual-dispatch pattern used by ``log_errors`` and
    other sibling decorators. The no-parens form previously left the
    target function un-wrapped because Python passes the function as
    the first positional argument (``default_return``), which then
    became the default value rather than triggering wrapping.
    """

    def decorator(func):
        _check_type(func, "sync", "handle_errors")

        @functools.wraps(func)
        def wrapper(*args, **kwargs):
            # C-164: ``nonlocal logger`` removed — ``logger`` is only
            # read here (assigned to ``_logger``), never reassigned in
            # the wrapper. The declaration was misleading: closures can
            # read enclosing-scope variables without ``nonlocal``.
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
                        f"Error in {func_name}:{line_no} at {file_name}: {type(e).__name__}: {e}",
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

    # R4-XX-1: dual-dispatch shim. When applied as ``@handle_errors``
    # (no parens) Python passes the decorated function as
    # ``default_return``. Detect the sync-function case and route
    # through ``decorator(func)`` so wrapping actually happens. The
    # check is restricted to sync callables that are NOT coroutine /
    # generator / async generator functions so a legitimate callable
    # default_return (e.g. a factory) is not hijacked.
    if (
        callable(default_return)
        and not inspect.iscoroutinefunction(default_return)
        and not inspect.isgeneratorfunction(default_return)
        and not inspect.isasyncgenfunction(default_return)
    ):
        func = default_return
        default_return = None
        return decorator(func)

    return decorator


def async_log_errors(func=None):
    """
    Decorator for async functions to log exceptions without affecting the function's behavior.

    C-162: supports both ``@async_log_errors`` (no parens) and
    ``@async_log_errors()`` (with parens) forms, matching the
    dual-dispatch pattern used by sibling decorators (``log_errors``,
    ``async_handle_errors``, etc.). The no-parens form is the common
    case; the parens form crashed with TypeError previously because
    the function-arg slot was unbound.
    """

    def decorator(real_func):
        _check_type(real_func, "async", "log_errors")

        @functools.wraps(real_func)
        async def wrapper(*args, **kwargs):
            # Get the logger from the first argument (self) if available
            _logger = None
            if args and hasattr(args[0], "_logger"):
                _logger = args[0]._logger
            elif hasattr(real_func, "_logger"):
                _logger = real_func._logger

            try:
                return await real_func(*args, **kwargs)
            except Exception as e:
                # Get useful information about where the error occurred
                func_name = real_func.__name__
                file_name = real_func.__code__.co_filename
                line_no = real_func.__code__.co_firstlineno

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

    # Dual-dispatch: no-parens form passes the target function in
    # directly (callable); parens form passes None and returns the
    # decorator to be applied later. Anything else is a usage bug —
    # raise so the call site surfaces it immediately. The previous
    # silent fall-back to the factory let `@async_log_errors(123)`
    # produce a decorator object instead of a wrapped function, which
    # then broke at invocation with a confusing error far from the
    # decorator site.
    if func is None:
        return decorator
    if callable(func):
        return decorator(func)
    raise TypeError(
        f"async_log_errors expected a coroutine function or no argument; "
        f"got {type(func).__name__}"
    )


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
                # Let RequestException propagate so callers can handle plugin/request errors
                if isinstance(e, RequestException):
                    raise
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

    # Handle case where decorator is used without parentheses.
    # W1-D2: tighten check to ``iscoroutinefunction`` so a legitimate
    # callable default_return (e.g. lambda factory) doesn't get hijacked
    # as the function-to-wrap. The no-parens form ``@async_handle_errors``
    # passes the async function as ``default_return``; that path still
    # matches iscoroutinefunction.
    if asyncio.iscoroutinefunction(default_return):
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
            # C-164: ``nonlocal logger`` removed — ``logger`` is only
            # read here (assigned to ``_logger``), never reassigned in
            # the wrapper. The declaration was misleading: closures can
            # read enclosing-scope variables without ``nonlocal``.
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

    # Handle case where decorator is used without parentheses.
    # R2-II-5: tighten check to ``isgeneratorfunction`` so a callable
    # logger or callable factory passed as the first arg isn't
    # misidentified as the function-to-wrap. The no-parens form
    # ``@gen_log_errors`` passes the generator function as ``logger``;
    # that path still matches isgeneratorfunction.
    if inspect.isgeneratorfunction(logger):
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

    R4-XX-1: supports both ``@gen_handle_errors`` (no parens) and
    ``@gen_handle_errors(default_return=...)`` (with parens) forms,
    matching the dual-dispatch pattern used by ``gen_log_errors``.
    The no-parens form previously left the target generator
    un-wrapped because Python passes the function as the first
    positional argument (``default_return``).
    """

    def decorator(func):
        _check_type(func, "gen", "handle_errors")

        @functools.wraps(func)
        def wrapper(*args, **kwargs):
            # C-164: ``nonlocal logger`` removed — ``logger`` is only
            # read here (assigned to ``_logger``), never reassigned in
            # the wrapper. The declaration was misleading: closures can
            # read enclosing-scope variables without ``nonlocal``.
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

    # R4-XX-1: dual-dispatch shim. When applied as
    # ``@gen_handle_errors`` (no parens) Python passes the decorated
    # generator function as ``default_return``. Detect the generator
    # function case via ``isgeneratorfunction`` (mirrors the
    # ``gen_log_errors`` shim) and route through ``decorator(func)``
    # so wrapping actually happens. ``isgeneratorfunction`` is strict
    # enough that a callable default_return (e.g. a factory) is not
    # hijacked.
    if inspect.isgeneratorfunction(default_return):
        func = default_return
        default_return = None
        return decorator(func)

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
            # C-164: ``nonlocal logger`` removed — ``logger`` is only
            # read here (assigned to ``_logger``), never reassigned.
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

    # Handle case where decorator is used without parentheses.
    # R2-II-5: tighten check to ``isasyncgenfunction`` so a callable
    # logger or callable factory passed as the first arg isn't
    # misidentified as the function-to-wrap. The no-parens form
    # ``@async_gen_log_errors`` passes the async generator function as
    # ``logger``; that path still matches isasyncgenfunction.
    if inspect.isasyncgenfunction(logger):
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
            # C-164: ``nonlocal logger`` removed — ``logger`` is only
            # read here (assigned to ``_logger``), never reassigned.
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

    # Handle case where decorator is used without parentheses.
    # R2-II-5: tighten check to ``isasyncgenfunction`` so a legitimate
    # callable default_return (e.g. lambda factory) doesn't get
    # hijacked as the function-to-wrap. The no-parens form
    # ``@async_gen_handle_errors`` passes the async generator function
    # as ``default_return``; that path still matches
    # isasyncgenfunction.
    if inspect.isasyncgenfunction(default_return):
        func = default_return
        default_return = None
        return decorator(func)

    return decorator
