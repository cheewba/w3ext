"""
Contract interaction module for w3ext.

This module provides enhanced contract interaction capabilities with automatic
transaction building, batch support, and dynamic ABI generation. It wraps
web3.py's contract functionality with additional convenience methods and
improved error handling.

Classes:
    Contract: Main contract wrapper with enhanced functionality
    ContractFunction: Wrapper for contract functions with batch support
    ContractFunctions: Container for contract functions
    NotBoundContractFunction: Dynamic function creation for unknown ABIs

Functions:
    signatureMatch: Check if arguments match transaction signature

Usage Patterns:

1. Contract with known ABI:
    >>> contract = await chain.load_contract("0x123...", abi)
    >>> result = await contract.functions.balanceOf(address).call()
    >>> tx_hash = await contract.functions.transfer(to, amount).transact(account)

2. Contract without ABI (dynamic calls):
    >>> contract = await chain.load_contract("0x123...")  # No ABI provided
    >>>
    >>> # Define function signature dynamically
    >>> balance_fn = contract.functions.balanceOf[['address'], 'uint256']
    >>> result = await balance_fn(address).call()
    >>>
    >>> # For transactions, provide signature with inputs only
    >>> transfer_fn = contract.functions.transfer[['address', 'uint256']]
    >>> tx_hash = await transfer_fn(to, amount).transact(account)

Dynamic Call/Transact Rules:
- For calls: Provide full signature [inputs, outputs] to get typed results
- For transactions: Provide inputs only [inputs] since return values are ignored
- Function signatures use Solidity ABI types: 'uint256', 'address', 'string', etc.
- Multiple outputs: ['uint256', 'address'] or single output: 'uint256'
"""

import binascii
from inspect import Signature, Parameter, BoundArguments
from typing import Any, Optional, Tuple, TYPE_CHECKING, List, Union

from web3.contract.async_contract import AsyncContract, AsyncContractFunction
from eth_typing import HexStr
from eth_abi import encode as encode_abi

from .utils import fill_nonce, fill_gas_price, fill_chain_id, to_checksum_address
from .batch import to_batch_aware_method

if TYPE_CHECKING:
    from .chain import Chain
    from .account import Account

__all__ = ["Contract"]

FunctionSignature = Tuple[List[str], str]


_overloadedTransactSig = Signature([
    Parameter('account', Parameter.POSITIONAL_OR_KEYWORD),
    Parameter('transaction', Parameter.POSITIONAL_OR_KEYWORD, default=None),
])

def signatureMatch(sig, *args, **kwargs) -> Tuple[bool, Optional[BoundArguments]]:
    """
    Check if function arguments match the transaction signature pattern.

    Determines if the provided arguments match the expected signature for
    transaction methods (account, transaction).

    Args:
        sig: Function signature to check against
        *args: Positional arguments
        **kwargs: Keyword arguments

    Returns:
        Tuple of (match_found, bound_arguments)

    Example:
        >>> match, bound = signatureMatch(sig, account, {"gas": 21000})
        >>> if match:
        ...     account = bound.arguments['account']
        ...     tx = bound.arguments['transaction']
    """
    try:
        # if arguments bound
        return True, _overloadedTransactSig.bind(*args, **kwargs)
    except TypeError:
        return False, None


