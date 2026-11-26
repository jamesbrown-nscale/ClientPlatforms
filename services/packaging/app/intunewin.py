"""Making an .intunewin, the way Microsoft's IntuneWinAppUtil.exe does, on Linux.

An .intunewin is a zip:

    IntuneWinPackage/Contents/IntunePackage.intunewin   the setup folder, zipped, then encrypted
    IntuneWinPackage/Metadata/Detection.xml             what's needed to decrypt it, and the MSI's details

The setup folder is zipped (deflate), and that zip is encrypted with AES-256-CBC (PKCS7 padding) under a
fresh random 32-byte key and 16-byte IV. The encrypted file is

    HMAC-SHA256 (32 bytes) | IV (16 bytes) | ciphertext

where the HMAC, under its own random 32-byte key, covers IV + ciphertext. Detection.xml holds both keys,
the IV and the HMAC (base64), and the SHA-256 of the unencrypted zip. Intune gets the encrypted file
and those details when the app is uploaded; it never needs the keys to be anywhere else.

build(source_dir, setup_file, out_path, msi=None) streams everything through temp files, so a large
installer doesn't need to fit in memory.
"""
import base64
import hashlib
import hmac
import os
import shutil
import tempfile
import zipfile
from pathlib import Path
from xml.sax.saxutils import escape

from cryptography.hazmat.primitives import padding
from cryptography.hazmat.primitives.ciphers import Cipher, algorithms, modes

TOOL_VERSION = "1.8.6.0"   # the IntuneWinAppUtil version whose output this matches
CHUNK = 1024 * 1024


def _b64(b: bytes) -> str:
    return base64.b64encode(b).decode()


def zip_folder(source: Path, out: Path) -> None:
    """The setup folder as a zip, every file at its path inside the folder (no top folder)."""
    with zipfile.ZipFile(out, "w", zipfile.ZIP_DEFLATED, compresslevel=6, allowZip64=True) as z:
        for p in sorted(source.rglob("*")):
            if p.is_file():
                z.write(p, p.relative_to(source).as_posix())


def encrypt(plain: Path, out: Path) -> dict:
    """AES-256-CBC with an HMAC-SHA256 in front; returns the encryption details (base64) and the digest."""
    key, mac_key, iv = os.urandom(32), os.urandom(32), os.urandom(16)
    enc = Cipher(algorithms.AES(key), modes.CBC(iv)).encryptor()
    pad = padding.PKCS7(128).padder()
    mac = hmac.new(mac_key, iv, hashlib.sha256)
    digest = hashlib.sha256()
    size = 0
    with open(plain, "rb") as src, open(out, "wb") as dst:
        dst.write(b"\0" * 32)   # the HMAC goes here once it's known
        dst.write(iv)
        while chunk := src.read(CHUNK):
            digest.update(chunk)
            size += len(chunk)
            ct = enc.update(pad.update(chunk))
            mac.update(ct)
            dst.write(ct)
        ct = enc.update(pad.finalize()) + enc.finalize()
        mac.update(ct)
        dst.write(ct)
        tag = mac.digest()
        dst.seek(0)
        dst.write(tag)
    return {"EncryptionKey": _b64(key), "MacKey": _b64(mac_key), "InitializationVector": _b64(iv), "Mac": _b64(tag),
            "ProfileIdentifier": "ProfileVersion1", "FileDigest": _b64(digest.digest()), "FileDigestAlgorithm": "SHA256",
            "_size": size}


def decrypt(enc_path: Path, info: dict, out: Path) -> None:
    """The other way (for checks): verifies the HMAC, then decrypts. Raises ValueError if it doesn't match."""
    key, mac_key = base64.b64decode(info["EncryptionKey"]), base64.b64decode(info["MacKey"])
    data = Path(enc_path).read_bytes()
    tag, iv, ct = data[:32], data[32:48], data[48:]
    if not hmac.compare_digest(tag, hmac.new(mac_key, iv + ct, hashlib.sha256).digest()):
        raise ValueError("The HMAC doesn't match.")
    dec = Cipher(algorithms.AES(key), modes.CBC(iv)).decryptor()
    unpad = padding.PKCS7(128).unpadder()
    Path(out).write_bytes(unpad.update(dec.update(ct) + dec.finalize()) + unpad.finalize())


MSI_FIELDS = ("MsiPublisher", "MsiProductCode", "MsiProductVersion", "MsiPackageCode", "MsiUpgradeCode", "MsiExecutionContext",
              "MsiRequiresLogon", "MsiRequiresReboot", "MsiIsMachineInstall", "MsiIsUserInstall", "MsiIncludesServices",
              "MsiIncludesODBCDataSource", "MsiContainsSystemRegistryKeys", "MsiContainsSystemFolders")


def detection_xml(name: str, setup_file: str, enc: dict, msi: dict | None = None) -> str:
    def el(tag, v):
        return f"<{tag}>{escape(str(v))}</{tag}>"
    info = "".join(el(k, enc[k]) for k in ("EncryptionKey", "MacKey", "InitializationVector", "Mac", "ProfileIdentifier",
                                            "FileDigest", "FileDigestAlgorithm"))
    msi_xml = ""
    if msi:
        msi_xml = "<MsiInfo>" + "".join(el(k, str(msi[k]).lower() if isinstance(msi[k], bool) else msi[k])
                                        for k in MSI_FIELDS if k in msi and msi[k] not in (None, "")) + "</MsiInfo>"
    return ('<?xml version="1.0" encoding="utf-8"?>\n'
            '<ApplicationInfo xmlns:xsd="http://www.w3.org/2001/XMLSchema" xmlns:xsi="http://www.w3.org/2001/XMLSchema-instance" '
            f'ToolVersion="{TOOL_VERSION}">'
            + el("Name", name) + el("UnencryptedContentSize", enc["_size"]) + el("FileName", "IntunePackage.intunewin")
            + el("SetupFile", setup_file) + "<EncryptionInfo>" + info + "</EncryptionInfo>" + msi_xml + "</ApplicationInfo>")


def build(source: Path, setup_file: str, out: Path, name: str = "", msi: dict | None = None) -> dict:
    """Packages the folder `source` (setup_file is inside it) into `out` (.intunewin). Returns
    {size, contentSize, digest} for the record."""
    source, out = Path(source), Path(out)
    if not (source / setup_file).is_file():
        raise ValueError(f"{setup_file} isn't in the files.")
    work = Path(tempfile.mkdtemp(prefix="iw-", dir=out.parent))
    try:
        plain, enc_path = work / "content.zip", work / "IntunePackage.intunewin"
        zip_folder(source, plain)
        enc = encrypt(plain, enc_path)
        plain.unlink()
        xml = detection_xml(name or setup_file, setup_file, enc, msi)
        tmp_out = out.with_suffix(".part")
        with zipfile.ZipFile(tmp_out, "w", zipfile.ZIP_STORED, allowZip64=True) as z:
            z.write(enc_path, "IntuneWinPackage/Contents/IntunePackage.intunewin")
            z.writestr(zipfile.ZipInfo("IntuneWinPackage/Metadata/Detection.xml", date_time=(2020, 1, 1, 0, 0, 0)), xml,
                       compress_type=zipfile.ZIP_DEFLATED)
        tmp_out.replace(out)
        return {"size": out.stat().st_size, "contentSize": enc["_size"], "digest": enc["FileDigest"]}
    finally:
        shutil.rmtree(work, ignore_errors=True)
