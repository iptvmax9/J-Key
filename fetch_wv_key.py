import base64
import json
import os
import sys
import time
import urllib.parse
import urllib.request

from pywidevine.cdm import Cdm
from pywidevine.device import Device
from pywidevine.pssh import PSSH

UA = {"User-Agent": "plaYtv/7.1.3 (Linux;Android 14) ExoPlayerLib/2.11.7"}


def b64u_from_bytes(data: bytes) -> str:
    return base64.urlsafe_b64encode(data).decode("ascii").rstrip("=")


def hex_to_b64u(hex_str: str) -> str:
    clean = hex_str.strip().replace("-", "")
    return b64u_from_bytes(bytes.fromhex(clean))


def http_get_json(url: str, timeout: int = 20):
    req = urllib.request.Request(url, headers=UA)
    with urllib.request.urlopen(req, timeout=timeout) as r:
        return json.loads(r.read().decode("utf-8", errors="ignore"))


def fetch_channel_matched_key(
    cid: str,
    aa_base: str,
    wv_base: str,
    cdm: Cdm,
    device: Device,
) -> dict | None:
    aa_url = f"{aa_base}?id={urllib.parse.quote(cid)}"
    aa_data = http_get_json(aa_url)

    mpd = aa_data.get("mpd") or {}
    mpd_key_url = (mpd.get("key") or "").strip()
    wv_pssh_b64 = (aa_data.get("pssh_b64") or mpd.get("pssh_b64") or "").strip()
    mpd_kids = [
        str(k).replace("-", "").lower()
        for k in (aa_data.get("mpd_kids") or mpd.get("mpd_kids") or [])
    ]

    if not mpd_key_url or not wv_pssh_b64:
        print(f"[{cid}] Skip: mpd.key किंवा pssh_b64 मिळाला नाही ({aa_data.get('pssh_error')})")
        return None

    session_id = cdm.open()
    content_keys: list[dict] = []
    matched: dict | None = None

    try:
        challenge = cdm.get_license_challenge(session_id, PSSH(wv_pssh_b64))
        req_url = (
            f"{wv_base}?id={urllib.parse.quote(cid)}"
            f"&u={b64u_from_bytes(mpd_key_url.encode('utf-8'))}&keep_token=1"
        )
        form_body = urllib.parse.urlencode({"challenge_b64": b64u_from_bytes(challenge)}).encode("utf-8")
        req = urllib.request.Request(
            req_url,
            data=form_body,
            headers={"Content-Type": "application/x-www-form-urlencoded", **UA},
        )
        with urllib.request.urlopen(req, timeout=25) as r:
            res = json.loads(r.read().decode("utf-8", errors="ignore"))

        http_code = int(res.get("http_code") or 0)
        resp_b64 = res.get("resp_b64") or ""
        if http_code != 200 or not resp_b64:
            print(f"[{cid}] Skip: wv.php http_code={http_code}")
            return None

        cdm.parse_license(session_id, base64.b64decode(resp_b64))
        for k in cdm.get_keys(session_id):
            if str(k.type) == "CONTENT":
                hex_kid = k.kid.hex.lower()
                hex_k = k.key.hex().lower()
                item = {
                    "kty": "oct",
                    "kid": hex_to_b64u(hex_kid),
                    "k": hex_to_b64u(hex_k),
                    "hex_kid": hex_kid,
                    "hex_k": hex_k,
                    "hex_pair": f"{hex_kid}:{hex_k}",
                }
                content_keys.append(item)
                if hex_kid in mpd_kids and matched is None:
                    matched = item
    finally:
        cdm.close(session_id)

    return matched or (content_keys[0] if content_keys else None)


def main() -> int:
    jiodata_url = os.environ.get(
        "JIODATA_URL", "https://mitthu786.github.io/tvepg/jiotv/jiodata.json"
    ).strip()
    aa_base = os.environ.get("AA_BASE", "https://iptvmax.site/Premium/jio/aa.php").strip()
    wv_base = os.environ.get("WV_BASE", "https://iptvmax.site/Premium/jio/wv.php").strip()
    wvd_path = os.environ.get("WVD_PATH", "device.wvd").strip()
    out_file = os.environ.get("OUT_FILE", "Coo.json").strip()
    limit = int(os.environ.get("CHANNEL_LIMIT", "0") or "0")
    delay = float(os.environ.get("REQUEST_DELAY", "0.2") or "0.2")

    if not os.path.isfile(wvd_path):
        print(f"Error: {wvd_path} फाइल सापडली नाही.")
        return 1

    channels_data = http_get_json(jiodata_url)
    if not isinstance(channels_data, list):
        print("Error: jiodata.json लिस्ट स्वरूपात नाही.")
        return 1

    seen_ids: set[str] = set()
    channel_ids: list[str] = []
    for entry in channels_data:
        if isinstance(entry, dict) and entry.get("channel_id") is not None:
            cid = str(entry["channel_id"]).strip()
            if cid and cid not in seen_ids:
                seen_ids.add(cid)
                channel_ids.append(cid)

    if limit > 0:
        channel_ids = channel_ids[:limit]

    print(f"एकूण चॅनेल्स: {len(channel_ids)}")

    device = Device.load(wvd_path)
    cdm = Cdm.from_device(device)

    keys_list: list[dict] = []
    seen_pairs: set[str] = set()

    for idx, cid in enumerate(channel_ids, start=1):
        try:
            key_obj = fetch_channel_matched_key(cid, aa_base, wv_base, cdm, device)
            if key_obj and key_obj["hex_pair"] not in seen_pairs:
                seen_pairs.add(key_obj["hex_pair"])
                keys_list.append(key_obj)
                print(f"[{idx}/{len(channel_ids)}] {cid} -> OK ({key_obj['hex_pair']})")
            elif key_obj:
                print(f"[{idx}/{len(channel_ids)}] {cid} -> Duplicate key skipped")
        except Exception as e:
            print(f"[{idx}/{len(channel_ids)}] {cid} -> Error: {e}")

        if delay > 0:
            time.sleep(delay)

    with open(out_file, "w", encoding="utf-8") as f:
        json.dump({"keys": keys_list}, f, indent=4)

    print(f"\nपूर्ण झाले! {out_file} मध्ये {len(keys_list)} keys सेव्ह केल्या आहेत.")
    return 0 if keys_list else 1


if __name__ == "__main__":
    sys.exit(main())
    
