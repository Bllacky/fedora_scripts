"""Use isolated files and mocked boot tools; never modify host configuration."""

import importlib.util
from pathlib import Path
import shutil
import subprocess
import sys
import tempfile
from types import SimpleNamespace
import unittest
from unittest.mock import patch

sys.dont_write_bytecode = True
BUNDLE = Path(__file__).resolve().parent.parent
spec = importlib.util.spec_from_file_location("edid_fix_test_backend", BUNDLE / "lib/edid_fix.py")
backend = importlib.util.module_from_spec(spec)
spec.loader.exec_module(backend)
from generate_edid import generate, SHA256  # noqa: E402


class ProfileTests(unittest.TestCase):
    def test_reproducible_binary_and_checksums(self):
        self.assertEqual((BUNDLE / "edid/dp-dummy-multimode.bin").read_bytes(), generate())
        self.assertEqual(len(generate()), 256)
        for offset in (0, 128):
            self.assertEqual(sum(generate()[offset:offset + 128]) % 256, 0)

    @unittest.skipUnless(shutil.which("edid-decode"), "optional edid-decode is unavailable")
    def test_independent_decoder(self):
        result = subprocess.run(["edid-decode", "--check", "--skip-hex-dump", "-"],
                                input=generate(), capture_output=True, check=True)
        report = result.stdout.decode()
        self.assertIn("EDID conformity: PASS", report)
        self.assertNotIn("Warnings:", report)
        self.assertNotIn("Failures:", report)
        import re
        decoded = {(int(w), int(h), round(float(rate))) for w, h, rate in
                   re.findall(r"DTD \d+:\s+(\d+)x(\d+)\s+([0-9.]+) Hz", report)}
        self.assertEqual(decoded, {(w, h, r) for w, h in [(1920, 1080), (2560, 1440)]
                                  for r in (60, 75, 90)})
        self.assertEqual(backend.digest(BUNDLE / "edid/dp-dummy-multimode.bin"), SHA256)


