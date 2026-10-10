import hashlib
import os
import sys
import threading
import unittest
from pathlib import Path
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parent))
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from aws import BUCKET, MockAWS
from session_diff import store as store_module
from session_diff.store import Store


def text(content):
    data = content.encode()
    return {"kind": "text", "hash": hashlib.sha256(data).hexdigest(), "size": len(data)}, data


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
        threads = [threading.Thread(target=self.change, args=("s", "r", f"/w/{i}", "a", f"b{i}", self.new_store()))
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


if __name__ == "__main__":
    unittest.main()
