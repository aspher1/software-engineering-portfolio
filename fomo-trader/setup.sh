#!/bin/bash
# One-command setup for the FOMO trader.
set -e
cd "$(dirname "$0")"
python3 -m venv .venv
.venv/bin/pip install -q --upgrade pip
.venv/bin/pip install -q solders requests bip-utils web3 base58
if [ ! -f config.json ]; then
  cp config.example.json config.json
  echo "created config.json from example"
fi
echo ""
echo "Setup done. Next:"
echo "  1. Put your Solana keypair JSON (array format) at ~/.config/solana/id.json"
echo "     (or point keypair_path in config.json at your file)"
echo "  2. Edit config.json - trade size, stops, take-profits"
echo "  3. Dry-run first: .venv/bin/python fomo_trader.py --config config.json --hunt"
echo "  4. Go live: set dry_run=false, then .venv/bin/python fomo_trader.py --config config.json"
