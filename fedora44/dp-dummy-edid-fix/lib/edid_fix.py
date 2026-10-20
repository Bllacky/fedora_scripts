#!/usr/bin/env python3
"""Fedora BLS installer behind fix-display.sh; no live DRM resets or reboot."""

import argparse
import base64
from contextlib import contextmanager
import fcntl
import hashlib
import json
import os
from pathlib import Path
import re
import signal
import stat
import subprocess
import sys
import tempfile

BUNDLE = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(BUNDLE))
from generate_edid import generate, TIMINGS  # noqa: E402

# ROOT is an internal filesystem adapter used by isolated tests, not a CLI option.
ROOT = Path("/")
FIRMWARE = "/usr/lib/firmware/edid/dp-dummy-multimode.bin"
DRACUT = "/etc/dracut.conf.d/95-dp-dummy-edid.conf"
STATE = "/var/lib/dp-dummy-edid-fix/state.json"
DRACUT_CONTENT = ('# Include the physical dummy EDID during early GPU initialization.\n'
                  'install_items+=" ' + FIRMWARE + ' "\n').encode()
EXPECTED_MODES = [(1920, 1080, 60.0)] + [
    (w, h, clock * 10000 / ((w + 80) * (h + vfront + 14)))
    for w, h, vfront, clock in TIMINGS]


def path(name):
    return ROOT / str(name).lstrip("/")


def run(command, env=None, timeout=30):
    process = subprocess.Popen(command, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                               env=env, start_new_session=True)
    try:
        out, err = process.communicate(timeout=timeout)
    except BaseException:
        try:
            os.killpg(process.pid, signal.SIGTERM)
            process.communicate(timeout=5)
        except (ProcessLookupError, subprocess.TimeoutExpired):
            try:
                os.killpg(process.pid, signal.SIGKILL)
            except ProcessLookupError:
                pass
            process.communicate()
        raise
    if process.returncode:
        raise RuntimeError(repr(command) + " failed:\n" +
                           out.decode(errors="replace") + err.decode(errors="replace"))
    return out


def digest(filename):
    result = hashlib.sha256()
    with filename.open("rb") as stream:
        for block in iter(lambda: stream.read(1024 * 1024), b""):
            result.update(block)
    return result.hexdigest()


def read_regular(filename):
    info = filename.lstat()
    if not stat.S_ISREG(info.st_mode) or info.st_uid != os.geteuid():
        raise RuntimeError("Expected an owned regular file: " + str(filename))
    return filename.read_bytes()


def replace_file(filename, data, metadata=None, mode=0o644):
    stage = filename.with_name(filename.name + ".dp-dummy-new")
    if filename.is_symlink() or stage.exists() or stage.is_symlink():
        raise RuntimeError("Symlink or occupied staging path: " + str(filename))
    selected_mode = metadata["mode"] if metadata else mode
    fd = os.open(stage, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, selected_mode)
    try:
        with os.fdopen(fd, "wb") as stream:
            stream.write(data)
            os.fchmod(stream.fileno(), selected_mode)
            if metadata:
                os.fchown(stream.fileno(), metadata["uid"], metadata["gid"])
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(stage, filename)
        run(["restorecon", str(filename)])
        directory_fd = os.open(filename.parent, os.O_RDONLY | os.O_DIRECTORY)
        try:
            os.fsync(directory_fd)
        finally:
            os.close(directory_fd)
    finally:
        if stage.exists():
            stage.unlink()


def profile():
    data = (BUNDLE / "edid/dp-dummy-multimode.bin").read_bytes()
    if data != generate():
        raise RuntimeError("Bundled EDID differs from its reproducible generator.")
    return data


def parameters(connector):
    return ("drm.edid_firmware=" + connector + ":edid/dp-dummy-multimode.bin",
            "video=" + connector + ":1920x1080@60e")


