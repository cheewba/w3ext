"""
Type definitions and re-exports for w3ext.

This module provides type definitions used throughout the w3ext library.
Currently re-exports common web3.py types for convenience.

Example:
    >>> from w3ext.types import TxParams
    >>> tx: TxParams = {'to': '0x123...', 'value': 1000}
"""

from web3.types import TxParams  # noqa: F401