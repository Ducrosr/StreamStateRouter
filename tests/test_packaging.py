from __future__ import annotations

from pathlib import Path
import unittest

from stream_state_router.services.single_instance import SINGLE_INSTANCE_MUTEX


class PackagingTests(unittest.TestCase):
    def test_release_attestation_is_tag_only_and_covers_final_artifacts(self):
        root = Path(__file__).resolve().parents[1]
        release = (
            root / ".github" / "workflows" / "release.yml"
        ).read_text(encoding="utf-8")

        self.assertNotIn("  pull_request:", release)
        for permission in (
            "  contents: write",
            "  id-token: write",
            "  attestations: write",
            "  artifact-metadata: write",
        ):
            self.assertIn(permission, release)

        attest_action = (
            "actions/attest@"
            "1e69f48acb82d1966a394da916b4c1698aa569d6 # v4"
        )
        self.assertIn(attest_action, release)
        self.assertIn(
            "if: github.event_name == 'push' && "
            "startsWith(github.ref, 'refs/tags/v')",
            release,
        )

        expected_subjects = (
            "release/Stream-State-Router-v"
            "${{ steps.version.outputs.version }}-portable.zip",
            "release/Stream-State-Router-v"
            "${{ steps.version.outputs.version }}-setup.exe",
            "streamdeck-plugin/*.streamDeckPlugin",
            "release/build-manifest.json",
            "release/SHA256SUMS.txt",
        )
        for subject in expected_subjects:
            self.assertIn(subject, release)

        self.assertNotIn("            release/*", release)
        self.assertLess(
            release.index("- name: Verify SHA-256 checksum manifest"),
            release.index("- name: Attest release artifacts"),
        )
        self.assertLess(
            release.index("- name: Attest release artifacts"),
            release.index("- name: Publish GitHub release"),
        )

    def test_installer_mutex_matches_runtime_single_instance_mutex(self):
        root = Path(__file__).resolve().parents[1]
        installer = (
            root / "installer" / "StreamStateRouter.iss"
        ).read_text(encoding="utf-8")

        expected = f"AppMutex={SINGLE_INSTANCE_MUTEX}"
        self.assertIn(expected, installer)


if __name__ == "__main__":
    unittest.main()
