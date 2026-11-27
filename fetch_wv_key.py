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
BROWSER_UA = {
    "User-Agent": (
        "Mozilla/5.0 (Linux; Android 14) AppleWebKit/537.36 "
        "Chrome/130.0.0.0 Mobile Safari/537.36"
    )
}


def b64u_from_bytes(data: bytes) -> str:
    return base64.urlsafe_b64encode(data).decode("ascii").rstrip("=")


def hex_to_b64u(hex_str: str) -> str:
    clean = hex_str.strip().replace("-", "")
    return b64u_from_bytes(bytes.fromhex(clean))


def http_get_json(url: str, timeout: int = 25, headers: dict | None = None):
    hdrs = headers if headers is not None else UA
    last_err = None
    for _ in range(3):
        try:
            req = urllib.request.Request(url, headers=hdrs)
            with urllib.request.urlopen(req, timeout=timeout) as r:
                raw = r.read().decode("utf-8", errors="ignore").strip()
                if raw:
                    return json.loads(raw)
        except Exception as e:
            last_err = e
        time.sleep(0.5)
    raise RuntimeError(f"JSON fetch failed for {url}: {last_err}")


def load_existing_coo(out_file: str) -> list[dict]:
    if not os.path.isfile(out_file):
        return []
    try:
        with open(out_file, "r", encoding="utf-8") as f:
            data = json.load(f)
        if isinstance(data, dict) and isinstance(data.get("keys"), list):
            return [item for item in data["keys"] if isinstance(item, dict)]
    except Exception:
        pass
    return []


def collect_existing_kids(existing_keys: list[dict]) -> set[str]:
    known: set[str] = set()
    for item in existing_keys:
        for field in ("kid", "hex_kid", "kid2", "hex_kid2"):
            val = item.get(field)
            if isinstance(val, str) and val.strip():
                known.add(val.strip().lower())
    return known


def build_key_entry(selected_pairs: list[tuple[str, str]]) -> dict:
    first_kid, first_k = selected_pairs[0]
    entry: dict = {
        "kty": "oct",
        "kid": hex_to_b64u(first_kid),
        "k": hex_to_b64u(first_k),
        "hex_kid": first_kid,
        "hex_k": first_k,
    }

    for idx, (kid_h, k_h) in enumerate(selected_pairs[1:], start=2):
        k_field = "K2" if idx == 2 else f"k{idx}"
        entry[f"kid{idx}"] = hex_to_b64u(kid_h)
        entry[k_field] = hex_to_b64u(k_h)
        entry[f"hex_kid{idx}"] = kid_h
        entry[f"hex_k{idx}"] = k_h

    entry["hex_pair"] = ",".join(f"{kid_h}:{k_h}" for kid_h, k_h in selected_pairs)
    return entry


def fetch_channel_matched_pairs(
    cid: str,
    aa_base: str,
    wv_base: str,
    cdm: Cdm,
) -> list[tuple[str, str]]:
    aa_url = f"{aa_base}?id={urllib.parse.quote(cid)}"
    aa_data = http_get_json(aa_url, timeout=25, headers=BROWSER_UA)

    mpd = aa_data.get("mpd") or {}
    mpd_key_url = (mpd.get("key") or "").strip()
    wv_pssh_b64 = (aa_data.get("pssh_b64") or mpd.get("pssh_b64") or "").strip()

    mpd_kids: list[str] = []
    for mk in (aa_data.get("mpd_kids") or mpd.get("mpd_kids") or []):
        mkl = str(mk).replace("-", "").lower().strip()
        if mkl and mkl not in mpd_kids:
            mpd_kids.append(mkl)

    if not mpd_key_url or not wv_pssh_b64:
        print(f"[{cid}] Skip: mpd.key किंवा pssh_b64 मिळाला नाही ({aa_data.get('pssh_error')})")
        return []

    session_id = cdm.open()
    key_map: dict[str, str] = {}
    ordered_kids: list[str] = []

    try:
        challenge = cdm.get_license_challenge(session_id, PSSH(wv_pssh_b64))
        req_url = (
            f"{wv_base}?id={urllib.parse.quote(cid)}"
            f"&u={b64u_from_bytes(mpd_key_url.encode('utf-8'))}&keep_token=1"
        )
        form_body = urllib.parse.urlencode(
            {"challenge_b64": b64u_from_bytes(challenge)}
        ).encode("utf-8")
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
            return []

        cdm.parse_license(session_id, base64.b64decode(resp_b64))
        for k in cdm.get_keys(session_id):
            if str(k.type) == "CONTENT":
                kh = k.kid.hex.lower()
                vh = k.key.hex().lower()
                if kh not in key_map:
                    key_map[kh] = vh
                    ordered_kids.append(kh)
    finally:
        cdm.close(session_id)

    selected_kids = [k for k in mpd_kids if k in key_map] if mpd_kids else ordered_kids
    return [(k, key_map[k]) for k in selected_kids]


