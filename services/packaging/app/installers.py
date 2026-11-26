"""What an installer is, and the Intune install and uninstall commands for it.

    MSI          read straight from the file (its Property table and summary): product name, version,
                 publisher, product code, upgrade code, package code, per-machine or per-user. The commands
                 use the product code; detection is the MSI's product code.
    EXE          the version information Windows shows (product, publisher, version), and what made the
                 installer (Inno Setup, NSIS, WiX Burn, InstallShield, Advanced Installer, Squirrel, Google
                 Update), from tell-tale text in the file. Each has its own silent switches.
    PowerShell   run with 64-bit PowerShell (Intune's agent is 32-bit, so sysnative), execution policy
                 bypassed; an Uninstall*.ps1 alongside is the uninstall.
    CMD / BAT    run with cmd.exe /c.

analyse(folder, setup_file) → {kind, builder, name, version, publisher, install, uninstall, detection,
behaviour, notes, msi (for Detection.xml)}. Placeholders the person has to fill in are in <angle brackets>.
"""
import re
import struct
from pathlib import Path

import olefile

IME_LOGS = r"%ProgramData%\Microsoft\IntuneManagementExtension\Logs"
PS64 = r"%SystemRoot%\sysnative\WindowsPowerShell\v1.0\powershell.exe -ExecutionPolicy Bypass -NoProfile -NonInteractive -WindowStyle Hidden -File"
RUNNABLE = (".msi", ".exe", ".ps1", ".cmd", ".bat")


# ---------- MSI: an OLE compound file; its tables are streams with encoded names ----------

def _mime(c: int) -> str:
    return "0123456789ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghijklmnopqrstuvwxyz._"[c] if c < 64 else ""


def msi_stream_name(raw: str) -> tuple[str, bool]:
    """An MSI stream's real name, and whether it's a table (Windows Installer packs two name characters
    into each character from U+3800, and starts a table's name with U+4840)."""
    out, table = [], False
    for i, ch in enumerate(raw):
        c = ord(ch)
        if c == 0x4840 and i == 0:
            table = True
        elif 0x3800 <= c < 0x4800:
            c -= 0x3800
            out.append(_mime(c & 0x3F) + _mime(c >> 6))
        elif 0x4800 <= c < 0x4840:
            out.append(_mime(c - 0x4800))
        else:
            out.append(ch)
    return "".join(out), table


def msi_strings(pool: bytes, data: bytes) -> tuple[list[str], int]:
    """The string table (index 0 is empty) and how wide a string reference is in the tables (2 or 3 bytes)."""
    if len(pool) < 4:
        return [""], 2
    lo, hi = struct.unpack_from("<HH", pool, 0)
    width = 3 if hi & 0x8000 else 2
    codepage = lo | ((hi & 0x7FFF) << 16)
    enc = "utf-8" if codepage == 65001 else f"cp{codepage}" if codepage else "cp1252"
    try:
        "".encode(enc)
    except LookupError:
        enc = "cp1252"
    words = struct.unpack_from(f"<{(len(pool) - 4) // 2}H", pool, 4)
    out, pos, i = [""], 0, 0
    n = len(words) // 2
    while i < n:
        length, refs = words[i * 2], words[i * 2 + 1]
        if length == 0 and refs == 0:
            out.append(""); i += 1; continue
        if length == 0:   # over 64k: the next entry holds the length
            length = (words[i * 2 + 3] << 16) + words[i * 2 + 2] if i + 1 < n else 0
            i += 2
        else:
            i += 1
        out.append(data[pos:pos + length].decode(enc, errors="replace"))
        pos += length
    return out, width


def msi_columns(table: bytes, ncols: int, width: int) -> list[list[int]]:
    """A table of string columns, stored column by column; each cell a string reference."""
    rows = len(table) // (ncols * width) if ncols and width else 0
    cols = []
    for c in range(ncols):
        base = c * rows * width
        cols.append([int.from_bytes(table[base + r * width: base + (r + 1) * width], "little") for r in range(rows)])
    return cols