def discover(connector, card=None):
    if not re.fullmatch(r"[A-Za-z]+(?:-[A-Za-z]+)?-[0-9]+", connector):
        raise RuntimeError("Invalid physical connector name.")
    if card and not re.fullmatch(r"card[0-9]+", card):
        raise RuntimeError("Invalid DRM card name; use e.g. card1.")
    candidates = [p for p in path("/sys/class/drm").glob((card or "card[0-9]*") + "-" + connector)
                  if re.fullmatch(r"card[0-9]+-" + re.escape(connector), p.name)]
    if len(candidates) != 1:
        raise RuntimeError("Expected exactly one matching connector; use --card cardN to disambiguate.")
    physical = candidates[0]
    selected_card = physical.name[:-len(connector) - 1]
    driver = (path("/sys/class/drm") / selected_card / "device/driver").resolve().name
    if driver != "amdgpu":
        raise RuntimeError("This bundle targets amdgpu; detected driver: " + driver)
    return physical, selected_card


def fields(text, key):
    return [line[len(key) + 1:].strip() for line in text.splitlines() if line.startswith(key + " ")]


def add_parameters(value, connector):
    if any(word.startswith(("drm.edid_firmware=", "drm_kms_helper.edid_firmware=", "video=" + connector + ":"))
           for word in value.split()):
        raise RuntimeError("Existing EDID or connector override requires manual review; no override replaced.")
    return value.rstrip() + " " + " ".join(parameters(connector))


def transform(entry, cmdline, grub, old_image, new_image, connector):
    if len(fields(entry, "options")) != 1 or len(fields(entry, "initrd")) != 1:
        raise RuntimeError("Expected one options line and one initrd line in the BLS entry.")
    lines = entry.splitlines(keepends=True)
    for index, line in enumerate(lines):
        if line.startswith("options "):
            lines[index] = "options " + add_parameters(line[8:].strip(), connector) + "\n"
        elif line.startswith("initrd "):
            if line.split()[1] != "/" + old_image.name:
                raise RuntimeError("Unexpected initramfs path; refusing to rewrite it.")
            lines[index] = line.replace("/" + old_image.name, "/" + new_image.name, 1)
    if "GRUB_ENABLE_BLSCFG=true" not in grub.splitlines():
        raise RuntimeError("GRUB BLS is not enabled; other boot loaders are unsupported.")
    matches = list(re.finditer(r'^GRUB_CMDLINE_LINUX="([^"\n]*)"$', grub, re.MULTILINE))
    if len(matches) != 1:
        raise RuntimeError('Expected one double-quoted GRUB_CMDLINE_LINUX assignment.')
    match = matches[0]
    grub = grub[:match.start(1)] + add_parameters(match.group(1), connector) + grub[match.end(1):]
    return "".join(lines).encode(), (add_parameters(cmdline.strip(), connector) + "\n").encode(), grub.encode()


