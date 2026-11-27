import base64
import json
import os
import sys
import urllib.parse
import urllib.request

from pywidevine.cdm import Cdm
from pywidevine.device import Device
from pywidevine.pssh import PSSH

UA = {"User-Agent": "plaYtv/7.1.3 (Linux;Android 14) ExoPlayerLib/2.11.7"}


def b64u(data: bytes) -> str:
    return base64.urlsafe_b64encode(data).decode("ascii").rstrip("=")


def http_get_text(url: str, timeout: int = 20) -> str:
    req = urllib.request.Request(url, headers=UA)
    with urllib.request.urlopen(req, timeout=timeout) as r:
        return r.read().decode("utf-8", errors="ignore")


def main() -> int:
    cid = os.environ.get("CHANNEL_ID", "1132").strip()
    aa_base = os.environ.get("AA_BASE", "https://iptvmax.site/Premium/jio/aa.php").strip()
    wv_base = os.environ.get("WV_BASE", "https://iptvmax.site/Premium/jio/wv.php").strip()
    wvd_path = os.environ.get("WVD_PATH", "device.wvd").strip()

    aa_url = f"{aa_base}?id={urllib.parse.quote(cid)}"
    print("AA URL:", aa_url)
    aa_data = json.loads(http_get_text(aa_url))

    mpd = aa_data.get("mpd") or {}
    mpd_key_url = (mpd.get("key") or "").strip()
    wv_pssh_b64 = (aa_data.get("pssh_b64") or mpd.get("pssh_b64") or "").strip()
    mpd_kids = [
        str(k).replace("-", "").lower()
        for k in (aa_data.get("mpd_kids") or mpd.get("mpd_kids") or [])
    ]

    print("MPD Key :", mpd_key_url)
    print("PSSH B64:", wv_pssh_b64)
    print("MPD KIDs:", mpd_kids)

    if not mpd_key_url or not wv_pssh_b64:
        print("Error: aa.php मधून mpd.key किंवा pssh_b64 मिळाला नाही.", aa_data.get("pssh_error"))
        return 1

    if not os.path.isfile(wvd_path):
        print(f"Error: {wvd_path} फाइल सापडली नाही.")
        return 1

    device = Device.load(wvd_path)
    cdm = Cdm.from_device(device)
    session_id = cdm.open()

    content_keys: list[dict] = []
    matched_key: dict | None = None
    wv_http_code = 0

    try:
        challenge = cdm.get_license_challenge(session_id, PSSH(wv_pssh_b64))
        req_url = (
            f"{wv_base}?id={urllib.parse.quote(cid)}"
            f"&u={b64u(mpd_key_url.encode('utf-8'))}&keep_token=1"
        )
        form_body = urllib.parse.urlencode({"challenge_b64": b64u(challenge)}).encode("utf-8")
        req = urllib.request.Request(
            req_url,
            data=form_body,
            headers={"Content-Type": "application/x-www-form-urlencoded", **UA},
        )
        with urllib.request.urlopen(req, timeout=25) as r:
            res = json.loads(r.read().decode("utf-8", errors="ignore"))

        wv_http_code = int(res.get("http_code") or 0)
        resp_b64 = res.get("resp_b64") or ""
        print(f"wv.php -> ok={res.get('ok')} | http_code={wv_http_code} | resp_len={res.get('resp_len')}")

        if wv_http_code != 200 or not resp_b64:
            print("wv.php Error snip:", res.get("resp_text_snip"))
            return 1

        cdm.parse_license(session_id, base64.b64decode(resp_b64))
        for k in cdm.get_keys(session_id):
            k_type = str(k.type)
            kid_h = k.kid.hex.lower()
            key_h = k.key.hex().lower()
            print(f"Key -> type={k_type} | kid={kid_h} | key={key_h}")
            if k_type == "CONTENT":
                is_matched = kid_h in mpd_kids
                entry = {
                    "kid": kid_h,
                    "key": key_h,
                    "kid_key": f"{kid_h}:{key_h}",
                    "matched_default_kid": is_matched,
                }
                content_keys.append(entry)
                if is_matched and matched_key is None:
                    matched_key = entry
    finally:
        cdm.close(session_id)

    if not matched_key and content_keys:
        matched_key = content_keys[0]

    out_obj = {
        "channel_id": cid,
        "wv_http_code": wv_http_code,
        "mpd_kids": mpd_kids,
        "pssh_b64": wv_pssh_b64,
        "matched_key": matched_key,
        "all_content_keys": content_keys,
    }

    with open("wv_result.json", "w", encoding="utf-8") as f:
        json.dump(out_obj, f, indent=2)

    with open("wv_keys.txt", "w", encoding="utf-8") as f:
        if matched_key:
            f.write(f"MATCHED: {matched_key['kid_key']}\n")
        for item in content_keys:
            tag = " [MATCHED]" if item.get("matched_default_kid") else ""
            f.write(f"{item['kid_key']}{tag}\n")

    if matched_key:
        print(f"\n>>> MATCHED KEY: {matched_key['kid_key']} <<<")
    else:
        print("\nCONTENT key मिळाली नाही.")
        return 1

    return 0


if __name__ == "__main__":
    sys.exit(main())
    
