from __future__ import annotations

import unittest
from pathlib import Path

from botocore.exceptions import ClientError

from scripts.s3_archive_ticks import (
    GLACIER_STORAGE_CLASS,
    VersionedObject,
    archive_one,
    archive_ticks,
)


def _not_found(operation: str = "HeadObject") -> ClientError:
    return ClientError(
        {"Error": {"Code": "404", "Message": "Not Found"}},
        operation,
    )


class _Paginator:
    def __init__(self, client: "_FakeS3", operation: str) -> None:
        self.client = client
        self.operation = operation

    def paginate(self, *, Bucket: str, Prefix: str):
        del Bucket
        if self.operation == "list_object_versions":
            versions = []
            for key, value in self.client.objects.items():
                if key.startswith(Prefix):
                    versions.append(
                        {
                            "Key": key,
                            "VersionId": value["VersionId"],
                            "Size": value["Size"],
                            "ETag": f'"{value["ETag"]}"',
                            "IsLatest": True,
                        }
                    )
            return [{"Versions": versions}]
        if self.operation == "list_objects_v2":
            contents = [
                {"Key": key, "Size": value["Size"]}
                for key, value in self.client.objects.items()
                if key.startswith(Prefix)
            ]
            return [{"Contents": contents}]
        raise AssertionError(self.operation)


class _FakeS3:
    def __init__(self) -> None:
        self.objects = {
            "ticks/BTCUSDT_ticks.h5": {
                "VersionId": "tick-v1",
                "Size": 100,
                "ETag": "tick-etag",
                "StorageClass": "STANDARD",
                "Metadata": {},
            },
            "ticks-parquet/binance/BTCUSDT/trades/day.parquet": {
                "VersionId": "pq-v1",
                "Size": 50,
                "ETag": "pq-etag",
                "StorageClass": "STANDARD",
                "Metadata": {},
            },
            "ohlcv/binance/BTCUSDT/1m/2026.parquet": {
                "VersionId": "ohlcv-v1",
                "Size": 200,
                "ETag": "ohlcv-etag",
                "StorageClass": "STANDARD",
                "Metadata": {},
            },
        }
        self.copy_calls: list[tuple] = []
        self.delete_calls: list[tuple] = []

    def get_paginator(self, operation: str) -> _Paginator:
        return _Paginator(self, operation)

    def head_object(self, *, Bucket: str, Key: str):
        del Bucket
        try:
            value = self.objects[Key]
        except KeyError as exc:
            raise _not_found() from exc
        return {
            "ContentLength": value["Size"],
            "StorageClass": value["StorageClass"],
            "Metadata": value["Metadata"],
        }

    def copy(
        self,
        copy_source: dict,
        bucket: str,
        destination_key: str,
        *,
        ExtraArgs: dict,
    ) -> None:
        del bucket
        source = self.objects[copy_source["Key"]]
        self.copy_calls.append((copy_source, destination_key, ExtraArgs))
        self.objects[destination_key] = {
            "VersionId": f"archive-{source['VersionId']}",
            "Size": source["Size"],
            "ETag": source["ETag"],
            "StorageClass": ExtraArgs["StorageClass"],
            "Metadata": ExtraArgs["Metadata"],
        }

    def delete_object(self, *, Bucket: str, Key: str, VersionId: str):
        del Bucket
        self.delete_calls.append((Key, VersionId))
        value = self.objects[Key]
        if value["VersionId"] != VersionId:
            raise AssertionError("wrong version deleted")
        del self.objects[Key]
        return {"DeleteMarker": False}


