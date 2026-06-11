#!/usr/bin/env python3
"""
lo_compare.py — LibreOffice native document comparison.

Run with LO's Python: /usr/lib/libreoffice/program/python lo_compare.py <orig> <revised> <output> <profile_dir>
"""
import sys, os, time

def main():
    original = os.path.abspath(sys.argv[1])
    revised = os.path.abspath(sys.argv[2])
    output = os.path.abspath(sys.argv[3])
    profile_dir = sys.argv[4] if len(sys.argv) > 4 else f"/tmp/lo-compare-{os.getpid()}"

    os.makedirs(profile_dir, exist_ok=True)
    os.environ["UserInstallation"] = f"file://{profile_dir}"

    import uno
    from com.sun.star.beans import PropertyValue

    localContext = uno.getComponentContext()
    smgr = localContext.ServiceManager
    desktop = smgr.createInstanceWithContext("com.sun.star.frame.Desktop", localContext)

    orig_url = uno.systemPathToFileUrl(original)
    doc = desktop.loadComponentFromURL(orig_url, "_blank", 0, ())
    if doc is None:
        print("FAIL: could not open original", file=sys.stderr)
        sys.exit(1)

    rev_url = uno.systemPathToFileUrl(revised)
    dispatcher = smgr.createInstanceWithContext("com.sun.star.frame.DispatchHelper", localContext)
    prop = PropertyValue()
    prop.Name = "URL"
    prop.Value = rev_url
    frame = doc.getCurrentController().getFrame()
    dispatcher.executeDispatch(frame, ".uno:CompareDocuments", "", 0, (prop,))

    time.sleep(1)

    out_url = uno.systemPathToFileUrl(output)

    # Determine output format from extension
    if output.lower().endswith(".pdf"):
        save_props = (PropertyValue(), PropertyValue())
        save_props[0].Name = "FilterName"
        save_props[0].Value = "writer_pdf_Export"
        save_props[1].Name = "Overwrite"
        save_props[1].Value = True
    else:
        save_props = (PropertyValue(), PropertyValue())
        save_props[0].Name = "FilterName"
        save_props[0].Value = "MS Word 2007 XML"
        save_props[1].Name = "Overwrite"
        save_props[1].Value = True

    doc.storeToURL(out_url, save_props)
    doc.close(True)
    print(f"OK: {output}")

if __name__ == "__main__":
    main()
