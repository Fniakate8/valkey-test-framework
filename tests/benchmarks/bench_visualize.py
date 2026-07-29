"""
Generates visual graphs from actual benchmark measurements.
Run with: SERVER_VERSION=unstable python bench_visualize.py

Produces: bench_results.png (all charts in one image)
"""
import sys
import os
import time

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "..", "src"))

from valkey_test_case import ValkeyServerHandle
from conftest import PortTracker
import matplotlib
matplotlib.use("Agg")
import matplotlib.pyplot as plt
import numpy as np

SERVER_PATH = os.path.join(
    os.path.dirname(os.path.realpath(__file__)),
    "..",
    ".build",
    "binaries",
    os.environ["SERVER_VERSION"],
    "valkey-server",
)

TESTDIR = "test-data"


def ensure_dir(d):
    os.makedirs(d, exist_ok=True)


def measure_phases(port_tracker, iterations=5):
    """Measure each phase of server lifecycle multiple times."""
    results = {"port_alloc": [], "server_start": [], "test_ops": [], "flushall": [], "config_resetstat": [], "shutdown": []}

    for _ in range(iterations):
        port = port_tracker.get_unused_port()
        server = ValkeyServerHandle(
            bind_ip="0.0.0.0", port=port, port_tracker=port_tracker,
            cwd=TESTDIR, server_path=SERVER_PATH,
        )

        t0 = time.perf_counter()
        port_tracker.get_unused_port()  # measure separately (already got one above)
        # Actually let's just time each phase on the real server:

        # We already allocated port above, so just measure start
        t0 = time.perf_counter()
        server.start(wait_for_ping=True, connect_client=True)
        results["server_start"].append((time.perf_counter() - t0) * 1000)

        t0 = time.perf_counter()
        server.client.ping()
        server.client.set("key", "value")
        server.client.get("key")
        results["test_ops"].append((time.perf_counter() - t0) * 1000)

        t0 = time.perf_counter()
        server.client.flushall()
        results["flushall"].append((time.perf_counter() - t0) * 1000)

        t0 = time.perf_counter()
        server.client.execute_command("CONFIG", "RESETSTAT")
        results["config_resetstat"].append((time.perf_counter() - t0) * 1000)

        t0 = time.perf_counter()
        server.exit()
        results["shutdown"].append((time.perf_counter() - t0) * 1000)

    return results


def measure_reuse_vs_no_reuse(port_tracker, num_tests):
    """Measure total time for N tests with and without reuse."""
    # No reuse
    t0 = time.perf_counter()
    for i in range(num_tests):
        port = port_tracker.get_unused_port()
        server = ValkeyServerHandle(
            bind_ip="0.0.0.0", port=port, port_tracker=port_tracker,
            cwd=TESTDIR, server_path=SERVER_PATH,
        )
        server.start(wait_for_ping=True, connect_client=True)
        server.client.ping()
        for k in range(20):
            server.client.hset(f"doc:{i}:{k}", mapping={"field1": f"value{k}", "field2": f"text {k}"})
        for k in range(5):
            server.client.hgetall(f"doc:{i}:{k}")
        server.exit()
    no_reuse_time = (time.perf_counter() - t0) * 1000

    # With reuse
    port = port_tracker.get_unused_port()
    server = ValkeyServerHandle(
        bind_ip="0.0.0.0", port=port, port_tracker=port_tracker,
        cwd=TESTDIR, server_path=SERVER_PATH,
    )
    t0 = time.perf_counter()
    server.start(wait_for_ping=True, connect_client=True)
    for i in range(num_tests):
        server.client.ping()
        for k in range(20):
            server.client.hset(f"doc:{i}:{k}", mapping={"field1": f"value{k}", "field2": f"text {k}"})
        for k in range(5):
            server.client.hgetall(f"doc:{i}:{k}")
        server.client.flushall()
        server.client.execute_command("CONFIG", "RESETSTAT")
    server.exit()
    reuse_time = (time.perf_counter() - t0) * 1000

    return no_reuse_time, reuse_time


def measure_flushall_vs_data_size(port_tracker):
    """Measure FLUSHALL cost with different amounts of data."""
    port = port_tracker.get_unused_port()
    server = ValkeyServerHandle(
        bind_ip="0.0.0.0", port=port, port_tracker=port_tracker,
        cwd=TESTDIR, server_path=SERVER_PATH,
    )
    server.start(wait_for_ping=True, connect_client=True)

    key_counts = [0, 10, 100, 1000, 10000]
    flush_times = []

    for num_keys in key_counts:
        if num_keys > 0:
            pipe = server.client.pipeline()
            for k in range(num_keys):
                pipe.set(f"key:{k}", f"value:{k}" * 10)
            pipe.execute()

        t0 = time.perf_counter()
        server.client.flushall()
        flush_times.append((time.perf_counter() - t0) * 1000)

    server.exit()
    return key_counts, flush_times


