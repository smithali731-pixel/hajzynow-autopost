"""Publish due HajzyNow posts to Instagram (@hajzynow) via the Instagram Graph API.

Instagram cross-posts to the linked HajzyNow Facebook page by itself, so this script
posts to Instagram only. Media is read by Meta straight from this public repository.

Environment: IG_USER_ID, IG_TOKEN (page access token), GITHUB_REPOSITORY.
"""
import json, os, sys, time, urllib.error, urllib.parse, urllib.request
from datetime import datetime, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parent
SCHEDULE = ROOT / "schedule.json"
STATE = ROOT / "state.json"
GRAPH = "https://graph.facebook.com/v21.0"
WINDOW_MIN = int(os.environ.get("WINDOW_MIN", "180"))  # GitHub cron can lag; skip anything older than this
RETRY_WAITS = (60, 180)  # after a failed attempt: retry in 1 minute, then again 3 minutes later


def log(level, msg):
    print(f"[{datetime.now(timezone.utc):%Y-%m-%d %H:%M:%S}Z] {level} {msg}", flush=True)


def api(path, params, method="POST"):
    data = urllib.parse.urlencode({**params, "access_token": os.environ["IG_TOKEN"]}).encode()
    url = f"{GRAPH}/{path}"
    req = urllib.request.Request(url + "?" + data.decode(), method="GET") if method == "GET" \
        else urllib.request.Request(url, data=data, method="POST")
    try:
        with urllib.request.urlopen(req, timeout=120) as r:
            return json.loads(r.read().decode())
    except urllib.error.HTTPError as ex:
        raise RuntimeError(f"{path}: HTTP {ex.code} {ex.read().decode()[:400]}") from None


def notify(text):
    """Send an alert to the owner through the HajzyNow Telegram bot. Never raises."""
    token, chat = os.environ.get("TG_BOT_TOKEN"), os.environ.get("TG_CHAT_ID")
    if not token or not chat:
        log("WARN", "telegram alert skipped: TG_BOT_TOKEN/TG_CHAT_ID not set")
        return
    data = urllib.parse.urlencode({"chat_id": chat, "text": text, "disable_web_page_preview": "true"}).encode()
    try:
        urllib.request.urlopen(f"https://api.telegram.org/bot{token}/sendMessage", data=data, timeout=30).read()
        log("INFO", "telegram alert sent")
    except urllib.error.HTTPError as ex:
        log("WARN", f"telegram alert failed: HTTP {ex.code} {ex.read().decode()[:200]}")
    except Exception as ex:
        log("WARN", f"telegram alert failed: {ex}")


def baghdad(at):
    return datetime.fromisoformat(at).strftime("%d/%m %H:%M")


def publish_with_retries(item, sleep=time.sleep):
    """Publish, retrying after 1 and 3 minutes; alert on every failure and on success after a failure."""
    attempts = len(RETRY_WAITS) + 1
    for n in range(1, attempts + 1):
        try:
            pid = publish(item)
            if n > 1:
                notify(f"✅ تم نشر منشور حجزي ناو بعد المحاولة {n}\n{item['id']} (موعده {baghdad(item['at'])} بغداد)")
            return pid
        except Exception as ex:
            log("ERROR", f"{item['id']} attempt {n}/{attempts}: {ex}")
            if n == attempts:
                notify(f"❌ فشل نشر منشور حجزي ناو بعد {attempts} محاولات\n{item['id']} (موعده {baghdad(item['at'])} بغداد)\nالسبب: {str(ex)[:300]}")
                raise
            wait = RETRY_WAITS[n - 1]
            notify(f"⚠️ منشور حجزي ناو لم ينشر في موعده\n{item['id']} (موعده {baghdad(item['at'])} بغداد)\nأعيد المحاولة بعد {wait // 60} دقيقة")
            sleep(wait)


