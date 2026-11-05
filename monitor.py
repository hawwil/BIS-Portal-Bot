"""
BIS Portal Bot — تنبيهات بوابة طلاب BIS غزة للأهل
بيدخل على بوابة كل طالب مسجّل، وبيبعت لأهله على تلغرام أي إشي جديد.
كل البيانات (كلمات السر، الواجبات، العلامات) محفوظة مشفّرة.
تم التطوير من NEXA للخدمات الرقمية
"""
import datetime
import html
import json
import os
import re
import secrets
import sys
from pathlib import Path
from urllib.parse import urlparse

import requests
from cryptography.fernet import Fernet
from playwright.sync_api import sync_playwright

VERSION = "2.0"
URL = os.environ.get("BIS_URL", "https://portal.bis.edu.ps/student")
TOKEN = os.environ.get("TG_TOKEN", "")
KEY = os.environ.get("STATE_KEY", "")
DRY = os.environ.get("TG_DRY") == "1"  # للتجربة: بيطبع بدل ما يبعت
STATE = Path(os.environ.get("STATE_DIR", "state"))
DATA_F = STATE / "data.enc"
SHOTS = Path("shots")
MAX_PAGES = int(os.environ.get("MAX_PAGES", "40"))
QUICK = os.environ.get("QUICK") == "1"  # فحص سريع: الأقسام المهمة بس
BOT_NAME = "BIS Portal Bot"

SKIP_LINK = re.compile(r"logout|log-out|signout|sign-out|خروج", re.I)
SKIP_EXT = re.compile(r"\.(pdf|jpe?g|png|gif|zip|docx?|xlsx?|pptx?)(\?|$)", re.I)
# أسطر بتتغير لحالها (ساعة، تاريخ، أرقام بس، "قبل ٥ دقائق"، عدّادات) — منتجاهلها عشان ما يصير إزعاج
D = r"[\d٠-٩]"
NOISE = re.compile(
    rf"^[\d٠-٩\s:/.\-،,()+]+$|^{D}{{1,2}}:{D}{{2}}"
    r"|^(قبل|بعد|منذ)\s|^(أمس|أول أمس|اليوم|الآن|غداً|غدا|بعد غد)$|\bago$"
)
RELTIME = re.compile(rf"\s*·?\s*(قبل|بعد|منذ)\s+{D}+\s*(ثانية|ثوانٍ|ثوان|دقيقة|دقائق|ساعة|ساعات|يوم|أيام|يومين|أسبوع|أسابيع|شهر|أشهر)")
IGNORE = {"أهلاً! محتاج مساعدة؟ اضغط عليّ 👋"}  # فقاعة المساعدة اللي بتطلع وبتختفي
COUNTER = re.compile(r"جديد|قادم|من|في هذا اليوم|غير مقروء|left|new")
# ترتيب الصفحات بالتنبيه: الإشعارات أول، بعدين الواجبات والعلامات
PRIORITY = ["notifications", "assignments", "grades", "quizzes", "inbox", "meetings"]
LOGIN_BTN = re.compile(r"دخول|login|log in|sign in", re.I)
CODE_RE = re.compile(r"^\s*(BIS-[A-Z0-9]{4,8})\s*$", re.I)
CODE_ALPHABET = "ABCDEFGHJKLMNPQRSTUVWXYZ23456789"  # بدون O/0 و I/1 عشان ما يتلخبطوا

FOOTER = (
    "\n\n— — —\n"
    'تم التطوير من <a href="https://instagram.com/nexa.ryx"><b><u>NEXA</u></b></a> للخدمات الرقمية\n'
    f"© NEXA {datetime.date.today().year} · جميع الحقوق محفوظة · v{VERSION}"
)


def log(*a):
    # الريبو عام، فالسجلات عامة: منطبع أرقام وحالات بس، ولا مرة أسماء أو محتوى
    print(*a, flush=True)


# ---------------- التخزين المشفّر ----------------
def load_data():
    if DATA_F.exists():
        return json.loads(Fernet(KEY.encode()).decrypt(DATA_F.read_bytes()))
    return {"admin": None, "offset": 0, "students": {}}