def read_msi(path: Path) -> dict:
    """{properties: {...}, packageCode, tables: {name: bytes size}}; raises ValueError if it isn't an MSI."""
    if not olefile.isOleFile(str(path)):
        raise ValueError("That .msi isn't a Windows Installer file.")
    with olefile.OleFileIO(str(path)) as ole:
        streams, tables = {}, {}
        for entry in ole.listdir(streams=True, storages=False):
            if len(entry) != 1:
                continue
            name, is_table = msi_stream_name(entry[0])
            if is_table:
                tables[name] = entry[0]
            streams[name] = entry[0]
        read = lambda n: ole.openstream(tables[n]).read() if n in tables else b""   # noqa: E731
        strings, width = msi_strings(read("_StringPool"), read("_StringData"))
        prop = read("Property")
        names, values = msi_columns(prop, 2, width) if prop else ([], [])
        props = {strings[k]: strings[v] for k, v in zip(names, values) if 0 < k < len(strings) and v < len(strings)}
        meta = ole.get_metadata()
        package = (meta.revision_number or b"")
        package = package.decode("latin-1", "replace") if isinstance(package, bytes) else str(package)
        sizes = {n: ole.get_size(tables[n]) for n in tables}
    return {"properties": props, "packageCode": package.strip(), "tables": sizes}


def msi_info(path: Path) -> dict:
    m = read_msi(path)
    p = m["properties"]
    allusers = p.get("ALLUSERS", "").strip()
    per_user = p.get("MSIINSTALLPERUSER", "").strip() == "1"
    machine = allusers == "1" or (allusers == "2" and not per_user)
    context = "System" if allusers == "1" else "Any" if allusers == "2" else "User"
    return {
        "name": p.get("ProductName", ""), "version": p.get("ProductVersion", ""), "publisher": p.get("Manufacturer", ""),
        "productCode": p.get("ProductCode", ""), "upgradeCode": p.get("UpgradeCode", ""), "packageCode": m["packageCode"],
        "detection_xml": {
            "MsiPublisher": p.get("Manufacturer", ""), "MsiProductCode": p.get("ProductCode", ""),
            "MsiProductVersion": p.get("ProductVersion", ""), "MsiPackageCode": m["packageCode"],
            "MsiUpgradeCode": p.get("UpgradeCode", ""), "MsiExecutionContext": context,
            "MsiRequiresLogon": False, "MsiRequiresReboot": p.get("REBOOT", "").lower() == "force",
            "MsiIsMachineInstall": machine, "MsiIsUserInstall": not machine,
            "MsiIncludesServices": m["tables"].get("ServiceInstall", 0) > 0,
            "MsiIncludesODBCDataSource": m["tables"].get("ODBCDataSource", 0) > 0,
            "MsiContainsSystemRegistryKeys": False, "MsiContainsSystemFolders": False},
        "machine": machine,
    }


# ---------- EXE ----------

BUILDERS = [   # (tell-tale bytes, builder) in the order they're checked
    ((b"Inno Setup Setup Data", b"JR.Inno.Setup", b"Inno Setup"), "Inno Setup"),
    ((b".wixburn", b"WixBundle", b"WiX Toolset"), "WiX Burn"),
    ((b"Nullsoft.NSIS", b"NullsoftInst", b"Nullsoft Install System"), "NSIS"),
    ((b"Advanced Installer",), "Advanced Installer"),
    ((b"InstallShield",), "InstallShield"),
    ((b"SquirrelSetup", b"Squirrel.Windows", b"squirrel.exe"), "Squirrel"),
    ((b"Google Update", b"GoogleUpdate", b"Omaha"), "Google Update"),
]
SCAN = 48 * 1024 * 1024   # how much of the file is searched for those


def exe_version(path: Path) -> dict:
    """ProductName, CompanyName, ProductVersion, FileDescription from the EXE's version information."""
    try:
        import pefile
        pe = pefile.PE(str(path), fast_load=True)
        pe.parse_data_directories(directories=[pefile.DIRECTORY_ENTRY["IMAGE_DIRECTORY_ENTRY_RESOURCE"]])
        out = {}
        for fi in getattr(pe, "FileInfo", None) or []:
            for entry in fi if isinstance(fi, list) else [fi]:
                for st in getattr(entry, "StringTable", []) or []:
                    for k, v in st.entries.items():
                        k, v = k.decode("utf-8", "replace"), v.decode("utf-8", "replace").strip("\x00 ")
                        if v and k not in out:
                            out[k] = v
        pe.close()
        return out
    except Exception:   # not a PE, or odd resources: the commands still work without it
        return {}


