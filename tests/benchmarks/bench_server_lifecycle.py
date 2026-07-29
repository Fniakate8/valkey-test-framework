"""
Benchmark script to measure actual server lifecycle costs.
Run with: SERVER_VERSION=unstable python bench_server_lifecycle.py

Measures:
1. Per-phase breakdown of a single server lifecycle
2. Reuse vs no-reuse at realistic test counts (10, 31, 100)
3. Cost of different reset strategies (FLUSHALL alone vs FLUSHALL + CONFIG RESETSTAT)
4. Simulated module-load overhead (represents search/json module startup penalty)
"""
import sys
import os
import time

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "..", "src"))

from valkey_test_case import ValkeyServerHandle
from conftest import PortTracker

SERVER_PATH = os.path.join(
    os.path.dirname(os.path.realpath(__file__)),
    "..",
    ".build",
    "binaries",
    os.environ["SERVER_VERSION"],
    "valkey-server",
)

TESTDIR = "test-data"
NUM_ITERATIONS = 5


def ensure_dir(d):
    os.makedirs(d, exist_ok=True)


def bench_single_server_lifecycle(port_tracker, iteration):
    """Measure each phase of a single server start-use-stop cycle."""
    times = {}

    # Phase 1: Port allocation
    t0 = time.perf_counter()
    port = port_tracker.get_unused_port()
    times["port_alloc"] = time.perf_counter() - t0

    # Phase 2: Server creation (object setup, no start yet)
    t0 = time.perf_counter()
    server = ValkeyServerHandle(
        bind_ip="0.0.0.0",
        port=port,
        port_tracker=port_tracker,
        cwd=TESTDIR,
        server_path=SERVER_PATH,
    )
    times["server_create"] = time.perf_counter() - t0

    # Phase 3: Server start (subprocess + wait for ready + connect + ping)
    t0 = time.perf_counter()
    server.start(wait_for_ping=True, connect_client=True)
    times["server_start"] = time.perf_counter() - t0

    # Phase 4: A simple operation (PING + SET + GET)
    t0 = time.perf_counter()
    server.client.ping()
    server.client.set("key", "value")
    server.client.get("key")
    times["test_ops"] = time.perf_counter() - t0

    # Phase 5: FLUSHALL (what reuse would cost between tests)
    t0 = time.perf_counter()
    server.client.flushall()
    times["flushall"] = time.perf_counter() - t0

    # Phase 6: CONFIG RESETSTAT (additional reset for reuse)
    t0 = time.perf_counter()
    server.client.execute_command("CONFIG", "RESETSTAT")
    times["config_resetstat"] = time.perf_counter() - t0

    # Phase 7: Shutdown
    t0 = time.perf_counter()
    server.exit()
    times["shutdown"] = time.perf_counter() - t0

    return times


def bench_reuse_scenario(port_tracker, num_tests, reset_strategy="flushall"):
    """Simulate reusing one server for N tests with reset between each.

    reset_strategy:
      - "flushall": just FLUSHALL
      - "full": FLUSHALL + CONFIG RESETSTAT + CLIENT SETNAME reset
    """
    port = port_tracker.get_unused_port()
    server = ValkeyServerHandle(
        bind_ip="0.0.0.0",
        port=port,
        port_tracker=port_tracker,
        cwd=TESTDIR,
        server_path=SERVER_PATH,
    )

    t_start = time.perf_counter()
    server.start(wait_for_ping=True, connect_client=True)
    startup_time = time.perf_counter() - t_start

    reset_times = []
    test_times = []
    for i in range(num_tests):
        # Simulate a test that creates some data (like search tests do)
        t0 = time.perf_counter()
        server.client.ping()
        # Simulate creating an index worth of keys (search tests create ~6 docs + index)
        for k in range(20):
            server.client.hset(f"doc:{i}:{k}", mapping={"field1": f"value{k}", "field2": f"text data {k}"})
        # Simulate some reads
        for k in range(5):
            server.client.hgetall(f"doc:{i}:{k}")
        test_times.append(time.perf_counter() - t0)

        # Reset between tests
        t0 = time.perf_counter()
        if reset_strategy == "flushall":
            server.client.flushall()
        elif reset_strategy == "full":
            server.client.flushall()
            server.client.execute_command("CONFIG", "RESETSTAT")
        reset_times.append(time.perf_counter() - t0)

    t0 = time.perf_counter()
    server.exit()
    shutdown_time = time.perf_counter() - t0

    return {
        "startup": startup_time,
        "avg_test": sum(test_times) / len(test_times),
        "avg_reset": sum(reset_times) / len(reset_times),
        "shutdown": shutdown_time,
        "total": startup_time + sum(test_times) + sum(reset_times) + shutdown_time,
        "num_tests": num_tests,
    }