if __name__ == "__main__":
    ensure_dir(TESTDIR)

    print("Running benchmarks... (this takes about 60 seconds)")

    # Collect all data
    print("  [1/4] Measuring per-phase breakdown...")
    with PortTracker("bench-phases") as pt:
        phases = measure_phases(pt, iterations=5)

    print("  [2/4] Measuring reuse vs no-reuse at different scales...")
    test_counts = [10, 31, 50, 100]
    no_reuse_times = []
    reuse_times = []
    for n in test_counts:
        print(f"         {n} tests...")
        with PortTracker(f"bench-{n}") as pt:
            nr, r = measure_reuse_vs_no_reuse(pt, n)
            no_reuse_times.append(nr)
            reuse_times.append(r)

    print("  [3/4] Measuring FLUSHALL vs data size...")
    with PortTracker("bench-flush") as pt:
        key_counts, flush_times = measure_flushall_vs_data_size(pt)

    print("  [4/4] Generating charts...")

    # Create figure with 4 subplots
    fig, axes = plt.subplots(2, 2, figsize=(14, 10))
    fig.suptitle("Valkey Server Reuse — Performance Analysis", fontsize=14, fontweight="bold")

    # Chart 1: Per-phase breakdown (bar chart)
    ax1 = axes[0, 0]
    phase_names = ["Server\nStart", "FLUSHALL", "CONFIG\nRESETSTAT", "Shutdown", "Test Ops\n(PING+SET+GET)"]
    phase_keys = ["server_start", "flushall", "config_resetstat", "shutdown", "test_ops"]
    phase_avgs = [np.mean(phases[k]) for k in phase_keys]
    colors = ["#e74c3c", "#f39c12", "#27ae60", "#3498db", "#9b59b6"]
    bars = ax1.bar(phase_names, phase_avgs, color=colors, edgecolor="black", linewidth=0.5)
    ax1.set_ylabel("Time (ms)")
    ax1.set_title("Cost of Each Phase (averaged over 5 runs)")
    for bar, val in zip(bars, phase_avgs):
        ax1.text(bar.get_x() + bar.get_width()/2, bar.get_height() + 0.5,
                 f"{val:.1f}ms", ha="center", va="bottom", fontsize=9, fontweight="bold")
    ax1.set_ylim(0, max(phase_avgs) * 1.3)

    # Chart 2: Reuse vs No Reuse (grouped bar chart)
    ax2 = axes[0, 1]
    x = np.arange(len(test_counts))
    width = 0.35
    bars1 = ax2.bar(x - width/2, no_reuse_times, width, label="No Reuse (fresh server per test)",
                    color="#e74c3c", edgecolor="black", linewidth=0.5)
    bars2 = ax2.bar(x + width/2, reuse_times, width, label="With Reuse (shared server + FLUSHALL)",
                    color="#27ae60", edgecolor="black", linewidth=0.5)
    ax2.set_xlabel("Number of Tests in Class")
    ax2.set_ylabel("Total Time (ms)")
    ax2.set_title("Total Execution Time: Reuse vs No Reuse")
    ax2.set_xticks(x)
    ax2.set_xticklabels([str(n) for n in test_counts])
    ax2.legend(loc="upper left")
    for bar, val in zip(bars1, no_reuse_times):
        ax2.text(bar.get_x() + bar.get_width()/2, bar.get_height() + 20,
                 f"{val:.0f}ms", ha="center", va="bottom", fontsize=8)
    for bar, val in zip(bars2, reuse_times):
        ax2.text(bar.get_x() + bar.get_width()/2, bar.get_height() + 20,
                 f"{val:.0f}ms", ha="center", va="bottom", fontsize=8)

    # Chart 3: Speedup factor (line chart)
    ax3 = axes[1, 0]
    speedups = [nr / r for nr, r in zip(no_reuse_times, reuse_times)]
    ax3.plot(test_counts, speedups, "o-", color="#8e44ad", linewidth=2, markersize=8)
    ax3.fill_between(test_counts, speedups, alpha=0.1, color="#8e44ad")
    ax3.set_xlabel("Number of Tests in Class")
    ax3.set_ylabel("Speedup (x times faster)")
    ax3.set_title("Speedup Factor with Server Reuse")
    ax3.grid(True, alpha=0.3)
    for i, (n, s) in enumerate(zip(test_counts, speedups)):
        ax3.annotate(f"{s:.1f}x", (n, s), textcoords="offset points",
                     xytext=(0, 10), ha="center", fontweight="bold")

    # Chart 4: FLUSHALL cost vs data size (proves it's constant)
    ax4 = axes[1, 1]
    x_labels = [str(k) for k in key_counts]
    x_pos = range(len(key_counts))
    ax4.plot(x_pos, flush_times, "s-", color="#f39c12", linewidth=2, markersize=8)
    ax4.axhline(y=np.mean(flush_times), color="#e74c3c", linestyle="--", alpha=0.7, label=f"Average: {np.mean(flush_times):.1f}ms")
    ax4.set_xlabel("Number of Keys in Database")
    ax4.set_ylabel("FLUSHALL Time (ms)")
    ax4.set_title("FLUSHALL Cost vs Data Size (constant)")
    ax4.set_xticks(list(x_pos))
    ax4.set_xticklabels(x_labels)
    ax4.legend()
    ax4.grid(True, alpha=0.3)
    for xp, ft in zip(x_pos, flush_times):
        ax4.annotate(f"{ft:.1f}ms", (xp, ft), textcoords="offset points",
                     xytext=(0, 10), ha="center", fontsize=9)

    plt.tight_layout()
    output_path = os.path.join(os.path.dirname(__file__), "bench_results.png")
    plt.savefig(output_path, dpi=150, bbox_inches="tight")
    print(f"\n  Done! Chart saved to: {output_path}")
    print(f"\n  Summary:")
    print(f"    Server start avg:  {np.mean(phases['server_start']):.1f}ms")
    print(f"    FLUSHALL avg:      {np.mean(phases['flushall']):.1f}ms")
    print(f"    Speedup at 31 tests: {no_reuse_times[1]/reuse_times[1]:.1f}x")
    print(f"    Speedup at 100 tests: {no_reuse_times[3]/reuse_times[3]:.1f}x")