def save_data(data):
    STATE.mkdir(exist_ok=True)
    DATA_F.write_bytes(Fernet(KEY.encode()).encrypt(json.dumps(data, ensure_ascii=False).encode()))


def migrate_v1(data):
    """نقل رضا من النسخة القديمة (ملفات غير مشفّرة + Secrets) للنظام الجديد."""
    old_chat = STATE / "chat_id.txt"
    if data["students"] or not os.environ.get("BIS_USERNAME"):
        return
    pages_f, seen_f = STATE / "pages.json", STATE / "seen.json"
    pages = json.loads(pages_f.read_text()) if pages_f.exists() else {}
    seen = json.loads(seen_f.read_text()) if seen_f.exists() else []
    if old_chat.exists():
        data["admin"] = old_chat.read_text().strip()
    sid = new_code(data)
    data["students"][sid] = {
        "name": os.environ.get("FIRST_STUDENT_NAME", "رضا"),
        "user": os.environ["BIS_USERNAME"], "pass": os.environ["BIS_PASSWORD"],
        "chats": [data["admin"]] if data["admin"] else [],
        "pages": pages, "seen": seen, "flags": {},
    }
    for f in ("chat_id.txt", "pages.json", "seen.json", "flags.json", "last_run.txt"):
        (STATE / f).unlink(missing_ok=True)
    log("migrated v1 → 1 student")


def new_code(data):
    while True:
        c = "BIS-" + "".join(secrets.choice(CODE_ALPHABET) for _ in range(5))
        if c not in data["students"]:
            return c


# ---------------- تلغرام ----------------
def tg(method, data=None, files=None):
    if DRY:
        print(f"[TG {method}]", (data or {}).get("chat_id", ""), (data or {}).get("text") or (data or {}).get("caption") or "",
              "| files:", list((files or {}).keys()))
        return {"ok": True, "result": []}
    try:
        r = requests.post(f"https://api.telegram.org/bot{TOKEN}/{method}", data=data, files=files, timeout=60)
        return r.json()
    except Exception as e:
        log("tg error", method, type(e).__name__)
        return {"ok": False}


def send_text(chat, text, footer=True):
    # تلغرام بيقبل لحد 4096 حرف بالرسالة
    chunks, cur = [], ""
    for block in text.split("\n\n"):
        if len(cur) + len(block) + 2 > 3500:
            chunks.append(cur)
            cur = ""
        cur += block + "\n\n"
    chunks.append(cur)
    if footer:
        chunks[-1] = chunks[-1].rstrip() + FOOTER
    for c in chunks:
        if c.strip():
            tg("sendMessage", {"chat_id": chat, "text": c.strip(), "parse_mode": "HTML", "disable_web_page_preview": "true"})


def send_photo(chat, path, caption):
    with open(path, "rb") as fh:
        tg("sendPhoto", {"chat_id": chat, "caption": caption[:1000], "parse_mode": "HTML"}, {"photo": fh})


def get_updates(data):
    if DRY:
        return json.loads(os.environ.get("DRY_UPDATES", "[]"))
    res = tg("getUpdates", {"offset": data.get("offset", 0) + 1, "timeout": 0, "allowed_updates": '["message"]'})
    return res.get("result", []) if res.get("ok") else []


WELCOME = (
    f"👋 أهلاً فيك بـ <b>{BOT_NAME}</b>\n\n"
    "البوت بيبعتلك أول بأول أي <b>واجب</b> أو <b>علامة</b> أو <b>إعلان</b> جديد بينزل على بوابة ابنك في مدرسة BIS.\n\n"
    "🔑 <b>ابعت كود الطالب</b> اللي وصلك (زي: <code>BIS-AB12C</code>) عشان نربطك فيه."
)

