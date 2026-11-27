"""Wallet: key from the environment only, transaction signing, and Solana RPC balance reads.

The private key is read from MEMEBOT_PRIVATE_KEY (base58 secret key as exported by Phantom/Solflare,
or a JSON array of 64 numbers as in a solana-keygen file). It is never written, logged or printed.
"""
from __future__ import annotations

import base64
import json
import logging
from typing import Any

import httpx

from .net import FetchError, request_json

log = logging.getLogger(__name__)


class WalletError(RuntimeError):
    pass


def load_keypair(secret: str):
    from solders.keypair import Keypair

    s = secret.strip()
    try:
        if s.startswith("["):
            arr = json.loads(s)
            return Keypair.from_bytes(bytes(arr))
        return Keypair.from_base58_string(s)
    except Exception:
        # Do not chain the exception: its text could include key material.
        raise WalletError("MEMEBOT_PRIVATE_KEY is not a valid Solana secret key "
                          "(expected base58 or a JSON array of 64 numbers)") from None


def sign_transaction_b64(tx_b64: str, keypair) -> str:
    """Sign a base64 VersionedTransaction from Jupiter. Keeps any signatures already present
    (some routes are partially signed by Jupiter) and fills in ours at our signer index."""
    from solders.message import to_bytes_versioned
    from solders.transaction import VersionedTransaction

    try:
        tx = VersionedTransaction.from_bytes(base64.b64decode(tx_b64))
    except Exception:
        raise WalletError("Jupiter returned a transaction that could not be decoded") from None
    msg = tx.message
    n_signers = msg.header.num_required_signatures
    signers = list(msg.account_keys[:n_signers])
    me = keypair.pubkey()
    if me not in signers:
        raise WalletError("Jupiter transaction does not list this wallet as a signer; refusing to sign")
    sigs = list(tx.signatures)
    sigs[signers.index(me)] = keypair.sign_message(to_bytes_versioned(msg))
    signed = VersionedTransaction.populate(msg, sigs)
    return base64.b64encode(bytes(signed)).decode()


class Rpc:
    def __init__(self, client: httpx.AsyncClient, url: str):
        self.client = client
        self.url = url

    async def call(self, method: str, params: list) -> Any:
        d = await request_json(self.client, "POST", self.url, label=f"rpc {method}",
                               json_body={"jsonrpc": "2.0", "id": 1, "method": method, "params": params})
        if not isinstance(d, dict) or "error" in d:
            raise FetchError(f"rpc {method}: {(d or {}).get('error', 'bad response')}")
        return d.get("result")

    async def sol_balance(self, owner: str) -> float:
        r = await self.call("getBalance", [owner, {"commitment": "confirmed"}])
        return int(r["value"]) / 1e9

    async def token_balance(self, owner: str, mint: str) -> tuple[int, int]:
        """(raw amount, decimals) summed over all token accounts of `mint` owned by `owner`."""
        r = await self.call("getTokenAccountsByOwner",
                            [owner, {"mint": mint}, {"encoding": "jsonParsed", "commitment": "confirmed"}])
        raw, dec = 0, 0
        for acc in r.get("value", []):
            info = acc["account"]["data"]["parsed"]["info"]["tokenAmount"]
            raw += int(info["amount"])
            dec = int(info["decimals"])
        return raw, dec

    async def tx_fee_lamports(self, signature: str) -> int | None:
        try:
            r = await self.call("getTransaction", [signature, {"encoding": "json", "commitment": "confirmed",
                                                               "maxSupportedTransactionVersion": 0}])
            return int(r["meta"]["fee"]) if r else None
        except (FetchError, KeyError, TypeError, ValueError):
            return None
