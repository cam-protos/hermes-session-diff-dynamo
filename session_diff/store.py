"""The change journal: metadata in DynamoDB, snapshot contents in S3.

Shared by the agent process (writer) and any HTTP reader, on any host, with no
local database file. Layout of the single DynamoDB table (``pk``/``sk`` keys,
global secondary index ``gsi`` on ``gpk``/``gsk``)::

    pk            sk               attributes                                   gpk / gsk
    S#<session>   META             sess, updated, runs, next_id                 META / updated
    S#<session>   RUN#<run>        sess, run, started, updated, cwd, notes      RUN  / updated
    S#<session>   CHG#<id:012d>    one captured change, plus blob refs          CHG  / created
    BLOB#<hash>   REF              refs (reference count), size
    STATS         BLOBS            bytes (running total for the content budget)

Snapshot contents are content-addressed S3 objects at ``<prefix>/blobs/<hash>``.
Change IDs stay small integers (the HTTP API addresses them) and are allocated
per session from an atomic counter on the session's META item.

Consistency and operating limits (see docs/reference.md):

* Every reference to a blob re-uploads it (idempotent for a content-addressed key), so a bucket
  lifecycle rule that expires objects N days after upload is measured from the *last reference*.
  Plugin retention should still be N days or less. No ``s3:ListBucket`` is needed: a missing blob
  reads as 403/404 and surfaces as "Snapshot content is no longer available".
* A blob REF is ``charged`` (bytes paid to STATS/BLOBS) by whichever acquirer finds it uncharged, in one
  transaction with the counter; finalize refunds only if ``charged`` is present, in the transaction that
  deletes the REF. A finalizer owns a blob through a unique ``dying_owner`` token, re-checks it right
  before deleting the S3 object and aborts if taken over. record() discards its change rows before
  releasing blobs and keeps the references if the discard cannot be confirmed. META/RUN items are pruned
  only when the session has no change rows left.
* Blob REF items carry ``gpk=BLOB`` / ``gsk=<time of last ref change>``. Dropping the last
  reference uses a ``dying`` marker so a releaser cannot delete an S3 object that another writer has
  just re-referenced and re-uploaded. ``prune`` sweeps zero-reference orphans.
* Mutating DynamoDB calls are NOT retried automatically (a replayed ``ADD`` after a lost response
  would double count). Only throttling errors, which guarantee the request was not applied, are retried.
* A crash between acquiring blob references and committing a change can leak a reference and its
  bytes (known limit): the blob is retained rather than lost, the S3 lifecycle bounds the object, but
  the STATS byte counter is not self-correcting.
* ``prune`` handles at most ``limit`` change records per call.
"""

import difflib
import json
import os
import threading
import time
import uuid
from collections import Counter
from decimal import Decimal
from pathlib import Path

UNKNOWN = {"unreadable", "excluded", "unsupported", "unstable", "unknown"}
BUDGET = 256 * 1024 * 1024
ENV_TABLE, ENV_BUCKET, ENV_PREFIX, ENV_REGION = (
    "SESSION_DIFF_TABLE", "SESSION_DIFF_BUCKET", "SESSION_DIFF_PREFIX", "SESSION_DIFF_REGION")
_settings = {}


def configure(table=None, bucket=None, prefix=None, region=None):
    """Set process-wide defaults (from plugin config); environment variables take precedence."""
    _settings.update({k: v for k, v in dict(table=table, bucket=bucket, prefix=prefix, region=region).items() if v})


