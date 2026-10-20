# Physical DP dummy EDID fix for Fedora / AMD

Supply a firmware EDID for a physical dummy plug that returns no readable EDID.
Keep **1920 × 1080 at 60 Hz** as the preferred/default mode while offering:

| Resolution | Advertised refresh rates |
| --- | --- |
| 1920 × 1080 | 60, 75, 90 Hz |
| 2560 × 1440 | approximately 60, 75, 90 Hz |

The 1440p timings decode as 59.999898, 75.001016, and 89.999294 Hz because EDID
detailed timings encode pixel clocks in 10 kHz steps. A VGA fallback is also
present. No HDR, audio, or variable refresh capability is advertised.

## What fixed the original problem

Linux initially read **zero EDID bytes** from the dummy and exposed only the
640 × 480 fallback. The working solution supplied a synthetic, valid EDID through
Linux's firmware interface for the physical connector. It did not program the
dummy plug itself.

For `DP-1`, the persistent kernel arguments are:

```text
drm.edid_firmware=DP-1:edid/dp-dummy-multimode.bin
video=DP-1:1920x1080@60e
```

The first selects the EDID file. The second forces the connector enabled at
1080p60. Including the file in the initramfs makes it available during early AMD
GPU initialization. GNOME RDP can then mirror the physical primary display.

The installer uses boot configuration rather than live AMD debugfs hotplug
resets. Earlier live-reset experiments on the original system disrupted remote
desktop access; those experiments are deliberately excluded from this bundle.

## Scope and validation

- Fedora-style **GRUB/BLS**, `/etc/kernel/cmdline`, dracut, and the **amdgpu** driver.
- A connected physical dummy plug, with no readable EDID or this exact profile.
- The running kernel must also be the default boot kernel. The installer requires
  a separate existing recovery entry and refuses conflicting EDID overrides.
- Multiple GPUs can be disambiguated with `--card cardN`.
- The original boot method and identical EDID were verified on Fedora 44,
  Radeon PRO W6400, kernel `7.2.11-200.fc44.x86_64`, and GNOME/Mutter 50.6.
  After reboot, DP-1 was the sole GNOME display at 1080p60 and all six requested
  modes were available.
- This packaged installer is exercised against isolated filesystem fixtures;
  the working host was not reinstalled to test packaging. Availability of 75/90 Hz
  modes does not establish physical scanout or RDP delivery at those rates.
- Other distributions, boot loaders, GPUs, and hardware combinations are untested.

## Files

```text
fix-display.sh                 Bash entry point
lib/edid_fix.py                 Boot transaction, recovery, status and GNOME RDP helper
generate_edid.py                Reproducible EDID generator, Python standard library only
edid/dp-dummy-multimode.bin      Exact working 256-byte EDID
tests/test_edid_fix.py          Isolated installation and rollback tests
```

There are no machine UUIDs, usernames, personal boot entries, or captured logs
in the bundle. Local recovery snapshots are created outside the repository.

## Requirements

Use Bash and Python 3. The installation commands also require `dracut`, `lsinitrd`,
`grubby`, `grub2-editenv`, `findmnt`, and `restorecon`, normally supplied by the
corresponding Fedora system packages. `edid-decode` is optional for an additional
standards check. GNOME inspection and `rdp-physical` additionally use PyGObject
and the GNOME desktop's session bus; boot installation itself does not need them.

If administering through RDP, keep a working SSH session with sudo access open
before changing boot configuration. Installation leaves the current display
layout and live DRM state unchanged and never reboots automatically.

## Install

From the cloned/downloaded directory:

```bash
bash ./fix-display.sh check-edid
bash ./fix-display.sh status --connector DP-1
sudo bash ./fix-display.sh plan --connector DP-1
sudo bash ./fix-display.sh install --connector DP-1
```

Read the plan before installation. If several GPUs have a `DP-1`, add the same
`--card cardN` option to these commands. Card numbering can change across boots;
the persistent kernel override uses the connector name.

The installer:

1. Saves the original affected file contents and metadata in a private recovery
   snapshot, and installs/verifies the firmware bytes.
