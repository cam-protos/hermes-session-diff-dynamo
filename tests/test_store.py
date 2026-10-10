import hashlib
import json
import os
import sys
import threading
import time
import unittest
from pathlib import Path
from unittest.mock import patch

from botocore.exceptions import ClientError

sys.path.insert(0, str(Path(__file__).resolve().parent))
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from aws import BUCKET, MockAWS
from session_diff import store as store_module
from session_diff.store import Store


def text(content):
    data = content.encode()
    return {"kind": "text", "hash": hashlib.sha256(data).hexdigest(), "size": len(data)}, data


class Atomic:
    """moto's handlers are not thread-safe (lost updates under threads); real DynamoDB calls are atomic.
    Serialising each call keeps interleaving at call granularity, like the real service."""

    def __init__(self, inner):
        self.inner, self.lock, self.exceptions = inner, threading.Lock(), inner.exceptions

    def __getattr__(self, name):
        target = getattr(self.inner, name)

        def call(**kwargs):
            with self.lock:
                return target(**kwargs)
        return call if callable(target) else target


class StoreTests(MockAWS, unittest.TestCase):
    def setUp(self):
        super().setUp()
        self.store = self.new_store()

    def change(self, session, run, path, old, new, store=None):
        (om, od), (nm, nd) = text(old), text(new)
        (store or self.store).record(session, run, "call", "write_file", "file tool", {path: om}, {path: nm}, {om["hash"]: od, nm["hash"]: nd})

    def test_ids_are_per_session_sequential_and_scoped(self):
        self.store.touch("a", "r1")
        self.store.touch("b", "r1")
        self.change("a", "r1", "/w/x", "1", "2")
        self.change("a", "r1", "/w/x", "2", "3")
        self.change("b", "r1", "/w/y", "1", "2")
        a, b = self.store.view("a")["files"][0], self.store.view("b")["files"][0]
        self.assertEqual((a["first_id"], a["last_id"], b["first_id"]), (1, 2, 1))
        self.assertIn("+3", self.store.diff("a", 1, 2)["patch"])
        self.assertIn("+2", self.store.diff("b", 1, 1)["patch"])

    def test_concurrent_writers_never_share_an_id(self):
        self.store.touch("s", "r")
        atomic = Atomic(self.ddb)
        threads = [threading.Thread(target=self.change, args=("s", "r", f"/w/{i}", "a", f"b{i}", self.new_store(dynamodb=atomic)))
                   for i in range(12)]
        for t in threads:
            t.start()
        for t in threads:
            t.join()
        ids = sorted(f["first_id"] for f in self.store.view("s")["files"])
        self.assertEqual(ids, list(range(1, 13)))

    def test_shared_blobs_are_reference_counted(self):
        self.store.touch("s", "r")
        self.change("s", "r", "/w/one", "same", "x")
        self.change("s", "r", "/w/two", "same", "y")
        self.assertEqual(self.blob_count(), 3)
        self.assertEqual(self.budget_bytes(), len(b"same") + 2)
        # Expire only the first change: the shared "same" blob must survive for the second.
        first = self.store._change("s", 1)
        self.ddb.update_item(TableName="session-diff-test", Key={"pk": {"S": first["pk"]}, "sk": {"S": first["sk"]}},
                             UpdateExpression="SET gsk = :z", ExpressionAttributeValues={":z": {"N": "0"}})
        self.store.prune(1)
        self.assertEqual(self.blob_count(), 2)
        self.assertIn("+y", self.store.diff("s", 2, 2)["patch"])
        self.assertEqual(self.budget_bytes(), len(b"same") + 1)

    def test_budget_is_enforced_and_failed_writes_leave_nothing(self):
        self.store.touch("s", "r")
        with patch.object(store_module, "BUDGET", 6):
            self.change("s", "r", "/w/a", "abc", "def")
            with self.assertRaisesRegex(RuntimeError, "snapshot budget is full"):
                self.change("s", "r", "/w/b", "ghi", "jkl")
        self.assertEqual(self.blob_count(), 2)
        self.assertEqual(self.budget_bytes(), 6)
        self.assertEqual([f["path"] for f in self.store.view("s")["files"]], ["/w/a"])

    def test_s3_failure_rolls_back_references_and_budget(self):
        self.store.touch("s", "r")
        with patch.object(self.s3, "put_object", side_effect=OSError("s3 down")):
            with self.assertRaisesRegex(OSError, "s3 down"):
                self.change("s", "r", "/w/a", "abc", "def")
        self.assertEqual((self.blob_count(), self.budget_bytes()), (0, 0))
        self.assertEqual(self.store.view("s")["files"], [])

    def test_missing_snapshot_is_reported(self):
        self.store.touch("s", "r")
        self.change("s", "r", "/w/a", "abc", "def")
        for obj in self.s3.list_objects_v2(Bucket=BUCKET)["Contents"]:
            self.s3.delete_object(Bucket=BUCKET, Key=obj["Key"])
        with self.assertRaisesRegex(LookupError, "no longer available"):
            self.store.diff("s", 1, 1)

    def test_blobs_use_prefix_and_encryption(self):
        store = self.new_store(prefix="/argus/session-diff/")
        store.touch("s", "r")
        self.change("s", "r", "/w/a", "abc", "def", store)
        keys = [o["Key"] for o in self.s3.list_objects_v2(Bucket=BUCKET)["Contents"]]
        self.assertTrue(all(k.startswith("argus/session-diff/blobs/") for k in keys), keys)
        self.assertEqual(self.s3.head_object(Bucket=BUCKET, Key=keys[0])["ServerSideEncryption"], "AES256")

    def test_sessions_and_runs_ordering_and_run_counts(self):
        self.store.touch("old", "r1")
        self.store.touch("new", "r1")
        self.store.touch("new", "r2", cwd="/w")
        self.store.touch("new", "r2", notes=["n1"])
        sessions = self.store.sessions()
        self.assertEqual([(s["session"], s["runs"]) for s in sessions], [("new", 2), ("old", 1)])
        view = self.store.view("new")
        self.assertEqual(view["run"], "r2")
        self.assertEqual(view["runs"][0]["cwd"], "/w")  # empty cwd keeps the earlier value
        self.assertEqual(view["notes"], ["n1"])

    def test_history_is_bounded_to_path_and_range(self):
        self.store.touch("s", "r")
        self.change("s", "r", "/w/a", "1", "2")
        self.change("s", "r", "/w/b", "1", "2")
        self.change("s", "r", "/w/a", "2", "3")
        self.assertEqual([h["id"] for h in self.store.history("s", 1, 3)], [3, 1])
        with self.assertRaises(LookupError):
            self.store.history("other", 1, 3)

    def test_large_snapshots_fit_because_contents_live_in_s3(self):
        self.store.touch("s", "r")
        big = "x" * (500 * 1024) + "\n"
        self.change("s", "r", "/w/big", "", big)
        self.assertEqual(self.store.diff("s", 1, 1)["added"], 1)

    def test_missing_configuration_is_a_clear_error(self):
        with patch.dict(os.environ, {}, clear=False):
            os.environ.pop("SESSION_DIFF_TABLE", None)
            os.environ.pop("SESSION_DIFF_BUCKET", None)
            with patch.dict(store_module._settings, {}, clear=True):
                with self.assertRaisesRegex(RuntimeError, "SESSION_DIFF_TABLE"):
                    Store()

    def test_configure_supplies_defaults(self):
        with patch.dict(store_module._settings, {}, clear=True):
            store_module.configure(table="t", bucket="b", prefix="p")
            store = Store(dynamodb=self.ddb, s3=self.s3)
            self.assertEqual((store.table, store.bucket, store.prefix), ("t", "b", "p"))


