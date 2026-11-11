"""
Benchmark: array vs linked list vs hash table lookups.
The classes below are copied from app.py so this file runs on its own.
"""
import time
import csv

# ---------- copied from app.py ----------
class HashTableIndex:
    def __init__(self, size=101):
        self.size = size
        self.buckets = [[] for _ in range(size)]

    def _hash(self, key):
        return hash(key) % self.size

    def insert(self, key, record):
        idx = self._hash(key)
        for i, (k, _) in enumerate(self.buckets[idx]):
            if k == key:
                self.buckets[idx][i] = (key, record)
                return
        self.buckets[idx].append((key, record))

    def search(self, key):
        idx = self._hash(key)
        for k, record in self.buckets[idx]:
            if k == key:
                return record
        return None


class Node:
    def __init__(self, student):
        self.student = student
        self.next = None


def linked_list_search(head, admission_no):
    current = head
    while current:
        if current.student["admission_no"] == admission_no:
            return current.student
        current = current.next
    return None
# ----------------------------------------


def array_search(records, admission_no):
    for r in records:
        if r["admission_no"] == admission_no:
            return r
    return None


def make_records(n):
    return [{"admission_no": f"DCS-01-{i:06d}/2025", "full_name": f"Student {i}"} for i in range(n)]


def timeit(fn, repeats):
    start = time.perf_counter()
    for _ in range(repeats):
        fn()
    return (time.perf_counter() - start) / repeats * 1_000_000  # microseconds per lookup


SIZES = [1_000, 10_000, 100_000]
MISSING = "DCS-99-999999/2099"   # never present, so every structure does its full search work

results = []
for n in SIZES:
    records = make_records(n)

    # linked list (built directly; add_record's duplicate check would make building O(n^2))
    head = None
    for r in records:
        node = Node(r)
        node.next = head
        head = node

    # hash table with your current fixed 101 buckets
    ht_fixed = HashTableIndex(101)
    # hash table sized to the data (what resizing would give)
    ht_scaled = HashTableIndex(n)
    for r in records:
        ht_fixed.insert(r["admission_no"], r)
        ht_scaled.insert(r["admission_no"], r)

    slow_repeats = max(3, 200_000 // n)
    fast_repeats = 5_000

    row = {
        "records": n,
        "array_us": timeit(lambda: array_search(records, MISSING), slow_repeats),
        "linked_list_us": timeit(lambda: linked_list_search(head, MISSING), slow_repeats),
        "hash_101_buckets_us": timeit(lambda: ht_fixed.search(MISSING), fast_repeats),
        "hash_scaled_us": timeit(lambda: ht_scaled.search(MISSING), fast_repeats),
    }
    results.append(row)

print()
print(f"{'Records':>9} | {'Array':>12} | {'Linked list':>12} | {'Hash (101)':>12} | {'Hash (scaled)':>13}   (microseconds per lookup)")
print("-" * 82)
for r in results:
    print(f"{r['records']:>9,} | {r['array_us']:>12.2f} | {r['linked_list_us']:>12.2f} | {r['hash_101_buckets_us']:>12.2f} | {r['hash_scaled_us']:>13.2f}")

with open("benchmark_results.csv", "w", newline="") as f:
    w = csv.DictWriter(f, fieldnames=list(results[0].keys()))
    w.writeheader()
    w.writerows(results)
print("\nSaved benchmark_results.csv")

try:
    import matplotlib
    matplotlib.use("Agg")
    import matplotlib.pyplot as plt
    xs = [r["records"] for r in results]
    plt.figure(figsize=(8, 5))
    plt.plot(xs, [r["array_us"] for r in results], marker="o", label="Array (linear scan)", color="#E15241")
    plt.plot(xs, [r["linked_list_us"] for r in results], marker="o", label="Linked list traversal", color="#4A90D9")
    plt.plot(xs, [r["hash_101_buckets_us"] for r in results], marker="o", label="Hash table (101 buckets)", color="#27AE60")
    plt.plot(xs, [r["hash_scaled_us"] for r in results], marker="o", label="Hash table (sized to data)", color="#2C5F8A")
    plt.xscale("log"); plt.yscale("log")
    plt.xlabel("Number of records"); plt.ylabel("Time per lookup (microseconds)")
    plt.title("Lookup time by data structure")
    plt.legend(); plt.grid(True, which="both", alpha=0.3)
    plt.tight_layout()
    plt.savefig("benchmark_chart.png", dpi=150)
    print("Saved benchmark_chart.png")
except ImportError:
    print("matplotlib not installed - skipping chart (pip install matplotlib to get it)")