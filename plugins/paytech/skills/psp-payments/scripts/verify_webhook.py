#!/usr/bin/env python3
"""Verify (or compute) the PSP webhook signature.

The PSP sends a `Signature` header with every webhook: an HMAC-SHA256 hash of
the raw JSON body computed with the Shop Signing Key. The output ENCODING (hex
or base64) is not documented, so both are checked; `verify` reports which one
matched — pin your handler to that encoding once you have observed it.

IMPORTANT: always verify against the RAW request body bytes, not a re-serialized
JSON object — any re-ordering or whitespace change breaks the hash.

Usage:
    # verify an inbound webhook (signing key from env PSP_SIGNING_KEY)
    python3 verify_webhook.py verify body.raw.json "<signature-header-value>"

    # print the signature for a body (both hex and base64 encodings)
    python3 verify_webhook.py sign body.raw.json
"""
import base64
import hashlib
import hmac
import os
import sys


def signatures(key: bytes, body: bytes) -> dict:
    digest = hmac.new(key, body, hashlib.sha256).digest()
    return {"hex": digest.hex(), "base64": base64.b64encode(digest).decode()}


def matches(provided, sigs):
    """Return the name of the encoding that matches, or None. Py3.8+ compatible."""
    # hex is case-insensitive; base64 is case-SENSITIVE and must match exactly.
    if hmac.compare_digest(provided.lower(), sigs["hex"]):
        return "hex"
    if hmac.compare_digest(provided, sigs["base64"]):
        return "base64"
    return None


def main():
    argv = sys.argv[1:]
    if len(argv) < 2 or argv[0] not in ("sign", "verify"):
        sys.exit(__doc__)
    mode, body_file = argv[0], argv[1]
    if mode == "verify" and len(argv) < 3:
        sys.exit("error: `verify` needs the Signature header value as the third "
                 "argument.\n\n" + __doc__)

    key = os.environ.get("PSP_SIGNING_KEY")
    if not key:
        sys.exit("error: PSP_SIGNING_KEY is not set. See wl-config.md.")
    try:
        with open(body_file, "rb") as f:
            body = f.read()
    except OSError as e:
        sys.exit(f"error: cannot read {body_file}: {e}")

    sigs = signatures(key.encode(), body)

    if mode == "sign":
        print("hex:   ", sigs["hex"])
        print("base64:", sigs["base64"])
        return

    encoding = matches(argv[2].strip(), sigs)
    if encoding:
        print(f"VALID ({encoding})")
        return
    print("INVALID — check that you hashed the raw body bytes (not re-serialized "
          "JSON) and used the Signing Key for the right environment "
          "(sandbox vs production).")
    sys.exit(1)


if __name__ == "__main__":
    main()
