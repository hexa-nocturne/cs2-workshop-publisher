"""Read-only Steam Web API queries used to verify an upload.

GetPublishedFileDetails needs no API key and only returns details for items that
are visible to anonymous users (public or unlisted). TLS certificates are always
verified.
"""

import json
import ssl
import urllib.error
import urllib.parse
import urllib.request

DETAILS_URL = "https://api.steampowered.com/ISteamRemoteStorage/GetPublishedFileDetails/v1/"


class SteamApiError(Exception):
    pass


def get_published_file_details(published_file_id, timeout=20, opener=None):
    body = urllib.parse.urlencode({"itemcount": 1, "publishedfileids[0]": str(published_file_id)}).encode()
    request = urllib.request.Request(DETAILS_URL, data=body, headers={"User-Agent": "workshop-publisher"})
    try:
        if opener is None:
            response = urllib.request.urlopen(request, timeout=timeout, context=ssl.create_default_context())
        else:
            response = opener(request, timeout)
        with response:
            payload = json.loads(response.read().decode("utf-8"))
    except (urllib.error.URLError, OSError, ValueError) as exc:
        raise SteamApiError("could not query the Steam Web API: %s" % exc)
    try:
        details = payload["response"]["publishedfiledetails"][0]
    except (KeyError, IndexError, TypeError):
        raise SteamApiError("unexpected response from the Steam Web API")
    if int(details.get("result", 0)) != 1:
        raise SteamApiError(
            "Steam returned result %s for item %s (private or friends-only items cannot be checked anonymously)"
            % (details.get("result"), published_file_id)
        )
    return {
        "publishedFileId": details.get("publishedfileid"),
        "appId": details.get("consumer_app_id"),
        "title": details.get("title"),
        "fileSize": int(details.get("file_size") or 0),
        "timeUpdated": int(details.get("time_updated") or 0),
        "visibility": details.get("visibility"),
        "banned": bool(details.get("banned")),
        "url": "https://steamcommunity.com/sharedfiles/filedetails/?id=%s" % published_file_id,
    }


def verify_update(published_file_id, app_id, uploaded_after, expected_size=None, opener=None):
    """Return (ok, details, problems) comparing Steam's view with what was just uploaded."""
    details = get_published_file_details(published_file_id, opener=opener)
    problems = []
    if details["appId"] is not None and int(details["appId"]) != int(app_id):
        problems.append("item belongs to app %s, expected %s" % (details["appId"], app_id))
    if details["timeUpdated"] < int(uploaded_after):
        problems.append("Steam's last-updated time is older than this upload (the API may lag a few minutes)")
    if details["banned"]:
        problems.append("the item is banned on Steam")
    if expected_size and details["fileSize"] and abs(details["fileSize"] - expected_size) > max(4096, expected_size // 100):
        problems.append("Steam reports %d bytes, the uploaded content was %d bytes" % (details["fileSize"], expected_size))
    return not problems, details, problems
