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
"""

import difflib
import json
import os
import threading
import time
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


def _client(service, region):
    # A Store is created per call; boto3 clients are expensive and thread-safe, so share them.
    with _clients_lock:
        if (service, region) not in _clients:
            import boto3
            from botocore.config import Config
            _clients[(service, region)] = boto3.client(
                service, region_name=region,
                config=Config(connect_timeout=3, read_timeout=10, retries={"max_attempts": 4, "mode": "standard"}))
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

    def _get(self, pk, sk):
        found = self.db.get_item(TableName=self.table, Key=self._key(pk, sk), ConsistentRead=True)
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
            page = self.db.query(**args)
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
        return self._dec(self.db.update_item(**args).get("Attributes", {}))

    def _put_all(self, items):
        for start in range(0, len(items), 25):
            pending = [{"PutRequest": {"Item": self._enc(item)}} for item in items[start:start + 25]]
            for attempt in range(8):
                pending = self.db.batch_write_item(RequestItems={self.table: pending}).get("UnprocessedItems", {}).get(self.table, [])
                if not pending:
                    break
                time.sleep(0.05 * 2 ** attempt)
            else:
                raise RuntimeError("DynamoDB did not accept all change records.")

    def _blob_key(self, digest):
        return f"{self.prefix}/blobs/{digest}" if self.prefix else f"blobs/{digest}"

    def _read_blob(self, digest):
        try:
            return self.s3.get_object(Bucket=self.bucket, Key=self._blob_key(digest))["Body"].read()
        except self.s3.exceptions.NoSuchKey:
            return None

    # -- blob lifecycle: S3 object + reference count + byte budget --------------
    def _acquire(self, digest, data, count):
        """Add ``count`` references to a blob, uploading it (within budget) on first reference."""
        old = self._update(f"BLOB#{digest}", "REF", "ADD refs :k SET #sz = :size",
                           {":k": count, ":size": len(data)}, names={"#sz": "size"}, returns="ALL_OLD")
        if "refs" in old:
            return
        try:
            self._update("STATS", "BLOBS", "ADD #b :n", {":n": len(data), ":room": BUDGET - len(data)},
                         names={"#b": "bytes"}, condition="attribute_not_exists(#b) OR #b <= :room")
        except self.db.exceptions.ConditionalCheckFailedException:
            self._release(digest, count, refund=False)
            raise RuntimeError("The 256 MiB snapshot budget is full. Reduce retention or remove old plugin data.") from None
        try:
            self.s3.put_object(Bucket=self.bucket, Key=self._blob_key(digest), Body=data,
                               ContentType="text/plain; charset=utf-8", ServerSideEncryption="AES256")
        except Exception:
            self._release(digest, count)
            raise

    def _release(self, digest, count, refund=True):
        """Drop ``count`` references; the last one out deletes the item, the S3 object and its budget."""
        now = self._update(f"BLOB#{digest}", "REF", "ADD refs :k", {":k": -count}, returns="ALL_NEW")
        if now.get("refs", 0) > 0:
            return
        try:
            self.db.delete_item(TableName=self.table, Key=self._key(f"BLOB#{digest}", "REF"),
                                ConditionExpression="refs <= :zero", ExpressionAttributeValues=self._enc({":zero": 0}))
        except self.db.exceptions.ConditionalCheckFailedException:
            return  # re-referenced in the meantime; that writer owns it now
        if refund and "size" in now:
            self._update("STATS", "BLOBS", "ADD #b :n", {":n": -int(now["size"])}, names={"#b": "bytes"})
        self.s3.delete_object(Bucket=self.bucket, Key=self._blob_key(digest))

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
        acquired = []
        try:
            for digest, count in Counter(h for *_, refs in entries for h in refs).items():
                self._acquire(digest, blobs[digest], count)
                acquired.append((digest, count))
            now = time.time()
            end = self._update(f"S#{session}", "META", "SET sess = :s, updated = :now, gpk = :gpk, gsk = :now ADD next_id :n",
                               {":s": session, ":now": now, ":gpk": "META", ":n": len(entries)}, returns="UPDATED_NEW")["next_id"]
            first = int(end) - len(entries) + 1
            self._put_all([
                {"pk": f"S#{session}", "sk": f"CHG#{first + i:012d}", "id": first + i, "sess": session, "run": run,
                 "tool_id": tool_id, "tool": tool, "source": source, "path": path, "before": json.dumps(old),
                 "after": json.dumps(new), "refs": json.dumps(refs), "created": now, "gpk": "CHG", "gsk": now}
                for i, (path, old, new, refs) in enumerate(entries)])
        except Exception:
            for digest, count in acquired:
                try:
                    self._release(digest, count)
                except Exception:
                    pass
            raise

    def prune(self, days=30):
        cutoff = time.time() - max(1, min(int(days), 365)) * 86400
        for kind in ("CHG", "RUN", "META"):
            for item in list(self._query("gpk = :g AND gsk < :c", {":g": kind, ":c": cutoff}, index="gsi")):
                try:
                    gone = self.db.delete_item(TableName=self.table, Key=self._key(item["pk"], item["sk"]), ReturnValues="ALL_OLD",
                                               ConditionExpression="gsk < :c", ExpressionAttributeValues=self._enc({":c": cutoff}))
                except self.db.exceptions.ConditionalCheckFailedException:
                    continue  # refreshed since the index read
                # Only the process whose delete succeeded releases references, so counts cannot double-drop.
                if kind == "CHG" and "Attributes" in gone:
                    for digest, count in Counter(json.loads(self._dec(gone["Attributes"]).get("refs", "[]"))).items():
                        self._release(digest, count)

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
