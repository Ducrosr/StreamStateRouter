from __future__ import annotations

import json
from pathlib import Path
import tempfile
import unittest

from stream_state_router.widgets import (
    import_html_module,
    inspect_html_module,
    list_widget_packages,
)


class HtmlWidgetPackageTests(unittest.TestCase):
    def test_standalone_import_copies_static_dependencies(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            source = root / "source"
            target = root / "library"
            source.mkdir()
            (source / "index.html").write_text(
                (
                    '<link rel="stylesheet" href="style.css">'
                    '<img src="img/logo.png">'
                    '<script src="app.js"></script>'
                ),
                encoding="utf-8",
            )
            (source / "style.css").write_text(
                "body{background:url('img/bg.png')}",
                encoding="utf-8",
            )
            (source / "app.js").write_text(
                "console.log('ok')",
                encoding="utf-8",
            )
            (source / "img").mkdir()
            (source / "img" / "logo.png").write_bytes(b"logo")
            (source / "img" / "bg.png").write_bytes(b"bg")

            inspection = inspect_html_module(source / "index.html")
            package = import_html_module(
                source / "index.html",
                name="Midgar Radio",
                target_root=target,
            )

            self.assertEqual(
                {
                    path.relative_to(
                        inspection.package_root
                    ).as_posix()
                    for path in inspection.local_files
                },
                {
                    "index.html",
                    "style.css",
                    "app.js",
                    "img/logo.png",
                    "img/bg.png",
                },
            )
            self.assertTrue(package.entry.is_file())
            self.assertTrue((package.root / "style.css").is_file())
            self.assertTrue((package.root / "img" / "bg.png").is_file())
            self.assertTrue(package.entry_uri.startswith("file:"))

    def test_folder_import_preserves_full_module_tree(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            source = root / "module"
            target = root / "library"
            source.mkdir()
            (source / "index.html").write_text(
                "<html></html>",
                encoding="utf-8",
            )
            (source / "assets").mkdir()
            (source / "assets" / "unused.svg").write_text(
                "<svg/>",
                encoding="utf-8",
            )
            (source / "node_modules").mkdir()
            (source / "node_modules" / "ignored.js").write_text(
                "ignored",
                encoding="utf-8",
            )

            package = import_html_module(
                source / "index.html",
                name="Events",
                package_root=source,
                target_root=target,
            )

            self.assertTrue(
                (package.root / "assets" / "unused.svg").is_file()
            )
            self.assertFalse((package.root / "node_modules").exists())

    def test_import_reports_remote_missing_and_unsafe_references(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            entry = root / "index.html"
            entry.write_text(
                (
                    '<script src="https://cdn.example.test/app.js"></script>'
                    '<img src="missing.png">'
                    '<img src="../outside.png">'
                ),
                encoding="utf-8",
            )

            inspection = inspect_html_module(entry)

            self.assertEqual(
                inspection.remote_references,
                ("https://cdn.example.test/app.js",),
            )
            self.assertEqual(
                inspection.missing_references,
                ("missing.png",),
            )
            self.assertEqual(
                inspection.unsafe_references,
                ("../outside.png",),
            )

    def test_package_manifest_can_be_discovered_without_source_path(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            source = root / "source"
            target = root / "library"
            source.mkdir()
            entry = source / "widget.html"
            entry.write_text("<html></html>", encoding="utf-8")

            package = import_html_module(
                entry,
                name="Chat Loveless",
                target_root=target,
            )
            manifest = json.loads(
                package.manifest.read_text(encoding="utf-8")
            )
            discovered = list_widget_packages(root=target)

            self.assertNotIn("source_path", manifest)
            self.assertEqual(len(discovered), 1)
            self.assertEqual(discovered[0].name, "Chat Loveless")
            self.assertEqual(
                discovered[0].entry.resolve(),
                package.entry.resolve(),
            )

    def test_entry_cannot_escape_selected_package_root(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            package_root = root / "module"
            package_root.mkdir()
            outside = root / "outside.html"
            outside.write_text("<html></html>", encoding="utf-8")

            with self.assertRaisesRegex(
                ValueError,
                "doit se trouver dans le dossier",
            ):
                inspect_html_module(
                    outside,
                    package_root=package_root,
                )


if __name__ == "__main__":
    unittest.main()