class TestTickOnlyArchive(unittest.TestCase):
    def test_archive_preserves_ohlcv_and_removes_tick_sources(self) -> None:
        client = _FakeS3()

        result = archive_ticks(client, "bucket", dry_run=False)

        self.assertEqual(result["copied"], 2)
        self.assertEqual(result["deleted"], 2)
        self.assertIn("ohlcv/binance/BTCUSDT/1m/2026.parquet", client.objects)
        self.assertNotIn("ticks/BTCUSDT_ticks.h5", client.objects)
        self.assertNotIn(
            "ticks-parquet/binance/BTCUSDT/trades/day.parquet",
            client.objects,
        )
        self.assertEqual(
            client.objects["cold-archive/ticks/BTCUSDT_ticks.h5"]["StorageClass"],
            GLACIER_STORAGE_CLASS,
        )
        self.assertEqual(
            client.objects[
                "cold-archive/ticks-parquet/binance/BTCUSDT/trades/day.parquet"
            ]["StorageClass"],
            GLACIER_STORAGE_CLASS,
        )

    def test_dry_run_changes_nothing(self) -> None:
        client = _FakeS3()
        before = dict(client.objects)

        result = archive_ticks(client, "bucket", dry_run=True)

        self.assertEqual(result["planned"], 2)
        self.assertEqual(client.objects, before)
        self.assertEqual(client.copy_calls, [])
        self.assertEqual(client.delete_calls, [])

    def test_dry_run_excludes_known_corrupt_hdf5(self) -> None:
        client = _FakeS3()

        result = archive_ticks(
            client,
            "bucket",
            dry_run=True,
            corrupt_keys={"ticks/BTCUSDT_ticks.h5"},
        )

        self.assertEqual(result["planned"], 1)
        self.assertEqual(client.copy_calls, [])
        self.assertIn("ticks/BTCUSDT_ticks.h5", client.objects)

    def test_execute_refuses_if_corrupt_source_was_not_deleted(self) -> None:
        client = _FakeS3()

        with self.assertRaisesRegex(RuntimeError, "refusing to archive corrupt HDF5"):
            archive_ticks(
                client,
                "bucket",
                dry_run=False,
                corrupt_keys={"ticks/BTCUSDT_ticks.h5"},
            )

        self.assertEqual(client.copy_calls, [])
        self.assertEqual(client.delete_calls, [])

    def test_matching_destination_is_reused_after_interrupted_run(self) -> None:
        client = _FakeS3()
        source = VersionedObject(
            key="ticks/BTCUSDT_ticks.h5",
            version_id="tick-v1",
            size=100,
            etag="tick-etag",
        )
        client.objects[source.destination_key] = {
            "VersionId": "archive-existing",
            "Size": source.size,
            "ETag": source.etag,
            "StorageClass": GLACIER_STORAGE_CLASS,
            "Metadata": {
                "source-version-id": source.version_id,
                "source-etag": source.etag,
            },
        }

        disposition = archive_one(client, "bucket", source)

        self.assertEqual(disposition, "reused")
        self.assertEqual(client.copy_calls, [])
        self.assertNotIn(source.key, client.objects)

    def test_conflicting_destination_aborts_before_source_delete(self) -> None:
        client = _FakeS3()
        source = VersionedObject(
            key="ticks/BTCUSDT_ticks.h5",
            version_id="tick-v1",
            size=100,
            etag="tick-etag",
        )
        client.objects[source.destination_key] = {
            "VersionId": "wrong",
            "Size": 99,
            "ETag": "wrong",
            "StorageClass": GLACIER_STORAGE_CLASS,
            "Metadata": {},
        }

        with self.assertRaisesRegex(RuntimeError, "destination conflict"):
            archive_one(client, "bucket", source)

        self.assertIn(source.key, client.objects)
        self.assertEqual(client.delete_calls, [])

    def test_shell_rejects_tick_archive_with_broad_glacier(self) -> None:
        script = (
            Path(__file__).resolve().parents[1]
            / "scripts"
            / "s3_archive_and_clean.sh"
        )
        text = script.read_text()
        self.assertIn(
            "--archive-ticks is the selective path that preserves ohlcv/",
            text,
        )
        self.assertIn(
            "Do not combine it with --glacier or --clean-live",
            text,
        )


if __name__ == "__main__":
    unittest.main()