def upsert_key_entry(keys_list: list[dict], new_entry: dict) -> None:
    target_kid = new_entry.get("kid")
    target_hex_kid = new_entry.get("hex_kid")
    for i, existing in enumerate(keys_list):
        if (target_kid and existing.get("kid") == target_kid) or (
            target_hex_kid and existing.get("hex_kid") == target_hex_kid
        ):
            keys_list[i] = new_entry
            return
    keys_list.append(new_entry)


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

    channels_data = http_get_json(jiodata_url, timeout=25, headers=BROWSER_UA)
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

    existing_keys = load_existing_coo(out_file)
    existing_kids = collect_existing_kids(existing_keys)
    keys_list: list[dict] = list(existing_keys)

    device = Device.load(wvd_path)
    cdm = Cdm.from_device(device)

    for idx, cid in enumerate(channel_ids, start=1):
        try:
            selected = fetch_channel_matched_pairs(cid, aa_base, wv_base, cdm)
            if not selected:
                print(f"[{idx}/{len(channel_ids)}] {cid} -> Skip (no matched CONTENT keys)")
            elif len(selected) == 1:
                only_kid_hex, only_k_hex = selected[0]
                only_kid_b64 = hex_to_b64u(only_kid_hex)
                if (
                    only_kid_b64.lower() in existing_kids
                    or only_kid_hex.lower() in existing_kids
                ):
                    entry_obj = build_key_entry(selected)
                    upsert_key_entry(keys_list, entry_obj)
                    print(f"[{idx}/{len(channel_ids)}] {cid} -> 1 KID found (exists in Coo.json, updated)")
                else:
                    print(f"[{idx}/{len(channel_ids)}] {cid} -> 1 KID found ({only_kid_hex}), not in Coo.json -> Skipped")
            else:
                entry_obj = build_key_entry(selected)
                upsert_key_entry(keys_list, entry_obj)
                existing_kids.add(entry_obj["kid"].lower())
                existing_kids.add(entry_obj["hex_kid"].lower())
                if "kid2" in entry_obj:
                    existing_kids.add(entry_obj["kid2"].lower())
                if "hex_kid2" in entry_obj:
                    existing_kids.add(entry_obj["hex_kid2"].lower())
                print(
                    f"[{idx}/{len(channel_ids)}] {cid} -> {len(selected)} KIDs matched -> "
                    f"Coo.json updated ({entry_obj['hex_pair']})"
                )
        except Exception as e:
            print(f"[{idx}/{len(channel_ids)}] {cid} -> Error: {e}")

        if delay > 0:
            time.sleep(delay)

    with open(out_file, "w", encoding="utf-8") as f:
        json.dump({"keys": keys_list}, f, indent=4)

    print(f"\nपूर्ण झाले! {out_file} मध्ये एकूण {len(keys_list)} entries सेव्ह केल्या आहेत.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
