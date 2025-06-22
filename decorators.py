import functools
import inspect
import logging
import traceback
from typing import Callable, Any, Optional

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
        @functools.wraps(func)
        def wrapper(*args, **kwargs):
            # Get the logger from the first argument (self) if not provided
            nonlocal logger
            _logger = logger
            if _logger is None and args and hasattr(args[0], '_logger'):
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
                    _logger.error(f"Error in (async) {func_name}:{line_no} at {file_name}: {type(e).__name__}: {e}", extra={
                        'func_name': func_name,
                        'file_name': file_name,
                        'line_no': line_no
                    })
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
        @functools.wraps(func)
        def wrapper(*args, **kwargs):
            # Get the logger from the first argument (self) if not provided
            nonlocal logger
            _logger = logger
            if _logger is None and args and hasattr(args[0], '_logger'):
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
                    _logger.error(f"Error in (async) {func_name}:{line_no} at {file_name}: {type(e).__name__}: {e}", extra={
                        'func_name': func_name,
                        'file_name': file_name,
                        'line_no': line_no
                    })
                    _logger.debug(f"Traceback: {traceback.format_exc()}")
                
                # Return the default value
                return default_return
        
        return wrapper
    
    return decorator

def async_log_errors(func):
    """
    Decorator for async functions to log exceptions without affecting the function's behavior.
    """
    @functools.wraps(func)
    async def wrapper(*args, **kwargs):
        # Get the logger from the first argument (self) if available
        _logger = None
        if args and hasattr(args[0], '_logger'):
            _logger = args[0]._logger
        elif hasattr(func, '_logger'):
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
                _logger.error(f"Error in (async) {func_name}:{line_no} at {file_name}: {type(e).__name__}: {e}", extra={
                    'func_name': func_name,
                    'file_name': file_name,
                    'line_no': line_no
                })
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
        @functools.wraps(func)
        async def wrapper(*args, **kwargs):
            # Get the logger from the first argument (self) if available
            _logger = None
            if args and hasattr(args[0], '_logger'):
                _logger = args[0]._logger
            elif hasattr(func, '_logger'):
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
                    _logger.error(f"Error in (async) {func_name}:{line_no} at {file_name}: {type(e).__name__}: {e}", extra={
                        'func_name': func_name,
                        'file_name': file_name,
                        'line_no': line_no
                    })
                    _logger.debug(f"Traceback: {traceback.format_exc()}")
                    
                return default_return
        
        return wrapper
    
    # Handle case where decorator is used without parentheses
    if callable(default_return):
        func = default_return
        default_return = None
        return decorator(func)
    
    return decorator