class TransactionTests(unittest.TestCase):
    kernel = "6.14.0-test.x86_64"

    def setUp(self):
        self.scratch = tempfile.TemporaryDirectory(prefix="dp-dummy-edid-test-")
        self.addCleanup(self.scratch.cleanup)
        self.root = Path(self.scratch.name)
        self.patch_root = patch.object(backend, "ROOT", self.root)
        self.patch_root.start()
        self.addCleanup(self.patch_root.stop)
        self.patch_kernel = patch.object(backend.os, "uname", return_value=SimpleNamespace(release=self.kernel))
        self.patch_kernel.start()
        self.addCleanup(self.patch_kernel.stop)
        self.patch_sync = patch.object(backend.os, "sync")
        self.patch_sync.start()
        self.addCleanup(self.patch_sync.stop)
        self.failure = None
        self.failed_once = False
        self.commands = []
        self.patch_run = patch.object(backend, "run", side_effect=self.fake_run)
        self.patch_run.start()
        self.addCleanup(self.patch_run.stop)
        for name in ("/etc/default", "/etc/kernel", "/etc/dracut.conf.d", "/boot/loader/entries",
                     "/usr/lib/firmware", "/var/tmp", "/proc", "/sys/class/drm/card1-DP-1",
                     "/sys/class/drm/card1/device", "/sys/bus/pci/drivers/amdgpu"):
            backend.path(name).mkdir(parents=True, exist_ok=True)
        backend.path("/sys/class/drm/card1/device/driver").symlink_to(backend.path("/sys/bus/pci/drivers/amdgpu"))
        backend.path("/sys/class/drm/card1-DP-1/status").write_text("connected\n")
        backend.path("/sys/class/drm/card1-DP-1/edid").write_bytes(b"")
        self.entry = backend.path("/boot/loader/entries/example-" + self.kernel + ".conf")
        self.entry.write_text("title Fedora test\nversion " + self.kernel + "\nlinux /vmlinuz-" + self.kernel +
                              "\ninitrd /initramfs-" + self.kernel + ".img $tuned_initrd\n"
                              "options root=UUID=EXAMPLE ro quiet $tuned_params\ngrub_class fedora\n")
        self.older = backend.path("/boot/loader/entries/example-older.conf")
        self.older.write_text("title older recovery\nlinux /vmlinuz-older\ninitrd /initramfs-older.img\noptions ro\n")
        self.original_image = backend.path("/boot/initramfs-" + self.kernel + ".img")
        self.original_image.write_bytes(b"ORIGINAL_INITRAMFS")
        backend.path("/etc/kernel/cmdline").write_text("root=UUID=EXAMPLE ro quiet\n")
        backend.path("/etc/default/grub").write_text('GRUB_DEFAULT=saved\nGRUB_ENABLE_BLSCFG=true\nGRUB_CMDLINE_LINUX="quiet"\n')
        self.before = {str(p): p.read_bytes() for p in
                       (self.entry, self.older, self.original_image, backend.path("/etc/kernel/cmdline"), backend.path("/etc/default/grub"))}

    def fake_run(self, command, env=None, timeout=30):
        self.commands.append(command)
        if command[0] == "grubby":
            return ("/boot/vmlinuz-" + self.kernel + "\n").encode()
        if command[0] == "grub2-editenv":
            return ("saved_entry=" + self.entry.stem + "\nboot_success=1\n").encode()
        if command[0] == "findmnt":
            return b"rw,relatime,seclabel\n"
        if command[0] == "restorecon":
            if self.failure == "commit" and command[1] == str(backend.path("/etc/kernel/cmdline")) and not self.failed_once:
                self.failed_once = True
                raise RuntimeError("simulated failure after replacing a configuration")
            return b""
        if command[0] == "dracut":
            self.assertEqual(self.original_image.read_bytes(), b"ORIGINAL_INITRAMFS")
            self.assertEqual(self.entry.read_bytes(), self.before[str(self.entry)])
            if self.failure == "build":
                raise RuntimeError("simulated build failure")
            Path(command[-2]).write_bytes(b"TEST_INITRAMFS_WITH_EDID" + generate())
            return b""
        if command[0] == "lsinitrd":
            return b"wrong EDID" if self.failure == "embedded" else generate()
        self.fail("Unexpected external command: " + repr(command))

    def assert_restored(self):
        for name, value in self.before.items():
            self.assertEqual(Path(name).read_bytes(), value)
        self.assertFalse(backend.path(backend.FIRMWARE).exists())
        self.assertFalse(backend.path(backend.DRACUT).exists())
        self.assertFalse(backend.path(backend.STATE).exists())
        self.assertFalse(list(backend.path("/boot").glob("*-dp-dummy.img")))
        self.assertFalse(list(self.root.rglob("*.dp-dummy-new")))

    def test_plan_does_not_write(self):
        plan = backend.make_plan("DP-1")
        self.assertFalse(plan["already_configured"])
        self.assertFalse(backend.path(backend.STATE).exists())
        self.assertEqual(self.entry.read_bytes(), self.before[str(self.entry)])
        self.assertNotIn("dracut", [c[0] for c in self.commands])

    def test_install_idempotency_and_uninstall(self):
        backend.install("DP-1")
        installed = self.entry.read_bytes()
        self.assertIn(b"$tuned_params", installed)
        self.assertIn(b"$tuned_initrd", installed)
        for arg in backend.parameters("DP-1"):
            self.assertIn(arg.encode(), installed)
        self.assertEqual(self.older.read_bytes(), self.before[str(self.older)])
        backend.install("DP-1")
        self.assertEqual(len([c for c in self.commands if c[0] == "dracut"]), 1)
        backend.uninstall()
        self.assert_restored()

    def test_build_failure_restores(self):
        self.failure = "build"
        with self.assertRaisesRegex(RuntimeError, "simulated build failure"):
            backend.install("DP-1")
        self.assert_restored()

    def test_wrong_embedded_edid_restores(self):
        self.failure = "embedded"
        with self.assertRaisesRegex(RuntimeError, "exact bundled EDID"):
            backend.install("DP-1")
        self.assert_restored()

    def test_mid_commit_failure_restores(self):
        self.failure = "commit"
        with self.assertRaisesRegex(RuntimeError, "simulated failure"):
            backend.install("DP-1")
        self.assert_restored()

    def test_uninstall_preserves_independent_edits(self):
        backend.install("DP-1")
        installed_entry = self.entry.read_bytes()
        backend.path("/etc/default/grub").write_bytes(b"INDEPENDENT EDIT")
        with self.assertRaisesRegex(RuntimeError, "changed independently"):
            backend.uninstall()
        self.assertEqual(self.entry.read_bytes(), installed_entry)
        self.assertEqual(backend.path("/etc/default/grub").read_bytes(), b"INDEPENDENT EDIT")
        self.assertTrue(backend.path(backend.STATE).exists())

    def test_conflicting_override_refused(self):
        backend.path("/etc/kernel/cmdline").write_text("root=UUID=EXAMPLE drm.edid_firmware=DP-1:other.bin\n")
        with self.assertRaisesRegex(RuntimeError, "manual review"):
            backend.make_plan("DP-1")
        self.assertFalse(backend.path(backend.STATE).exists())

    def test_real_monitor_edid_refused(self):
        backend.path("/sys/class/drm/card1-DP-1/edid").write_bytes(b"A DIFFERENT REAL MONITOR EDID")
        with self.assertRaisesRegex(RuntimeError, "different readable EDID"):
            backend.make_plan("DP-1")

    def test_symlink_configuration_refused(self):
        filename = backend.path("/etc/kernel/cmdline")
        filename.unlink()
        filename.symlink_to(backend.path("/proc/cmdline"))
        backend.path("/proc/cmdline").write_text("untouched\n")
        with self.assertRaisesRegex(RuntimeError, "regular file"):
            backend.make_plan("DP-1")
        self.assertEqual(backend.path("/proc/cmdline").read_text(), "untouched\n")

    def test_matching_unmanaged_install_is_not_adopted(self):
        backend.install("DP-1")
        backend.path(backend.STATE).unlink()  # Model an independently configured matching setup.
        self.assertTrue(backend.make_plan("DP-1")["already_configured"])
        old_entry = self.entry.read_bytes()
        backend.install("DP-1")
        self.assertEqual(self.entry.read_bytes(), old_entry)
        self.assertFalse(backend.path(backend.STATE).exists())


if __name__ == "__main__":
    unittest.main()
