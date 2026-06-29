# GdkPixbuf is collected via gstreamer wheel collect_all in the spec. PyInstaller's
# stock hook calls get_libdir() at build time, which fails on macOS when wheel dylibs
# are not on the system library search path.


def hook(hook_api):
    return