ADMIN_HELP = (
    "🛠 <b>أوامر المدير</b>\n\n"
    "<code>/add اسم_المستخدم كلمة_السر اسم_الطالب</code>\n"
    "إضافة طالب جديد. البوت بيحذف رسالتك فوراً وبيعطيك كود للأهل.\n\n"
    "<code>/list</code> — كل الطلاب وأكوادهم وعدد الأهل المربوطين\n"
    "<code>/remove BIS-XXXXX</code> — حذف طالب\n"
    "<code>/newcode BIS-XXXXX</code> — تغيير كود طالب (الأهل المربوطين بيضلّوا)\n"
)


def parent_message(name, code, bot_user):
    return (
        f"السلام عليكم 🌷\n"
        f"لتفعيل تنبيهات بوابة المدرسة للطالب <b>{esc(name)}</b>:\n\n"
        f"١. افتحوا البوت: https://t.me/{bot_user}\n"
        f"٢. اكبسوا <b>Start</b>\n"
        f"٣. ابعتوا الكود: <code>{code}</code>\n\n"
        "وبتوصلكم الواجبات والعلامات والإعلانات أول بأول ✅"
    )


def handle_updates(data, check_login):
    """بنقرأ رسائل البوت الجديدة: أوامر المدير، وأكواد الأهل."""
    updates = get_updates(data)
    bot_user = data.get("bot_user") or "BISPortalBot"
    added = []
    for u in updates:
        data["offset"] = max(data.get("offset", 0), u["update_id"])
        m = u.get("message")
        if not m or "text" not in m:
            continue
        chat = str(m["chat"]["id"])
        text = m["text"].strip()
        cmd = text.split()[0].lower().split("@")[0] if text.startswith("/") else ""
        is_admin = data.get("admin") == chat

        if data.get("admin") is None and cmd == "/start":
            data["admin"] = chat  # أول واحد بيكبس Start هو المدير (بس لما النظام فاضي)
            is_admin = True

        if is_admin and cmd == "/add":
            tg("deleteMessage", {"chat_id": chat, "message_id": m["message_id"]})  # نشيل كلمة السر من المحادثة
            parts = text.split(maxsplit=3)
            if len(parts) < 4:
                send_text(chat, "⚠️ الصيغة: <code>/add اسم_المستخدم كلمة_السر اسم_الطالب</code>", footer=False)
                continue
            _, user, pw, name = parts
            if any(s["user"] == user for s in data["students"].values()):
                send_text(chat, f"ℹ️ الطالب <b>{esc(user)}</b> مسجّل من قبل. ابعت <code>/list</code> لتشوف كوده.", footer=False)
                continue
            if not check_login(user, pw):
                send_text(chat, "❌ ما قدرت أدخل على البوابة بهالبيانات. تأكد من اسم المستخدم وكلمة السر وجرّب كمان مرة.", footer=False)
                continue
            code = new_code(data)
            data["students"][code] = {"name": name, "user": user, "pass": pw, "chats": [],
                                      "pages": {}, "seen": [], "flags": {}}
            added.append(code)
            send_text(chat, f"✅ تمت إضافة <b>{esc(name)}</b>\nالكود: <code>{code}</code>\n\n"
                            "👇 هاي رسالة جاهزة، اعملها Forward للأهل:", footer=False)
            send_text(chat, parent_message(name, code, bot_user), footer=False)

        elif is_admin and cmd == "/list":
            if not data["students"]:
                send_text(chat, "ما في طلاب لسا. ضيف واحد بـ <code>/add</code>", footer=False)
                continue
            rows = [f"• <b>{esc(s['name'])}</b> — <code>{c}</code> — 👨‍👩‍👦 {len(s['chats'])}"
                    + (" ⚠️ مشكلة دخول" if s["flags"].get("login_failed") else "")
                    for c, s in data["students"].items()]
            send_text(chat, f"📋 <b>الطلاب ({len(rows)})</b>\n\n" + "\n".join(rows), footer=False)

        elif is_admin and cmd in ("/remove", "/newcode"):
            parts = text.split()
            code = parts[1].upper() if len(parts) > 1 else ""
            if code not in data["students"]:
                send_text(chat, "⚠️ ما لقيت هالكود. ابعت <code>/list</code> لتشوف الأكواد.", footer=False)
                continue
            if cmd == "/remove":
                s = data["students"].pop(code)
                for c in s["chats"]:
                    if c != chat:
                        send_text(c, f"ℹ️ تم إيقاف تنبيهات الطالب <b>{esc(s['name'])}</b>.")
                send_text(chat, f"🗑 تم حذف <b>{esc(s['name'])}</b>.", footer=False)
            else:
                nc = new_code(data)
                data["students"][nc] = data["students"].pop(code)
                send_text(chat, f"🔄 الكود الجديد لـ <b>{esc(data['students'][nc]['name'])}</b>: <code>{nc}</code>", footer=False)

        elif cmd in ("/help", "/admin") and is_admin:
            send_text(chat, ADMIN_HELP, footer=False)

        elif cmd == "/stop":
            names = [s["name"] for s in data["students"].values() if chat in s["chats"]]
            for s in data["students"].values():
                if chat in s["chats"]:
                    s["chats"].remove(chat)
            send_text(chat, "🔕 تم إيقاف التنبيهات." if names else "ما إنت مربوط بأي طالب.")

        elif CODE_RE.match(text):
            code = CODE_RE.match(text).group(1).upper()
            s = data["students"].get(code)
            if not s:
                send_text(chat, "❌ الكود مش صحيح. تأكد منه وابعته كمان مرة، أو تواصل مع الإدارة.")
            elif chat in s["chats"]:
                send_text(chat, f"✅ إنت مربوط أصلاً بالطالب <b>{esc(s['name'])}</b>.")
            else:
                s["chats"].append(chat)
                send_text(chat, f"✅ تم ربطك بالطالب <b>{esc(s['name'])}</b>\n\n"
                                "من هلأ ورايح، أي واجب أو علامة أو إعلان جديد رح يوصلك هون 🔔\n"
                                "لإيقاف التنبيهات بأي وقت ابعت /stop")
                if data.get("admin") and data["admin"] != chat:
                    send_text(data["admin"], f"👨‍👩‍👦 ولي أمر جديد انربط بـ <b>{esc(s['name'])}</b>", footer=False)

        elif cmd == "/start" or not is_admin:
            mine = [s["name"] for s in data["students"].values() if chat in s["chats"]]
            if mine and cmd != "/start":
                send_text(chat, "إنت مربوط بـ: " + "، ".join(f"<b>{esc(n)}</b>" for n in mine)
                          + "\nلإضافة طالب ثاني ابعت كوده، ولإيقاف التنبيهات ابعت /stop")
            else:
                send_text(chat, WELCOME + ("\n\n" + ADMIN_HELP if is_admin else ""))
        else:
            send_text(chat, ADMIN_HELP, footer=False)
    return added