def bench_no_reuse_scenario(port_tracker, num_tests):
    """Simulate spawning a fresh server for each of N tests."""
    startup_times = []
    test_times = []
    shutdown_times = []

    for i in range(num_tests):
        port = port_tracker.get_unused_port()
        server = ValkeyServerHandle(
            bind_ip="0.0.0.0",
            port=port,
            port_tracker=port_tracker,
            cwd=TESTDIR,
            server_path=SERVER_PATH,
        )

        t0 = time.perf_counter()
        server.start(wait_for_ping=True, connect_client=True)
        startup_times.append(time.perf_counter() - t0)

        # Same workload as reuse scenario
        t0 = time.perf_counter()
        server.client.ping()
        for k in range(20):
            server.client.hset(f"doc:{i}:{k}", mapping={"field1": f"value{k}", "field2": f"text data {k}"})
        for k in range(5):
            server.client.hgetall(f"doc:{i}:{k}")
        test_times.append(time.perf_counter() - t0)

        t0 = time.perf_counter()
        server.exit()
        shutdown_times.append(time.perf_counter() - t0)

    return {
        "avg_startup": sum(startup_times) / len(startup_times),
        "avg_test": sum(test_times) / len(test_times),
        "avg_shutdown": sum(shutdown_times) / len(shutdown_times),
        "total": sum(startup_times) + sum(test_times) + sum(shutdown_times),
        "num_tests": num_tests,
    }


def bench_flushall_with_data(port_tracker):
    """Measure FLUSHALL cost with varying amounts of data (simulates test residue)."""
    port = port_tracker.get_unused_port()
    server = ValkeyServerHandle(
        bind_ip="0.0.0.0",
        port=port,
        port_tracker=port_tracker,
        cwd=TESTDIR,
        server_path=SERVER_PATH,
    )
    server.start(wait_for_ping=True, connect_client=True)

    results = {}
    for num_keys in [0, 10, 100, 1000, 10000]:
        # Insert data
        if num_keys > 0:
            pipe = server.client.pipeline()
            for k in range(num_keys):
                pipe.set(f"key:{k}", f"value:{k}" * 10)
            pipe.execute()

        # Measure FLUSHALL
        t0 = time.perf_counter()
        server.client.flushall()
        results[num_keys] = time.perf_counter() - t0

    server.exit()
    return results


