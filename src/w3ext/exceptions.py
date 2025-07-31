"""
Exception classes for w3ext.

This module defines custom exception classes used throughout the w3ext library
to provide more specific error handling for blockchain operations.

Example:
    >>> try:
    ...     await chain.connect()
    ... except ChainException as e:
    ...     print(f"Chain operation failed: {e}")
"""


class ChainException(Exception):
    """
    Base exception class for chain-related errors.

    Raised when blockchain operations fail or encounter errors.
    This serves as the base class for all w3ext-specific exceptions.

    Example:
        >>> raise ChainException("Failed to connect to RPC endpoint")
    """
    pass