# ---------------- البوابة ----------------
def clean_lines(text):
    out, seen = [], set()
    for l in text.splitlines():
        l = RELTIME.sub("", re.sub(r"\s+", " ", l)).strip(" ·")
        if len(l) < 2 or NOISE.search(l) or l in seen or l in IGNORE:
            continue
        # عدّاد قصير زي "٤ جديد" أو "سُلّم ٠ من ٢"
        if len(l) <= 30 and re.search(D, l) and COUNTER.search(l):
            continue
        seen.add(l)
        out.append(l)
    return out


def settle(page, max_ms=10000):
    """نستنى لحد ما نص الصفحة يثبت (الصفحات بتحمّل المحتوى على دفعات)."""
    last, waited = -1, 0
    while waited < max_ms:
        page.wait_for_timeout(500)
        waited += 500
        n = len(page.inner_text("body"))
        if n == last:
            return
        last = n


def has_password(page):
    return page.locator("input[type=password]:visible").count() > 0


def login(page, user, pw):
    page.goto(URL, wait_until="networkidle", timeout=90000)
    page.wait_for_timeout(2000)
    if not has_password(page):
        # يمكن في زر "دخول" قبل الفورم
        btn = page.get_by_role("button", name=LOGIN_BTN)
        if btn.count() == 0:
            btn = page.get_by_role("link", name=LOGIN_BTN)
        if btn.count() > 0:
            btn.first.click()
            page.wait_for_load_state("networkidle", timeout=60000)
    if not has_password(page):
        return True  # يا إما داخل أصلاً، يا إما ما في فورم
    page.locator(
        "input:visible:not([type=password]):not([type=hidden]):not([type=checkbox])"
        ":not([type=radio]):not([type=submit]):not([type=button])"
    ).first.fill(user)
    pwi = page.locator("input[type=password]:visible").first
    pwi.fill(pw)
    pwi.press("Enter")
    try:
        page.wait_for_load_state("networkidle", timeout=60000)
    except Exception:
        pass
    page.wait_for_timeout(3000)
    if has_password(page):
        # جرّب زر الإرسال لو الـ Enter ما زبط
        sub = page.locator("button[type=submit]:visible, input[type=submit]:visible, button:visible").first
        if sub.count():
            sub.click()
            try:
                page.wait_for_load_state("networkidle", timeout=60000)
            except Exception:
                pass
            page.wait_for_timeout(3000)
    return not has_password(page)


