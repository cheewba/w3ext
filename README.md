# w3ext - Enhanced Ethereum Web3 Library

## Overview

`w3ext` is a Python library that extends [`web3.py`](https://github.com/ethereum/web3.py) with enhanced functionality for Ethereum blockchain interactions. It provides a higher-level, more intuitive API while maintaining full compatibility with the underlying web3.py functionality.

## Key Features

- **Enhanced Chain Management**: Simplified connection and interaction with Ethereum networks
- **Advanced Token Support**: Built-in ERC20 token handling with balance management and transfers
- **NFT Integration**: Complete ERC721 support with metadata handling and external provider integration
- **Account Management**: Streamlined account creation, signing, and transaction management
- **Batch Operations**: Efficient batching of multiple blockchain calls for improved performance
- **Smart Contract Interaction**: Enhanced contract interaction with automatic ABI handling
- **Type Safety**: Full type hints and modern Python features for better development experience

## Installation

```bash
pip install -e git+https://github.com/cheewba/w3ext.git#egg=w3ext
```

### Requirements

- Python 3.11+
- web3.py 7.9.*
- aiohttp 3.11.*

## Quick Start

```python
import asyncio
from w3ext import Chain, Account

async def main():
    # Connect to Ethereum mainnet
    chain = await Chain.connect(
        rpc="https://mainnet.infura.io/v3/YOUR_PROJECT_ID",
        chain_id=1,
        name="Ethereum Mainnet",
        scan="https://etherscan.io"
    )

    # Create account from private key
    account = Account.from_key("your_private_key_here")

    # Get ETH balance
    balance = await chain.get_balance(account.address)
    print(f"ETH Balance: {balance}")

if __name__ == "__main__":
    asyncio.run(main())
```

## Core Components

### Chain Class

The `Chain` class is the central component for blockchain interaction.

#### Connection and Setup

```python
# Basic connection
chain = await Chain.connect(
    rpc="https://mainnet.infura.io/v3/YOUR_PROJECT_ID",
    chain_id=1
)

# Full configuration
chain = await Chain.connect(
    rpc="https://polygon-rpc.com",
    chain_id=137,
    currency="MATIC",
    name="Polygon Mainnet",
    scan="https://polygonscan.com",
    request_kwargs={"timeout": 30}
)

# Alternative: Create then connect
chain = Chain(
    chain_id=1,
    currency="ETH",
    name="Ethereum Mainnet",
    scan="https://etherscan.io"
)
await chain.connect_rpc("https://mainnet.infura.io/v3/YOUR_PROJECT_ID")
```

#### Balance Queries

```python
# Get native currency balance (ETH, MATIC, etc.)
eth_balance = await chain.get_balance("0x742d35Cc6aF4c4a7E3F4BA9814d7492A9cC6F8c0")
print(f"Balance: {eth_balance.to_fixed(4)} ETH")

# Get balance for an Account instance
account = Account.from_key("0x1234...")
balance = await chain.get_balance(account)
print(f"Account balance: {balance}")

# Get token balance (covered in Token section)
usdc = await chain.load_token("0xA0b86a33E6441b8e776f1b0b8c8e6e8b8e8e8e8e")
token_balance = await chain.get_balance(account.address, usdc)
print(f"USDC Balance: {token_balance}")
```

#### Transaction Operations

```python
# Send raw transaction
tx_params = {
    "to": "0x742d35Cc6aF4c4a7E3F4BA9814d7492A9cC6F8c0",
    "value": chain.currency(0.1).amount,  # 0.1 ETH in wei
    "gas": 21000,
    "gasPrice": 20000000000  # 20 gwei
}

# With account signing
tx_hash = await chain.send_transaction(tx_params, account)
print(f"Transaction sent: {tx_hash.hex()}")

# Wait for confirmation
receipt = await chain.wait_for_transaction_receipt(tx_hash)
print(f"Transaction confirmed in block: {receipt.blockNumber}")

# Get transaction explorer URL
explorer_url = chain.get_tx_scan(tx_hash)
print(f"View on explorer: {explorer_url}")
```

### Account Class

The `Account` class handles private key management and signing operations.

#### Account Creation and Management

```python
# Create from private key
account = Account.from_key("0x1234567890abcdef...")
print(f"Address: {account.address}")

# Create from private key without 0x prefix
account = Account.from_key("1234567890abcdef...")

# Access underlying web3 account properties
private_key = account.key
public_key = account.public_key
```

#### Message Signing

```python
# Sign plain text message
signature = await account.sign("Hello, world!")
print(f"Signature: {signature.hex()}")

# Sign with full signature components
full_sig = await account.sign("Hello, world!", hex_only=False)
print(f"r: {full_sig.r}, s: {full_sig.s}, v: {full_sig.v}")

# Sign hex data
hex_data = "0x1234567890abcdef"
signature = await account.sign(hex_data)

# Sign raw bytes
raw_bytes = b"Hello, world!"
signature = await account.sign(raw_bytes)

# Sign EIP-712 typed data
typed_data = {
    "types": {
        "EIP712Domain": [
            {"name": "name", "type": "string"},
            {"name": "version", "type": "string"},
            {"name": "chainId", "type": "uint256"},
            {"name": "verifyingContract", "type": "address"}
        ],
        "Mail": [
            {"name": "from", "type": "address"},
            {"name": "to", "type": "address"},
            {"name": "contents", "type": "string"}
        ]
    },
    "primaryType": "Mail",
    "domain": {
        "name": "Example DApp",
        "version": "1",
        "chainId": 1,
        "verifyingContract": "0x1234567890123456789012345678901234567890"
    },
    "message": {
        "from": "0x1234567890123456789012345678901234567890",
        "to": "0x0987654321098765432109876543210987654321",
        "contents": "Hello, world!"
    }
}
signature = await account.sign(typed_data)
```

#### Chain Binding

```python
# Bind account to a specific chain
chain_account = account.use_chain(chain)

# Get balances through chain account
eth_balance = await chain_account.get_balance()
token_balance = await chain_account.get_balance(usdc_token)

# Access chain and account properties
print(f"Chain: {chain_account.chain()}")
print(f"Address: {chain_account.address}")
```

#### Multi-Chain Operations

```python
# Temporary signing middleware for multiple chains
ethereum_chain = await Chain.connect("https://mainnet.infura.io/v3/...", 1)
polygon_chain = await Chain.connect("https://polygon-rpc.com", 137)

with account.onchain(ethereum_chain, polygon_chain) as chain_accounts:
    eth_account, poly_account = chain_accounts

    # Transactions are automatically signed within this context
    eth_balance = await eth_account.get_balance()
    poly_balance = await poly_account.get_balance()

    # Send transactions without explicit account parameter
    tx_hash = await ethereum_chain.send_transaction({
        "to": "0x742d35Cc6aF4c4a7E3F4BA9814d7492A9cC6F8c0",
        "value": ethereum_chain.currency(0.1).amount
    })

# Single chain context
with account.onchain(ethereum_chain) as eth_account:
    balance = await eth_account.get_balance()
```

### Token and Currency Classes

#### Currency Operations

```python
# Create currency instances
eth = Currency("Ethereum", "ETH", 18)
usdc = Currency("USD Coin", "USDC", 6)

# Create amounts
eth_amount = eth.parse_amount(1.5)  # 1.5 ETH
usdc_amount = usdc(100.50)  # Shorthand for parse_amount

# Convert raw amounts
raw_wei = 1500000000000000000  # 1.5 ETH in wei
eth_amount = eth.to_amount(raw_wei)

print(f"Amount: {eth_amount}")  # "1.500 ETH"
print(f"Raw value: {eth_amount.amount}")  # 1500000000000000000
print(f"Formatted: {eth_amount.to_fixed(2)}")  # 1.50
```

#### Currency Amount Arithmetic

```python
eth = Currency("Ethereum", "ETH", 18)

# Basic arithmetic
amount1 = eth(1.0)    # 1 ETH
amount2 = eth(0.5)    # 0.5 ETH

total = amount1 + amount2      # 1.5 ETH
difference = amount1 - amount2  # 0.5 ETH
doubled = amount1 * 2          # 2.0 ETH
half = amount1 / 2             # 0.5 ETH

# Comparisons
print(amount1 > amount2)   # True
print(amount1 == eth(1.0)) # True
print(amount1 != amount2)  # True

# Chain operations
result = eth(2.0) + eth(1.5) - eth(0.5)  # 3.0 ETH
print(f"Result: {result.to_fixed(1)}")   # "3.0"
```

#### ERC20 Token Operations

```python
# Load token with automatic metadata fetching
usdc = await chain.load_token(
    "0xA0b86a33E6441b8e776f1b0b8c8e6e8b8e8e8e8e",
    cache_as="usdc"  # Cache as chain.usdc
)

# Load token with predefined metadata (no RPC calls)
custom_token = await chain.load_token(
    "0x1234567890123456789012345678901234567890",
    name="Custom Token",
    symbol="CTK",
    decimals=18
)

# Access cached token
balance = await chain.usdc.get_balance(account.address)

# Token properties
print(f"Token: {usdc.name} ({usdc.symbol})")
print(f"Decimals: {usdc.decimals}")
print(f"Address: {usdc.address}")
print(f"Chain ID: {usdc.chain_id}")
```

#### Token Balance and Transfer Operations

```python
# Get token balance
balance = await usdc.get_balance(account.address)
print(f"USDC Balance: {balance.to_fixed(2)}")

# Create token amounts
amount = usdc(100.50)  # 100.50 USDC
raw_amount = usdc.to_amount(100500000)  # 100.50 USDC (6 decimals)

# Transfer tokens (using TokenAmount)
tx_hash = await amount.transfer(
    account,
    "0x742d35Cc6aF4c4a7E3F4BA9814d7492A9cC6F8c0"
)

# Transfer tokens (using Token directly)
tx_hash = await usdc.functions.transfer(
    "0x742d35Cc6aF4c4a7E3F4BA9814d7492A9cC6F8c0",
    usdc(50.0).amount
).transact(account)
```

#### Token Approval Operations

```python
# Unlimited approval
uniswap_router = "0x7a250d5630B4cF539739dF2C5dAcb4c659F2488D"
tx_hash = await usdc.approve(account, uniswap_router)

# Specific amount approval
tx_hash = await usdc.approve(account, uniswap_router, usdc(1000))

# Check allowance
allowance = await usdc.get_allowance(account.address, uniswap_router)
print(f"Allowance: {allowance.to_fixed(2)} USDC")

# Approve using TokenAmount
amount = usdc(500.0)
tx_hash = amount.approve(account, uniswap_router)
```

### NFT (ERC721) Operations

#### NFT Collection Management

```python
# Load NFT collection
collection = await chain.load_nft721(
    "0xb47e3cd837dDF8e4c57F05d70Ab865de6e193BBB",  # CryptoPunks
    cache_as="cryptopunks"
)

# Collection properties
print(f"Collection: {collection.name}")
print(f"Address: {collection.address}")
print(f"Chain ID: {collection.chain_id}")

# Get collection balance for an address
balance = await collection.get_balance(account.address)
print(f"User owns {balance} NFTs")
```

#### NFT Ownership and Enumeration

```python
# Get all NFTs owned by an address (requires ERC721Enumerable)
owned_nfts = await collection.get_owned_by(account.address)
print(f"Found {len(owned_nfts)} NFTs")

# Using external data provider (faster, requires provider setup)
# owned_nfts = await collection.get_owned_by(account.address, alchemy_provider)

# Work with individual NFTs
for nft in owned_nfts:
    print(f"Token ID: {nft.id}")
    owner = await nft.get_owner()
    print(f"Owner: {owner}")
```

#### Individual NFT Operations

```python
# Get specific NFT by token ID
nft = collection.get_item(123)

# Fetch and cache metadata
await nft.refresh_metadata()

# Access metadata
print(f"Name: {nft.meta.name}")
print(f"Description: {nft.meta.description}")
print(f"Image: {nft.meta.image}")
print(f"Attributes: {nft.meta.attributes}")

# Access parsed attributes
if "Background" in nft.meta.attributes:
    print(f"Background: {nft.meta.attributes['Background']}")

# Get current owner
owner = await nft.get_owner()
print(f"Current owner: {owner}")

# Force refresh owner from blockchain
current_owner = await nft.get_owner(force=True)
```

#### NFT Transfer Operations

```python
# Transfer NFT to another address
tx_hash = await nft.transfer(
    account,
    "0x742d35Cc6aF4c4a7E3F4BA9814d7492A9cC6F8c0"
)

# Transfer with custom gas settings
tx_hash = await nft.transfer(
    account,
    "0x742d35Cc6aF4c4a7E3F4BA9814d7492A9cC6F8c0",
    tx={"gas": 100000, "gasPrice": 20000000000}
)

print(f"Transfer transaction: {tx_hash.hex()}")
```

### Smart Contract Interaction

#### Contract with Known ABI

```python
# Load contract with ABI
erc20_abi = await chain.erc20_abi()
contract = chain.contract("0xA0b86a33E6441b8e776f1b0b8c8e6e8b8e8e8e8e", erc20_abi)

# Call read functions
name = await contract.functions.name().call()
symbol = await contract.functions.symbol().call()
decimals = await contract.functions.decimals().call()
total_supply = await contract.functions.totalSupply().call()

print(f"Token: {name} ({symbol}), Decimals: {decimals}")

# Call with parameters
balance = await contract.functions.balanceOf(account.address).call()
allowance = await contract.functions.allowance(
    account.address,
    "0x7a250d5630B4cF539739dF2C5dAcb4c659F2488D"
).call()
```

#### Contract Transactions

```python
# Build transaction
tx = await contract.functions.transfer(
    "0x742d35Cc6aF4c4a7E3F4BA9814d7492A9cC6F8c0",
    1000000  # 1 USDC (6 decimals)
).build_transaction(account)

# Send transaction directly
tx_hash = await contract.functions.transfer(
    "0x742d35Cc6aF4c4a7E3F4BA9814d7492A9cC6F8c0",
    1000000
).transact(account)

# Transaction with custom parameters
tx_hash = await contract.functions.approve(
    "0x7a250d5630B4cF539739dF2C5dAcb4c659F2488D",
    2**256 - 1  # Max approval
).transact(account, {"gas": 50000})
```

#### Dynamic Contract Interaction (No ABI)

```python
# Contract without ABI
contract = chain.contract("0x1234567890123456789012345678901234567890")

# Define function signatures dynamically
# Format: [input_types, output_type] or [input_types] for transactions

# Read function with return value
balance_fn = contract.functions.balanceOf[['address'], 'uint256']
balance = await balance_fn(account.address).call()

# Write function (transaction)
transfer_fn = contract.functions.transfer[['address', 'uint256']]
tx_hash = await transfer_fn(
    "0x742d35Cc6aF4c4a7E3F4BA9814d7492A9cC6F8c0",
    1000000
).transact(account)

# Complex function signatures
swap_fn = contract.functions.swapExactTokensForTokens[[
    'uint256',      # amountIn
    'uint256',      # amountOutMin
    'address[]',    # path
    'address',      # to
    'uint256'       # deadline
], ['uint256[]']]   # amounts out

amounts = await swap_fn(
    1000000,  # 1 USDC
    950000,   # Min 0.95 USDC out
    ["0xA0b86a33E6441b8e776f1b0b8c8e6e8b8e8e8e8e", "0x..."],
    account.address,
    int(time.time()) + 300  # 5 minutes
).call()
```

#### Static Contract Utilities

```python
# ABI encoding
encoded = Contract.encode(
    ['uint256', 'address', 'string'],
    123,
    "0x742d35Cc6aF4c4a7E3F4BA9814d7492A9cC6F8c0",
    "Hello"
)

# Custom packing (non-standard encoding)
packed = Contract.pack(
    ['uint256', 'address'],
    123,
    "0x742d35Cc6aF4c4a7E3F4BA9814d7492A9cC6F8c0"
)

print(f"Encoded: {encoded}")
print(f"Packed: {packed}")
```

### Batch Operations

Batch operations allow you to group multiple blockchain calls together for improved performance.

#### Basic Batch Usage

```python
# Simple batch context
async with chain.use_batch(max_size=10, max_wait=0.1) as batch:
    # All calls within this context are batched
    balances = await asyncio.gather(
        usdc.get_balance("0x742d35Cc6aF4c4a7E3F4BA9814d7492A9cC6F8c0"),
        usdc.get_balance("0x1234567890123456789012345678901234567890"),
        usdc.get_balance("0x0987654321098765432109876543210987654321")
    )

for i, balance in enumerate(balances):
    print(f"Address {i}: {balance.to_fixed(2)} USDC")
```

#### Advanced Batch Operations

```python
# Batch multiple contract calls
async with chain.use_batch(max_size=20, max_wait=0.05):
    # Token metadata calls
    token_info = await asyncio.gather(
        contract.functions.name().call(),
        contract.functions.symbol().call(),
        contract.functions.decimals().call(),
        contract.functions.totalSupply().call()
    )

    # Multiple balance checks
    addresses = [
        "0x742d35Cc6aF4c4a7E3F4BA9814d7492A9cC6F8c0",
        "0x1234567890123456789012345678901234567890",
        "0x0987654321098765432109876543210987654321"
    ]

    balances = await asyncio.gather(*[
        contract.functions.balanceOf(addr).call()
        for addr in addresses
    ])

name, symbol, decimals, total_supply = token_info
print(f"Token: {name} ({symbol})")
print(f"Total Supply: {total_supply / 10**decimals:,.2f}")

for addr, balance in zip(addresses, balances):
    print(f"{addr}: {balance / 10**decimals:,.2f} {symbol}")
```

#### Batch Configuration

```python
# Small batches with quick execution
async with chain.use_batch(max_size=5, max_wait=0.01):
    # Calls are sent quickly in small batches
    pass

# Large batches with longer wait times
async with chain.use_batch(max_size=50, max_wait=0.5):
    # More calls are grouped together, but with longer delays
    pass

# Access batch instance
async with chain.use_batch() as batch:
    # You can access batch statistics if needed
    result = await contract.functions.balanceOf(account.address).call()
    # batch contains information about the batching process
```

## Advanced Usage Patterns

### Multi-Chain Operations

```python
# Setup multiple chains
ethereum = await Chain.connect("https://mainnet.infura.io/v3/...", 1, name="Ethereum")
polygon = await Chain.connect("https://polygon-rpc.com", 137, name="Polygon")
bsc = await Chain.connect("https://bsc-dataseed.binance.org", 56, name="BSC")

# Load same token on different chains
eth_usdc = await ethereum.load_token("0xA0b86a33E6441b8e776f1b0b8c8e6e8b8e8e8e8e")
poly_usdc = await polygon.load_token("0x2791Bca1f2de4661ED88A30C99A7a9449Aa84174")

# Check balances across chains
account = Account.from_key("0x...")

eth_balance = await eth_usdc.get_balance(account.address)
poly_balance = await poly_usdc.get_balance(account.address)

print(f"Ethereum USDC: {eth_balance.to_fixed(2)}")
print(f"Polygon USDC: {poly_balance.to_fixed(2)}")
```

### Error Handling

```python
from w3ext.exceptions import ChainException, NftException

try:
    # Chain operations
    chain = await Chain.connect("https://invalid-rpc.com", 1)
except ChainException as e:
    print(f"Chain error: {e}")

try:
    # NFT operations
    nft = collection.get_item(999999)
    await nft.refresh_metadata()
except NftException as e:
    print(f"NFT error: {e}")

try:
    # Token operations
    balance = await token.get_balance("invalid_address")
except Exception as e:
    print(f"Token error: {e}")
```

### Custom Gas Strategies

```python
# EIP-1559 transactions (if supported)
if await chain.is_eip1559():
    tx_params = {
        "maxFeePerGas": 30000000000,      # 30 gwei
        "maxPriorityFeePerGas": 2000000000  # 2 gwei
    }
else:
    tx_params = {
        "gasPrice": 20000000000  # 20 gwei
    }

# Use in transactions
tx_hash = await contract.functions.transfer(
    recipient,
    amount
).transact(account, tx_params)
```

### Integration with External Services

```python
# Example: DeFi operations
async def swap_tokens(chain, account, token_in, token_out, amount_in):
    """Example token swap using Uniswap V2 Router"""

    router_address = "0x7a250d5630B4cF539739dF2C5dAcb4c659F2488D"
    router_abi = [...] # Uniswap V2 Router ABI

    router = chain.contract(router_address, router_abi)

    # Approve token spending
    await token_in.approve(account, router_address, amount_in)

    # Get amounts out
    path = [token_in.address, token_out.address]
    amounts_out = await router.functions.getAmountsOut(
        amount_in.amount, path
    ).call()

    min_amount_out = int(amounts_out[1] * 0.95)  # 5% slippage

    # Execute swap
    tx_hash = await router.functions.swapExactTokensForTokens(
        amount_in.amount,
        min_amount_out,
        path,
        account.address,
        int(time.time()) + 300  # 5 minutes deadline
    ).transact(account)

    return tx_hash

# Usage
usdc = await chain.load_token("0xA0b86a33E6441b8e776f1b0b8c8e6e8b8e8e8e8e")
weth = await chain.load_token("0xC02aaA39b223FE8D0A0e5C4F27eAD9083C756Cc2")

tx_hash = await swap_tokens(chain, account, usdc, weth, usdc(100))
```

## API Reference

### Chain Class Methods

- `Chain.connect(rpc, chain_id, *, currency='ETH', scan=None, name=None, request_kwargs=None)` - Create and connect to blockchain
- `chain.connect_rpc(rpc, request_kwargs=None)` - Connect to RPC endpoint
- `chain.load_token(contract, *, cache_as=None, abi=None, name=None, symbol=None, decimals=None, **kwargs)` - Load ERC20 token
- `chain.load_nft721(contract, *, cache_as=None, abi=None)` - Load ERC721 collection
- `chain.get_balance(address, token=None)` - Get balance (native or token)
- `chain.send_transaction(tx, account=None)` - Send transaction
- `chain.send_raw_transaction(data)` - Send raw transaction
- `chain.wait_for_transaction_receipt(tx_hash, timeout=180)` - Wait for confirmation
- `chain.contract(address, abi=None)` - Create contract instance
- `chain.use_batch(max_size=20, max_wait=0.1)` - Batch context manager
- `chain.is_eip1559()` - Check EIP-1559 support
- `chain.get_tx_scan(tx_hash)` - Get explorer URL

### Account Class Methods

- `Account.from_key(private_key)` - Create account from private key
- `account.sign(data, hex_only=True)` - Sign data (supports EIP-712)
- `account.use_chain(chain)` - Bind account to chain
- `account.onchain(*chains)` - Context manager for multi-chain operations

### Token Class Methods

- `token.get_balance(address)` - Get token balance
- `token.approve(account, spender, amount=None, transaction=None)` - Approve spending
- `token.get_allowance(owner, spender)` - Get current allowance
- `token.parse_amount(amount)` - Convert human-readable amount
- `token.to_amount(amount)` - Create TokenAmount from raw value

### Currency/CurrencyAmount Methods

- `currency.parse_amount(amount)` or `currency(amount)` - Create amount
- `currency.to_amount(raw_amount)` - Create amount from raw value
- `amount.to_fixed(decimals=3)` - Format as decimal string
- Arithmetic: `+`, `-`, `*`, `/`
- Comparisons: `>`, `<`, `>=`, `<=`, `==`, `!=`

### Contract Class Methods

- `Contract.encode(types, *values)` - ABI encode values
- `Contract.pack(types, *values)` - Custom pack values
- `contract.functions.method_name(*args)` - Call contract methods
- `contract.functions.method_name[signature]` - Dynamic function calls

### NFT Class Methods

- `collection.get_balance(address)` - Get NFT count
- `collection.get_owned_by(address, provider=None)` - Get owned NFTs
- `collection.get_item(token_id)` - Get specific NFT
- `nft.refresh_metadata()` - Fetch metadata
- `nft.get_owner(force=False)` - Get current owner
- `nft.transfer(account, to, *, tx=None)` - Transfer NFT
- `Nft721.parse_attributes(attrs)` - Parse metadata attributes

## Type Definitions

The library provides extensive type hints for better development experience:

```python
from typing import Optional, Union, List, Dict, Any
from eth_typing import HexAddress, ChecksumAddress
from web3.types import TxParams, TxReceipt, HexBytes

# Common type aliases used throughout the library
Address = Union[HexAddress, ChecksumAddress, str]
TxHash = HexBytes
```

## License

This project is licensed under the MIT License.