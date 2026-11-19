"""Kodi-side hands for scripts/kodi_e2e_test.py. CI-only, never shipped.

JSON-RPC has no way to run a built-in (which is how settings.xml launches the
add-on's RunScript() buttons) or to touch another add-on's settings, so the
driver calls this through Addons.ExecuteAddon:

    ["builtin", "<Kodi built-in>"]      run it
    ["getsettings", "<nonce>", id...]   log the target add-on's setting values
    ["loadsettings", "<json file>"]     set target add-on settings from a file

Settings are loaded from a file rather than passed as arguments so that
credentials never travel through a JSON-RPC call Kodi might log. Every call
ends with one machine-readable "E2EHELPER" line the driver waits for.
"""
import json
import sys

import xbmc
import xbmcaddon

TARGET_ADDON = "service.subtitles.opensubtitles-com"


def main(argv):
    command, args = argv[0], argv[1:]
    if command == "builtin":
        xbmc.executebuiltin(args[0])
        return "builtin", {}
    if command == "getsettings":
        addon = xbmcaddon.Addon(TARGET_ADDON)
        return args[0], {key: addon.getSetting(key) for key in args[1:]}
    if command == "loadsettings":
        with open(args[0], "r", encoding="utf-8") as handle:
            values = json.load(handle)
        addon = xbmcaddon.Addon(TARGET_ADDON)
        for key, value in values.items():
            addon.setSetting(key, value)
        return "loadsettings", {"count": len(values)}
    raise ValueError("unknown command: %s" % command)


if __name__ == "__main__":
    try:
        tag, payload = main(sys.argv[1:])
        xbmc.log("E2EHELPER OK %s %s" % (tag, json.dumps(payload)), xbmc.LOGINFO)
    except Exception as err:  # the driver needs a verdict line either way
        xbmc.log("E2EHELPER FAIL %s: %s" % (type(err).__name__, err), xbmc.LOGINFO)