def page_key(url):
    p = urlparse(url)
    # نشيل التاريخ من الرابط عشان نفس الصفحة ما تنحسب "جديدة" كل يوم
    q = "&".join(x for x in p.query.split("&") if x and not x.startswith("date="))
    path = p.path.rstrip("/") or "/"
    return path + (("#" + p.fragment) if p.fragment else "") + (("?" + q) if q else "")


def clean_title(t):
    return RELTIME.sub("", re.sub(r"\s+", " ", t)).strip(" ·")


def collect(page, quick):
    host = urlparse(URL).netloc
    pages = {}
    SHOTS.mkdir(exist_ok=True)

    def grab(key, title):
        shot = SHOTS / f"p{len(pages)}.png"
        try:
            page.screenshot(path=str(shot), full_page=True)
        except Exception:
            shot = None
        pages[key] = {"title": title, "lines": clean_lines(page.inner_text("body")), "shot": str(shot) if shot else None}

    settle(page)
    grab(page_key(page.url), "الصفحة الرئيسية")

    links = page.eval_on_selector_all(
        "a[href]", "els => els.map(e => ({h: e.href, t: (e.innerText || e.title || '').trim()}))"
    )
    targets = []
    for l in links:
        h, t = l["h"], clean_title(l["t"])[:60]
        u = urlparse(h)
        if u.scheme not in ("http", "https") or u.netloc != host:
            continue
        if SKIP_LINK.search(h) or SKIP_LINK.search(t) or SKIP_EXT.search(h):
            continue
        k = page_key(h)
        if k in pages or any(k == x[0] for x in targets):
            continue
        targets.append((k, h, t))
    if quick:
        targets = [x for x in targets if x[0].rstrip("/").endswith(tuple(PRIORITY))]
    for k, h, t in targets[:MAX_PAGES]:
        try:
            page.goto(h, wait_until="networkidle", timeout=60000)
            settle(page)
            if has_password(page):  # طلعنا برا الجلسة
                continue
            grab(k, t or page.title() or k)
        except Exception as e:
            log("skip page", type(e).__name__)
    return pages


def icon(title):
    if re.search(r"واجب|homework|assignment|task|مهام", title, re.I):
        return "📚"
    if re.search(r"علام|درج|grade|mark|result|report|تقرير|نتيج", title, re.I):
        return "📊"
    if re.search(r"إعلان|اعلان|announce|news|خبر|أخبار|رسال|message|inbox", title, re.I):
        return "📢"
    return "📌"


def esc(s):
    return html.escape(str(s), quote=False)