def make_plan(connector, card=None):
    data = profile()
    physical, selected_card = discover(connector, card)
    if (physical / "status").read_text().strip() != "connected":
        raise RuntimeError("Connect the physical dummy plug first.")
    if (physical / "edid").read_bytes() not in (b"", data):
        raise RuntimeError("The connector supplies a different readable EDID. Confirm the hardware before overriding it.")
    if (path(FIRMWARE).exists() or path(FIRMWARE).is_symlink()) and read_regular(path(FIRMWARE)) != data:
        raise RuntimeError("Firmware destination contains different data.")
    kernel = os.uname().release
    if not re.fullmatch(r"[A-Za-z0-9_.+-]+", kernel):
        raise RuntimeError("Unexpected kernel version format.")
    if run(["grubby", "--default-kernel"]).decode().strip() != "/boot/vmlinuz-" + kernel:
        raise RuntimeError("Boot the default kernel before installing; default and running kernels differ.")
    entries = {str(p): read_regular(p) for p in sorted(path("/boot/loader/entries").glob("*.conf"))}
    matching = [name for name, value in entries.items()
                if fields(value.decode(), "linux") == ["/vmlinuz-" + kernel]]
    if len(matching) != 1:
        raise RuntimeError("Cannot uniquely identify the running kernel's BLS entry.")
    entry = Path(matching[0])
    original_entry = entries[str(entry)].decode()
    cmdline = read_regular(path("/etc/kernel/cmdline")).decode()
    grub = read_regular(path("/etc/default/grub")).decode()
    args = parameters(connector)
    grub_args = re.findall(r'^GRUB_CMDLINE_LINUX="([^"\n]*)"$', grub, re.MULTILINE)
    compatible = (path(FIRMWARE).is_file() and path(FIRMWARE).read_bytes() == data and
                  path(DRACUT).is_file() and DRACUT_CONTENT.decode().splitlines()[-1]
                  in path(DRACUT).read_text().splitlines() and
                  len(fields(original_entry, "options")) == 1 and len(grub_args) == 1 and
                  all(arg in fields(original_entry, "options")[0].split() and
                      arg in cmdline.split() and arg in grub_args[0].split() for arg in args))
    if compatible:
        return {"already_configured": True, "kernel": kernel, "connector": connector, "card": selected_card}
    initrds = fields(original_entry, "initrd")
    if len(initrds) != 1 or not initrds[0].split() or not initrds[0].split()[0].startswith("/"):
        raise RuntimeError("Unexpected BLS initrd format.")
    image_name = initrds[0].split()[0]
    if "/" in image_name[1:] or not image_name.endswith(".img"):
        raise RuntimeError("Expected a conventional /boot initramfs image.")
    old_image = path("/boot") / image_name[1:]
    read_regular(old_image)
    new_image = old_image.with_name(old_image.stem + "-dp-dummy.img")
    if new_image.exists() or new_image.is_symlink() or path(DRACUT).exists() or path(DRACUT).is_symlink():
        raise RuntimeError("An intended output is occupied; no existing configuration will be replaced.")
    others = {name: value for name, value in entries.items() if name != str(entry)}
    if not others:
        raise RuntimeError("Keep a separate recovery boot entry before installing.")
    environment = run(["grub2-editenv", "/boot/grub2/grubenv", "list"])
    if b"next_entry=" in environment or ("saved_entry=" + entry.stem).encode() not in environment.splitlines():
        raise RuntimeError("Unexpected saved or pending one-shot GRUB boot entry.")
    if "rw" not in run(["findmnt", "-n", "-o", "OPTIONS", "/boot"]).decode().strip().split(","):
        raise RuntimeError("/boot is not writable.")
    new_entry, new_cmdline, new_grub = transform(original_entry, cmdline, grub, old_image, new_image, connector)
    changes = {str(path(FIRMWARE)): data, str(path(DRACUT)): DRACUT_CONTENT,
               str(path("/etc/default/grub")): new_grub,
               str(path("/etc/kernel/cmdline")): new_cmdline, str(entry): new_entry}
    records = {}
    for name, after in changes.items():
        filename = Path(name)
        stage = filename.with_name(filename.name + ".dp-dummy-new")
        if filename.is_symlink() or stage.exists() or stage.is_symlink():
            raise RuntimeError("Symlink or occupied staging path: " + name)
        before = read_regular(filename) if filename.exists() else None
        if name == str(path(FIRMWARE)) and before not in (None, data):
            raise RuntimeError("Firmware changed during inspection; no changes made.")
        info = filename.stat() if before is not None else None
        records[name] = {"before": base64.b64encode(before).decode() if before is not None else None,
                         "after": base64.b64encode(after).decode(),
                         "metadata": {"mode": stat.S_IMODE(info.st_mode), "uid": info.st_uid,
                                      "gid": info.st_gid} if info else None}
    return {"format": 1, "phase": "preparing", "already_configured": False,
            "kernel": kernel, "connector": connector, "card": selected_card, "entry": str(entry),
            "old_image": str(old_image), "old_image_sha256": digest(old_image),
            "new_image": str(new_image), "new_image_sha256": None,
            "grub_environment": base64.b64encode(environment).decode(), "files": records,
            "other_entries": {name: base64.b64encode(value).decode() for name, value in others.items()}}


def save_state(state):
    replace_file(path(STATE), (json.dumps(state, indent=2) + "\n").encode(), mode=0o600)


