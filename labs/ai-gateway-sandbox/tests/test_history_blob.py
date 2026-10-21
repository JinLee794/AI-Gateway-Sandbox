import email.utils
import importlib.util
import json
from pathlib import Path
import unittest
from unittest.mock import patch
from xml.sax.saxutils import escape


LAB = Path(__file__).resolve().parents[1]
spec = importlib.util.spec_from_file_location("sandbox_app", LAB / "src" / "app.py")
app = importlib.util.module_from_spec(spec)
spec.loader.exec_module(app)


class FakeBlobs:
    """In-memory stand-in for the Blob Storage REST calls app._blob makes (PUT, GET, DELETE, list)."""

    def __init__(self):
        self.blobs, self.clock, self.fail_list = {}, 1_700_000_000, False

    def __call__(self, method, name="", query="", body=None, timeout=60):
        if query.startswith("restype=container&comp=list"):
            if self.fail_list:
                return 403, "AuthorizationPermissionMismatch"
            prefix = app.urllib.parse.unquote(query.split("prefix=")[1].split("&")[0])
            items = "".join(f"<Blob><Name>{escape(n)}</Name><Properties><Last-Modified>{email.utils.formatdate(t, usegmt=True)}</Last-Modified></Properties></Blob>"
                            for n, (t, _) in self.blobs.items() if n.startswith(prefix))
            return 200, f'<?xml version="1.0" encoding="utf-8"?><EnumerationResults><Blobs>{items}</Blobs><NextMarker /></EnumerationResults>'
        if method == "PUT":
            self.clock += 1
            self.blobs[name] = (self.clock, body)
            return 201, ""
        if method == "GET":
            return (200, self.blobs[name][1]) if name in self.blobs else (404, "BlobNotFound")
        if method == "DELETE":
            return (202, "") if self.blobs.pop(name, None) else (404, "BlobNotFound")
        raise AssertionError(method)


class HistoryBlobTests(unittest.TestCase):
    def setUp(self):
        self.store = FakeBlobs()
        for p in (patch.object(app, "HISTORY_BLOB_URL", "https://sthist.blob.core.windows.net/run-history"), patch.object(app, "_blob", self.store)):
            p.start()
            self.addCleanup(p.stop)
        self.headers = {}
        self.uid = app.history_user(self.headers)[0]

    def save(self, run_id, started, **extra):
        return app.history_save(self.headers, {"run": {"id": run_id, "started": started, "title": run_id, **extra}})

    def test_runs_survive_and_come_back_oldest_first(self):
        self.assertEqual(self.save("run-bbbbbbbb", 2)[0], 200)
        self.assertEqual(self.save("run-aaaaaaaa", 1)[0], 200)
        status, result = app.history(self.headers)
        self.assertEqual(status, 200)
        self.assertEqual(result["store"], "Azure Blob Storage")
        self.assertEqual([r["id"] for r in result["runs"]], ["run-aaaaaaaa", "run-bbbbbbbb"])
        self.assertIn(f"{self.uid}/run-aaaaaaaa.json", self.store.blobs)

    def test_saving_a_run_again_replaces_it(self):
        self.save("run-aaaaaaaa", 1, status="running")
        self.save("run-aaaaaaaa", 1, status="finished")
        runs = app.history(self.headers)[1]["runs"]
        self.assertEqual([(r["id"], r["status"]) for r in runs], [("run-aaaaaaaa", "finished")])

    def test_keeps_the_newest_runs(self):
        for k in range(app.HISTORY_KEEP + 11):
            self.save(f"run-{k:08d}", k)
        self.assertEqual(len(self.store.blobs), app.HISTORY_KEEP)
        runs = app.history(self.headers)[1]["runs"]
        self.assertEqual(runs[-1]["id"], f"run-{app.HISTORY_KEEP + 10:08d}")

    def test_clear_deletes_only_this_users_runs(self):
        self.save("run-aaaaaaaa", 1)
        self.store.blobs["someone-else/run-cccccccc.json"] = (1, json.dumps({"run": {"id": "run-cccccccc"}}))
        self.assertEqual(app.history_clear(self.headers)[0], 200)
        self.assertEqual(list(self.store.blobs), ["someone-else/run-cccccccc.json"])

    def test_storage_error_is_reported_not_an_empty_history(self):
        self.store.fail_list = True
        status, result = app.history(self.headers)
        self.assertEqual(status, 200)
        self.assertTrue(result["enabled"])
        self.assertEqual(result["runs"], [])
        self.assertIn("HTTP 403", result["error"])


class BlobRequestTests(unittest.TestCase):
    def test_put_and_list_requests(self):
        sent = []

        class Response:
            def __init__(self, status): self.status = status
            def __enter__(self): return self
            def __exit__(self, *a): return False
            def read(self): return "\ufeff<EnumerationResults><Blobs /><NextMarker /></EnumerationResults>".encode("utf-8")

        def urlopen(request, timeout=60):
            sent.append(request)
            return Response(201 if request.get_method() == "PUT" else 200)

        with patch.object(app, "HISTORY_BLOB_URL", "https://sthist.blob.core.windows.net/run-history"), \
             patch.object(app, "az_token", lambda resource: f"token-for-{resource}"), patch.object(app.urllib.request, "urlopen", urlopen):
            app._blob("PUT", "user 1/run-aaaaaaaa.json", body='{"run":{}}')
            self.assertEqual(app._blob_list("user 1/"), [])
        put, listing = sent
        self.assertEqual(put.full_url, "https://sthist.blob.core.windows.net/run-history/user%201/run-aaaaaaaa.json")
        self.assertEqual(put.get_method(), "PUT")
        self.assertEqual(put.get_header("Authorization"), "Bearer token-for-https://storage.azure.com/")
        self.assertEqual(put.get_header("X-ms-blob-type"), "BlockBlob")
        self.assertTrue(put.get_header("X-ms-version") and put.get_header("X-ms-date"))
        self.assertEqual(put.data, b'{"run":{}}')
        self.assertEqual(listing.full_url, "https://sthist.blob.core.windows.net/run-history?restype=container&comp=list&prefix=user%201/")
        self.assertIsNone(listing.data)


if __name__ == "__main__":
    unittest.main()