def check_student(browser, data, code, s, quick):
    ctx = browser.new_context(locale="ar", timezone_id="Asia/Gaza", viewport={"width": 1280, "height": 900})
    page = ctx.new_page()
    try:
        ok = login(page, s["user"], s["pass"])
        if not ok:
            if not s["flags"].get("login_failed") and data.get("admin"):
                page.screenshot(path="shots/login.png", full_page=True)
                send_photo(data["admin"], "shots/login.png",
                           f"⚠️ ما قدرت أدخل على بوابة <b>{esc(s['name'])}</b> (<code>{code}</code>).\n"
                           "يمكن كلمة السر تغيّرت — احذفه بـ /remove وضيفه من جديد بـ /add.")
            s["flags"]["login_failed"] = True
            return "login_failed"
        if s["flags"].pop("login_failed", None) and data.get("admin"):
            send_text(data["admin"], f"✅ رجع الدخول يشتغل لـ <b>{esc(s['name'])}</b>.", footer=False)
        first = not s["pages"]
        pages = collect(page, quick and not first)
    finally:
        ctx.close()

    seen = set(s["seen"])
    if first:
        # أول فحص: بنحفظ نسخة بس بدون تنبيهات
        for c in s["chats"]:
            send_text(c, f"👁 صار البوت يتابع بوابة <b>{esc(s['name'])}</b>.\nمن هلأ ورايح أي إشي جديد رح يوصلك هون.")
    else:
        msgs, photos = [], []
        reported = set(seen)

        def rank(item):
            return next((i for i, w in enumerate(PRIORITY) if item[0].rstrip("/").endswith(w)), len(PRIORITY))

        for k, v in sorted(pages.items(), key=rank):
            added = [l for l in v["lines"] if l not in reported]
            reported.update(added)
            if not added:
                continue
            head = f"{icon(v['title'])} <b>{'' if k in s['pages'] else 'جديد: '}{esc(v['title'])}</b>"
            body = "\n".join(f"• {esc(l[:200])}" for l in added[:15])
            if len(added) > 15:
                body += f"\n… و{len(added) - 15} سطر كمان"
            msgs.append(f"{head}\n{body}")
            if v.get("shot"):
                photos.append((v["shot"], head))
        if msgs:
            for c in s["chats"]:
                send_text(c, f"🔔 <b>جديد على بوابة {esc(s['name'])}</b>\n\n" + "\n\n".join(msgs))
                for path, cap in photos[:3]:
                    send_photo(c, path, cap)

    saved = dict(s["pages"]) if quick else {}
    saved.update({k: {"title": v["title"], "lines": v["lines"]} for k, v in pages.items()})
    s["pages"] = saved
    for v in pages.values():
        for l in v["lines"]:
            if l not in seen:
                seen.add(l)
                s["seen"].append(l)
    s["seen"] = s["seen"][-20000:]
    s["last"] = datetime.datetime.now(datetime.timezone.utc).isoformat(timespec="minutes")
    return "ok"


# ---------------- التشغيل ----------------
def main():
    if not ((TOKEN or DRY) and KEY):
        sys.exit("ناقص Secrets: TG_TOKEN / STATE_KEY")
    STATE.mkdir(exist_ok=True)
    data = load_data()
    migrate_v1(data)
    me = {} if DRY else tg("getMe")
    log("telegram", "ok" if me.get("ok") else "TOKEN_INVALID")
    if me.get("ok"):
        data["bot_user"] = me["result"]["username"]

    with sync_playwright() as p:
        try:
            # كروم مثبّت جاهز على سيرفر GitHub — أسرع بكثير من تنزيل متصفح كل مرة
            browser = p.chromium.launch(channel="chrome")
        except Exception:
            browser = p.chromium.launch()

        def check_login(user, pw):
            ctx = browser.new_context(locale="ar")
            try:
                return login(ctx.new_page(), user, pw)
            except Exception:
                return False
            finally:
                ctx.close()

        added = handle_updates(data, check_login)
        save_data(data)  # نحفظ فوراً عشان ما نعيد معالجة نفس الرسائل لو صار خطأ بعدين

        results = {}
        for code, s in list(data["students"].items()):
            try:
                results[code] = check_student(browser, data, code, s, QUICK)
            except Exception as e:
                results[code] = "error:" + type(e).__name__
            save_data(data)
        browser.close()

    save_data(data)
    ok = sum(r == "ok" for r in results.values())
    log(f"students={len(results)} ok={ok} new={len(added)} quick={QUICK}")


if __name__ == "__main__":
    main()
