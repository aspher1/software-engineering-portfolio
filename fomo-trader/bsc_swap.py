"""PancakeSwap V2 swap client for BSC (FOMO Trader).

Paper-first: quotes are read-only eth_call, no key needed. Live swaps need
a key file and stay dormant while dry_run=true.

Mirrors the Solana/Jupiter safety pattern:
  - entry quotes retried 3x with backoff; transport failure ("API
    unreachable") is logged separately from "no route"
  - honeypot check = buy quote + immediate sell quote on the proceeds.
    If the round trip comes back dust, the token is a honeypot (or has a
    99% sell tax) and we fail closed.
"""

import random
import time

from web3 import Web3

PANCAKE_V2_ROUTER = "0x10ED43C718714eb63d5aA57B78B54704E256024E"
# == PANCAKE_V2_ROUTER.factory() on BSC mainnet
PANCAKE_V2_FACTORY = "0xcA143Ce32Fe78f1f7019d7d551a6402fC5350c73"
WBNB = "0xbb4CdB9CBd36B01bD1cBaEBF2De08d9173bc095c"
DEAD_ADDRESS = "0x000000000000000000000000000000000000dEaD"
ZERO_ADDRESS = "0x0000000000000000000000000000000000000000"
BSC_CHAIN_ID = 56
LP_BURN_THRESHOLD_PCT = 50.0
# Wall-clock budget across the sequential LP-lock reads; no retries.
LP_LOCK_BUDGET_SEC = 8.0

DEFAULT_BSC_RPCS = [
    "https://bsc-dataseed.binance.org",
    "https://bsc-dataseed1.binance.org",
    "https://bsc-dataseed2.binance.org",
]

_ROUTER_ABI = [
    {"name": "getAmountsOut", "type": "function", "stateMutability": "view",
     "inputs": [{"name": "amountIn", "type": "uint256"},
                {"name": "path", "type": "address[]"}],
     "outputs": [{"name": "amounts", "type": "uint256[]"}]},
    {"name": "swapExactETHForTokens", "type": "function",
     "stateMutability": "payable",
     "inputs": [{"name": "amountOutMin", "type": "uint256"},
                {"name": "path", "type": "address[]"},
                {"name": "to", "type": "address"},
                {"name": "deadline", "type": "uint256"}],
     "outputs": [{"name": "amounts", "type": "uint256[]"}]},
    {"name": "swapExactTokensForETHSupportingFeeOnTransferTokens",
     "type": "function", "stateMutability": "nonpayable",
     "inputs": [{"name": "amountIn", "type": "uint256"},
                {"name": "amountOutMin", "type": "uint256"},
                {"name": "path", "type": "address[]"},
                {"name": "to", "type": "address"},
                {"name": "deadline", "type": "uint256"}],
     "outputs": []},
]

_ERC20_ABI = [
    {"name": "decimals", "type": "function", "stateMutability": "view",
     "inputs": [], "outputs": [{"name": "", "type": "uint8"}]},
    {"name": "allowance", "type": "function", "stateMutability": "view",
     "inputs": [{"name": "owner", "type": "address"},
                {"name": "spender", "type": "address"}],
     "outputs": [{"name": "", "type": "uint256"}]},
    {"name": "approve", "type": "function", "stateMutability": "nonpayable",
     "inputs": [{"name": "spender", "type": "address"},
                {"name": "amount", "type": "uint256"}],
     "outputs": [{"name": "", "type": "bool"}]},
]

_FACTORY_ABI = [
    {"name": "getPair", "type": "function", "stateMutability": "view",
     "inputs": [{"name": "tokenA", "type": "address"},
                {"name": "tokenB", "type": "address"}],
     "outputs": [{"name": "pair", "type": "address"}]},
]

_PAIR_ABI = [
    {"name": "totalSupply", "type": "function", "stateMutability": "view",
     "inputs": [], "outputs": [{"name": "", "type": "uint256"}]},
    {"name": "balanceOf", "type": "function", "stateMutability": "view",
     "inputs": [{"name": "owner", "type": "address"}],
     "outputs": [{"name": "", "type": "uint256"}]},
]


def _log(msg):
    print("[%s] %s" % (time.strftime("%H:%M:%S"), msg), flush=True)


