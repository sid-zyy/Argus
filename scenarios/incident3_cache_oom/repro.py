"""Sandbox check for incident 3: replay campaign traffic (unique URLs) and measure memory held afterwards.

Usage: python repro.py <repo_dir>. Exit 0 = healthy, 1 = incident reproduced.
"""
import gc
import json
import random
import sys
import tracemalloc

sys.path.insert(0, sys.argv[1])

tracemalloc.start()
from app.catalog import product_page  # noqa: E402

rng = random.Random(5)
requests = 6000
baseline = tracemalloc.get_traced_memory()[0]
for i in range(requests):
    pid = rng.randint(1, 400)
    product_page(pid, f"http://catalog/products/{pid}?utm_source=email&utm_campaign=october-picks&sid={i:08x}")
gc.collect()
held_mb = (tracemalloc.get_traced_memory()[0] - baseline) / 1e6
limit_mb = 8.0  # 400 products fully cached is ~2.5 MB; growth per unique URL means a leak
healthy = held_mb <= limit_mb
print(json.dumps({"requests": requests, "unique_urls": requests, "memory_held_mb": round(held_mb, 1),
                  "limit_mb": limit_mb, "healthy": healthy}))
sys.exit(0 if healthy else 1)
