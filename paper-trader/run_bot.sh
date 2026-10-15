#!/bin/bash
cd "$(dirname "$0")"
python3 paper_trader.py --config config.json
code=$?
echo "$(date -Iseconds) exit=$code" >> deaths.log
exit $code
