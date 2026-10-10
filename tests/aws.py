"""In-memory DynamoDB + S3 (moto) for the store tests. No AWS account or network is used."""

import os
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

TABLE, BUCKET, REGION = "session-diff-test", "session-diff-test-bucket", "us-east-1"


class MockAWS:
    """Mixin: starts moto for each test and provisions the journal table and bucket."""

    def setUp(self):
        for key, value in {"AWS_ACCESS_KEY_ID": "testing", "AWS_SECRET_ACCESS_KEY": "testing",
                           "AWS_SESSION_TOKEN": "testing", "AWS_DEFAULT_REGION": REGION}.items():
            os.environ[key] = value
        import boto3
        from moto import mock_aws
        from session_diff.store import table_definition
        self._moto = mock_aws()
        self._moto.start()
        self.addCleanup(self._moto.stop)
        self.ddb = boto3.client("dynamodb", region_name=REGION)
        self.s3 = boto3.client("s3", region_name=REGION)
        self.ddb.create_table(**table_definition(TABLE))
        self.s3.create_bucket(Bucket=BUCKET)

    def new_store(self, **kwargs):
        from session_diff.store import Store
        options = dict(table=TABLE, bucket=BUCKET, region=REGION, dynamodb=self.ddb, s3=self.s3)
        options.update(kwargs)
        return Store(**options)

    def items(self, prefix=None):
        found, args = [], {"TableName": TABLE}
        while True:
            page = self.ddb.scan(**args)
            found.extend(page["Items"])
            if "LastEvaluatedKey" not in page:
                break
            args["ExclusiveStartKey"] = page["LastEvaluatedKey"]
        return [i for i in found if prefix is None or i["pk"]["S"].startswith(prefix)]

    def blob_count(self):
        objects = self.s3.list_objects_v2(Bucket=BUCKET).get("KeyCount", 0)
        refs = len(self.items("BLOB#"))
        self.assertEqual(objects, refs, "S3 objects and DynamoDB blob references disagree")
        return objects

    def budget_bytes(self):
        item = self.ddb.get_item(TableName=TABLE, Key={"pk": {"S": "STATS"}, "sk": {"S": "BLOBS"}}).get("Item")
        return int(item["bytes"]["N"]) if item else 0

    def age_everything(self):
        """Backdate every session, run and change so retention treats them as expired."""
        for item in self.items("S#"):
            self.ddb.update_item(TableName=TABLE, Key={"pk": item["pk"], "sk": item["sk"]},
                                 UpdateExpression="SET gsk = :zero", ExpressionAttributeValues={":zero": {"N": "0"}})