class NotBoundContractFunction:
    """
    Dynamic contract function for contracts without predefined ABI.

    This class allows calling contract functions when the ABI is not known
    at compile time. It dynamically generates ABI entries based on provided
    function signatures and creates callable contract functions.

    Attributes:
        name: Function name
        chain: Chain instance for blockchain operations
        address: Contract address

    Example:
        >>> # For a function not in the ABI
        >>> dynamic_fn = contract.functions.unknownFunction
        >>> # Define signature: inputs and outputs
        >>> typed_fn = dynamic_fn[['uint256', 'address'], 'bool']
        >>> result = await typed_fn(123, "0x123...").call()
    """

    def __init__(self, name: str, contract_address: str, chain: "Chain") -> None:
        """
        Initialize dynamic contract function.

        Args:
            name: Function name
            contract_address: Contract address
            chain: Chain instance
        """
        self.name = name
        self.chain = chain
        self.address = contract_address

    def _get_abi(self, signature: FunctionSignature):
        """
        Generate ABI entry from function signature.

        Creates a valid ABI function definition from input/output types.
        Handles various signature formats including empty inputs/outputs.

        Args:
            signature: Function signature in one of these formats:
                - [] : no inputs, no outputs (e.g., deposit())
                - [[]] : explicitly empty inputs, no outputs
                - [[], 'type'] : no inputs, with output
                - ['type1', 'type2'] : inputs only, no outputs
                - [['type1'], 'type'] : inputs and outputs

        Returns:
            ABI function definition dict

        Example:
            >>> # No inputs, no outputs
            >>> abi = self._get_abi([])
            >>> # Full signature with inputs and outputs
            >>> abi = self._get_abi([['uint256', 'address'], 'bool'])
            >>> # Input-only signature
            >>> abi = self._get_abi(['uint256', 'address'])
        """
        # Handle empty signature (no inputs, no outputs)
        if not signature or (len(signature) == 1 and not signature[0]):
            sig_input = []
            sig_output = []
        # Check if first element is a string (input-only format)
        elif isinstance(signature[0], str):
            sig_input = signature
            sig_output = []
        # First element is a list (full format with inputs and optional outputs)
        else:
            sig_input = signature[0]
            sig_output = signature[1] if len(signature) > 1 else []

        inputs = [{"name": f"arg{i}", "type": item}
                  for i, item in enumerate(sig_input)]

        output = [sig_output] if isinstance(sig_output, str) else sig_output
        outputs = [{"name": "", "type": item} for item in output]

        return {
            "type": "function",
            "name": self.name,
            "inputs": inputs,
            "outputs": outputs,
            "stateMutability": "payable"
        }

    def __getitem__(self, signature: FunctionSignature):
        """
        Create a typed contract function from signature.

        Args:
            signature: Function signature defining inputs and optionally outputs

        Returns:
            ContractFunction instance ready for calling

        Example:
            >>> # Read function with return value
            >>> balance_fn = contract.functions.balanceOf[['address'], 'uint256']
            >>> balance = await balance_fn(address).call()
            >>>
            >>> # Write function (no return value needed)
            >>> transfer_fn = contract.functions.transfer[['address', 'uint256']]
            >>> tx_hash = await transfer_fn(to, amount).transact(account)
        """
        fn = AsyncContractFunction.factory(
            self.name, w3=self.chain, address=self.address,
            abi=(abi:=self._get_abi(signature)), fn_name=self.name,
            contract_abi=[abi]
        )
        return ContractFunction(fn, self.chain)


class ContractFunction:
    """
    Enhanced wrapper for contract functions with batch support and auto-signing.

    This class wraps web3.py's AsyncContractFunction with additional functionality:
    - Automatic transaction building with account integration
    - Batch-aware call operations for performance optimization
    - Simplified transaction sending with auto-signing
    - Enhanced error handling and type safety

    Example:
        >>> # Call a read function
        >>> balance = await contract.functions.balanceOf(address).call()
        >>>
        >>> # Send a transaction
        >>> tx_hash = await contract.functions.transfer(to, amount).transact(account)
        >>>
        >>> # Build transaction without sending
        >>> tx = await contract.functions.transfer(to, amount).build_transaction(account)
    """

    def __init__(self, function: AsyncContractFunction, chain: "Chain") -> None:
        """
        Initialize ContractFunction wrapper.

        Args:
            function: Web3 AsyncContractFunction to wrap
            chain: Chain instance for blockchain operations
        """
        # Underlying AsyncContractFunction
        self.__function: AsyncContractFunction = function
        # Chain instance for blockchain operations
        self._chain: "Chain" = chain

    @property
    def chain(self) -> "Chain":
        """Get the chain this function is bound to."""
        return self._chain

    async def build_transaction(self, *args, **kwargs):
        """
        Build a transaction for this function call.

        Automatically fills in transaction parameters like nonce, gas price,
        and chain ID when an account is provided.

        Args:
            *args: Function arguments or (account, transaction_params)
            **kwargs: Additional parameters

        Returns:
            Built transaction dict ready for signing

        Example:
            >>> # Build with account (auto-fills parameters)
            >>> tx = await contract.functions.transfer(to, amount).build_transaction(account)
            >>>
            >>> # Build with custom parameters
            >>> tx = await contract.functions.transfer(to, amount).build_transaction(
            ...     account, {"gas": 50000, "gasPrice": 20000000000}
            ... )
        """
        tx, _ = await self._build_transaction(*args, **kwargs)
        return tx

    async def transact(self, *args, **kwargs):
        """
        Execute the function as a transaction.

        Builds the transaction, signs it with the provided account, and
        sends it to the blockchain.

        Args:
            account: Account to sign and send the transaction
            transaction: Optional transaction parameters

        Returns:
            Transaction hash

        Example:
            >>> # Simple transaction
            >>> tx_hash = await contract.functions.transfer(to, amount).transact(account)
            >>>
            >>> # With custom gas settings
            >>> tx_hash = await contract.functions.transfer(to, amount).transact(
            ...     account, {"gas": 50000}
            ... )
        """
        tx, account = await self._build_transaction(*args, **kwargs)
        return await self._chain.eth.send_raw_transaction(
            account.sign_transaction(tx).raw_transaction
        )

    async def _build_transaction(self, *args, **kwargs):
        """
        Internal method to build transactions with account integration.

        Handles both standard web3 transaction building and enhanced
        account-aware building with automatic parameter filling.

        Returns:
            Tuple of (transaction_dict, account_or_none)
        """
        match, bound = signatureMatch(_overloadedTransactSig, *args, **kwargs)
        if not match:
            return await self.__function.build_transaction(*args, **kwargs), None

        kwargs = dict(bound.arguments)
        account: Account = kwargs.pop('account')
        tx = kwargs.setdefault('transaction', {}) or {}
        tx['from'] = account.address
        tx = await fill_chain_id(self._chain, tx)
        tx = await fill_nonce(self._chain, tx)
        tx = await fill_gas_price(self._chain, tx)
        tx = await self.__function.build_transaction(tx)

        return tx, account

    def __getattr__(self, name) -> Any:
        """Delegate attribute access to the underlying function."""
        return getattr(self.__function, name)

    async def call(self, *args, **kwargs):
        """
        Call the function (read-only operation).

        This method is batch-aware and will be automatically included in
        batch requests when batching is enabled on the chain.

        Args:
            *args: Function arguments
            **kwargs: Additional call parameters

        Returns:
            Function return value(s)

        Example:
            >>> # Simple call
            >>> balance = await contract.functions.balanceOf(address).call()
            >>>
            >>> # Call with block specification
            >>> balance = await contract.functions.balanceOf(address).call(
            ...     block_identifier='latest'
            ... )
        """
        return await to_batch_aware_method(self._chain, self.__function.call)(*args, **kwargs)

    def __call__(self, *args: Any, **kwargs: Any) -> Any:
        """
        Bind arguments to the function.

        Returns a new ContractFunction instance with arguments bound,
        ready for calling or transaction building.

        Example:
            >>> # Bind arguments
            >>> bound_fn = contract.functions.transfer(to, amount)
            >>> # Then call or transact
            >>> result = await bound_fn.call()
            >>> tx_hash = await bound_fn.transact(account)
        """
        return self.__class__(self.__function(*args, **kwargs), self._chain)


