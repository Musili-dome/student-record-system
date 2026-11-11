"""
Capacity test: how many student records can the system hold?

This script adds fake students through the same steps the app uses
(linked list duplicate check -> MySQL insert -> linked list -> hash table)
and checks after each stage that nothing was lost.

It uses a SEPARATE database called student_records_test, so your real
student_records data is never touched. The test database is deleted at the end.
"""
import csv
import random
import time

import mysql.connector

DB_NAME = "student_records_test"
STEPS = [1000, 5000, 10000, 20000]   # cumulative record counts to check
SEARCHES = 200                        # random lookups used to verify the index


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


class LinkedListStore:
    def __init__(self):
        self.head = None

    def traverse_check_duplicate(self, admission_no):
        current = self.head
        while current:
            if current.student["admission_no"] == admission_no:
                return True
            current = current.next
        return False

    def add_record(self, student):
        if self.traverse_check_duplicate(student["admission_no"]):
            return False
        node = Node(student)
        node.next = self.head
        self.head = node
        return True
# ----------------------------------------


def connect(database=None):
    kwargs = dict(host="localhost", user="root", password="")
    if database:
        kwargs["database"] = database
    return mysql.connector.connect(**kwargs)


def make_student(i):
    return {
        "full_name": f"Test Student {i}",
        "admission_no": f"TEST-{i:06d}/2025",
        "course": "Capacity Test",
        "year_of_study": "1.1",
        "contact": f"07{i % 100000000:08d}",
        "email": f"student{i}@example.com",
        "password_hash": "x" * 64,
    }


def count_nodes(store):
    n, current = 0, store.head
    while current:
        n += 1
        current = current.next
    return n


def count_hash_entries(index):
    return sum(len(bucket) for bucket in index.buckets)


# ---- set up the separate test database ----
admin = connect()
cur = admin.cursor()
cur.execute(f"DROP DATABASE IF EXISTS {DB_NAME}")
cur.execute(f"CREATE DATABASE {DB_NAME}")
admin.close()

conn = connect(DB_NAME)
cursor = conn.cursor()
cursor.execute("""
    CREATE TABLE students (
        student_id      INT AUTO_INCREMENT PRIMARY KEY,
        full_name       VARCHAR(100) NOT NULL,
        admission_no    VARCHAR(30) UNIQUE NOT NULL,
        course          VARCHAR(100),
        year_of_study   VARCHAR(20),
        contact         VARCHAR(20),
        email           VARCHAR(100),
        password_hash   VARCHAR(64),
        failed_attempts INT DEFAULT 0,
        locked_until    DATETIME NULL
    )
""")
conn.commit()

store = LinkedListStore()
index = HashTableIndex()

INSERT = """
    INSERT INTO students (full_name, admission_no, course, year_of_study,
                          contact, email, password_hash)
    VALUES (%(full_name)s, %(admission_no)s, %(course)s, %(year_of_study)s,
            %(contact)s, %(email)s, %(password_hash)s)
"""

print(f"Testing up to {STEPS[-1]:,} records. This can take a few minutes, please wait...\n")
rows = []
added = 0
last_batch_start = time.perf_counter()

for target in STEPS:
    batch_start = time.perf_counter()
    batch_size = target - added
    while added < target:
        student = make_student(added)
        # same order as the app: duplicate check, MySQL, linked list, hash table
        if store.traverse_check_duplicate(student["admission_no"]):
            raise SystemExit("Unexpected duplicate while adding " + student["admission_no"])
        cursor.execute(INSERT, student)
        conn.commit()
        store.add_record(student)
        index.insert(student["admission_no"], student)
        added += 1
    batch_seconds = time.perf_counter() - batch_start

    # ---- checks ----
    cursor.execute("SELECT COUNT(*) FROM students")
    db_count = cursor.fetchone()[0]
    list_count = count_nodes(store)
    hash_count = count_hash_entries(index)

    sample = random.sample(range(added), min(SEARCHES, added))
    keys = [f"TEST-{i:06d}/2025" for i in sample]
    t0 = time.perf_counter()
    found = sum(1 for k in keys if index.search(k) is not None)
    search_us = (time.perf_counter() - t0) / len(keys) * 1_000_000

    # a duplicate must be refused by both safeguards
    dup = make_student(0)
    list_refuses = store.traverse_check_duplicate(dup["admission_no"])
    try:
        cursor.execute(INSERT, dup)
        conn.commit()
        db_refuses = False
    except mysql.connector.IntegrityError:
        conn.rollback()
        db_refuses = True

    row = {
        "records": added,
        "batch_added": batch_size,
        "batch_seconds": round(batch_seconds, 2),
        "ms_per_add": round(batch_seconds / batch_size * 1000, 2),
        "mysql_rows": db_count,
        "list_nodes": list_count,
        "hash_entries": hash_count,
        "searches_found": f"{found}/{len(keys)}",
        "search_us": round(search_us, 2),
        "duplicate_refused": "yes" if (list_refuses and db_refuses) else "NO",
    }
    rows.append(row)
    print(f"  {added:>7,} records stored | MySQL {db_count:,} | list {list_count:,} | "
          f"hash {hash_count:,} | found {found}/{len(keys)} | {batch_seconds:.1f}s for this batch")

# ---- clean up ----
conn.close()
admin = connect()
admin.cursor().execute(f"DROP DATABASE {DB_NAME}")
admin.close()

print()
print(f"{'Records':>8} | {'Added':>6} | {'Seconds':>8} | {'ms/add':>7} | {'MySQL':>7} | "
      f"{'List':>7} | {'Hash':>7} | {'Found':>8} | {'Search us':>9} | Duplicate refused")
print("-" * 112)
for r in rows:
    print(f"{r['records']:>8,} | {r['batch_added']:>6,} | {r['batch_seconds']:>8.2f} | {r['ms_per_add']:>7.2f} | "
          f"{r['mysql_rows']:>7,} | {r['list_nodes']:>7,} | {r['hash_entries']:>7,} | "
          f"{r['searches_found']:>8} | {r['search_us']:>9.2f} | {r['duplicate_refused']}")

all_ok = all(r["mysql_rows"] == r["list_nodes"] == r["hash_entries"] == r["records"] for r in rows)
print("\nAll three stores agree at every step:", "YES" if all_ok else "NO - check the table above")
print("Test database deleted. Your real student_records data was not touched.")

with open("capacity_results.csv", "w", newline="") as f:
    w = csv.DictWriter(f, fieldnames=list(rows[0].keys()))
    w.writeheader()
    w.writerows(rows)
print("Saved capacity_results.csv")

try:
    import matplotlib
    matplotlib.use("Agg")
    import matplotlib.pyplot as plt
    xs = [r["records"] for r in rows]
    plt.figure(figsize=(8, 5))
    plt.plot(xs, [r["ms_per_add"] for r in rows], marker="o", color="#2C5F8A")
    plt.xlabel("Records already stored")
    plt.ylabel("Average time to add one record (ms)")
    plt.title("Cost of adding a record as the system grows")
    plt.grid(True, alpha=0.3)
    plt.tight_layout()
    plt.savefig("capacity_chart.png", dpi=150)
    print("Saved capacity_chart.png")
except ImportError:
    print("matplotlib not installed - skipping chart")