def load_state():
    filename = path(STATE)
    data = read_regular(filename)
    if stat.S_IMODE(filename.stat().st_mode) & 0o077:
        raise RuntimeError("Recovery state must be private to its owner.")
    state = json.loads(data)
    entry = Path(state["entry"])
    expected = {str(path(n)) for n in (FIRMWARE, DRACUT, "/etc/default/grub", "/etc/kernel/cmdline")}
    expected.add(str(entry))
    if (state.get("format") != 1 or set(state["files"]) != expected or
            entry.parent != path("/boot/loader/entries") or entry.suffix != ".conf" or
            Path(state["old_image"]).parent != path("/boot") or
            Path(state["new_image"]).parent != path("/boot") or
            not Path(state["new_image"]).name.endswith("-dp-dummy.img")):
        raise RuntimeError("Recovery state's identity or target set is invalid.")
    return state


def restore(state):
    # Check every target before restoring any: independent edits are never overwritten.
    for name, record in state["files"].items():
        filename = Path(name)
        before = base64.b64decode(record["before"]) if record["before"] is not None else None
        after = base64.b64decode(record["after"])
        current = read_regular(filename) if filename.exists() or filename.is_symlink() else None
        if current not in (before, after):
            raise RuntimeError("Configuration changed independently; refusing overwrite: " + name)
    for name in [state["entry"]] + [n for n in state["files"] if n != state["entry"]]:
        filename = Path(name)
        record = state["files"][name]
        before = base64.b64decode(record["before"]) if record["before"] is not None else None
        if before is None:
            filename.unlink(missing_ok=True)
        elif filename.read_bytes() != before:
            replace_file(filename, before, record["metadata"])


