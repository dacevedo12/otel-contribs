import aiobotocore.session  # pylint: disable=import-error
import asyncio
import json
from moto import (
    mock_dynamodb2,
)
from opentelemetry.semconv.trace import (
    SpanAttributes,
)
from opentelemetry.test.test_base import (
    TestBase,
)
from opentelemetry.trace.span import (
    Span as SpanBase,
)
from otelcontribs.instrumentation.aiobotocore import (
    AiobotocoreInstrumentor,
)
from typing import (
    Any,
    Awaitable,
    TypeVar,
)


class Span(SpanBase):
    attributes: dict[str, str]


T = TypeVar("T")


def async_call(coro: Awaitable[T]) -> T:
    loop = asyncio.get_event_loop()
    return loop.run_until_complete(coro)


class TestDynamoDbExtension(
    TestBase
):  # pylint: disable=too-many-public-methods
    def setUp(self) -> None:
        super().setUp()
        AiobotocoreInstrumentor().instrument()

        session = aiobotocore.session.get_session()
        session.set_credentials(
            access_key="access-key", secret_key="secret-key"
        )
        self.client = async_call(
            # pylint: disable=unnecessary-dunder-call
            session.create_client(
                "dynamodb", region_name="us-west-2"
            ).__aenter__()
        )
        self.default_table_name = "test_table"

    def tearDown(self) -> None:
        super().tearDown()
        AiobotocoreInstrumentor().uninstrument()

    def _create_table(self, **kwargs: Any) -> None:
        create_args = {
            "TableName": self.default_table_name,
            "AttributeDefinitions": [
                {"AttributeName": "id", "AttributeType": "S"},
                {"AttributeName": "idl", "AttributeType": "S"},
                {"AttributeName": "idg", "AttributeType": "S"},
            ],
            "KeySchema": [{"AttributeName": "id", "KeyType": "HASH"}],
            "ProvisionedThroughput": {
                "ReadCapacityUnits": 5,
                "WriteCapacityUnits": 5,
            },
            "LocalSecondaryIndexes": [
                {
                    "IndexName": "lsi",
                    "KeySchema": [{"AttributeName": "idl", "KeyType": "HASH"}],
                    "Projection": {"ProjectionType": "KEYS_ONLY"},
                }
            ],
            "GlobalSecondaryIndexes": [
                {
                    "IndexName": "gsi",
                    "KeySchema": [{"AttributeName": "idg", "KeyType": "HASH"}],
                    "Projection": {"ProjectionType": "KEYS_ONLY"},
                }
            ],
        }
        create_args.update(kwargs)

        async_call(self.client.create_table(**create_args))

    def _create_prepared_table(self, **kwargs: Any) -> None:
        self._create_table(**kwargs)

        table = kwargs.get("TableName", self.default_table_name)
        async_call(
            self.client.put_item(
                TableName=table,
                Item={"id": {"S": "1"}, "idl": {"S": "2"}, "idg": {"S": "3"}},
            )
        )

        self.memory_exporter.clear()

    def assert_span(self, operation: str) -> Span:
        spans = self.memory_exporter.get_finished_spans()
        self.assertEqual(1, len(spans))
        span = spans[0]

        self.assertEqual("dynamodb", span.attributes[SpanAttributes.DB_SYSTEM])
        self.assertEqual(
            operation, span.attributes[SpanAttributes.DB_OPERATION]
        )
        self.assertEqual(
            "dynamodb.us-west-2.amazonaws.com",
            span.attributes[SpanAttributes.NET_PEER_NAME],
        )
        return span

    def assert_table_names(self, span: Span, *table_names: str) -> None:
        self.assertEqual(
            tuple(table_names),
            span.attributes[SpanAttributes.AWS_DYNAMODB_TABLE_NAMES],
        )

    def assert_consumed_capacity(self, span: Span, *table_names: str) -> None:
        cap = span.attributes[SpanAttributes.AWS_DYNAMODB_CONSUMED_CAPACITY]
        self.assertEqual(len(cap), len(table_names))
        cap_tables = set()
        for item in cap:
            # should be like {"TableName": name, "CapacityUnits": number, ...}
            deserialized = json.loads(item)
            cap_tables.add(deserialized["TableName"])
        for table_name in table_names:
            self.assertIn(table_name, cap_tables)

    def assert_item_col_metrics(self, span: Span) -> None:
        actual = span.attributes[
            SpanAttributes.AWS_DYNAMODB_ITEM_COLLECTION_METRICS
        ]
        self.assertIsNotNone(actual)
        json.loads(actual)

    def assert_provisioned_read_cap(self, span: Span, expected: int) -> None:
        actual = span.attributes[
            SpanAttributes.AWS_DYNAMODB_PROVISIONED_READ_CAPACITY
        ]
        self.assertEqual(expected, actual)

    def assert_provisioned_write_cap(self, span: Span, expected: int) -> None:
        actual = span.attributes[
            SpanAttributes.AWS_DYNAMODB_PROVISIONED_WRITE_CAPACITY
        ]
        self.assertEqual(expected, actual)

    def assert_consistent_read(self, span: Span, expected: bool) -> None:
        actual = span.attributes[SpanAttributes.AWS_DYNAMODB_CONSISTENT_READ]
        self.assertEqual(expected, actual)

    def assert_projection(self, span: Span, expected: str) -> None:
        actual = span.attributes[SpanAttributes.AWS_DYNAMODB_PROJECTION]
        self.assertEqual(expected, actual)

    def assert_attributes_to_get(self, span: Span, *attrs: str) -> None:
        self.assertEqual(
            tuple(attrs),
            span.attributes[SpanAttributes.AWS_DYNAMODB_ATTRIBUTES_TO_GET],
        )

    def assert_index_name(self, span: Span, expected: str) -> None:
        self.assertEqual(
            expected, span.attributes[SpanAttributes.AWS_DYNAMODB_INDEX_NAME]
        )

    def assert_limit(self, span: Span, expected: int) -> None:
        self.assertEqual(
            expected, span.attributes[SpanAttributes.AWS_DYNAMODB_LIMIT]
        )

    def assert_select(self, span: Span, expected: str) -> None:
        self.assertEqual(
            expected, span.attributes[SpanAttributes.AWS_DYNAMODB_SELECT]
        )

    @mock_dynamodb2
    def test_batch_get_item(self) -> None:
        table_name1 = "test_table1"
        table_name2 = "test_table2"
        self._create_prepared_table(TableName=table_name1)
        self._create_prepared_table(TableName=table_name2)

        async_call(
            self.client.batch_get_item(
                RequestItems={
                    table_name1: {"Keys": [{"id": {"S": "test_key"}}]},
                    table_name2: {"Keys": [{"id": {"S": "test_key2"}}]},
                },
                ReturnConsumedCapacity="TOTAL",
            )
        )

        span = self.assert_span("BatchGetItem")
        self.assert_table_names(span, table_name1, table_name2)
        self.assert_consumed_capacity(span, table_name1, table_name2)

    @mock_dynamodb2
    def test_batch_write_item(self) -> None:
        table_name1 = "test_table1"
        table_name2 = "test_table2"
        self._create_prepared_table(TableName=table_name1)
        self._create_prepared_table(TableName=table_name2)

        async_call(
            self.client.batch_write_item(
                RequestItems={
                    table_name1: [
                        {"PutRequest": {"Item": {"id": {"S": "123"}}}}
                    ],
                    table_name2: [
                        {"PutRequest": {"Item": {"id": {"S": "456"}}}}
                    ],
                },
                ReturnConsumedCapacity="TOTAL",
                ReturnItemCollectionMetrics="SIZE",
            )
        )

        span = self.assert_span("BatchWriteItem")
        self.assert_table_names(span, table_name1, table_name2)
        self.assert_consumed_capacity(span, table_name1, table_name2)
        self.assert_item_col_metrics(span)

    @mock_dynamodb2
    def test_create_table(self) -> None:
        local_sec_idx = {
            "IndexName": "local_sec_idx",
            "KeySchema": [{"AttributeName": "value", "KeyType": "HASH"}],
            "Projection": {"ProjectionType": "KEYS_ONLY"},
        }
        global_sec_idx = {
            "IndexName": "global_sec_idx",
            "KeySchema": [{"AttributeName": "value", "KeyType": "HASH"}],
            "Projection": {"ProjectionType": "KEYS_ONLY"},
        }

        async_call(
            self.client.create_table(
                AttributeDefinitions=[
                    {"AttributeName": "id", "AttributeType": "S"},
                    {"AttributeName": "value", "AttributeType": "S"},
                ],
                KeySchema=[{"AttributeName": "id", "KeyType": "HASH"}],
                LocalSecondaryIndexes=[local_sec_idx],
                GlobalSecondaryIndexes=[global_sec_idx],
                ProvisionedThroughput={
                    "ReadCapacityUnits": 42,
                    "WriteCapacityUnits": 17,
                },
                TableName=self.default_table_name,
            )
        )

        span = self.assert_span("CreateTable")
        self.assert_table_names(span, self.default_table_name)
        self.assertEqual(
            (json.dumps(global_sec_idx),),
            span.attributes[
                SpanAttributes.AWS_DYNAMODB_GLOBAL_SECONDARY_INDEXES
            ],
        )
        self.assertEqual(
            (json.dumps(local_sec_idx),),
            span.attributes[
                SpanAttributes.AWS_DYNAMODB_LOCAL_SECONDARY_INDEXES
            ],
        )
        self.assert_provisioned_read_cap(span, 42)

    @mock_dynamodb2
    def test_delete_item(self) -> None:
        self._create_prepared_table()

        async_call(
            self.client.delete_item(
                TableName=self.default_table_name,
                Key={"id": {"S": "1"}},
                ReturnConsumedCapacity="TOTAL",
                ReturnItemCollectionMetrics="SIZE",
            )
        )

        span = self.assert_span("DeleteItem")
        self.assert_table_names(span, self.default_table_name)

    @mock_dynamodb2
    def test_delete_table(self) -> None:
        self._create_prepared_table()

        async_call(self.client.delete_table(TableName=self.default_table_name))

        span = self.assert_span("DeleteTable")
        self.assert_table_names(span, self.default_table_name)

    @mock_dynamodb2
    def test_describe_table(self) -> None:
        self._create_prepared_table()

        async_call(
            self.client.describe_table(TableName=self.default_table_name)
        )

        span = self.assert_span("DescribeTable")
        self.assert_table_names(span, self.default_table_name)

    @mock_dynamodb2
    def test_get_item(self) -> None:
        self._create_prepared_table()

        async_call(
            self.client.get_item(
                TableName=self.default_table_name,
                Key={"id": {"S": "1"}},
                ConsistentRead=True,
                AttributesToGet=["id"],
                ProjectionExpression="1,2",
                ReturnConsumedCapacity="TOTAL",
            )
        )

        span = self.assert_span("GetItem")
        self.assert_table_names(span, self.default_table_name)
        self.assert_consistent_read(span, True)
        self.assert_projection(span, "1,2")
        self.assert_consumed_capacity(span, self.default_table_name)

    @mock_dynamodb2
    def test_list_tables(self) -> None:
        self._create_table(TableName="my_table")
        self._create_prepared_table()

        async_call(
            self.client.list_tables(
                ExclusiveStartTableName="my_table", Limit=5
            )
        )

        span = self.assert_span("ListTables")
        self.assertEqual(
            "my_table",
            span.attributes[SpanAttributes.AWS_DYNAMODB_EXCLUSIVE_START_TABLE],
        )
        self.assertEqual(
            1, span.attributes[SpanAttributes.AWS_DYNAMODB_TABLE_COUNT]
        )
        self.assertEqual(5, span.attributes[SpanAttributes.AWS_DYNAMODB_LIMIT])

    @mock_dynamodb2
    def test_put_item(self) -> None:
        table = "test_table"
        self._create_prepared_table(TableName=table)

        async_call(
            self.client.put_item(
                TableName=table,
                Item={"id": {"S": "1"}, "idl": {"S": "2"}, "idg": {"S": "3"}},
                ReturnConsumedCapacity="TOTAL",
                ReturnItemCollectionMetrics="SIZE",
            )
        )

        span = self.assert_span("PutItem")
        self.assert_table_names(span, table)
        self.assert_consumed_capacity(span, table)

    @mock_dynamodb2
    def test_query(self) -> None:
        self._create_prepared_table()

        async_call(
            self.client.query(
                TableName=self.default_table_name,
                IndexName="lsi",
                Select="ALL_ATTRIBUTES",
                AttributesToGet=["id"],
                Limit=42,
                ConsistentRead=True,
                KeyConditions={
                    "id": {
                        "AttributeValueList": [{"S": "123"}],
                        "ComparisonOperator": "EQ",
                    }
                },
                ScanIndexForward=True,
                ProjectionExpression="1,2",
                ReturnConsumedCapacity="TOTAL",
            )
        )

        span = self.assert_span("Query")
        self.assert_table_names(span, self.default_table_name)
        self.assertTrue(
            span.attributes[SpanAttributes.AWS_DYNAMODB_SCAN_FORWARD]
        )
        self.assert_attributes_to_get(span, "id")
        self.assert_consistent_read(span, True)
        self.assert_index_name(span, "lsi")
        self.assert_limit(span, 42)
        self.assert_projection(span, "1,2")
        self.assert_select(span, "ALL_ATTRIBUTES")
        self.assert_consumed_capacity(span, self.default_table_name)

    @mock_dynamodb2
    def test_scan(self) -> None:
        self._create_prepared_table()

        async_call(
            self.client.scan(
                TableName=self.default_table_name,
                IndexName="lsi",
                AttributesToGet=["id", "idl"],
                Limit=42,
                Select="ALL_ATTRIBUTES",
                TotalSegments=17,
                Segment=21,
                ProjectionExpression="1,2",
                ConsistentRead=True,
                ReturnConsumedCapacity="TOTAL",
            )
        )

        span = self.assert_span("Scan")
        self.assert_table_names(span, self.default_table_name)
        self.assertEqual(
            21, span.attributes[SpanAttributes.AWS_DYNAMODB_SEGMENT]
        )
        self.assertEqual(
            17, span.attributes[SpanAttributes.AWS_DYNAMODB_TOTAL_SEGMENTS]
        )
        self.assertEqual(1, span.attributes[SpanAttributes.AWS_DYNAMODB_COUNT])
        self.assertEqual(
            1, span.attributes[SpanAttributes.AWS_DYNAMODB_SCANNED_COUNT]
        )
        self.assert_attributes_to_get(span, "id", "idl")
        self.assert_consistent_read(span, True)
        self.assert_index_name(span, "lsi")
        self.assert_limit(span, 42)
        self.assert_projection(span, "1,2")
        self.assert_select(span, "ALL_ATTRIBUTES")
        self.assert_consumed_capacity(span, self.default_table_name)

    @mock_dynamodb2
    def test_update_item(self) -> None:
        self._create_prepared_table()

        async_call(
            self.client.update_item(
                TableName=self.default_table_name,
                Key={"id": {"S": "123"}},
                AttributeUpdates={
                    "id": {"Value": {"S": "456"}, "Action": "PUT"}
                },
                ReturnConsumedCapacity="TOTAL",
                ReturnItemCollectionMetrics="SIZE",
            )
        )

        span = self.assert_span("UpdateItem")
        self.assert_table_names(span, self.default_table_name)
        self.assert_consumed_capacity(span, self.default_table_name)

    @mock_dynamodb2
    def test_update_table(self) -> None:
        self._create_prepared_table()

        global_sec_idx_updates = {
            "Update": {
                "IndexName": "gsi",
                "ProvisionedThroughput": {
                    "ReadCapacityUnits": 777,
                    "WriteCapacityUnits": 666,
                },
            }
        }
        attr_definition = {"AttributeName": "id", "AttributeType": "N"}

        async_call(
            self.client.update_table(
                TableName=self.default_table_name,
                AttributeDefinitions=[attr_definition],
                ProvisionedThroughput={
                    "ReadCapacityUnits": 23,
                    "WriteCapacityUnits": 19,
                },
                GlobalSecondaryIndexUpdates=[global_sec_idx_updates],
            )
        )

        span = self.assert_span("UpdateTable")
        self.assert_table_names(span, self.default_table_name)
        self.assert_provisioned_read_cap(span, 23)
        self.assert_provisioned_write_cap(span, 19)
        self.assertEqual(
            (json.dumps(attr_definition),),
            span.attributes[SpanAttributes.AWS_DYNAMODB_ATTRIBUTE_DEFINITIONS],
        )
        self.assertEqual(
            (json.dumps(global_sec_idx_updates),),
            span.attributes[
                SpanAttributes.AWS_DYNAMODB_GLOBAL_SECONDARY_INDEX_UPDATES
            ],
        )