class ContractFunctions:
    """
    Function accessor for contract instances.

    Provides access to contract functions with automatic fallback to dynamic
    function creation for functions not in the ABI. This enables interaction
    with any contract function, whether it's defined in the ABI or not.

    Example:
        >>> # Access known function (from ABI)
        >>> balance_fn = contract.functions.balanceOf
        >>> balance = await balance_fn(address).call()
        >>>
        >>> # Access unknown function (dynamic ABI generation)
        >>> unknown_fn = contract.functions.unknownFunction
        >>> typed_fn = unknown_fn[['uint256'], 'bool']
        >>> result = await typed_fn(123).call()
    """

    def __init__(self, contract: Union[AsyncContract, str], chain: "Chain") -> None:
        """
        Initialize contract functions accessor.

        Args:
            contract: AsyncContract instance or contract address string
            chain: Chain instance for blockchain operations
        """
        # Contract instance or address
        self.__contract: Union[AsyncContract, str] = contract
        # Chain instance for blockchain operations
        self._chain: "Chain" = chain

    def __getattr__(self, function_name: str) -> "ContractFunction":
        """
        Get a contract function by name.

        First attempts to get the function from the contract's ABI. If not found,
        creates a dynamic function that can be typed using bracket notation.

        Args:
            function_name: Name of the contract function

        Returns:
            ContractFunction for known functions, NotBoundContractFunction for unknown

        Example:
            >>> # Known function (in ABI)
            >>> transfer_fn = contract.functions.transfer
            >>>
            >>> # Unknown function (not in ABI)
            >>> custom_fn = contract.functions.customFunction
            >>> typed_fn = custom_fn[['uint256', 'address'], 'bool']
        """
        addr = self.__contract
        if isinstance(addr, AsyncContract):
            addr = addr.address

        try:
            return ContractFunction(getattr(self.__contract.functions, function_name), self._chain)
        except AttributeError:
            return NotBoundContractFunction(function_name, addr, self._chain)


