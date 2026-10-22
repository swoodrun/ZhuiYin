"""Distribution closure tests; no paid APIs, server or business runtime imports."""
import json
from pathlib import Path
import tempfile
import unittest
import zipfile

from tools.repair_eight_arm_package import (check_archive, close_dependencies,
    digest, inspect_links, local_links, portable_copy, resolve_local, source_hash_checks)


class EightArmPackageLinksTests(unittest.TestCase):
    def test_static_footer_and_evidence_generated_links_are_both_checked(self):
        payload = {"sources": [{"path": "data/report.json"}], "real_study": {"source": "report.md"}}
        html = ('<a href="guide.md">guide</a><script id="evidence" type="application/json">'
                + json.dumps(payload) + '</script><script>const s=`<a href="${x}">`;</script>')
        links = local_links("demo.html", html.encode())
        self.assertEqual({p for _, p in links}, {"guide.md", "data/report.json", "report.md"})

    def test_markdown_examples_do_not_produce_clickable_links(self):
        value = b'[real](yes.md)\n```md\n[fake](not-a-file.md)\n```\n[ref][id]\n[id]: target.json\n'
        self.assertEqual({p for _, p in local_links("a.md", value)}, {"yes.md", "target.json"})

    def test_sibling_review_file_is_inside_outer_archive(self):
        self.assertEqual(resolve_local("zhuiyin/qa.md", "../review_artifacts/report.md", Path(".")),
                         "review_artifacts/report.md")

    def test_absolute_paths_are_rejected_even_when_drive_is_original(self):
        for value in ("C:/Users/old/file.md", "file:///C:/file.md", "/absolute/file.md"):
            with self.subTest(value=value), self.assertRaises(ValueError):
                resolve_local("a.md", value, Path("."))

    def test_escape_is_rejected(self):
        with self.assertRaises(ValueError):
            resolve_local("a.md", "../outside.md", Path("."))

    def test_external_and_fragment_urls_are_not_offline_file_claims(self):
        for value in ("https://example.com/a.md", "mailto:a@example.com", "#here", "data:image/png,abc"):
            self.assertIsNone(resolve_local("a.md", value, Path(".")))

    def test_encoded_path_and_optional_line_suffix(self):
        self.assertEqual(resolve_local("zhuiyin/a.md", "doc%20name.md:12?x=1", Path(".")), "zhuiyin/doc name.md")

    def test_portable_supplement_does_not_change_original_source(self):
        workspace = Path("C:/example")
        raw = b'[r](C:/example/review_artifacts/r.json:12)'
        derived = portable_copy("zhuiyin/qa.md", raw, workspace)
        self.assertEqual(derived, b'[r](../review_artifacts/r.json)')
        self.assertEqual(raw, b'[r](C:/example/review_artifacts/r.json:12)')

    def test_recursive_dependencies_and_directory_links_close(self):
        with tempfile.TemporaryDirectory() as temp:
            workspace = Path(temp)
            (workspace / "docs").mkdir()
            (workspace / "docs/next.md").write_text('[value](value.json)', encoding="utf-8")
            (workspace / "docs/value.json").write_text('{}', encoding="utf-8")
            files = {"start.md": b'[folder](docs/)'}
            added = close_dependencies(files, workspace)
            self.assertEqual(set(added), {"docs/next.md", "docs/value.json"})
            self.assertTrue(all(item["exists_in_package"] for item in inspect_links(files)))

    def test_missing_dependency_is_an_error_not_an_ignored_link(self):
        with tempfile.TemporaryDirectory() as temp, self.assertRaisesRegex(ValueError, "missing local dependency"):
            close_dependencies({"start.md": b'[x](absent.md)'}, Path(temp))

    def test_relocated_archive_check_needs_only_archive_members(self):
        with tempfile.TemporaryDirectory() as temp:
            path = Path(temp) / "moved.zip"
            files = {"zhuiyin/a.md": b'[x](b.json)', "zhuiyin/b.json": b'{}'}
            manifest = {n: {"sha256": digest(data), "bytes": len(data)} for n, data in files.items()}
            with zipfile.ZipFile(path, "w") as archive:
                for n, data in files.items():
                    archive.writestr(n, data)
                archive.writestr("链接修正版文件清单.json", json.dumps(manifest))
            self.assertTrue(check_archive(path)["passed"])

    def test_resigned_broken_destination_still_fails(self):
        with tempfile.TemporaryDirectory() as temp:
            path = Path(temp) / "broken.zip"
            files = {"zhuiyin/a.md": b'[x](absent.json)'}
            manifest = {n: {"sha256": digest(data), "bytes": len(data)} for n, data in files.items()}
            with zipfile.ZipFile(path, "w") as archive:
                for n, data in files.items():
                    archive.writestr(n, data)
                archive.writestr("链接修正版文件清单.json", json.dumps(manifest))
            result = check_archive(path)
            self.assertFalse(result["passed"])
            self.assertEqual(len(result["missing_local_links"]), 1)

    def test_embedded_source_hash_detects_changed_dependency(self):
        payload = {"sources": [{"path": "value.json", "sha256": digest(b'{}')}]}
        html = '<script id="evidence" type="application/json">' + json.dumps(payload) + '</script>'
        files = {"zhuiyin/demo.html": html.encode(), "zhuiyin/value.json": b'{}'}
        self.assertTrue(source_hash_checks(files)[0]["passed"])
        files["zhuiyin/value.json"] = b'{"edited": true}'
        self.assertFalse(source_hash_checks(files)[0]["passed"])

    def test_null_real_study_payload_is_a_valid_older_demo(self):
        html = b'<script id="evidence" type="application/json">{"sources": [], "real_study": null}</script>'
        self.assertEqual(local_links("older.html", html), [])


if __name__ == "__main__":
    unittest.main()