def table_definition(name):
    """``create_table`` arguments for the journal table (used by tests and provisioning docs)."""
    return dict(
        TableName=name,
        BillingMode="PAY_PER_REQUEST",
        AttributeDefinitions=[{"AttributeName": "pk", "AttributeType": "S"}, {"AttributeName": "sk", "AttributeType": "S"},
                              {"AttributeName": "gpk", "AttributeType": "S"}, {"AttributeName": "gsk", "AttributeType": "N"}],
        KeySchema=[{"AttributeName": "pk", "KeyType": "HASH"}, {"AttributeName": "sk", "KeyType": "RANGE"}],
        GlobalSecondaryIndexes=[{"IndexName": "gsi", "Projection": {"ProjectionType": "ALL"},
                                 "KeySchema": [{"AttributeName": "gpk", "KeyType": "HASH"},
                                               {"AttributeName": "gsk", "KeyType": "RANGE"}]}])


_clients, _clients_lock = {}, threading.Lock()
THROTTLE_CODES = {"ThrottlingException", "ProvisionedThroughputExceededException", "RequestLimitExceeded", "ThrottledException"}
MISSING_BLOB_CODES = {"AccessDenied", "403", "NoSuchKey", "404"}
CONDITIONAL_FAILED = "ConditionalCheckFailedException"
CANCELED = "TransactionCanceledException"
DYING_TTL = 60  # a 'dying' marker older than this is abandoned (checked on its own clock, NOT the retention cutoff, which is days)
ACQUIRE_ATTEMPTS, ACQUIRE_WAIT = 50, 0.1
THROTTLE_ATTEMPTS = 5
FINALIZE_ATTEMPTS = 3


def _code(exc):
    """The AWS error code of a botocore ClientError (or None)."""
    return (getattr(exc, "response", None) or {}).get("Error", {}).get("Code")


def _reasons(exc):
    """Per-item CancellationReasons of a TransactionCanceledException."""
    return (getattr(exc, "response", None) or {}).get("CancellationReasons") or []


def _client(service, region):
    # A Store is created per call; boto3 clients are expensive and thread-safe, so share them.
    with _clients_lock:
        if (service, region) not in _clients:
            import boto3
            from botocore.config import Config
            # Timeouts: connect 3s / read 10s so a DynamoDB or S3 brownout cannot stall a tool call
            # for more than a few seconds per call. Automatic retries are OFF (total_max_attempts=1 means a single attempt, no retries): botocore
            # would replay a mutating UpdateItem ADD after a lost response and double count refs, ids,
            # runs and bytes. Store._call retries only errors that guarantee the request was not applied.
            _clients[(service, region)] = boto3.client(
                service, region_name=region,
                config=Config(connect_timeout=3, read_timeout=10, retries={"total_max_attempts": 1, "mode": "standard"}))
        return _clients[(service, region)]


def _num(value):
    if isinstance(value, Decimal):
        return int(value) if value == value.to_integral_value() else float(value)
    return value