2. Rebuilds the current initramfs into a **separate** `*-dp-dummy.img` file and
   checks that it contains the exact EDID.
3. Preserves the original initramfs and all other existing boot entries.
4. Adds the EDID/connector arguments to the current BLS entry,
   `/etc/kernel/cmdline`, and `/etc/default/grub`.
5. Adds a dracut include for future kernel images. The current BLS entry is
   updated last, after the new image passes validation.
6. Restores its changes if installation fails. If independent edits prevent
   safe rollback, it retains the recovery snapshot and reports the problem.

Once installation succeeds, save your work and reboot when ready. Then check:

```bash
bash ./fix-display.sh verify --connector DP-1
```

Verification checks the running kernel's arguments and adopted physical EDID.
When the GNOME session is reachable, it also prints current display modes.
Confirm 1080p60 in GNOME Settings if that is the desired active mode.

For GNOME's built-in **desktop sharing** service, run this as your normal desktop
user, without sudo, after the physical connector is part of the active primary
desktop:

```bash
bash ./fix-display.sh rdp-physical --connector DP-1
```

It sets `org.gnome.desktop.remote-desktop.rdp screen-share-mode` to
`mirror-primary`. Close and reconnect the RDP client without signing out.
Check that the physical desktop remains active while RDP is disconnected.
This setting is separate from GNOME Remote Login and xrdp. The installer does
not silently change your RDP preference; to restore a previous `extend` preference:

```bash
gsettings set org.gnome.desktop.remote-desktop.rdp screen-share-mode extend
```

## Existing installations and undo

An already matching boot configuration is left unchanged. If it was created by
another installer, this bundle does not claim ownership or manufacture an undo
snapshot for it.

To undo an installation made by **this bundle**:

```bash
sudo bash ./fix-display.sh uninstall
```

Uninstall restores the recorded original files and removes only the bundle's
additional initramfs. It does not change the running display; reboot when ready.
It refuses to overwrite independently modified files or remove a changed image.

Kernel upgrades may replace or remove files recorded in an older recovery
snapshot. If automatic undo refuses, inspect the private snapshot and the
current boot entries before making manual changes. Booting an unchanged older
kernel or restoring the original current entry through SSH are recovery routes;
their availability depends on those files still existing.

Local installation outputs:

- `/usr/lib/firmware/edid/dp-dummy-multimode.bin`
- `/etc/dracut.conf.d/95-dp-dummy-edid.conf`
- The current BLS entry, `/etc/kernel/cmdline`, and `/etc/default/grub`
- `/boot/<original-initramfs-name>-dp-dummy.img`
- `/var/lib/dp-dummy-edid-fix/state.json` — private recovery state; do not commit it
- Temporary `/var/tmp/dp-dummy-edid-fix-*` and adjacent `.dp-dummy-new` files,
  removed after use

## Reproduce and test

The committed binary has SHA-256:

```text
a370a75525fb634c05b3f75a2a29777da7a40ceae35d8443e830bbf65a78d448
```

```bash
python3 -B generate_edid.py --check edid/dp-dummy-multimode.bin
# Create another file for comparison; existing files are never overwritten:
python3 -B generate_edid.py --output /tmp/dp-dummy-rebuilt.bin
bash -n fix-display.sh
python3 -B -m unittest discover -s tests -v
```

The tests create and clean `/tmp/dp-dummy-edid-test-*` fixtures. External boot
tools are mocked there; tests do not install firmware, edit the real `/boot`,
change RDP settings, or reboot the host. An optional `edid-decode` test validates
the actual binary's conformity and modes.

## References

- [Linux EDID firmware overrides](https://docs.kernel.org/admin-guide/kernel-parameters.html)
- [Linux connector video options, including force-enable `e`](https://docs.kernel.org/fb/modedb.html)
- [dracut configuration, including `install_items`](https://github.com/dracut-ng/dracut-ng/blob/main/man/dracut.conf.5.adoc)
- [Fedora BLS boot entries](https://fedoraproject.org/wiki/Changes/BootLoaderSpecByDefault)
