"""MetaMask-compatible wallet keys for FOMO Trader.

One MetaMask seed phrase derives both chains' keys (BIP44):
  Solana : m/44'/501'/0'/0'  (ed25519)
  BSC    : m/44'/60'/0'/0/0  (secp256k1, same as Ethereum)

PREFERRED SETUP (least privilege): in MetaMask, export each trading
account's *private key* (Account details -> Show private key) -- one for
the Solana account, one for the BNB Chain account -- and save each to its
own file with 0600 permissions. The bot never needs your seed phrase.

Accepted file formats (whitespace stripped):
  Solana: JSON int array (solana CLI), base58 64-byte secret, base58
          32-byte seed, or hex.
  BSC:    hex with or without 0x prefix.

Key material is NEVER logged. Only (shortened) addresses are logged.
"""
import base58
import json
import os
import stat

from solders.keypair import Keypair


def short(addr, n=6):
    a = str(addr)
    return "%s...%s" % (a[:n], a[-4:]) if len(a) > n + 4 else a


def check_perms(path):
    """Warn if a key file is readable by anyone but the owner."""
    try:
        mode = stat.S_IMODE(os.stat(path).st_mode)
        if mode & 0o077:
            print("WARNING: key file %s has permissions %o; run: chmod 600 %s"
                  % (path, mode, path))
            return False
    except OSError:
        pass
    return True


def _read_secret(path):
    check_perms(path)
    with open(os.path.expanduser(path)) as f:
        return f.read().strip()


def load_solana_keypair(path):
    """Load a Solana keypair from any accepted format. Returns Keypair."""
    raw = _read_secret(path)
    secret = None
    if raw.startswith("["):
        secret = bytes(json.loads(raw))
    else:
        # try base58, then hex
        try:
            secret = base58.b58decode(raw)
        except Exception:
            secret = bytes.fromhex(raw[2:] if raw.startswith(("0x", "0X")) else raw)
    if len(secret) == 64:
        kp = Keypair.from_bytes(secret)
    elif len(secret) == 32:
        kp = Keypair.from_seed(secret)
    else:
        raise ValueError("unrecognized Solana key format (%d bytes)" % len(secret))
    return kp


def load_evm_key(path):
    """Load a BSC/EVM private key. Returns (key_bytes, checksum_address)."""
    from eth_account import Account
    raw = _read_secret(path)
    if raw.startswith(("0x", "0X")):
        raw = raw[2:]
    key_bytes = bytes.fromhex(raw)
    if len(key_bytes) != 32:
        raise ValueError("unrecognized EVM key format (%d bytes)" % len(key_bytes))
    return key_bytes, Account.from_key(key_bytes).address


def derive_from_mnemonic(mnemonic, passphrase=""):
    """Derive (solana Keypair, evm key_bytes) from a BIP39 seed phrase.

    Provided for users who prefer seed-based setup over exporting
    per-account keys. The caller is responsible for handling the mnemonic
    securely -- it must never be logged or written to disk by this module.
    """
    from bip_utils import Bip39SeedGenerator, Bip44, Bip44Coins
    seed = Bip39SeedGenerator(mnemonic).Generate(passphrase)
    sol_raw = (Bip44.FromSeed(seed, Bip44Coins.SOLANA)
               .DeriveDefaultPath().PrivateKey().Raw().ToBytes())
    evm_raw = (Bip44.FromSeed(seed, Bip44Coins.ETHEREUM)
               .DeriveDefaultPath().PrivateKey().Raw().ToBytes())
    return Keypair.from_seed(sol_raw), evm_raw


def evm_address_of(key_bytes):
    from eth_account import Account
    return Account.from_key(key_bytes).address