def client_error(code, op="Op"):
    return ClientError({"Error": {"Code": code, "Message": code}}, op)


class Flaky:
    """Wraps a DynamoDB client; ``fail`` may return an exception to raise (before the call is applied)."""

    def __init__(self, inner, fail):
        self.inner, self.fail, self.calls = inner, fail, []
        self.exceptions = inner.exceptions

    def __getattr__(self, name):
        target = getattr(self.inner, name)
        if not callable(target):
            return target

        def call(**kwargs):
            self.calls.append(name)
            error = self.fail(name, len(self.calls), kwargs)
            if error:
                raise error
            return target(**kwargs)
        return call


class HardeningTests(MockAWS, unittest.TestCase):
    def setUp(self):
        super().setUp()
        self.store = self.new_store()

    def ref(self, digest):
        item = self.ddb.get_item(TableName="session-diff-test", Key={"pk": {"S": f"BLOB#{digest}"}, "sk": {"S": "REF"}}, ConsistentRead=True).get("Item")
        return {k: (int(v["N"]) if k == "refs" else float(v["N"])) if "N" in v else v["S"] for k, v in item.items()} if item else None

    def change(self, session, run, path, old, new, store=None):
        (om, od), (nm, nd) = text(old), text(new)
        (store or self.store).record(session, run, "call", "write_file", "file tool", {path: om}, {path: nm}, {om["hash"]: od, nm["hash"]: nd})

    def backdate_changes(self):
        for item in self.items("S#"):
            if item["sk"]["S"].startswith("CHG#"):
                self.ddb.update_item(TableName="session-diff-test", Key={"pk": item["pk"], "sk": item["sk"]},
                                     UpdateExpression="SET gsk = :z", ExpressionAttributeValues={":z": {"N": "0"}})

    def backdate_refs(self):
        for item in self.items("BLOB#"):
            self.ddb.update_item(TableName="session-diff-test", Key={"pk": item["pk"], "sk": item["sk"]},
                                 UpdateExpression="SET gsk = :z", ExpressionAttributeValues={":z": {"N": "0"}})

    # 1. existing REF without an S3 object (first writer died after ADD)
    def test_acquire_reuploads_missing_object_for_existing_ref(self):
        meta, data = text("hello")
        self.ddb.put_item(TableName="session-diff-test", Item={"pk": {"S": f"BLOB#{meta['hash']}"}, "sk": {"S": "REF"},
                                                              "refs": {"N": "1"}, "size": {"N": "5"}, "charged": {"N": "5"}})
        self.store._acquire(meta["hash"], data, 1)
        key = self.store._blob_key(meta["hash"])
        self.assertEqual(self.s3.get_object(Bucket=BUCKET, Key=key)["Body"].read(), data)
        self.assertEqual(self.ref(meta["hash"])["refs"], 2)
        self.assertEqual(self.budget_bytes(), 0)  # REF already carries 'charged': not charged again

    def test_acquire_refreshes_object_on_every_reference(self):
        meta, data = text("hello")
        self.store._acquire(meta["hash"], data, 1)
        with patch.object(self.s3, "put_object", wraps=self.s3.put_object) as put:
            self.store._acquire(meta["hash"], data, 1)
        self.assertEqual(put.call_count, 1)
        self.assertEqual(self.budget_bytes(), 5)

    def test_put_failure_on_shared_blob_releases_only_its_count(self):
        meta, data = text("hello")
        self.store._acquire(meta["hash"], data, 1)
        with patch.object(self.s3, "put_object", side_effect=OSError("down")):
            with self.assertRaises(OSError):
                self.store._acquire(meta["hash"], data, 1)
        self.assertEqual(self.ref(meta["hash"])["refs"], 1)
        self.assertEqual(self.budget_bytes(), 5)

    # 2. dying marker
    def test_acquire_waits_for_live_dying_marker_then_fails(self):
        meta, data = text("hello")
        self.store._acquire(meta["hash"], data, 1)
        self.ddb.update_item(TableName="session-diff-test", Key={"pk": {"S": f"BLOB#{meta['hash']}"}, "sk": {"S": "REF"}},
                             UpdateExpression="SET refs = :z, dying = :now",
                             ExpressionAttributeValues={":z": {"N": "0"}, ":now": {"N": str(time.time())}})
        with patch.object(store_module, "ACQUIRE_WAIT", 0), patch.object(store_module, "ACQUIRE_ATTEMPTS", 3):
            with self.assertRaisesRegex(RuntimeError, "being deleted"):
                self.store._acquire(meta["hash"], data, 1)
        self.assertEqual(self.ref(meta["hash"])["refs"], 0)

    def test_acquire_clears_stale_dying_marker_without_recharging(self):
        meta, data = text("hello")
        self.store._acquire(meta["hash"], data, 1)
        self.ddb.update_item(TableName="session-diff-test", Key={"pk": {"S": f"BLOB#{meta['hash']}"}, "sk": {"S": "REF"}},
                             UpdateExpression="SET refs = :z, dying = :old",
                             ExpressionAttributeValues={":z": {"N": "0"}, ":old": {"N": str(time.time() - 600)}})
        self.store._acquire(meta["hash"], data, 1)
        ref = self.ref(meta["hash"])
        self.assertEqual(ref["refs"], 1)
        self.assertNotIn("dying", ref)
        self.assertEqual(self.budget_bytes(), 5)  # the REF still carries 'charged': its payment stands, no double charge

    def test_releaser_does_not_delete_blob_rereferenced_before_marker(self):
        """Another writer re-references between the releaser's ADD(-1) and its dying marker."""
        meta, data = text("hello")
        self.store._acquire(meta["hash"], data, 1)
        other = self.new_store()
        real = self.store._finalize

        def interleave(digest, refund=True):
            other._acquire(digest, data, 1)  # re-reference + re-upload wins the race
            real(digest, refund)
        with patch.object(self.store, "_finalize", side_effect=interleave):
            self.store._release(meta["hash"], 1)
        self.assertEqual(self.ref(meta["hash"])["refs"], 1)
        self.assertEqual(self.s3.get_object(Bucket=BUCKET, Key=self.store._blob_key(meta["hash"]))["Body"].read(), data)

    def test_release_of_last_reference_cleans_everything(self):
        meta, data = text("hello")
        self.store._acquire(meta["hash"], data, 2)
        self.store._release(meta["hash"], 2)
        self.assertEqual((self.blob_count(), self.budget_bytes()), (0, 0))

    # 3. retries
    def test_throttling_is_retried_and_applied_once(self):
        seen = []
        flaky = Flaky(self.ddb, lambda op, n, kw: client_error("ThrottlingException") if op == "update_item" and not seen.append(1) and len(seen) == 1 else None)
        store = self.new_store(dynamodb=flaky)
        with patch.object(store_module.time, "sleep"):
            store.touch("s", "r")
        self.assertEqual(flaky.calls.count("update_item"), 3)  # 1 throttled + RUN + META
        meta = self.ddb.get_item(TableName="session-diff-test", Key={"pk": {"S": "S#s"}, "sk": {"S": "META"}})["Item"]
        self.assertEqual(meta["runs"]["N"], "1")

    def test_non_throttle_error_is_not_retried(self):
        flaky = Flaky(self.ddb, lambda op, n, kw: client_error("InternalServerError") if op == "update_item" else None)
        store = self.new_store(dynamodb=flaky)
        with self.assertRaises(ClientError):
            store.touch("s", "r")
        self.assertEqual(flaky.calls.count("update_item"), 1)

    def test_throttling_gives_up_after_five_attempts(self):
        flaky = Flaky(self.ddb, lambda op, n, kw: client_error("ProvisionedThroughputExceededException"))
        store = self.new_store(dynamodb=flaky)
        with patch.object(store_module.time, "sleep"), self.assertRaises(ClientError):
            store.touch("s", "r")
        self.assertEqual(len(flaky.calls), 5)

    def test_shared_clients_disable_automatic_retries(self):
        with patch.dict(store_module._clients, {}, clear=True):
            client = store_module._client("dynamodb", REGION_FOR_CLIENT)
            self.assertEqual(client.meta.config.retries["total_max_attempts"], 1)

    # 4. partial batch
    def test_failed_batch_removes_written_change_rows_before_releasing_blobs(self):
        self.store.touch("s", "r")
        calls = []
        original = self.ddb.transact_write_items

        def batch(**kw):
            calls.append(kw)
            if len(calls) == 2:  # second chunk (26+ items) fails after the first landed
                raise client_error("InternalServerError")
            return original(**kw)
        before, after, blobs = {}, {}, {}
        for i in range(30):
            (om, od), (nm, nd) = text(f"a{i}"), text(f"b{i}")
            before[f"/w/{i}"], after[f"/w/{i}"] = om, nm
            blobs[om["hash"]], blobs[nm["hash"]] = od, nd
        order = []
        real_release = self.store._release
        with patch.object(self.ddb, "transact_write_items", side_effect=batch), \
                patch.object(self.store, "_release", side_effect=lambda *a, **k: (order.append(len(self.items("S#s".replace("S#s", "S#s")))), real_release(*a, **k))):
            with self.assertRaises(ClientError):
                self.store.record("s", "r", "call", "write_file", "file tool", before, after, blobs)
        chg = [i for i in self.items("S#s") if i["sk"]["S"].startswith("CHG#")]
        self.assertEqual(chg, [])
        self.assertEqual((self.blob_count(), self.budget_bytes()), (0, 0))
        self.assertTrue(order and all(n == 2 for n in order), order)  # only RUN-less META+... remained when releasing

    def test_failed_write_after_first_chunk_removes_written_rows(self):
        self.store.touch("s", "r")
        original, calls = self.ddb.transact_write_items, []

        def txn(**kw):
            calls.append(1)
            if len(calls) == 2:
                raise client_error("RequestTimeout")  # outcome unknown
            return original(**kw)
        before, after, blobs = {}, {}, {}
        for i in range(30):
            (om, od), (nm, nd) = text(f"c{i}"), text(f"d{i}")
            before[f"/w/{i}"], after[f"/w/{i}"] = om, nm
            blobs[om["hash"]], blobs[nm["hash"]] = od, nd
        with patch.object(self.ddb, "transact_write_items", side_effect=txn), patch.object(store_module.time, "sleep"):
            with self.assertRaises(ClientError):
                self.store.record("s", "r", "call", "write_file", "file tool", before, after, blobs)
        self.assertEqual([i for i in self.items("S#s") if i["sk"]["S"].startswith("CHG#")], [])
        self.assertEqual((self.blob_count(), self.budget_bytes()), (0, 0))

    # 5. prune
    def test_prune_is_atomic_and_crash_between_transaction_and_finalize_is_repaired(self):
        self.store.touch("s", "r")
        self.change("s", "r", "/w/a", "abc", "def")
        self.backdate_changes()
        with patch.object(self.store, "_finalize", side_effect=RuntimeError("crash")):
            with self.assertRaises(RuntimeError):
                self.store.prune(1)
        self.assertEqual([i for i in self.items("S#") if i["sk"]["S"].startswith("CHG#")], [])
        self.assertEqual(len(self.items("BLOB#")), 2)  # orphans: zero-ref REFs still present
        self.assertTrue(all(self.ref(i["pk"]["S"][5:])["refs"] == 0 for i in self.items("BLOB#")))
        self.backdate_refs()
        self.store.prune(1)
        self.assertEqual((self.blob_count(), self.budget_bytes()), (0, 0))

    def test_prune_twice_never_double_releases(self):
        self.store.touch("s", "r")
        self.change("s", "r", "/w/a", "same", "x")
        self.change("s", "r", "/w/b", "same", "y")
        first = self.store._change("s", 1)
        self.ddb.update_item(TableName="session-diff-test", Key={"pk": {"S": first["pk"]}, "sk": {"S": first["sk"]}},
                             UpdateExpression="SET gsk = :z", ExpressionAttributeValues={":z": {"N": "0"}})
        stale = list(self.store._query("gpk = :g AND gsk < :c", {":g": "CHG", ":c": 1}, index="gsi"))
        self.assertEqual(len(stale), 1)
        self.store.prune(1)
        real = self.store._query
        # A second pruner that read the index before the first finished still sees the deleted row.
        with patch.object(self.store, "_query", side_effect=lambda cond, vals, **kw: iter(stale) if vals.get(":g") == "CHG" else real(cond, vals, **kw)):
            self.store.prune(1)
        self.assertEqual(self.ref(text("same")[0]["hash"])["refs"], 1)

    def test_prune_limit_bounds_change_records(self):
        self.store.touch("s", "r")
        for i in range(5):
            self.change("s", "r", f"/w/{i}", f"a{i}", f"b{i}")
        self.backdate_changes()
        self.store.prune(1, limit=2)
        left = [i for i in self.items("S#s") if i["sk"]["S"].startswith("CHG#")]
        self.assertEqual(len(left), 3)
        self.store.prune(1, limit=500)
        self.assertEqual((self.blob_count(), self.budget_bytes()), (0, 0))

    def test_prune_concurrent_with_record_keeps_counts_consistent(self):
        self.store.touch("s", "r")
        for i in range(4):
            self.change("s", "r", f"/w/{i}", "shared", f"v{i}")
        self.backdate_changes()
        errors = []

        def run(fn):
            try:
                fn()
            except Exception as exc:  # pragma: no cover
                errors.append(exc)
        atomic = Atomic(self.ddb)
        pruner, writer = self.new_store(dynamodb=atomic), self.new_store(dynamodb=atomic)
        threads = [threading.Thread(target=run, args=(lambda: pruner.prune(1),)),
                   threading.Thread(target=run, args=(lambda: self.change("t", "r", "/w/new", "shared", "fresh", writer),)),
                   threading.Thread(target=run, args=(lambda: pruner.prune(1),))]
        for t in threads:
            t.start()
        for t in threads:
            t.join()
        self.assertEqual(errors, [])
        shared = text("shared")[0]["hash"]
        self.assertEqual(self.ref(shared)["refs"], 1)
        self.assertEqual(self.blob_count(), 2)
        self.assertEqual(self.budget_bytes(), len("shared") + len("fresh"))
        self.assertIn("+fresh", self.store.diff("t", 1, 1)["patch"])

    def stats_vs_charged(self):
        return self.budget_bytes(), sum(int(i["charged"]["N"]) for i in self.items("BLOB#") if "charged" in i)

    # Defect 1: a failed charge by A must not leave B's live blob unpaid
    def test_failed_charge_by_first_acquirer_does_not_leave_concurrent_holder_unpaid(self):
        meta, data = text("hello")
        self.ddb.put_item(TableName=TABLE_NAME, Item={"pk": {"S": "STATS"}, "sk": {"S": "BLOBS"}, "bytes": {"N": "3"}})
        entered, proceed = threading.Event(), threading.Event()

        def park(op, n, kw):
            if op == "transact_write_items":
                entered.set()
                proceed.wait(10)
        other = self.new_store(dynamodb=Flaky(self.ddb, park))
        result = []
        thread = threading.Thread(target=lambda: result.append(other._acquire(meta["hash"], data, 1)))
        real_charge = self.store._charge

        def charge(ref, size):
            thread.start()  # B adds its reference (sees refs>=1) and parks before charging
            self.assertTrue(entered.wait(10))
            real_charge(ref, size)
        with patch.object(store_module, "BUDGET", 6), patch.object(self.store, "_charge", side_effect=charge):
            with self.assertRaisesRegex(RuntimeError, "budget is full"):
                self.store._acquire(meta["hash"], data, 1)
            self.assertEqual(self.ref(meta["hash"])["refs"], 1)  # only B's reference is left
            self.ddb.put_item(TableName=TABLE_NAME, Item={"pk": {"S": "STATS"}, "sk": {"S": "BLOBS"}, "bytes": {"N": "0"}})
            proceed.set()
            thread.join(10)
        self.assertEqual(self.stats_vs_charged(), (5, 5))  # B paid; STATS == sum of live charged sizes
        self.store._release(meta["hash"], 1)
        self.assertEqual((self.blob_count(), self.budget_bytes()), (0, 0))

    def test_reacquire_of_charged_ref_does_not_recharge_and_release_refunds_once(self):
        meta, data = text("hello")
        self.store._acquire(meta["hash"], data, 1)
        self.store._acquire(meta["hash"], data, 1)
        self.assertEqual(self.stats_vs_charged(), (5, 5))
        self.store._release(meta["hash"], 2)
        self.assertEqual((self.blob_count(), self.budget_bytes()), (0, 0))

    # Defect 2: finalize failure after delete_object
    def test_finalize_transaction_failure_is_retried_then_swept_and_recharges_once(self):
        meta, data = text("hello")
        self.store._acquire(meta["hash"], data, 1)
        fail = {"left": 3}

        def boom(op, n, kw):
            if op == "transact_write_items" and fail["left"] > 0:
                fail["left"] -= 1
                return client_error("InternalServerError")
        flaky = Flaky(self.ddb, boom)
        store = self.new_store(dynamodb=flaky)
        with patch.object(store_module.time, "sleep"), self.assertRaises(ClientError):
            store._release(meta["hash"], 1)
        self.assertEqual(flaky.calls.count("transact_write_items"), 3)  # retried 3 times
        ref = self.ref(meta["hash"])
        self.assertEqual((ref["refs"], ref["charged"]), (0, 5))  # S3 object gone, REF + charge remain
        self.assertEqual(self.s3.list_objects_v2(Bucket=BUCKET).get("KeyCount", 0), 0)
        self.ddb.update_item(TableName=TABLE_NAME, Key={"pk": {"S": f"BLOB#{meta['hash']}"}, "sk": {"S": "REF"}},
                             UpdateExpression="SET dying = :old", ExpressionAttributeValues={":old": {"N": str(time.time() - 600)}})
        # once the marker is stale a re-acquire charges nothing more (the REF still carries 'charged')
        self.store._acquire(meta["hash"], data, 1)
        self.assertEqual(self.budget_bytes(), 5)
        self.store._release(meta["hash"], 1)
        self.assertEqual((self.blob_count(), self.budget_bytes()), (0, 0))

    def test_prune_sweeps_stale_dying_ref_regardless_of_retention_cutoff(self):
        meta, data = text("hello")
        self.store._acquire(meta["hash"], data, 1)
        self.ddb.update_item(TableName=TABLE_NAME, Key={"pk": {"S": f"BLOB#{meta['hash']}"}, "sk": {"S": "REF"}},
                             UpdateExpression="SET refs = :z, dying = :old, dying_owner = :o",  # fresh gsk, stale marker
                             ExpressionAttributeValues={":z": {"N": "0"}, ":old": {"N": str(time.time() - 600)}, ":o": {"S": "dead"}})
        self.store.prune(30)
        self.assertEqual((self.blob_count(), self.budget_bytes()), (0, 0))

    def test_prune_leaves_fresh_dying_ref_alone(self):
        meta, data = text("hello")
        self.store._acquire(meta["hash"], data, 1)
        self.ddb.update_item(TableName=TABLE_NAME, Key={"pk": {"S": f"BLOB#{meta['hash']}"}, "sk": {"S": "REF"}},
                             UpdateExpression="SET refs = :z, dying = :now",
                             ExpressionAttributeValues={":z": {"N": "0"}, ":now": {"N": str(time.time())}})
        self.store.prune(30)
        self.assertIsNotNone(self.ref(meta["hash"]))

    # Defect 3: META/RUN outlive the session's CHG rows
    def chg_rows(self, session="s"):
        return sorted(i["sk"]["S"] for i in self.items(f"S#{session}") if i["sk"]["S"].startswith("CHG#"))

    def test_truncated_prune_keeps_meta_so_ids_are_never_reused(self):
        self.store.touch("s", "r")
        for i in range(3):
            self.change("s", "r", f"/w/{i}", f"a{i}", f"b{i}")
        self.backdate_changes()
        self.ddb.update_item(TableName=TABLE_NAME, Key={"pk": {"S": "S#s"}, "sk": {"S": "META"}}, UpdateExpression="SET gsk = :z",
                             ExpressionAttributeValues={":z": {"N": "0"}})
        self.ddb.update_item(TableName=TABLE_NAME, Key={"pk": {"S": "S#s"}, "sk": {"S": "RUN#r"}}, UpdateExpression="SET gsk = :z",
                             ExpressionAttributeValues={":z": {"N": "0"}})
        self.store.prune(1, limit=1)
        self.assertEqual(len(self.chg_rows()), 2)
        self.assertEqual(len([i for i in self.items("S#s") if i["sk"]["S"] in ("META", "RUN#r")]), 2)
        before = {i["sk"]["S"]: i for i in self.items("S#s") if i["sk"]["S"].startswith("CHG#")}
        self.change("s", "r", "/w/new", "n1", "n2")
        rows = self.chg_rows()
        self.assertEqual(rows, ["CHG#000000000002", "CHG#000000000003", "CHG#000000000004"])
        after = {i["sk"]["S"]: i for i in self.items("S#s") if i["sk"]["S"].startswith("CHG#")}
        for key, item in before.items():
            self.assertEqual(after[key], item)  # survivors untouched
        live = sum(int(json.loads(i["refs"]["S"]).__len__()) for i in after.values())
        self.assertEqual(sum(self.ref(i["pk"]["S"][5:])["refs"] for i in self.items("BLOB#")), live)
        self.backdate_changes()
        for sk in ("META", "RUN#r"):
            self.ddb.update_item(TableName=TABLE_NAME, Key={"pk": {"S": "S#s"}, "sk": {"S": sk}}, UpdateExpression="SET gsk = :z",
                                 ExpressionAttributeValues={":z": {"N": "0"}})
        self.store.prune(1)
        self.assertEqual((self.blob_count(), self.budget_bytes()), (0, 0))
        self.assertEqual([i for i in self.items("S#s")], [])  # now META/RUN go too

    def test_record_refuses_to_overwrite_an_existing_change_row(self):
        self.store.touch("s", "r")
        self.change("s", "r", "/w/a", "a1", "a2")
        self.ddb.delete_item(TableName=TABLE_NAME, Key={"pk": {"S": "S#s"}, "sk": {"S": "META"}})  # next_id lost -> reuse attempt
        with self.assertRaises(ClientError):
            self.change("s", "r", "/w/b", "b1", "b2")
        self.assertEqual(self.chg_rows(), ["CHG#000000000001"])
        self.assertIn("+a2", self.store.diff("s", 1, 1)["patch"])
        self.assertEqual(self.blob_count(), 2)  # b's blobs were released again

    # Defect 4: discard failure must not release blobs
    def test_failed_discard_leaves_refs_and_prune_stays_consistent(self):
        self.store.touch("s", "r")
        (am, ad), (bm, bd) = text("old"), text("new")
        real_txn = self.ddb.transact_write_items
        landed = []

        def txn(**kw):  # the write lands but the response is lost
            real_txn(**kw)
            landed.append(1)
            if any("Put" in op and op["Put"]["Item"]["pk"]["S"].startswith("S#") for op in kw["TransactItems"]):
                raise client_error("RequestTimeout")
            return {}
        with patch.object(self.ddb, "transact_write_items", side_effect=txn), \
                patch.object(self.ddb, "batch_write_item", side_effect=client_error("InternalServerError")), \
                patch.object(store_module.time, "sleep"):
            with self.assertRaises(ClientError):
                self.store.record("s", "r", "c", "t", "src", {"/w/a": am}, {"/w/a": bm}, {am["hash"]: ad, bm["hash"]: bd})
        self.assertEqual(len(self.chg_rows()), 1)  # the row survived (discard failed)
        self.assertEqual(self.ref(am["hash"])["refs"], 1)  # ...so its blobs were NOT released
        self.assertEqual(self.ref(bm["hash"])["refs"], 1)
        self.assertIn("+new", self.store.diff("s", 1, 1)["patch"])
        self.backdate_changes()
        self.store.prune(1)
        self.assertEqual((self.blob_count(), self.budget_bytes()), (0, 0))  # released exactly once

    def test_discard_reports_whether_every_key_was_deleted(self):
        self.store.touch("s", "r")
        self.assertTrue(self.store._discard([("S#s", "CHG#000000000009")]))
        flaky = Flaky(self.ddb, lambda op, n, kw: client_error("InternalServerError") if op == "batch_write_item" else None)
        with patch.object(store_module.time, "sleep"):
            self.assertFalse(self.new_store(dynamodb=flaky)._discard([("S#s", "CHG#000000000009")]))

    # Defect 5: a stalled finalizer must not delete an object that was taken over
    def test_stalled_finalizer_aborts_when_marker_was_taken_over(self):
        meta, data = text("hello")
        self.store._acquire(meta["hash"], data, 1)
        other = self.new_store()
        real_update = self.store._update
        state = {"done": False}

        def hook(*a, **kw):  # runs right after the finalizer set its marker: a stale-marker takeover happens
            result = real_update(*a, **kw)
            if not state["done"] and "#o = :me" in a[2]:
                state["done"] = True
                self.ddb.update_item(TableName=TABLE_NAME, Key={"pk": {"S": f"BLOB#{meta['hash']}"}, "sk": {"S": "REF"}},
                                     UpdateExpression="SET dying = :old", ExpressionAttributeValues={":old": {"N": str(time.time() - 600)}})
                other._acquire(meta["hash"], data, 1)  # B re-references and re-uploads
            return result
        with patch.object(self.store, "_update", side_effect=hook):
            self.store._release(meta["hash"], 1)
        self.assertEqual(self.ref(meta["hash"])["refs"], 1)
        self.assertEqual(self.s3.get_object(Bucket=BUCKET, Key=self.store._blob_key(meta["hash"]))["Body"].read(), data)
        self.assertEqual(self.budget_bytes(), 5)

    def test_finalizer_stalled_past_half_ttl_aborts_without_deleting(self):
        meta, data = text("hello")
        self.store._acquire(meta["hash"], data, 1)
        clock = iter([1000.0, 1000.0, 1000.0 + store_module.DYING_TTL])
        fake = type("T", (), {"time": staticmethod(lambda: next(clock, 5000.0)), "sleep": staticmethod(lambda s: None)})
        with patch.object(store_module, "time", fake):
            self.store._release(meta["hash"], 1)
        self.assertIsNotNone(self.ref(meta["hash"]))
        self.assertEqual(self.s3.list_objects_v2(Bucket=BUCKET).get("KeyCount", 0), 1)

    # Extras
    def test_prune_retries_transaction_conflicts_and_only_skips_conditional_failures(self):
        self.store.touch("s", "r")
        self.change("s", "r", "/w/a", "abc", "def")
        self.backdate_changes()
        left = {"n": 2}

        def conflict(op, n, kw):
            if op == "transact_write_items" and left["n"] > 0:
                left["n"] -= 1
                return ClientError({"Error": {"Code": "TransactionCanceledException", "Message": "x"},
                                    "CancellationReasons": [{"Code": "TransactionConflict"}, {"Code": "None"}, {"Code": "None"}]}, "TransactWriteItems")
        flaky = Flaky(self.ddb, conflict)
        with patch.object(store_module.time, "sleep"):
            self.new_store(dynamodb=flaky).prune(1)
        self.assertEqual(flaky.calls.count("transact_write_items") >= 3, True)
        self.assertEqual(self.chg_rows(), [])
        self.assertEqual((self.blob_count(), self.budget_bytes()), (0, 0))

    def test_orphan_sweep_ignores_referenced_blobs_even_beyond_limit(self):
        for i in range(4):
            meta, data = text(f"live{i}")
            self.store._acquire(meta["hash"], data, 1)
        self.backdate_refs()
        orphan, odata = text("orphan")
        self.store._acquire(orphan["hash"], odata, 1)
        self.ddb.update_item(TableName=TABLE_NAME, Key={"pk": {"S": f"BLOB#{orphan['hash']}"}, "sk": {"S": "REF"}},
                             UpdateExpression="SET refs = :z, gsk = :g", ExpressionAttributeValues={":z": {"N": "0"}, ":g": {"N": "1"}})
        self.store.prune(1, limit=2)  # 4 old live blobs > limit; the orphan must still be found
        self.assertEqual(self.blob_count(), 4)
        self.assertEqual(self.budget_bytes(), sum(len(f"live{i}") for i in range(4)))
        self.assertTrue(all(self.ref(i["pk"]["S"][5:])["refs"] == 1 for i in self.items("BLOB#")))

    # 6. missing blobs
    def test_access_denied_blob_is_reported_as_missing(self):
        self.store.touch("s", "r")
        self.change("s", "r", "/w/a", "abc", "def")
        for code in ("AccessDenied", "403", "NoSuchKey", "404"):
            stub = type("S3", (), {"get_object": lambda self, **kw: (_ for _ in ()).throw(client_error(code, "GetObject")),
                                  "exceptions": self.s3.exceptions})()
            store = self.new_store(s3=stub)
            with self.assertRaisesRegex(LookupError, "no longer available"):
                store.diff("s", 1, 1)

    def test_other_s3_errors_still_propagate(self):
        stub = type("S3", (), {"get_object": lambda self, **kw: (_ for _ in ()).throw(client_error("InternalError")),
                              "exceptions": self.s3.exceptions})()
        with self.assertRaises(ClientError):
            self.new_store(s3=stub)._read_blob("abc")


from aws import REGION as REGION_FOR_CLIENT, TABLE as TABLE_NAME


if __name__ == "__main__":
    unittest.main()