class Contract:
    """
    Enhanced contract wrapper with dynamic function support and utility methods.

    This class provides a unified interface for interacting with Ethereum contracts,
    whether they have a known ABI or not. It supports:
    - Function calls and transactions through the functions accessor
    - Dynamic ABI generation for unknown functions
    - Static utility methods for encoding and packing data
    - Seamless integration with the Chain and Account systems

    Example:
        >>> # Contract with known ABI
        >>> contract = chain.load_contract(address, abi)
        >>> balance = await contract.functions.balanceOf(user).call()
        >>>
        >>> # Contract without ABI (dynamic)
        >>> contract = chain.load_contract(address)
        >>> custom_fn = contract.functions.customFunction[['uint256'], 'bool']
        >>> result = await custom_fn(123).call()
        >>>
        >>> # Static utility methods
        >>> encoded = Contract.encode(['uint256', 'address'], 123, "0x123...")
        >>> packed = Contract.pack(['uint256', 'address'], 123, "0x123...")
    """

    def __init__(self,
                 contract: Union[AsyncContract, str],
                 chain: "Chain") -> None:
        """
        Initialize contract wrapper.

        Args:
            contract: AsyncContract instance with ABI or contract address string
            chain: Chain instance for blockchain operations
        """
        # Underlying AsyncContract or contract address
        self.__contract: Union[AsyncContract, str] = contract
        # Chain instance for blockchain operations
        self.__chain: "Chain" = chain
        # ContractFunctions accessor for function calls
        self.functions: ContractFunctions = ContractFunctions(contract, chain)

    @property
    def chain_id(self) -> str:
        """Get the chain ID this contract is deployed on."""
        return self.__chain.chain_id

    @property
    def address(self) -> str:
        """Get the contract's checksummed address."""
        return (self.__contract.address if isinstance(self.__contract, AsyncContract)
                else to_checksum_address(self.__contract))

    def __getattr__(self, name) -> Any:
        """
        Delegate attribute access to underlying contract when available.

        This allows access to web3.py contract properties and methods
        when the contract has a known ABI.
        """
        # let use token as a contract with predefined ABI and web3 instance
        if isinstance(self.__contract, AsyncContract):
            return getattr(self.__contract, name)
        super().__getattribute__(name)

    @classmethod
    def encode(cls, types: List[str], *values: List[Any]) -> HexStr:
        """
        Encode values using standard Ethereum ABI encoding.

        Takes Ethereum ABI types and corresponding values, encodes them into
        a hexadecimal string suitable for the `data` field in an Ethereum
        transaction. Uses standard ABI encoding with proper padding.

        Args:
            types: List of Ethereum ABI types (e.g., 'uint256', 'address', 'string')
            *values: Values to encode, corresponding to each type

        Returns:
            Hexadecimal string (prefixed with '0x') representing encoded values

        Example:
            >>> # Encode function arguments
            >>> encoded = Contract.encode(
            ...     ['uint256', 'address', 'string'],
            ...     12345,
            ...     '0x123456789abcdef123456789abcdef123456789a',
            ...     'Hello, Ethereum!'
            ... )
            >>> # Use in transaction data
            >>> tx = {'to': contract_address, 'data': encoded}
        """
        return f"0x{encode_abi(types, values).hex()}"

    @classmethod
    def _single_pack(cls, type_str, value):
        """
        Pack a single value according to its type with minimal padding.

        This is a helper method for the pack() function that handles individual
        value encoding with custom rules for integers and addresses.

        Args:
            type_str: Ethereum ABI type string
            value: Value to encode

        Returns:
            Encoded bytes for the value
        """
        if type_str.startswith('uint') or type_str.startswith('int'):
            # Determine the size of the integer based on its type
            size = int(type_str[4:]) if type_str[4:] else 256
            byte_size = (size + 7) // 8  # Convert bit size to byte size
            return encode_abi([type_str], [value])[-byte_size:]
        elif type_str == 'address':
            # Address: decode hex, ensure it's 20 bytes
            return binascii.unhexlify(value[2:].rjust(40, '0'))

        # Fallback for other types
        return encode_abi([type_str], [value])

    @classmethod
    def pack(cls, types: List[str], *values: List[Any]) -> HexStr:
        """
        Pack values into a single hex string with minimal padding.

        This function encodes each value according to its Ethereum ABI type and
        concatenates them into a single byte string. Unlike standard ABI encoding,
        this method uses minimal padding for integers and custom formatting for
        addresses to create tightly packed data.

        Args:
            types: List of Ethereum ABI types (e.g., 'uint256', 'address', 'string')
            *values: Values to encode and pack, corresponding to each type

        Returns:
            Hexadecimal string representing the packed values

        Raises:
            ValueError: If the lengths of types and values lists don't match

        Note:
            This function does not use standard Ethereum ABI encoding. It removes
            padding from integers and uses custom address formatting. Use this for
            contexts where tightly packed data is required (e.g., hash generation).

        Example:
            >>> # Pack for hash generation
            >>> packed = Contract.pack(
            ...     ['uint256', 'address', 'uint256'],
            ...     12345,
            ...     '0x123456789abcdef123456789abcdef123456789a',
            ...     67890
            ... )
            >>> # Use for creating unique identifiers
            >>> hash_input = packed
        """
        if len(types) != len(values):
            raise ValueError("Types and values lists must have the same length.")

        # Encode and concatenate values
        encoded_bytes = b''.join(cls._single_pack(t, v) for t, v in zip(types, values))

        # Convert to hex string
        return f"0x{encoded_bytes.hex()}"
