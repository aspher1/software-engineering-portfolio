#!/bin/bash
# predict-bot launcher. Idempotent: refuses to start a duplicate.
cd "$(dirname "$0")"
if [ -f runs/paper/bot.pid ] && kill -0 "$(cat runs/paper/bot.pid)" 2>/dev/null; then
  echo "predict-bot already running (pid $(cat runs/paper/bot.pid))"
  exit 0
fi
mkdir -p runs/paper
setsid nohup .venv/bin/python -u -m src.run_loop --loop 600 >> runs/paper/bot.log 2>&1 &
echo $! > runs/paper/bot.pid
echo "predict-bot started (pid $!)"
