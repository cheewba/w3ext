import binascii
from inspect import Signature, Parameter, BoundArguments
from typing import Any, Optional, Tuple, TYPE_CHECKING, List, Union

from web3.contract.async_contract import AsyncContract, AsyncContractFunction
from eth_typing import HexStr
from eth_abi import encode as encode_abi

from .utils import fill_nonce, fill_gas_price, fill_chain_id

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
    try:
        # if arguments bound
        return True, _overloadedTransactSig.bind(*args, **kwargs)
    except TypeError:
        return False, None


class NotBoundContractFunction:
    def __init__(self, name: str, contract_address: str, chain: "Chain") -> None:
        self.name = name
        self.chain = chain
        self.address = contract_address

    def _get_abi(self, signature: FunctionSignature):
        sig_input = signature if (no_output := isinstance(signature[0], str)) else signature[0]
        sig_output = signature[1] if not no_output else []
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
        fn = AsyncContractFunction.factory(
            self.name, w3=self.chain, address=self.address,
            abi=self._get_abi(signature), function_identifier=self.name
        )
        return ContractFunction(fn, self.chain)


class ContractFunction:
    def __init__(self, function: AsyncContractFunction, chain: "Chain") -> None:
        self.__function = function
        self._chain = chain

    async def build_transaction(self, *args, **kwargs):
        tx, _ = await self._build_transaction(*args, **kwargs)
        return tx

    async def transact(self, *args, **kwargs):
        tx, account = await self._build_transaction(*args, **kwargs)
        return await self._chain.eth.send_raw_transaction(
            account.sign_transaction(tx).rawTransaction
        )

    async def _build_transaction(self, *args, **kwargs):
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
        return getattr(self.__function, name)

    def __call__(self, *args: Any, **kwargs: Any) -> Any:
        return self.__class__(self.__function(*args, **kwargs), self._chain)


class ContractFunctions:
    def __init__(self, contract: Union[AsyncContract, str], chain: "Chain") -> None:
        self.__contract = contract
        self._chain = chain

    def __getattr__(self, function_name: str) -> "ContractFunction":
        addr = self.__contract
        if isinstance(addr, AsyncContract):
            addr = addr.address

        try:
            return ContractFunction(getattr(self.__contract.functions, function_name), self._chain)
        except AttributeError:
            return NotBoundContractFunction(function_name, addr, self._chain)


class Contract:
    def __init__(self,
                 contract: Union[AsyncContract, str],
                 chain: "Chain") -> None:
        self.__contract = contract
        self.__chain = chain
        self.functions = ContractFunctions(contract, chain)

    @property
    def chain_id(self) -> str:
        return self.__chain.chain_id

    def __getattr__(self, name) -> Any:
        # let use token as a contract with predefined ABI and web3 instance
        if isinstance(self.__contract, AsyncContract):
            return getattr(self.__contract, name)
        super().__getattribute__(name)

    @classmethod
    def encode(cls, types: List[str], *values: List[Any]) -> HexStr:
        """
        Encode values for use as data in a transaction for a contract call.

        This method takes Ethereum ABI types and corresponding values, encodes
        them into a hexadecimal string suitable for the `data` field in an
        Ethereum transaction. Useful for encoding contract function call
        arguments.

        Parameters:
        types : List[str]
            A list of Ethereum ABI types (e.g., 'uint256', 'address', 'string').
            Each type corresponds to an item in the `values`.
        values : List[Any]
            A variable number of arguments, each representing the value to
            be encoded, corresponding to a type in the `types` list.

        Returns:
        str
            A hexadecimal string (prefixed with '0x') representing the encoded
            values, suitable for use as transaction data.

        Example:
        >>> types = ['uint256', 'address', 'string']
        >>> values = [12345, '0x123456789abcdef123456789abcdef123456789a',
        ...           'Hello, Ethereum!']
        >>> Contract.encode(types, *values)
        '0x[encoded hex string]'
        """
        return f"0x{encode_abi(types, values).hex()}"

    @classmethod
    def _single_pack(cls, type_str, value):
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
        Packs a list of values into a single hex string based on their respective types.

        This function encodes each value according to its Ethereum ABI type and concatenates
        them into a single byte string. The encoding is customized to avoid padding integer
        types with leading zeros, and to ensure address types are properly formatted. Strings
        and bytes are encoded using standard ABI encoding. The concatenated byte string is
        then converted into a hexadecimal representation.

        Parameters:
        types (List[str]): A list of Ethereum ABI types (e.g., 'uint256', 'address', 'string').
                        Each type in this list corresponds to an item in the `values` list.
        *values (List[Any]): A list of values to be encoded and packed. Each value in this list
                            corresponds to a type in the `types` list.

        Returns:
        str: A hexadecimal string representing the packed values.

        Raises:
        ValueError: If the lengths of `types` and `values` lists do not match.

        Note:
        This function does not adhere to the standard Ethereum ABI encoding in certain cases
        (e.g., removing leading zeros from integers). It should be used in contexts where
        such a custom encoding approach is acceptable.

        Example:
        >>> types = ['uint256', 'address', 'string']
        >>> values = [12345, '0x123456789abcdef123456789abcdef123456789a', 'Hello, Ethereum!']
        >>> Contract.pack(types, *values)
        '0x3039...[hex string]'
        """
        if len(types) != len(values):
            raise ValueError("Types and values lists must have the same length.")

        # Encode and concatenate values
        encoded_bytes = b''.join(cls._single_pack(t, v) for t, v in zip(types, values))

        # Convert to hex string
        return f"0x{encoded_bytes.hex()}"