class BscSwap:
    def __init__(self, rpc_urls=None, key_file=None):
        self.rpc_urls = rpc_urls or DEFAULT_BSC_RPCS
        self.key_file = key_file
        self._key_bytes = None
        self._address = None
        self._decimals_cache = {}
        self._connect()

    def _connect(self):
        last = None
        for url in self.rpc_urls:
            try:
                w3 = Web3(Web3.HTTPProvider(url, request_kwargs={"timeout": 15}))
                if w3.eth.chain_id != BSC_CHAIN_ID:
                    raise ValueError("unexpected chain id %s" % w3.eth.chain_id)
                self.w3 = w3
                self.rpc_url = url
                self.router = w3.eth.contract(
                    address=Web3.to_checksum_address(PANCAKE_V2_ROUTER),
                    abi=_ROUTER_ABI)
                return
            except Exception as e:
                last = e
        raise ConnectionError("all BSC RPCs failed (last: %s)" % last)

    # -- key (live swaps only; never needed for quotes or paper mode) --
    def _ensure_key(self):
        if self._key_bytes is None:
            if not self.key_file:
                raise RuntimeError("no BSC key file configured")
            from keystore import load_evm_key
            self._key_bytes, self._address = load_evm_key(self.key_file)
        return self._key_bytes, self._address

    # -- read-only --
    def token_decimals(self, token):
        token = Web3.to_checksum_address(token)
        if token not in self._decimals_cache:
            c = self.w3.eth.contract(address=token, abi=_ERC20_ABI)
            self._decimals_cache[token] = c.functions.decimals().call()
        return self._decimals_cache[token]

    def _amounts_out(self, amount_in, path, tries=3):
        last = None
        for i in range(tries):
            try:
                return self.router.functions.getAmountsOut(
                    amount_in, [Web3.to_checksum_address(p) for p in path]
                ).call()
            except Exception as e:
                last = e
                time.sleep(0.5 * (2 ** i) + random.uniform(0, 0.3))
        raise last

    def quote_buy(self, token, bnb_wei):
        """Tokens out for bnb_wei in. Raises on failure (fail closed)."""
        return self._quote_amount(int(bnb_wei), [WBNB, token])

    def quote_sell(self, token, tokens_raw):
        """BNB wei out for tokens_raw in. Raises on failure (fail closed)."""
        return self._quote_amount(int(tokens_raw), [token, WBNB])

    def _quote_amount(self, amount_in, path):
        if amount_in <= 0:
            raise ValueError("quote input must be positive")
        amounts = self._amounts_out(amount_in, path)
        if (not isinstance(amounts, (list, tuple)) or len(amounts) != 2
                or any(type(value) is not int or value <= 0 for value in amounts)
                or amounts[0] != amount_in):
            raise ValueError("invalid BSC router quote")
        return amounts[1]

    def honeypot_check(self, token, name, bnb_wei, min_roundtrip_pct=50):
        """Buy-quote then immediate sell-quote on the proceeds. A honeypot
        (or 99% sell tax) round-trips to dust. Returns (ok, reason)."""
        tag = name or token[:10]
        try:
            tokens_out = self.quote_buy(token, bnb_wei)
        except Exception as e:
            return False, "quote unreachable (%s: %s)" % (
                type(e).__name__, str(e)[:80])
        if not tokens_out:
            return False, "no buy route"
        try:
            bnb_back = self.quote_sell(token, tokens_out)
        except Exception:
            # can buy but not sell = classic honeypot signature
            return False, "sell route missing (honeypot)"
        rt_pct = 100.0 * bnb_back / bnb_wei if bnb_wei else 0
        if rt_pct < min_roundtrip_pct:
            return False, "round trip only %.1f%% (honeypot/tax)" % rt_pct
        return True, "round trip %.1f%%" % rt_pct

    def lp_lock_status(self, token, threshold_pct=LP_BURN_THRESHOLD_PCT):
        """Share of the token/WBNB PancakeSwap V2 LP sent to 0x...dEaD.

        Read-only eth_calls on self.w3, sequential, no retries, no key.
        Returns {"pair": None, "verdict": "no_pair"},
        {"pair", "burn_pct", "verdict": "burned"|"unlocked"}, or
        {"verdict": "unknown", "error": str}. Never raises.
        """
        try:
            t0 = time.time()

            def _budget():
                if time.time() - t0 > LP_LOCK_BUDGET_SEC:
                    raise TimeoutError("lp lock budget %.0fs exceeded"
                                       % LP_LOCK_BUDGET_SEC)

            factory = self.w3.eth.contract(
                address=Web3.to_checksum_address(PANCAKE_V2_FACTORY),
                abi=_FACTORY_ABI)
            pair = factory.functions.getPair(
                Web3.to_checksum_address(token),
                Web3.to_checksum_address(WBNB)).call()
            if not pair or str(pair).lower() == ZERO_ADDRESS:
                return {"pair": None, "verdict": "no_pair"}
            pair = Web3.to_checksum_address(pair)
            _budget()
            lp = self.w3.eth.contract(address=pair, abi=_PAIR_ABI)
            total = lp.functions.totalSupply().call()
            _budget()
            dead = lp.functions.balanceOf(
                Web3.to_checksum_address(DEAD_ADDRESS)).call()
            if type(total) is not int or type(dead) is not int:
                raise ValueError("non-integer LP supply/balance")
            if total <= 0 or dead < 0 or dead > total:
                raise ValueError("implausible LP supply %s / dead %s"
                                 % (total, dead))
            burn_pct = dead / total * 100.0
            return {"pair": pair, "burn_pct": burn_pct,
                    "verdict": ("burned" if burn_pct >= float(threshold_pct)
                                else "unlocked")}
        except Exception as e:
            try:
                err = "%s: %s" % (type(e).__name__, str(e)[:120])
            except Exception:
                err = "unprintable error"
            return {"verdict": "unknown", "error": err}

    # -- execution --
    def execute_buy(self, token, bnb_wei, slippage_bps=500, dry_run=True):
        tokens_out = self.quote_buy(token, bnb_wei)
        if dry_run:
            _log("DRY bsc buy %s: %d wei BNB -> %d token raw"
                 % (token[:10], bnb_wei, tokens_out))
            return tokens_out, "dryrun-bsc-%d" % int(time.time())
        key, addr = self._ensure_key()
        amount_out_min = int(tokens_out * (1 - slippage_bps / 10000.0))
        tx = self.router.functions.swapExactETHForTokens(
            amount_out_min,
            [Web3.to_checksum_address(WBNB),
             Web3.to_checksum_address(token)],
            addr, int(time.time()) + 300,
        ).build_transaction({
            "from": addr, "value": int(bnb_wei),
            "gasPrice": self.w3.eth.gas_price, "nonce": self.w3.eth.get_transaction_count(addr),
        })
        tx["gas"] = int(self.w3.eth.estimate_gas(tx) * 1.2)
        sig = self.w3.eth.send_raw_transaction(
            self.w3.eth.account.sign_transaction(tx, key).raw_transaction)
        _log("bsc buy sent: %s" % sig.hex())
        self.w3.eth.wait_for_transaction_receipt(sig, timeout=120)
        return tokens_out, sig.hex()

    def execute_sell(self, token, tokens_raw, slippage_bps=500, dry_run=True):
        bnb_out = self.quote_sell(token, tokens_raw)
        if dry_run:
            _log("DRY bsc sell %s: %d token raw -> %d wei BNB"
                 % (token[:10], tokens_raw, bnb_out))
            return bnb_out, "dryrun-bsc-%d" % int(time.time())
        key, addr = self._ensure_key()
        token = Web3.to_checksum_address(token)
        erc = self.w3.eth.contract(address=token, abi=_ERC20_ABI)
        router_addr = Web3.to_checksum_address(PANCAKE_V2_ROUTER)
        if erc.functions.allowance(addr, router_addr).call() < tokens_raw:
            _log("approving router for %s" % token[:10])
            atx = erc.functions.approve(
                router_addr, tokens_raw).build_transaction({
                    "from": addr, "gasPrice": self.w3.eth.gas_price,
                    "nonce": self.w3.eth.get_transaction_count(addr)})
            atx["gas"] = int(self.w3.eth.estimate_gas(atx) * 1.2)
            asig = self.w3.eth.send_raw_transaction(
                self.w3.eth.account.sign_transaction(atx, key).raw_transaction)
            self.w3.eth.wait_for_transaction_receipt(asig, timeout=120)
        amount_out_min = int(bnb_out * (1 - slippage_bps / 10000.0))
        tx = self.router.functions.swapExactTokensForETHSupportingFeeOnTransferTokens(
            int(tokens_raw), amount_out_min,
            [token, Web3.to_checksum_address(WBNB)],
            addr, int(time.time()) + 300,
        ).build_transaction({
            "from": addr, "gasPrice": self.w3.eth.gas_price,
            "nonce": self.w3.eth.get_transaction_count(addr)})
        tx["gas"] = int(self.w3.eth.estimate_gas(tx) * 1.2)
        sig = self.w3.eth.send_raw_transaction(
            self.w3.eth.account.sign_transaction(tx, key).raw_transaction)
        _log("bsc sell sent: %s" % sig.hex())
        self.w3.eth.wait_for_transaction_receipt(sig, timeout=120)
        return bnb_out, sig.hex()
