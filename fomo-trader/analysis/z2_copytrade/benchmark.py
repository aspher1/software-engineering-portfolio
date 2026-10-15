"""Offline generation benchmark; synthetic data only."""
import json
from pathlib import Path
from statistics import median
from time import perf_counter_ns
from .sources import LeaderTrade
from .copytrade import CopyConfig, generate_opportunities


class SyntheticPrices:
    def price_at(self, mint, ts): return 1.0


def benchmark(path=None):
    address = 'A' * 32
    trades = []
    for i in range(5000):
        trades.extend((LeaderTrade(address, 'M', 'BUY', 1000+i*120, 1, 1, f'b{i}'),
                       LeaderTrade(address, 'M', 'SELL', 1060+i*120, 1, 1, f's{i}')))
    # 100 independent batches of 100 leader trades (50 closed opportunities).
    # p50/p99 are per-opportunity wall-time approximations, including sorting.
    samples = []
    for start in range(0, len(trades), 100):
        t0 = perf_counter_ns()
        got = generate_opportunities(trades[start:start+100], SyntheticPrices(), CopyConfig(30))
        samples.append((perf_counter_ns()-t0)/len(got.opportunities)/1e6)
    samples.sort()
    result = {'data': 'synthetic', 'leader_trades': len(trades), 'opportunities': 5000,
              'p50_ms_per_opportunity': median(samples), 'p99_ms_per_opportunity': samples[98],
              'note': 'Offline generation only; a live version must prove <50 ms decision budget.'}
    if path:
        Path(path).write_text(json.dumps(result, indent=2) + '\n')
    return result


if __name__ == '__main__':
    print(json.dumps(benchmark(Path(__file__).parent / 'latency.json'), indent=2))