if __name__ == "__main__":
    ensure_dir(TESTDIR)

    print("=" * 70)
    print("VALKEY SERVER LIFECYCLE BENCHMARK")
    print("=" * 70)

    # ---- Phase 1: Single lifecycle breakdown ----
    print("\n--- Single Server Lifecycle (5 iterations) ---")
    print(f"{'Phase':<20} {'Min':>10} {'Max':>10} {'Avg':>10}")
    print("-" * 52)

    all_times = []
    with PortTracker("bench-single") as pt:
        for i in range(NUM_ITERATIONS):
            times = bench_single_server_lifecycle(pt, i)
            all_times.append(times)

    phases = ["port_alloc", "server_create", "server_start", "test_ops", "flushall", "config_resetstat", "shutdown"]
    for phase in phases:
        values = [t[phase] for t in all_times]
        print(f"{phase:<20} {min(values)*1000:>8.2f}ms {max(values)*1000:>8.2f}ms {sum(values)/len(values)*1000:>8.2f}ms")

    total_per_iter = [sum(t.values()) for t in all_times]
    print(f"{'TOTAL':<20} {min(total_per_iter)*1000:>8.2f}ms {max(total_per_iter)*1000:>8.2f}ms {sum(total_per_iter)/len(total_per_iter)*1000:>8.2f}ms")

    # ---- Phase 2: FLUSHALL cost vs data size ----
    print("\n--- FLUSHALL Cost vs. Data Size ---")
    print(f"{'Keys in DB':<15} {'FLUSHALL time':>15}")
    print("-" * 32)

    with PortTracker("bench-flush") as pt:
        flush_results = bench_flushall_with_data(pt)

    for num_keys, duration in flush_results.items():
        print(f"{num_keys:<15} {duration*1000:>12.2f}ms")

    # ---- Phase 3: Realistic scale comparison ----
    test_counts = [10, 31, 100]

    for num_tests in test_counts:
        print(f"\n{'=' * 70}")
        print(f"--- {num_tests} Tests: Reuse vs. No Reuse ---")
        print(f"    (Simulates valkey-search workload: 20 HSET + 5 HGETALL per test)")
        print(f"{'=' * 70}")

        with PortTracker(f"bench-reuse-{num_tests}") as pt:
            reuse = bench_reuse_scenario(pt, num_tests=num_tests, reset_strategy="flushall")

        with PortTracker(f"bench-reuse-full-{num_tests}") as pt:
            reuse_full = bench_reuse_scenario(pt, num_tests=num_tests, reset_strategy="full")

        with PortTracker(f"bench-no-reuse-{num_tests}") as pt:
            no_reuse = bench_no_reuse_scenario(pt, num_tests=num_tests)

        print(f"\n  WITHOUT REUSE (fresh server per test):")
        print(f"    Avg startup:        {no_reuse['avg_startup']*1000:.2f}ms")
        print(f"    Avg test:           {no_reuse['avg_test']*1000:.2f}ms")
        print(f"    Avg shutdown:       {no_reuse['avg_shutdown']*1000:.2f}ms")
        print(f"    TOTAL:              {no_reuse['total']*1000:.2f}ms")

        print(f"\n  WITH REUSE (FLUSHALL only):")
        print(f"    Startup (once):     {reuse['startup']*1000:.2f}ms")
        print(f"    Avg test:           {reuse['avg_test']*1000:.2f}ms")
        print(f"    Avg FLUSHALL:       {reuse['avg_reset']*1000:.2f}ms")
        print(f"    Shutdown (once):    {reuse['shutdown']*1000:.2f}ms")
        print(f"    TOTAL:              {reuse['total']*1000:.2f}ms")

        print(f"\n  WITH REUSE (FLUSHALL + CONFIG RESETSTAT):")
        print(f"    Startup (once):     {reuse_full['startup']*1000:.2f}ms")
        print(f"    Avg test:           {reuse_full['avg_test']*1000:.2f}ms")
        print(f"    Avg reset:          {reuse_full['avg_reset']*1000:.2f}ms")
        print(f"    Shutdown (once):    {reuse_full['shutdown']*1000:.2f}ms")
        print(f"    TOTAL:              {reuse_full['total']*1000:.2f}ms")

        speedup = no_reuse["total"] / reuse["total"] if reuse["total"] > 0 else float("inf")
        saved = no_reuse["total"] - reuse["total"]
        print(f"\n  SAVINGS (FLUSHALL only): {saved*1000:.1f}ms ({speedup:.1f}x faster)")

        speedup_full = no_reuse["total"] / reuse_full["total"] if reuse_full["total"] > 0 else float("inf")
        saved_full = no_reuse["total"] - reuse_full["total"]
        print(f"  SAVINGS (full reset):    {saved_full*1000:.1f}ms ({speedup_full:.1f}x faster)")

    # ---- Phase 4: Projected savings with module loading ----
    print(f"\n{'=' * 70}")
    print("--- Projected Savings with Module Loading (estimated) ---")
    print("    NOTE: Module .so not available; projections use measured bare startup")
    print("    + estimated module init overhead based on industry benchmarks.")
    print(f"{'=' * 70}")

    bare_startup_avg = sum(t["server_start"] for t in all_times) / len(all_times)
    flush_avg = sum(t["flushall"] for t in all_times) / len(all_times)

    # Module load estimates based on typical Valkey module init times
    module_overheads = {
        "bare valkey (measured)": 0,
        "single module (est. json)": 0.050,       # ~50ms for simple module
        "two modules (est. search+json)": 0.150,  # ~150ms for search + json
    }

    for label, overhead in module_overheads.items():
        startup = bare_startup_avg + overhead
        print(f"\n  {label}:")
        print(f"    Estimated startup: {startup*1000:.1f}ms")
        for num_tests in [31, 100]:
            no_reuse_total = num_tests * (startup + flush_avg + 0.001)  # startup + work + shutdown
            reuse_total = startup + num_tests * (flush_avg + 0.001) + 0.001
            speedup = no_reuse_total / reuse_total
            saved_sec = no_reuse_total - reuse_total
            print(f"    {num_tests} tests: no-reuse={no_reuse_total*1000:.0f}ms, reuse={reuse_total*1000:.0f}ms, savings={saved_sec*1000:.0f}ms ({speedup:.1f}x)")
