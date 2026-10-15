"""BSC wallet client for FOMO Trader (MetaMask-compatible keys).

Read-only verification + signing readiness. Live swap execution is a
later phase; this module proves the key works and the chain is reachable:
  - connects to BSC RPC with fallback rotation (same pattern as Solana Rpc)
  - verifies chain id 56
  - reads BNB balance (wei -> BNB)
  - proves local signing with eth_account (no on-chain transaction)

Key material is NEVER logged. Only the address is logged.
"""
import time

from web3 import Web3

from keystore import load_evm_key, short

BSC_CHAIN_ID = 56
DEFAULT_BSC_RPCS = [
    "https://bsc-dataseed.binance.org",
    "https://bsc-dataseed1.binance.org",
    "https://bsc-dataseed2.binance.org",
]


class BscWallet:
    def __init__(self, key_file, rpc_urls=None):
        self.key_bytes, self.address = load_evm_key(key_file)
        self.rpc_urls = rpc_urls or DEFAULT_BSC_RPCS
        self.w3 = None
        self._connect()

    def _connect(self):
        last = None
        for url in self.rpc_urls:
            try:
                w3 = Web3(Web3.HTTPProvider(url, request_kwargs={"timeout": 15}))
                cid = w3.eth.chain_id
                if cid != BSC_CHAIN_ID:
                    raise ValueError("unexpected chain id %s" % cid)
                self.w3 = w3
                self.rpc_url = url
                return
            except Exception as e:
                last = e
        raise ConnectionError("all BSC RPCs failed (last: %s)" % last)

    def bnb_balance(self):
        """BNB balance of the wallet as float."""
        return self.w3.eth.get_balance(self.address) / 1e18

    def sign_proof(self):
        """Local sign/verify round-trip. No chain interaction, no gas."""
        from eth_account.messages import encode_defunct
        msg = encode_defunct(text="fomo-trader key check %d" % int(time.time()))
        signed = self.w3.eth.account.sign_message(msg, private_key=self.key_bytes)
        from eth_account import Account
        recovered = Account.recover_message(msg, signature=signed.signature)
        assert recovered == self.address, "signature did not recover our address"
        return True

    def status_line(self):
        return "bsc %s | BNB %.4f" % (short(self.address), self.bnb_balance())