class Store:
    def __init__(self, table=None, bucket=None, prefix=None, region=None, dynamodb=None, s3=None):
        self.table = table or os.environ.get(ENV_TABLE) or _settings.get("table")
        self.bucket = bucket or os.environ.get(ENV_BUCKET) or _settings.get("bucket")
        if not self.table or not self.bucket:
            raise RuntimeError(f"Session Diff storage is not configured. Set {ENV_TABLE} and {ENV_BUCKET}.")
        if prefix is None:
            prefix = os.environ.get(ENV_PREFIX) or _settings.get("prefix") or "session-diff"
        self.prefix = prefix.strip("/")
        region = region or os.environ.get(ENV_REGION) or _settings.get("region") or None
        self.db = dynamodb or _client("dynamodb", region)
        self.s3 = s3 or _client("s3", region)
        from boto3.dynamodb.types import TypeDeserializer, TypeSerializer
        self._ser, self._de = TypeSerializer(), TypeDeserializer()

    # -- DynamoDB plumbing ------------------------------------------------------
    def _enc(self, data):
        return {k: self._ser.serialize(Decimal(repr(v)) if isinstance(v, float) else v) for k, v in data.items()}

    def _dec(self, item):
        return {k: _num(self._de.deserialize(v)) for k, v in item.items()}

    def _key(self, pk, sk):
        return self._enc({"pk": pk, "sk": sk})

    def _call(self, op, **args):
        """Run a DynamoDB operation, retrying ONLY throttling errors.

        Throttling codes guarantee the request was not applied, so replaying is safe. Any other error
        (including a timeout whose outcome is unknown) is raised: replaying a non-idempotent write such
        as ``ADD`` could silently double count.
        """
        for attempt in range(THROTTLE_ATTEMPTS):
            try:
                return getattr(self.db, op)(**args)
            except Exception as exc:
                if _code(exc) not in THROTTLE_CODES or attempt == THROTTLE_ATTEMPTS - 1:
                    raise
                time.sleep(0.05 * 2 ** attempt)

    def _get(self, pk, sk):
        found = self._call("get_item", TableName=self.table, Key=self._key(pk, sk), ConsistentRead=True)
        return self._dec(found["Item"]) if "Item" in found else None

    def _query(self, condition, values, names=None, index=None, forward=True, filter=None, limit=None):
        args = dict(TableName=self.table, KeyConditionExpression=condition, ScanIndexForward=forward,
                    ExpressionAttributeValues=self._enc(values))
        if index:
            args["IndexName"] = index
        else:
            args["ConsistentRead"] = True
        if names:
            args["ExpressionAttributeNames"] = names
        if filter:
            args["FilterExpression"] = filter
        count = 0
        while True:
            page = self._call("query", **args)
            for item in page.get("Items", []):
                yield self._dec(item)
                count += 1
                if limit and count >= limit:
                    return
            if "LastEvaluatedKey" not in page:
                return
            args["ExclusiveStartKey"] = page["LastEvaluatedKey"]

    def _update(self, pk, sk, expression, values, names=None, condition=None, returns="NONE"):
        args = dict(TableName=self.table, Key=self._key(pk, sk), UpdateExpression=expression,
                    ExpressionAttributeValues=self._enc(values), ReturnValues=returns)
        if names:
            args["ExpressionAttributeNames"] = names
        if condition:
            args["ConditionExpression"] = condition
        return self._dec(self._call("update_item", **args).get("Attributes", {}))

    def _discard(self, keys):
        """Remove change items a failed record() may have written. True only if EVERY key was deleted."""
        for start in range(0, len(keys), 25):
            pending = [{"DeleteRequest": {"Key": self._key(pk, sk)}} for pk, sk in keys[start:start + 25]]
            for attempt in range(4):
                try:
                    pending = self._call("batch_write_item", RequestItems={self.table: pending}).get("UnprocessedItems", {}).get(self.table, [])
                except Exception:
                    pending = pending  # outcome unknown: deleting again is idempotent, so just retry
                if not pending:
                    break
                time.sleep(0.05 * 2 ** attempt)
            else:
                return False
        return True

    def _put_changes(self, items, written):
        """Write change rows with a not-exists condition (never overwrite a surviving row), 25 per transaction.

        ``written`` collects the keys that may have landed (a cancelled transaction applied nothing, so its
        keys are not listed), which is all that _discard may later delete.
        """
        for start in range(0, len(items), 25):
            chunk = items[start:start + 25]
            keys = [(i["pk"], i["sk"]) for i in chunk]
            written.extend(keys)
            ops = [{"Put": {"TableName": self.table, "Item": self._enc(item), "ConditionExpression": "attribute_not_exists(pk)"}} for item in chunk]
            try:
                self._call("transact_write_items", TransactItems=ops)
            except Exception as exc:
                if _code(exc) == CANCELED:
                    del written[len(written) - len(keys):]
                raise

    def _blob_key(self, digest):
        return f"{self.prefix}/blobs/{digest}" if self.prefix else f"blobs/{digest}"

    def _read_blob(self, digest):
        try:
            return self.s3.get_object(Bucket=self.bucket, Key=self._blob_key(digest))["Body"].read()
        except Exception as exc:
            # Without s3:ListBucket, S3 answers 403 AccessDenied (not NoSuchKey) for a missing object.
            if _code(exc) in MISSING_BLOB_CODES:
                return None
            raise

    # -- blob lifecycle: S3 object + reference count + byte budget --------------
    def _acquire(self, digest, data, count):
        """Add ``count`` references to a blob and (re)upload it; make sure the blob is charged to the budget.

        Payment is tracked on the REF itself (attribute ``charged`` = bytes paid), so whoever finds the
        REF uncharged pays, whatever the reference count was, and a release can only refund what was paid.
        The upload happens on every acquire: it is idempotent for a content-addressed key, repairs a
        blob whose first writer failed after taking its reference, and refreshes the object's age so
        a lifecycle rule never expires a blob that is still referenced.
        """
        size, ref = len(data), f"BLOB#{digest}"
        for attempt in range(ACQUIRE_ATTEMPTS):
            now = time.time()
            try:
                old = self._update(ref, "REF", "SET #sz = :size, gpk = :gpk, gsk = :now REMOVE #d, #o ADD refs :k",
                                   {":k": count, ":size": size, ":gpk": "BLOB", ":now": now, ":stale": now - DYING_TTL},
                                   names={"#sz": "size", "#d": "dying", "#o": "dying_owner"},
                                   condition="attribute_not_exists(#d) OR #d < :stale", returns="ALL_OLD")
                break
            except Exception as exc:
                if _code(exc) != CONDITIONAL_FAILED:
                    raise
                time.sleep(ACQUIRE_WAIT)  # a releaser is deleting this blob right now
        else:
            raise RuntimeError("A snapshot is being deleted and could not be re-referenced. Try again.")
        if "charged" not in old:
            try:
                self._charge(ref, size)
            except Exception:
                self._release(digest, count)
                raise
        try:
            self.s3.put_object(Bucket=self.bucket, Key=self._blob_key(digest), Body=data,
                               ContentType="text/plain; charset=utf-8", ServerSideEncryption="AES256")
        except Exception:
            self._release(digest, count)
            raise

    def _charge(self, ref, size):
        """Charge ``size`` bytes to the budget and mark the REF ``charged``, in ONE transaction.

        If the REF is already charged (another writer paid first) nothing is charged. Because the flag and
        the byte counter change atomically, a failed charge leaves nobody holding an unpaid-but-flagged
        blob and a crash cannot leave the flag set without the bytes.
        """
        items = [
            {"Update": {"TableName": self.table, "Key": self._key("STATS", "BLOBS"), "UpdateExpression": "ADD #b :n",
                        "ConditionExpression": "attribute_not_exists(#b) OR #b <= :room",
                        "ExpressionAttributeNames": {"#b": "bytes"},
                        "ExpressionAttributeValues": self._enc({":n": size, ":room": BUDGET - size})}},
            {"Update": {"TableName": self.table, "Key": self._key(ref, "REF"), "UpdateExpression": "SET charged = :n",
                        "ConditionExpression": "attribute_not_exists(charged)",
                        "ExpressionAttributeValues": self._enc({":n": size})}}]
        for attempt in range(10):
            try:
                self._call("transact_write_items", TransactItems=items)
                return
            except Exception as exc:
                if _code(exc) != CANCELED:
                    raise
                codes = [r.get("Code") for r in _reasons(exc)]
                if len(codes) > 1 and codes[1] == "ConditionalCheckFailed":
                    return  # already charged by another writer
                if codes and codes[0] == "ConditionalCheckFailed":
                    raise RuntimeError("The 256 MiB snapshot budget is full. Reduce retention or remove old plugin data.") from None
                time.sleep(0.05 * (attempt + 1))  # TransactionConflict etc.: nothing was applied
        raise RuntimeError("Could not charge the snapshot budget after repeated transaction conflicts.")

    def _release(self, digest, count, refund=True):
        """Drop ``count`` references; the last one out finalizes (deletes) the blob."""
        now = self._update(f"BLOB#{digest}", "REF", "ADD refs :k SET gpk = :gpk, gsk = :now",
                           {":k": -count, ":gpk": "BLOB", ":now": time.time()}, returns="ALL_NEW")
        if now.get("refs", 0) <= 0:
            self._finalize(digest, refund)

    def _finalize(self, digest, refund=True):
        """Delete the S3 object, budget and REF of a blob whose references are gone.

        A conditional ``dying`` marker plus a unique ``dying_owner`` token make one process the owner;
        writers that want the blob back wait until the REF is gone (or the marker is stale). A stalled
        owner re-asserts ownership right before deleting and aborts if it was taken over or held the
        marker for more than half of DYING_TTL, so it can never delete an object a new reference re-uploaded.
        The refund (only if the REF is ``charged``) and the REF removal are one transaction.
        """
        now, token = time.time(), uuid.uuid4().hex
        ref = f"BLOB#{digest}"
        try:
            item = self._update(ref, "REF", "SET #d = :now, #o = :me", {":now": now, ":me": token, ":zero": 0, ":stale": now - DYING_TTL},
                                names={"#d": "dying", "#o": "dying_owner"}, returns="ALL_NEW",
                                condition="refs <= :zero AND (attribute_not_exists(#d) OR #d < :stale)")
        except Exception as exc:
            if _code(exc) != CONDITIONAL_FAILED:
                raise
            return  # re-referenced, gone, or another releaser owns it
        if time.time() - now > DYING_TTL / 2:
            return  # stalled: leave the marker; the orphan sweep retries once it is stale
        try:  # still the owner and still unreferenced? (a takeover clears the marker or changes the token)
            self._update(ref, "REF", "SET gpk = :gpk", {":gpk": "BLOB", ":me": token, ":zero": 0},
                         names={"#o": "dying_owner"}, condition="#o = :me AND refs <= :zero")
        except Exception as exc:
            if _code(exc) != CONDITIONAL_FAILED:
                raise
            return
        self.s3.delete_object(Bucket=self.bucket, Key=self._blob_key(digest))
        charged = int(item.get("charged", 0))  # refund exactly what the REF says was paid (``refund`` is kept for compatibility)
        ops = [{"Delete": {"TableName": self.table, "Key": self._key(ref, "REF"), "ConditionExpression": "#o = :me AND refs <= :zero",
                           "ExpressionAttributeNames": {"#o": "dying_owner"},
                           "ExpressionAttributeValues": self._enc({":me": token, ":zero": 0})}}]
        if charged:
            ops.insert(0, {"Update": {"TableName": self.table, "Key": self._key("STATS", "BLOBS"), "UpdateExpression": "ADD #b :n",
                                      "ExpressionAttributeNames": {"#b": "bytes"}, "ExpressionAttributeValues": self._enc({":n": -charged})}})
        for attempt in range(FINALIZE_ATTEMPTS):
            try:
                self._call("transact_write_items", TransactItems=ops)
                return
            except Exception as exc:
                if _code(exc) == CANCELED and any(r.get("Code") == "ConditionalCheckFailed" for r in _reasons(exc)):
                    return  # taken over or already gone
                if attempt == FINALIZE_ATTEMPTS - 1:
                    raise  # state is left for the orphan sweep (REF at zero refs with a stale marker)
                time.sleep(0.05 * (attempt + 1))

    # -- writes ------------------------------------------------------------------
    def touch(self, session, run, cwd="", notes=()):
        now, pk = time.time(), f"S#{session}"
        previous = self._get(pk, f"RUN#{run}")
        merged = list(dict.fromkeys([*(json.loads(previous["notes"]) if previous else []), *notes]))[:30]
        expression = ("SET sess = :s, #r = :run, started = if_not_exists(started, :now), updated = :now, "
                      "gpk = :gpk, gsk = :now, notes = :notes, cwd = ")
        values = {":s": session, ":run": run, ":now": now, ":gpk": "RUN", ":notes": json.dumps(merged)}
        if cwd:
            expression += ":cwd"
            values[":cwd"] = cwd
        else:
            expression += "if_not_exists(cwd, :empty)"
            values[":empty"] = ""
        old = self._update(pk, f"RUN#{run}", expression, values, names={"#r": "run"}, returns="ALL_OLD")
        self._update(pk, "META", "SET sess = :s, updated = :now, gpk = :gpk, gsk = :now ADD runs :inc",
                     {":s": session, ":now": now, ":gpk": "META", ":inc": 0 if "started" in old else 1})

    def record(self, session, run, tool_id, tool, source, before, after, blobs):
        absent = {"kind": "absent"}
        entries = []
        for path in sorted(before.keys() | after.keys()):
            old, new = before.get(path, absent), after.get(path, absent)
            if old == new or old["kind"] in UNKNOWN or new["kind"] in UNKNOWN:
                continue
            # Store only blobs referenced by actual changes, not every scanned file.
            refs = [m["hash"] for m in (old, new) if m["kind"] == "text" and m.get("hash") in blobs]
            entries.append((path, old, new, refs))
        if not entries:
            return
        acquired, attempted = [], []
        try:
            for digest, count in Counter(h for *_, refs in entries for h in refs).items():
                self._acquire(digest, blobs[digest], count)
                acquired.append((digest, count))
            now = time.time()
            end = self._update(f"S#{session}", "META", "SET sess = :s, updated = :now, gpk = :gpk, gsk = :now ADD next_id :n",
                               {":s": session, ":now": now, ":gpk": "META", ":n": len(entries)}, returns="UPDATED_NEW")["next_id"]
            first = int(end) - len(entries) + 1
            items = [
                {"pk": f"S#{session}", "sk": f"CHG#{first + i:012d}", "id": first + i, "sess": session, "run": run,
                 "tool_id": tool_id, "tool": tool, "source": source, "path": path, "before": json.dumps(old),
                 "after": json.dumps(new), "refs": json.dumps(refs), "created": now, "gpk": "CHG", "gsk": now}
                for i, (path, old, new, refs) in enumerate(entries)]
            self._put_changes(items, attempted)
        except Exception:
            # Remove any change rows that may have landed BEFORE the blobs they point at are released.
            # If that cannot be confirmed, keep the references (documented leak): a retained blob is
            # safe, whereas releasing blobs a surviving row still points at would let prune double release.
            if self._discard(attempted):
                for digest, count in acquired:
                    try:
                        self._release(digest, count)
                    except Exception:
                        pass
            raise

    def prune(self, days=30, limit=500):
        """Expire records older than ``days``; handles at most ``limit`` change records per call."""
        cutoff = time.time() - max(1, min(int(days), 365)) * 86400
        digests, errors = set(), []
        for item in list(self._query("gpk = :g AND gsk < :c", {":g": "CHG", ":c": cutoff}, index="gsi", limit=max(1, int(limit)))):
            counts = Counter(json.loads(item.get("refs", "[]")))
            ops = [{"Delete": {"TableName": self.table, "Key": self._key(item["pk"], item["sk"]),
                               "ConditionExpression": "attribute_exists(pk) AND gsk < :c",
                               "ExpressionAttributeValues": self._enc({":c": cutoff})}}]
            ops += [{"Update": {"TableName": self.table, "Key": self._key(f"BLOB#{digest}", "REF"),
                                "UpdateExpression": "ADD refs :neg SET gpk = :b, gsk = :now",
                                "ExpressionAttributeValues": self._enc({":neg": -n, ":b": "BLOB", ":now": time.time()})}}
                    for digest, n in counts.items()]
            for attempt in range(10):
                try:  # deleting the change and dropping its references is one atomic, idempotent step
                    self._call("transact_write_items", TransactItems=ops)
                    digests.update(counts)
                    break
                except Exception as exc:
                    if _code(exc) != CANCELED:
                        raise
                    reasons = (getattr(exc, "response", {}) or {}).get("CancellationReasons") or []
                    if reasons and reasons[0].get("Code") == "ConditionalCheckFailed":
                        break  # already deleted by another pruner, or refreshed since the index read
                    time.sleep(0.05 * (attempt + 1))  # TransactionConflict etc.: nothing was applied, try again
            else:
                errors.append(RuntimeError("Could not expire a change record after repeated transaction conflicts."))
        # Repair: blobs left at zero references (crash after the transaction, before finalize).
        for orphan in self._query("gpk = :g AND gsk < :c", {":g": "BLOB", ":c": cutoff, ":zero": 0}, index="gsi",
                                  filter="refs <= :zero", limit=max(1, int(limit))):
            digests.add(orphan["pk"][len("BLOB#"):])
        # A finalizer that died while holding a 'dying' marker: DYING_TTL (not the retention cutoff) decides
        # when its marker is abandoned, and the REF's gsk (last ref change) may be far newer than the cutoff.
        stale = time.time() - DYING_TTL
        for orphan in self._query("gpk = :g", {":g": "BLOB", ":zero": 0, ":stale": stale}, index="gsi",
                                  filter="refs <= :zero AND dying < :stale", limit=max(1, int(limit))):
            digests.add(orphan["pk"][len("BLOB#"):])
        for digest in sorted(digests):
            try:
                self._finalize(digest)
            except Exception as exc:
                errors.append(exc)
        for kind in ("RUN", "META"):
            for item in list(self._query("gpk = :g AND gsk < :c", {":g": kind, ":c": cutoff}, index="gsi")):
                # META/RUN go only once the session has no change rows left (a limit-truncated prune would
                # otherwise reset next_id and let record() reuse ids of surviving rows).
                if next(self._query("pk = :p AND begins_with(sk, :c)", {":p": item["pk"], ":c": "CHG#"}, limit=1), None):
                    continue
                try:
                    self._call("delete_item", TableName=self.table, Key=self._key(item["pk"], item["sk"]),
                               ConditionExpression="gsk < :c", ExpressionAttributeValues=self._enc({":c": cutoff}))
                except Exception as exc:
                    if _code(exc) != CONDITIONAL_FAILED:
                        raise  # a refreshed record is skipped
        if errors:
            raise errors[0]

    # -- reads -------------------------------------------------------------------
    def sessions(self):
        return [{"session": i["sess"], "updated": i["updated"], "runs": i.get("runs", 0)}
                for i in self._query("gpk = :g", {":g": "META"}, index="gsi", forward=False, limit=100)]

    def view(self, session, run="latest"):
        runs = sorted(self._query("pk = :p AND begins_with(sk, :r)", {":p": f"S#{session}", ":r": "RUN#"}),
                      key=lambda r: r["started"], reverse=True)[:100]
        runs = [{"session": r["sess"], "run": r["run"], "started": r["started"], "updated": r["updated"],
                 "cwd": r.get("cwd", ""), "notes": r.get("notes", "[]")} for r in runs]
        selected = runs[0]["run"] if run == "latest" and runs else run
        values, names, filter = {":p": f"S#{session}", ":c": "CHG#"}, None, None
        if selected != "all":
            filter, names, values[":run"] = "#r = :run", {"#r": "run"}, selected
        rows = list(self._query("pk = :p AND begins_with(sk, :c)", values, names=names, filter=filter, limit=10001))
        groups = {}
        for row in rows[:10000]:
            old, new = json.loads(row["before"]), json.loads(row["after"])
            entry = groups.setdefault(row["path"], {"path": row["path"], "first_id": row["id"], "last_id": row["id"],
                "before": old, "after": old, "edits": 0, "observed": False, "gap": False})
            entry["gap"] |= entry["after"] != old
            entry.update(after=new, last_id=row["id"], edits=entry["edits"] + 1)
            entry["observed"] |= row["source"] == "observed"
        files = []
        for entry in groups.values():
            old, new = entry["before"], entry["after"]
            status = ("restored" if old == new else "added" if old["kind"] == "absent"
                      else "deleted" if new["kind"] == "absent" else "modified")
            files.append(dict(entry, status=status))
        notes = list(dict.fromkeys(note for r in runs if selected == "all" or r["run"] == selected
                                  for note in json.loads(r["notes"])))
        if len(rows) > 10000:
            notes.append("Showing the first 10,000 changes in this scope. Select a single run for a smaller view.")
        return {"session": session, "run": selected, "runs": [{k: v for k, v in r.items() if k != "notes"} for r in runs],
                "files": files, "notes": notes}

    def _change(self, session, change_id):
        return self._get(f"S#{session}", f"CHG#{int(change_id):012d}")

    def diff(self, session, first_id, last_id):
        first, last = self._change(session, first_id), self._change(session, last_id)
        if not first or not last or first["path"] != last["path"] or first_id > last_id:
            raise LookupError("Recorded change not found")
        old, new = json.loads(first["before"]), json.loads(last["after"])
        if any(m["kind"] not in {"text", "absent"} for m in (old, new)):
            return {"patch": "", "note": "Binary or large file. Content diff unavailable; metadata was recorded.", "added": 0, "removed": 0}
        contents = []
        for meta in (old, new):
            data = self._read_blob(meta.get("hash", "")) if meta["kind"] == "text" else b""
            if data is None:
                raise LookupError("Snapshot content is no longer available")
            contents.append(data.decode("utf-8"))
        # Keep SequenceMatcher bounded even for pathological repeated-line files.
        if sum(len(s.splitlines()) for s in contents) > 12000:
            return {"patch": "", "note": "This file exceeds the 12,000-line diff limit.", "added": 0, "removed": 0}
        root = (self._get(f"S#{session}", f"RUN#{first['run']}") or {}).get("cwd", "")
        try:
            label = Path(first["path"]).relative_to(root).as_posix() if root else first["path"]
        except ValueError:
            label = first["path"]
        label = label.replace("\n", "\\n").replace("\r", "\\r")
        lines = list(difflib.unified_diff(contents[0].splitlines(keepends=True), contents[1].splitlines(keepends=True),
                    fromfile="/dev/null" if old["kind"] == "absent" else "a/" + label,
                    tofile="/dev/null" if new["kind"] == "absent" else "b/" + label))
        rendered = [line if line.endswith("\n") else line + "\n\\ No newline at end of file\n" for line in lines]
        note = ""
        if not lines:
            if old["kind"] == "absent" and new["kind"] != "absent":
                note = "An empty file was added. There are no text lines to compare."
            elif new["kind"] == "absent" and old["kind"] != "absent":
                note = "An empty file was deleted. There are no text lines to compare."
            elif old.get("mode") != new.get("mode"):
                note = "File mode changed."
        return {"patch": "".join(rendered), "note": note,
                "added": sum(l.startswith("+") for l in lines[2:]),
                "removed": sum(l.startswith("-") for l in lines[2:])}

    def history(self, session, first_id, last_id):
        row = self._change(session, first_id)
        if not row:
            raise LookupError("Recorded change not found")
        rows = self._query("pk = :p AND sk BETWEEN :a AND :b",
                           {":p": f"S#{session}", ":a": f"CHG#{int(first_id):012d}", ":b": f"CHG#{int(last_id):012d}",
                            ":path": row["path"]}, names={"#path": "path"}, filter="#path = :path", forward=False, limit=100)
        return [{"id": r["id"], "run": r["run"], "tool": r["tool"], "source": r["source"], "created": r["created"]} for r in rows]