def exe_builder(path: Path) -> str:
    with open(path, "rb") as f:
        head = f.read(SCAN)
        f.seek(0, 2)
        size = f.tell()
        tail = b""
        if size > SCAN:
            f.seek(max(SCAN, size - 4 * 1024 * 1024))
            tail = f.read()
    blob = head + tail
    for marks, name in BUILDERS:
        if any(m in blob for m in marks):
            return name
    return ""


# ---------- Commands ----------

def q(s: str) -> str:
    return f'"{s}"'


def _safe(s: str) -> str:
    return re.sub(r"[^\w.-]+", "", s.replace(" ", "")) or "app"


def analyse(folder: Path, setup_file: str) -> dict:
    folder = Path(folder)
    path = folder / setup_file
    ext = path.suffix.lower()
    files = [p.relative_to(folder).as_posix() for p in folder.rglob("*") if p.is_file()]
    notes: list[str] = []
    out = {"kind": ext.lstrip(".").upper(), "builder": "", "name": "", "version": "", "publisher": "", "behaviour": "System",
           "msi": None, "productCode": "", "upgradeCode": ""}

    if ext == ".msi":
        m = msi_info(path)
        code, log = m["productCode"], _safe(m["name"] or path.stem)
        mst = [f for f in files if f.lower().endswith(".mst")]
        tr = f' TRANSFORMS={q(mst[0])}' if mst else ""
        if len(mst) > 1:
            notes.append(f"There are {len(mst)} transforms (.mst); the install uses {mst[0]}. Change it if you want another.")
        out.update(name=m["name"], version=m["version"], publisher=m["publisher"], productCode=code, upgradeCode=m["upgradeCode"],
                   msi=m["detection_xml"], builder="Windows Installer", behaviour="System" if m["machine"] else "User",
                   install=f'msiexec /i {q(setup_file)}{tr} /qn /norestart /l*v {q(IME_LOGS + "\\" + log + "-install.log")}',
                   uninstall=(f'msiexec /x {q(code)} /qn /norestart /l*v {q(IME_LOGS + "\\" + log + "-uninstall.log")}' if code
                              else f'msiexec /x {q(setup_file)} /qn /norestart'),
                   detection={"type": "MSI", "productCode": code, "productVersion": m["version"],
                              "text": f"MSI product code {code}" + (f", version {m['version']} or later" if m["version"] else "")})
        if not m["machine"]:
            notes.append("This MSI installs per user (ALLUSERS isn't 1): set Install behavior to User, or add ALLUSERS=1 to install for everyone.")
        notes.append("Return code 3010 means it worked but needs a restart; Intune treats it as a soft reboot by default.")
        return {**out, "notes": notes}

    if ext == ".exe":
        v = exe_version(path)
        b = exe_builder(path)
        name = v.get("ProductName") or v.get("FileDescription") or path.stem
        folder_name = name.strip() or "<App folder>"
        out.update(name=name, version=v.get("ProductVersion") or v.get("FileVersion") or "", publisher=v.get("CompanyName", ""), builder=b)
        s = q(setup_file)
        if b == "Inno Setup":
            out["install"] = f"{s} /VERYSILENT /SUPPRESSMSGBOXES /NORESTART /SP- /LOG={q(IME_LOGS + chr(92) + _safe(name) + '-install.log')}"
            out["uninstall"] = f'{q("%ProgramFiles%" + chr(92) + folder_name + chr(92) + "unins000.exe")} /VERYSILENT /SUPPRESSMSGBOXES /NORESTART'
            notes.append("Inno Setup puts unins000.exe in the app's folder: check the folder name (and whether it's Program Files (x86)) on a test PC.")
        elif b == "NSIS":
            out["install"] = f"{s} /S"
            out["uninstall"] = f'{q("%ProgramFiles%" + chr(92) + folder_name + chr(92) + "Uninstall.exe")} /S'
            notes.append("NSIS: /S is case-sensitive. The uninstaller's name varies (Uninstall.exe, uninst.exe): check it on a test PC.")
        elif b == "WiX Burn":
            out["install"] = f"{s} /quiet /norestart /log {q(IME_LOGS + chr(92) + _safe(name) + '-install.log')}"
            out["uninstall"] = f"{s} /uninstall /quiet /norestart"
            notes.append("A WiX bundle uninstalls with the same file, so the uninstall runs from the package.")
        elif b == "Advanced Installer":
            out["install"] = f"{s} /exenoui /qn /norestart"
            out["uninstall"] = f"{s} /x /exenoui /qn /norestart"
        elif b == "InstallShield":
            out["install"] = f'{s} /s /v"/qn /norestart"'
            out["uninstall"] = 'msiexec /x "<ProductCode>" /qn /norestart'
            notes.append("InstallShield usually wraps an MSI: find its product code under HKLM\\SOFTWARE\\Microsoft\\Windows\\CurrentVersion\\Uninstall on a test PC.")
        elif b == "Squirrel":
            out["install"] = f"{s} --silent"
            out["uninstall"] = f'{q("%LocalAppData%" + chr(92) + folder_name + chr(92) + "Update.exe")} --uninstall -s'
            out["behaviour"] = "User"
            notes.append("Squirrel installers install per user, into %LocalAppData%: set Install behavior to User.")
        elif b == "Google Update":
            out["install"] = f"{s} /silent /install"
            out["uninstall"] = "<Uninstall command from the app's registry entry>"
            notes.append("This is an online installer that downloads the app when it runs. The vendor's offline or enterprise MSI is usually better for Intune.")
        else:
            out["install"] = f"{s} /S"
            out["uninstall"] = "<Uninstall command from the app's registry entry>"
            notes.append("Couldn't tell what made this installer, so /S is a guess. Check the vendor's silent switches (often /S, /silent, /quiet or /qn).")
        if "<" in out["uninstall"]:
            notes.append("Find the uninstall command under HKLM\\SOFTWARE\\Microsoft\\Windows\\CurrentVersion\\Uninstall (QuietUninstallString, or UninstallString plus its silent switch) on a test PC.")
        out["detection"] = {"type": "Registry", "text": f"Registry: an Uninstall key whose DisplayName is {name}" + (f" and DisplayVersion {out['version']}" if out["version"] else "")
                            + ", or a file the app installs (e.g. its main .exe in Program Files)."}
        return {**out, "notes": notes}

    if ext == ".ps1":
        un = next((f for f in files if re.match(r"(?i)^(.*/)?uninstall.*\.ps1$", f) and f != setup_file), "")
        out.update(kind="PowerShell", name=Path(setup_file).stem, builder="PowerShell script",
                   install=f"{PS64} {q(setup_file)}",
                   uninstall=f"{PS64} {q(un)}" if un else f"{PS64} {q(setup_file)} -Uninstall",
                   detection={"type": "Script", "text": "A custom detection script, or a file or registry value the script leaves behind."})
        notes.append("sysnative runs 64-bit PowerShell: Intune's agent is 32-bit, and would otherwise write to the 32-bit registry and Program Files.")
        if not un:
            notes.append(f"No Uninstall*.ps1 alongside: the uninstall runs {setup_file} with -Uninstall. Add that parameter to the script, or upload an Uninstall.ps1.")
        return {**out, "notes": notes}

    if ext in (".cmd", ".bat"):
        un = next((f for f in files if re.match(r"(?i)^(.*/)?uninstall.*\.(cmd|bat)$", f) and f != setup_file), "")
        out.update(kind=ext.lstrip(".").upper(), name=Path(setup_file).stem, builder="Batch script",
                   install=f"cmd.exe /c {q(setup_file)}", uninstall=f"cmd.exe /c {q(un)}" if un else "<Uninstall command>",
                   detection={"type": "Script", "text": "A file or registry value the script leaves behind."})
        return {**out, "notes": notes}

    raise ValueError("Pick the file Intune should run: an .msi, .exe, .ps1, .cmd or .bat.")


def pick_setup(files: list[str]) -> str:
    """The likeliest setup file among the uploaded ones: an MSI, then an EXE, then install*.ps1 / any .ps1, then .cmd/.bat;
    the shallowest first."""
    def rank(f):
        low, ext = f.lower(), Path(f).suffix.lower()
        order = {".msi": 0, ".exe": 1, ".ps1": 2, ".cmd": 3, ".bat": 3}.get(ext, 9)
        if ext == ".ps1" and Path(low).stem.startswith("uninstall"):
            order = 8
        if ext == ".ps1" and Path(low).stem.startswith("install"):
            order = 1.5 if not any(x.lower().endswith((".msi", ".exe")) for x in files) else 2
        return (order, f.count("/"), len(f))
    runnable = [f for f in files if f.lower().endswith(RUNNABLE)]
    return sorted(runnable, key=rank)[0] if runnable else ""