@contextmanager
def install_lock():
    directory = path(STATE).parent
    directory.mkdir(mode=0o700, parents=True, exist_ok=True)
    info = directory.lstat()
    if not stat.S_ISDIR(info.st_mode) or info.st_uid != os.geteuid() or info.st_mode & 0o022:
        raise RuntimeError("Recovery directory is not a protected owned directory.")
    fd = os.open(directory, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
    try:
        fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        yield
    finally:
        os.close(fd)


def verify_disk(state):
    for name, record in state["files"].items():
        if read_regular(Path(name)) != base64.b64decode(record["after"]):
            raise RuntimeError("Installed configuration changed: " + name)
    if digest(Path(state["old_image"])) != state["old_image_sha256"]:
        raise RuntimeError("Preserved original initramfs changed or was removed.")
    if not state["new_image_sha256"] or digest(Path(state["new_image"])) != state["new_image_sha256"]:
        raise RuntimeError("Additional initramfs is missing or changed.")
    for name, before in state["other_entries"].items():
        if read_regular(Path(name)) != base64.b64decode(before):
            raise RuntimeError("An original recovery boot entry changed: " + name)


def install(connector, card=None):
    with install_lock():
        if path(STATE).exists():
            verify_disk(load_state())
            print("Already installed by this bundle; configuration verified, no changes made.")
            return
        state = make_plan(connector, card)
        if state["already_configured"]:
            print("Matching existing EDID boot configuration detected; no boot or firmware changes made.")
            print("It is not managed by this bundle, so uninstall will not remove it.")
            return
        save_state(state)  # Required recovery snapshot, before the first target edit.
        new_image = Path(state["new_image"])
        try:
            firmware_record = state["files"][str(path(FIRMWARE))]
            if firmware_record["before"] != firmware_record["after"]:
                expected_before = (base64.b64decode(firmware_record["before"])
                                   if firmware_record["before"] is not None else None)
                current = read_regular(path(FIRMWARE)) if path(FIRMWARE).exists() or path(FIRMWARE).is_symlink() else None
                if current != expected_before:
                    raise RuntimeError("Firmware changed after inspection; refusing overwrite.")
                path(FIRMWARE).parent.mkdir(parents=True, exist_ok=True)
                replace_file(path(FIRMWARE), profile(), firmware_record["metadata"])
            print("Building a separate initramfs; original boot entry/image remain intact.", flush=True)
            with tempfile.TemporaryDirectory(prefix="dp-dummy-edid-fix-", dir=path("/var/tmp")) as work:
                env = os.environ.copy()
                env["TMPDIR"] = work
                # Engineering timeout; not a promise about build duration.
                run(["dracut", "--rebuild", state["old_image"], "--tmpdir", work,
                     "--logfile", work + "/dracut.log", "--quiet", "--include",
                     str(path(FIRMWARE)), FIRMWARE, str(new_image), state["kernel"]], env, timeout=180)
                embedded = run(["lsinitrd", "-f", FIRMWARE.lstrip("/"), str(new_image)], env)
                if embedded != profile():
                    raise RuntimeError("New initramfs does not contain the exact bundled EDID.")
            if digest(Path(state["old_image"])) != state["old_image_sha256"]:
                raise RuntimeError("Original initramfs changed during the build.")
            state["new_image_sha256"] = digest(new_image)
            save_state(state)
            run(["restorecon", str(new_image)])
            # Keep /etc/kernel/cmdline newer than /etc/default/grub for Fedora's kernel-install sync.
            for name, record in state["files"].items():
                if name == str(path(FIRMWARE)):
                    continue
                filename = Path(name)
                before = base64.b64decode(record["before"]) if record["before"] is not None else None
                current = read_regular(filename) if filename.exists() or filename.is_symlink() else None
                if current != before:
                    raise RuntimeError("Configuration changed during the build: " + name)
                replace_file(filename, base64.b64decode(record["after"]), record["metadata"])
            if run(["grub2-editenv", "/boot/grub2/grubenv", "list"]) != base64.b64decode(state["grub_environment"]):
                raise RuntimeError("GRUB environment changed during installation.")
            verify_disk(state)
            state["phase"] = "installed"
            save_state(state)
            os.sync()
        except BaseException:
            try:
                restore(state)
                new_image.unlink(missing_ok=True)
                path(STATE).unlink()
                print("Original configuration restored; no reboot was performed.", file=sys.stderr)
            except BaseException as error:
                print("Automatic recovery could not finish: " + str(error) +
                      ". Do not reboot; inspect " + str(path(STATE)), file=sys.stderr)
            raise
        print("Installed. Embedded EDID, original initramfs and recovery entries verified.")
        print("Reboot when ready, then run ./fix-display.sh verify. No reboot was performed.")


def uninstall():
    with install_lock():
        state = load_state()
        if digest(Path(state["old_image"])) != state["old_image_sha256"]:
            raise RuntimeError("The original initramfs changed; automatic restoration is not safe.")
        new_image = Path(state["new_image"])
        if new_image.exists() and state["new_image_sha256"] and digest(new_image) != state["new_image_sha256"]:
            raise RuntimeError("Additional initramfs changed; refusing to remove it.")
        restore(state)
        if new_image.exists() and not state["new_image_sha256"]:
            print("Restored configuration; retained an unverified partial image: " + str(new_image))
        else:
            new_image.unlink(missing_ok=True)
        path(STATE).unlink()
        os.sync()
        print("Restored this bundle's original configuration. Running display unchanged; reboot when ready.")


def desktop_state():
    from gi.repository import Gio
    bus = Gio.bus_get_sync(Gio.BusType.SESSION, None)
    state = bus.call_sync("org.gnome.Mutter.DisplayConfig", "/org/gnome/Mutter/DisplayConfig",
                          "org.gnome.Mutter.DisplayConfig", "GetCurrentState", None, None,
                          Gio.DBusCallFlags.NONE, 5000, None).unpack()
    return {"monitors": [{"connector": spec[0],
                           "requested_modes": [[m[1], m[2], m[3]] for m in modes
                                               if any((m[1], m[2]) == (w, h) and abs(m[3] - rate) < 1e-4
                                                      for w, h, rate in EXPECTED_MODES)],
                           "current_modes": [[m[1], m[2], m[3]] for m in modes if m[-1].get("is-current")]}
                          for spec, modes, properties in state[1]], "logical_monitors": state[2]}


def maybe_desktop_state():
    try:
        if os.geteuid() == 0 and os.environ.get("SUDO_UID", "0") != "0":
            import pwd
            uid = int(os.environ["SUDO_UID"])
            return json.loads(run(["runuser", "-u", pwd.getpwuid(uid).pw_name, "--", "env",
                                   "DBUS_SESSION_BUS_ADDRESS=unix:path=/run/user/" + str(uid) + "/bus",
                                   "XDG_RUNTIME_DIR=/run/user/" + str(uid),
                                   "PYTHONDONTWRITEBYTECODE=1", "python3", str(Path(__file__).resolve()),
                                   "desktop-state"]))
        os.environ.setdefault("DBUS_SESSION_BUS_ADDRESS", "unix:path=/run/user/" + str(os.geteuid()) + "/bus")
        return desktop_state()
    except Exception as error:
        return {"unavailable": str(error)}


def status(connector, card=None, verify=False):
    physical, selected_card = discover(connector, card)
    adopted = (physical / "edid").read_bytes()
    arguments = path("/proc/cmdline").read_text().split()
    state = {"kernel": os.uname().release, "card": selected_card, "connector": connector,
             "connector_status": (physical / "status").read_text().strip(),
             "edid_bytes": len(adopted), "edid_matches": adopted == profile(),
             "edid_sha256": hashlib.sha256(adopted).hexdigest(),
             "boot_parameters_active": all(arg in arguments for arg in parameters(connector)),
             "gnome": maybe_desktop_state()}
    print(json.dumps(state, indent=2))
    if verify and not (state["edid_matches"] and state["boot_parameters_active"] and
                       state["connector_status"] == "connected"):
        raise RuntimeError("Boot/display EDID verification failed. Check the selected connector and reboot status.")
    if verify:
        gnome = state["gnome"]
        if "monitors" in gnome:
            monitor = next((m for m in gnome["monitors"] if m["connector"] == connector), None)
            if not monitor or not all(any((mw, mh) == (w, h) and abs(mr - rate) < 1e-4
                                          for mw, mh, mr in monitor["requested_modes"])
                                      for w, h, rate in EXPECTED_MODES):
                raise RuntimeError("Kernel EDID matches, but GNOME does not expose all six requested modes.")
        print("Boot parameters and physical EDID adoption verified; high-rate scanout is a separate test.")


def rdp_physical(connector):
    if os.geteuid() == 0:
        raise RuntimeError("Run rdp-physical as the GNOME desktop user, without sudo.")
    os.environ.setdefault("DBUS_SESSION_BUS_ADDRESS", "unix:path=/run/user/" + str(os.geteuid()) + "/bus")
    state = desktop_state()
    if not any(logical[4] and any(monitor[0] == connector for monitor in logical[5])
               for logical in state["logical_monitors"]):
        raise RuntimeError("The physical connector must be active in the primary desktop first.")
    run(["gsettings", "set", "org.gnome.desktop.remote-desktop.rdp", "screen-share-mode", "mirror-primary"])
    print("RDP will mirror the physical primary display on the next connection. Reconnect without signing out.")


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("command", choices=("plan", "install", "status", "verify", "uninstall", "rdp-physical", "desktop-state"))
    parser.add_argument("--connector", default="DP-1")
    parser.add_argument("--card", help="Resolve ambiguity on systems with more than one GPU, e.g. card1")
    args = parser.parse_args()
    if args.command in ("plan", "install", "uninstall") and os.geteuid() != 0:
        parser.error("Run this command with sudo")
    if args.command == "plan":
        state = make_plan(args.connector, args.card)
        print(json.dumps({key: state[key] for key in ("already_configured", "kernel", "connector", "card")}, indent=2))
        if not state["already_configured"]:
            print("Write: " + str(path(STATE)))
            print("Build: " + state["new_image"])
            for name in state["files"]:
                print("Update: " + name)
            print("Preserve: " + state["old_image"] + "; all other existing BLS entries")
            print("Temporary files: /var/tmp/dp-dummy-edid-fix-* and adjacent .dp-dummy-new files")
        print("Read-only plan; no files changed.")
    elif args.command == "install":
        install(args.connector, args.card)
    elif args.command == "uninstall":
        uninstall()
    elif args.command == "rdp-physical":
        rdp_physical(args.connector)
    elif args.command == "desktop-state":
        print(json.dumps(desktop_state()))
    else:
        status(args.connector, args.card, args.command == "verify")


if __name__ == "__main__":
    try:
        main()
    except (Exception, KeyboardInterrupt) as error:
        print("ERROR: " + str(error), file=sys.stderr)
        sys.exit(1)