def media_url(name):
    if not (ROOT / "media" / name).exists():
        raise RuntimeError(f"missing media/{name}")
    repo = os.environ.get("GITHUB_REPOSITORY", "smithali731-pixel/hajzynow-autopost")
    branch = os.environ.get("GITHUB_REF_NAME", "main")
    return f"https://raw.githubusercontent.com/{repo}/{branch}/media/{urllib.parse.quote(name)}"


def wait_ready(cid, tries=60, pause=10):
    for _ in range(tries):
        st = api(cid, {"fields": "status_code,status"}, "GET")
        if st.get("status_code") == "FINISHED":
            return
        if st.get("status_code") in ("ERROR", "EXPIRED"):
            raise RuntimeError(f"container {cid} failed: {st}")
        time.sleep(pause)
    raise RuntimeError(f"container {cid} not ready after {tries * pause}s")


def publish(item):
    ig = os.environ["IG_USER_ID"]
    kind, files, caption = item.get("type", "feed"), item["media"], item.get("caption", "")
    if kind == "reel":
        cid = api(f"{ig}/media", {"media_type": "REELS", "video_url": media_url(files[0]),
                                  "caption": caption, "share_to_feed": "true"})["id"]
    elif kind == "story":
        key = "video_url" if files[0].endswith(".mp4") else "image_url"
        cid = api(f"{ig}/media", {"media_type": "STORIES", key: media_url(files[0])})["id"]
    elif len(files) == 1:
        cid = api(f"{ig}/media", {"image_url": media_url(files[0]), "caption": caption})["id"]
    else:
        kids = [api(f"{ig}/media", {"image_url": media_url(f), "is_carousel_item": "true"})["id"] for f in files]
        for k in kids:
            wait_ready(k)
        cid = api(f"{ig}/media", {"media_type": "CAROUSEL", "children": ",".join(kids), "caption": caption})["id"]
    wait_ready(cid)
    return api(f"{ig}/media_publish", {"creation_id": cid})["id"]


def main():
    if os.environ.get("TEST_ALERT"):
        notify("🔔 تجربة تنبيهات حجزي ناو\nإذا وصلتك هذه الرسالة فالتنبيهات تعمل. أي منشور يتأخر أو يفشل سيصلك عنه تنبيه هنا.")
        return
    now = datetime.now(timezone.utc)
    schedule = json.loads(SCHEDULE.read_text(encoding="utf-8"))
    state = json.loads(STATE.read_text(encoding="utf-8")) if STATE.exists() else {"done": {}, "skipped": {}}
    due = []
    for it in schedule:
        if it["id"] in state["done"] or it["id"] in state["skipped"]:
            continue
        at = datetime.fromisoformat(it["at"])
        late = (now - at).total_seconds() / 60
        if late < 0:
            continue
        if late > WINDOW_MIN:
            state["skipped"][it["id"]] = f"missed by {int(late)} min"
            log("WARN", f"skipped {it['id']}: {int(late)} min late")
            notify(f"❌ منشور حجزي ناو فات موعده بأكثر من {WINDOW_MIN // 60} ساعات ولم ينشر\n{it['id']} (موعده {baghdad(it['at'])} بغداد)")
            continue
        due.append((at, it))
    failed = False
    for _, it in sorted(due, key=lambda x: x[0]):
        try:
            pid = publish_with_retries(it)
            state["done"][it["id"]] = {"post_id": pid, "at": now.isoformat(timespec="seconds")}
            log("INFO", f"published {it['id']} -> {pid}")
        except Exception as ex:  # keep going so one bad post does not block the rest
            failed = True
            log("ERROR", f"{it['id']}: {ex}")
    if not due:
        acct = api(os.environ["IG_USER_ID"], {"fields": "username"}, "GET")
        log("INFO", f"nothing due; token OK for @{acct.get('username')}")
    STATE.write_text(json.dumps(state, ensure_ascii=False, indent=1) + "\n", encoding="utf-8")
    sys.exit(1 if failed else 0)


if __name__ == "__main__":
    main()
