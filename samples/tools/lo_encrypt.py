r"""Save an .odt as a password-protected copy using LibreOffice itself (for test fixtures).

Run with LibreOffice's bundled Python:
    "C:\Program Files\LibreOffice\program\python.exe" samples/tools/lo_encrypt.py IN.odt OUT.odt PASSWORD [--odf12]

--odf12 saves as ODF 1.2 extended (legacy PBKDF2 + AES-CBC per-file encryption). The
LibreOffice setting is switched only for this save and restored afterwards.
"""
import sys
from pathlib import Path

import officehelper
import uno
from com.sun.star.beans import PropertyValue

ODF_12_EXTENDED = 9


def prop(name, value):
    p = PropertyValue()
    p.Name, p.Value = name, value
    return p


def odf_settings(ctx):
    provider = ctx.ServiceManager.createInstanceWithContext(
        "com.sun.star.configuration.ConfigurationProvider", ctx)
    return provider.createInstanceWithArguments(
        "com.sun.star.configuration.ConfigurationUpdateAccess",
        (prop("nodepath", "/org.openoffice.Office.Common/Save/ODF"),))


def main(src, dst, password, odf12=False):
    ctx = officehelper.bootstrap()
    desktop = ctx.ServiceManager.createInstanceWithContext("com.sun.star.frame.Desktop", ctx)
    cfg = odf_settings(ctx)
    old_version = cfg.getPropertyValue("DefaultVersion")
    doc = None
    try:
        if odf12:
            cfg.setPropertyValue("DefaultVersion", ODF_12_EXTENDED)
            cfg.commitChanges()
        doc = desktop.loadComponentFromURL(uno.systemPathToFileUrl(str(Path(src).resolve())),
                                           "_default", 0, (prop("Hidden", True),))
        doc.storeToURL(uno.systemPathToFileUrl(str(Path(dst).resolve())),
                       (prop("FilterName", "writer8"), prop("Password", password)))
    finally:
        if doc is not None:
            doc.close(True)
        cfg.setPropertyValue("DefaultVersion", old_version)
        cfg.commitChanges()
        try:
            desktop.terminate()
        except Exception:
            pass


if __name__ == "__main__":
    args = [a for a in sys.argv[1:] if a != "--odf12"]
    main(*args[:3], odf12="--odf12" in sys.argv)
